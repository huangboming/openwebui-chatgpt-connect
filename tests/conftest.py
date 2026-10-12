"""Load the complete deliverable and replace only host/network boundaries."""

import importlib.util
import logging
import sys
from types import SimpleNamespace

import httpx
import pytest

from scripts.build import artifacts
from tests.support import (
    REAL_ASYNC_CLIENT,
    Harness,
    Host,
    OpenAI,
    WorkspaceModelForm,
    install_modules,
)


@pytest.fixture(scope="session")
def built_artifacts(tmp_path_factory):
    directory = tmp_path_factory.mktemp("functions")
    for name, content in artifacts().items():
        (directory / name).write_text(content, encoding="utf-8")
    return directory


@pytest.fixture(scope="session")
def artifact(built_artifacts):
    return built_artifacts / "openwebui_chatgpt_connect.py"


@pytest.fixture(scope="session")
def filter_artifact(built_artifacts):
    return built_artifacts / "chatgpt_responses_history.py"


@pytest.fixture
def history_filter(filter_artifact, host):
    spec = importlib.util.spec_from_file_location("_built_history_filter", filter_artifact)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Filter()


@pytest.fixture
def host(tmp_path, monkeypatch):
    host = Host(
        SimpleNamespace(
            DATA_DIR=tmp_path,
            AUDIT_LOG_LEVEL="NONE",
            ENABLE_AUDIT_GET_REQUESTS=False,
            AUDIT_INCLUDED_PATHS=[],
            AUDIT_EXCLUDED_PATHS=[],
        )
    )
    bindings = {
        "open_webui": {},
        "open_webui.env": vars(host.env),
        "open_webui.models": {},
        "open_webui.models.files": {},
        "open_webui.models.functions": {"Functions": host},
        "open_webui.models.models": {"ModelForm": WorkspaceModelForm, "Models": host},
        "open_webui.models.users": {"Users": host},
        "open_webui.storage": {},
        "open_webui.storage.provider": {},
        "open_webui.utils": {},
        "open_webui.utils.access_control": {},
        "open_webui.utils.access_control.files": {},
        "open_webui.utils.auth": {"get_admin_user": host.get_admin_user},
    }
    install_modules(monkeypatch, bindings)
    host.env = sys.modules["open_webui.env"]
    return host


@pytest.fixture
async def harness(artifact, host, monkeypatch):
    name = "_built_connect_function"
    spec = importlib.util.spec_from_file_location(name, artifact)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    upstream = OpenAI()
    transport = httpx.MockTransport(upstream.handle)

    def async_client(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    for logger_name in ("uvicorn.access", "gunicorn.access"):
        monkeypatch.setattr(logging.getLogger(logger_name), "filters", [])
    instance = Harness(module, host, upstream)
    await instance.restart()
    try:
        yield instance
    finally:
        await instance.client.aclose()
