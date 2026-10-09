"""Administrator, CSRF, lifecycle, and callback logging contracts."""

import json
import logging
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from tests.support import KEY


@pytest.mark.parametrize(
    "path", ["/siwc/admin", "/siwc/admin/status", "/siwc/admin/models", "/siwc/authorize/invalid"]
)
@pytest.mark.parametrize("session,status", [(None, 401), ("user", 403)])
async def test_management_requires_admin(harness, path, session, status):
    harness.client.cookies.clear()
    if session:
        harness.client.cookies.set("token", session, domain="webui.example.test")
    assert (await harness.client.get(path)).status_code == status


@pytest.mark.parametrize(
    "path,body",
    [
        ("start", {}),
        ("cancel", {}),
        ("sign-out", {}),
        ("models", {"enabled": ["catalog-model"]}),
    ],
)
async def test_mutations_require_admin_even_with_csrf(harness, path, body):
    await harness.page()
    harness.client.cookies.set("token", "user", domain="webui.example.test")
    assert (await harness.post_admin(path, json=body)).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-siwc-csrf": "invalid"},
        {"origin": "https://attacker.test"},
        {"sec-fetch-site": "cross-site"},
    ],
)
async def test_mutations_reject_missing_csrf_or_cross_origin(harness, headers):
    await harness.page()
    if "origin" in headers or "sec-fetch-site" in headers:
        headers = {"x-siwc-csrf": harness.csrf, **headers}
    response = await harness.client.post("/siwc/admin/start", headers=headers, json={})
    assert response.status_code == 403
    assert (await harness.status())["pending"] is None


async def test_pending_sign_in_survives_page_refresh_and_can_be_cancelled(harness):
    started, params = await harness.prepare()
    page = await harness.page()
    initial = re.search(r'<meta name="siwc-initial-state" content="([^"]+)">', page.text)
    pending = json.loads(unescape(initial[1]))["pending"]
    assert pending == started["status"]["pending"]
    assert set(pending) == {"phase", "ssh_command", "authorize_path"}

    cancelled = await harness.post_admin("cancel", json={})
    assert cancelled.json()["status"]["pending"] is None
    assert (await harness.callback(params)).status_code == 400


async def test_csrf_is_bound_to_admin_session(harness):
    await harness.page()
    harness.host.users["other"] = type(harness.host.users["admin"])(id="other", role="admin")
    harness.client.cookies.set("token", "other", domain="webui.example.test")
    assert (await harness.post_admin("sign-out", json={})).status_code == 403


async def test_management_reads_reject_cross_origin(harness):
    response = await harness.client.get(
        "/siwc/admin/status", headers={"origin": "https://attacker.test"}
    )
    assert response.status_code == 403


async def test_page_has_security_headers_and_no_credentials(harness):
    await harness.connect()
    response = await harness.page()
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    for secret in (KEY, "access-1", "refresh-1"):
        assert secret not in response.text
        assert secret not in str(await harness.status())


@pytest.mark.parametrize("key,status", [(None, 401), ("wrong", 401)])
async def test_inference_routes_require_internal_key(harness, key, status):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    assert (await harness.client.get("/siwc/v1/models", headers=headers)).status_code == status
    assert not harness.upstream.requests


async def test_page_embeds_current_state_settings_and_account_resources(harness):
    await harness.connect()
    response = await harness.page()
    initial = re.search(r'<meta name="siwc-initial-state" content="([^"]+)">', response.text)
    assert json.loads(unescape(initial[1])) == await harness.status()
    assert "http://127.0.0.1:8080/siwc/v1" in response.text
    assert 'href="https://chatgpt.com/settings/security"' in response.text
    assert 'href="https://chatgpt.com/settings/usage"' in response.text
    assert harness.module.MODEL_AVATAR_URL in response.text


