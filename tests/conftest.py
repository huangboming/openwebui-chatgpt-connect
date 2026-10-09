"""Load the complete deliverable and replace only host/network boundaries."""

import importlib.util
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from tests.support import REAL_ASYNC_CLIENT, Harness, Host, OpenAI, WorkspaceModelForm

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def artifact(tmp_path_factory):
    path = tmp_path_factory.mktemp("function") / "connect.py"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build.py"), "--output", str(path)], check=True
    )
    return path


@pytest.fixture
async def harness(artifact, tmp_path, monkeypatch):
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
        "open_webui.models.functions": {"Functions": host},
        "open_webui.models.models": {"ModelForm": WorkspaceModelForm, "Models": host},
        "open_webui.models.users": {"Users": host},
        "open_webui.utils": {},
        "open_webui.utils.auth": {"get_admin_user": host.get_admin_user},
    }
    for name, values in bindings.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(sys.modules[parent], child, module)
    host.env = sys.modules["open_webui.env"]

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
