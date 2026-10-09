"""OAuth transactions through the public management and loopback routes."""

import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from tests.support import SCOPES


async def test_login_uses_pkce_and_issued_client_without_webui_callback_cookie(harness):
    result, params = await harness.prepare()
    assert params["client_id"] == ["dynamic_agent_client"]
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == [SCOPES]
    assert params["ext_agent_host_id"][0].startswith("urn:uuid:")
    assert params["redirect_uri"] == ["http://127.0.0.1:1455/api/v1/siwc/oauth/callback"]
    assert result["ssh_command"] == (
        "ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:1455:127.0.0.1:8080 user@ct"
    )
    assert result["status"]["pending"]["phase"] == "waiting"
    assert (await harness.callback(params)).status_code == 200
    exchange = parse_qs(harness.upstream.calls("/api/accounts/oauth/token")[0].content.decode())
    assert exchange["client_id"] == ["issued-client"]
    assert exchange["redirect_uri"] == params["redirect_uri"]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"][0].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert params["code_challenge"] == [challenge]
    assert (await harness.callback(params)).status_code == 400
    assert len(harness.upstream.calls("/api/accounts/oauth/token")) == 1


@pytest.mark.parametrize(
    "query",
    [
        {"state": "wrong"},
        {"state": ["first", "second"]},
        {"code": ["first", "second"]},
        {"code": ""},
        {"client_id": ""},
        {"error": "access_denied"},
    ],
)
async def test_invalid_callback_never_exchanges_code(harness, query):
    _, params = await harness.prepare()
    assert (await harness.callback(params, **query)).status_code == 400
    assert not harness.upstream.calls("/api/accounts/oauth/token")


async def test_revoked_admin_cannot_finish_login(harness):
    _, params = await harness.prepare()
    harness.host.users["admin"].role = "user"
    assert (await harness.callback(params)).status_code == 403
    assert not harness.upstream.calls("/api/accounts/oauth/token")


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "wrong"},
        {"aud": "other"},
        {"iss": "https://attacker.test"},
        {"sub": "other"},
        {"exp": int(time.time()) - 100},
        {"azp": "other"},
        {"aud": ["issued-client", "other"]},
    ],
)
async def test_invalid_identity_does_not_replace_existing_connection(harness, claims):
    await harness.connect()
    _, params = await harness.prepare()
    harness.upstream.claims = claims
    assert (await harness.callback(params)).status_code == 400
    assert (await harness.status())["connected"]
    assert (await harness.models()).status_code == 200
    assert harness.upstream.calls("/v1/models")[-1].headers["authorization"] == "Bearer access-1"
    revoked = parse_qs(harness.upstream.calls("/revoke")[-1].content.decode())
    assert revoked["token"] == ["refresh-2"]


async def test_invalid_signature_is_not_accepted(harness):
    _, params = await harness.prepare()
    valid = harness.upstream.token_data()["id_token"]
    harness.upstream.token_overrides["id_token"] = valid.rsplit(".", 1)[0] + ".AAAA"
    assert (await harness.callback(params)).status_code == 400
    assert not (await harness.status())["connected"]


async def test_registered_client_cannot_be_replaced_by_callback(harness):
    await harness.connect()
    _, params = await harness.prepare()
    assert params["client_id"] == ["issued-client"]
    assert (await harness.callback(params, client_id="different")).status_code == 400
    assert len(harness.upstream.calls("/api/accounts/oauth/token")) == 1
    assert (await harness.models()).status_code == 200


async def test_declined_plan_permission_keeps_verified_registration(harness):
    harness.upstream.token_overrides = {"scope": "openid profile email", "refresh_token": None}
    await harness.connect()
    status = await harness.status()
    assert status["registered"] and not status["connected"]
    assert status["email"] is None
    assert (await harness.models()).status_code == 403

    # Upgrade compatibility: older registrations did not persist this derived flag.
    path = harness.host.env.DATA_DIR / "siwc-connect" / "credentials.json"
    state = json.loads(path.read_text())
    state["registration"].pop("plan_permission")
    path.write_text(json.dumps(state))

    _, params = await harness.prepare()
    assert params["client_id"] == ["issued-client"]
    assert params["prompt"] == ["consent"]


async def test_new_attempt_invalidates_only_the_previous_login(harness):
    await harness.connect()
    _, first = await harness.prepare()
    _, second = await harness.prepare()
    assert (await harness.callback(first)).status_code == 400
    assert (await harness.callback(second)).status_code == 200
    assert (await harness.models()).status_code == 200


async def test_disable_during_exchange_cannot_commit_new_tokens(harness):
    await harness.connect()
    _, params = await harness.prepare()
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_exchange(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=harness.upstream.token_data())

    harness.upstream.on_token = delayed_exchange
    task = asyncio.create_task(harness.callback(params))
    await asyncio.wait_for(started.wait(), 2)
    await harness.event.event(
        {"subject": {"id": "connect"}},
        __event_name__="function.disable_started",
        __id__="connect",
        __app__=harness.app,
    )
    release.set()
    assert (await task).status_code == 409
    await harness.event.event(
        {"subject": {"id": "connect"}},
        __event_name__="function.enable_started",
        __id__="connect",
        __app__=harness.app,
    )
    assert (await harness.models()).status_code == 200
    assert harness.upstream.calls("/v1/models")[-1].headers["authorization"] == "Bearer access-1"


async def test_failed_exchange_retains_issued_registration(harness):
    async def refused(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    harness.upstream.on_token = refused
    _, params = await harness.prepare()
    assert (await harness.callback(params)).status_code == 400
    _, again = await harness.prepare()
    assert again["client_id"] == ["issued-client"]
    assert not (await harness.status())["connected"]