async def test_model_edits_during_save_remain_unsaved(harness, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for admin page interaction tests.")
    await harness.connect()
    page = tmp_path / "admin.html"
    page.write_text((await harness.page()).text)
    script = Path(__file__).with_name("admin_page.test.mjs")
    result = subprocess.run(
        [node, str(script), str(page)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_models_default_to_enabled_and_save_the_bundled_avatar(harness):
    await harness.connect()
    catalog = (await harness.client.get("/siwc/admin/models")).json()["models"]
    assert catalog == [
        {
            "id": "catalog-model",
            "name": "ChatGPT/Catalog Model",
            "enabled": True,
            "manageable": True,
            "synchronized": False,
            "reasoning_efforts": ["low", "medium"],
        }
    ]

    response = await harness.post_admin("models", json={"enabled": ["catalog-model"]})
    assert response.status_code == 200
    assert response.json() == {
        "total": 1,
        "enabled": 1,
        "updated": 1,
        "custom_avatars_preserved": 0,
    }

    model = harness.host.workspace_models["catalog-model"]
    assert model.name == "ChatGPT/Catalog Model"
    assert model.is_active
    assert model.meta["profile_image_url"] == harness.module.MODEL_AVATAR_URL
    assert model.meta["openwebui_chatgpt_connect"]["avatar"]
    assert harness.module.MODEL_AVATAR_URL.startswith("data:image/png;base64,")

    refreshed = (await harness.client.get("/siwc/admin/models")).json()["models"]
    assert refreshed[0]["synchronized"]
    repeated = await harness.post_admin("models", json={"enabled": ["catalog-model"]})
    assert repeated.json()["updated"] == 0


async def test_model_selection_uses_workspace_activation(harness):
    harness.upstream.models.insert(
        1, {"slug": "second-model", "display_name": "Second Model", "visibility": "list"}
    )
    await harness.connect()
    catalog = (await harness.client.get("/siwc/admin/models")).json()["models"]
    assert [model["enabled"] for model in catalog] == [True, True]

    response = await harness.post_admin("models", json={"enabled": ["second-model"]})
    assert response.json()["enabled"] == 1
    assert not harness.host.workspace_models["catalog-model"].is_active
    assert harness.host.workspace_models["second-model"].is_active

    refreshed = (await harness.client.get("/siwc/admin/models")).json()["models"]
    assert {model["id"]: model["enabled"] for model in refreshed} == {
        "catalog-model": False,
        "second-model": True,
    }


async def test_model_selection_preserves_workspace_customizations(harness):
    harness.upstream.models.insert(
        1, {"slug": "second-model", "display_name": "Second Model", "visibility": "list"}
    )
    await harness.connect()
    await harness.post_admin("models", json={"enabled": ["catalog-model", "second-model"]})
    model = harness.host.workspace_models["catalog-model"]
    model.name = "My model"
    model.params = {"temperature": 0.5}
    model.access_grants = [{"user_id": "user", "permission": "read"}]
    model.meta["profile_image_url"] = "https://example.test/custom.png"
    model.meta.pop("openwebui_chatgpt_connect")

    response = await harness.post_admin("models", json={"enabled": ["second-model"]})
    assert response.json()["custom_avatars_preserved"] == 1
    saved = harness.host.workspace_models["catalog-model"]
    assert not saved.is_active
    assert saved.name == "My model"
    assert saved.params == {"temperature": 0.5}
    assert saved.access_grants == [{"user_id": "user", "permission": "read"}]
    assert saved.meta["profile_image_url"] == "https://example.test/custom.png"


@pytest.mark.parametrize(
    "selection,error",
    [
        ([], "Select at least one"),
        (["catalog-model", "catalog-model"], "duplicates"),
        (["other"], "unavailable"),
    ],
)
async def test_model_selection_rejects_invalid_choices(harness, selection, error):
    await harness.connect()
    response = await harness.post_admin("models", json={"enabled": selection})
    assert response.status_code == 400
    assert error in response.json()["detail"]
    assert not harness.host.workspace_models


async def test_weak_internal_key_rejects_requests(harness):
    harness.host.valves["INTERNAL_API_KEY"] = "short"
    assert (await harness.models()).status_code == 503
    assert not harness.upstream.requests


async def test_old_routes_cannot_serve_a_disabled_function(harness):
    harness.host.active = False
    assert (await harness.models()).status_code == 404
    assert (await harness.client.get("/siwc/admin/status")).status_code == 404


async def test_lifecycle_removes_and_reinstalls_routes_before_spa(harness):
    spa = FastAPI()

    @spa.get("/{path:path}")
    async def fallback(path: str):
        return PlainTextResponse("spa")

    harness.app.mount("/", spa)
    await harness.event.event(
        {"subject": {"id": "connect"}},
        __event_name__="function.disable_started",
        __id__="connect",
        __app__=harness.app,
    )
    assert (await harness.client.get("/siwc/admin")).text == "spa"
    await harness.event.event(
        {"subject": {"id": "connect"}},
        __event_name__="function.enable_started",
        __id__="connect",
        __app__=harness.app,
    )
    assert (await harness.page()).status_code == 200


async def test_function_update_replaces_routes_without_restart(harness):
    await harness.connect()
    old_admin = harness.event.admin
    old_responses = harness.event.responses_api
    before = [route for route in harness.app.router.routes if route.path.startswith("/siwc/")]

    replacement = harness.module.Event()
    await replacement.event(
        {"subject": {"id": "connect"}},
        __event_name__="function.updated",
        __id__="connect",
        __app__=harness.app,
    )

    after = [route for route in harness.app.router.routes if route.path.startswith("/siwc/")]
    owners = {getattr(route.endpoint, "__self__", None) for route in after}
    assert len(after) == len(before)
    assert old_admin not in owners
    assert old_responses not in owners
    assert replacement.admin in owners
    assert replacement.responses_api in owners
    assert (await harness.page()).status_code == 200
    assert (await harness.models()).status_code == 200


async def test_multiple_functions_cannot_claim_same_routes(harness):
    other = harness.module.Event()
    with pytest.raises(RuntimeError, match="already owns"):
        await other.event(
            {}, __event_name__="system.startup.completed", __id__="other", __app__=harness.app
        )
    assert (await harness.page()).status_code == 200


@pytest.mark.parametrize(
    "included,excluded,allowed",
    [
        ([], [], False),
        ([], ["siwc/oauth/callback"], True),
        (["siwc/.*"], ["siwc/oauth/callback"], False),
        (["functions"], [], True),
    ],
)
async def test_login_refuses_audited_callback(harness, included, excluded, allowed):
    env = harness.host.env
    env.AUDIT_LOG_LEVEL = "REQUEST_RESPONSE"
    env.ENABLE_AUDIT_GET_REQUESTS = True
    env.AUDIT_INCLUDED_PATHS = included
    env.AUDIT_EXCLUDED_PATHS = excluded
    response = await harness.post_admin("start", json={})
    assert response.status_code == (200 if allowed else 503)


def test_callback_access_log_queries_are_redacted(harness, caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "client",
            "GET",
            "/api/v1/siwc/oauth/callback?code=secret-code&state=secret-state",
            "1.1",
            200,
        )
    assert "/api/v1/siwc/oauth/callback" in caplog.text
    assert "secret-code" not in caplog.text
    assert "secret-state" not in caplog.text
