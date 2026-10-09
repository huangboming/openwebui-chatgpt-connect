"""Persistent storage and sign-out behavior visible across runtime restarts."""

import json
import stat


async def test_credentials_survive_restart_with_protected_permissions(harness):
    params = await harness.connect()
    directory = harness.host.env.DATA_DIR / "siwc-connect"
    path = directory / "credentials.json"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(directory.glob(".credentials-*"))
    await harness.restart()
    assert (await harness.models()).status_code == 200
    _, again = await harness.prepare()
    assert again["ext_agent_host_id"] == params["ext_agent_host_id"]
    assert again["client_id"] == ["issued-client"]


async def test_sign_out_ends_the_session_but_reuses_the_registered_client(harness):
    params = await harness.connect()
    response = await harness.post_admin("sign-out", json={})
    assert response.status_code == 200
    assert response.json()["session_revocation_confirmed"]
    status = await harness.status()
    assert status["registered"] and not status["connected"]
    assert status["email"] is None
    assert "account@example.test" not in (await harness.page()).text
    assert (await harness.models()).status_code == 503
    await harness.restart()
    _, again = await harness.prepare()
    assert again["client_id"] == ["issued-client"]
    assert "prompt" not in again
    assert again["ext_agent_host_id"] == params["ext_agent_host_id"]
    assert len(harness.upstream.calls("/revoke")) == 1


async def test_corrupt_credentials_are_not_overwritten(harness):
    await harness.connect()
    path = harness.host.env.DATA_DIR / "siwc-connect" / "credentials.json"
    path.write_text(json.dumps({"version": 999}))
    before = path.read_bytes()
    assert (await harness.models()).status_code == 503
    assert path.read_bytes() == before
    assert not harness.upstream.calls("/v1/models")
