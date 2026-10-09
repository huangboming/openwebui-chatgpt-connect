"""Renewal behavior at the OAuth HTTP boundary, without private-method patches."""

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from tests.support import Harness


async def near_expiry(harness):
    harness.upstream.expires_in = 1
    await harness.connect()
    harness.upstream.expires_in = 3600


async def test_concurrent_runtimes_refresh_once_and_persist_rotated_tokens(harness):
    await near_expiry(harness)
    other = Harness(harness.module, harness.host, harness.upstream)
    await other.restart()

    async def refresh(request):
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=harness.upstream.token_data())

    harness.upstream.on_token = refresh
    try:
        responses = await asyncio.gather(harness.models(), other.models())
        assert all(response.status_code == 200 for response in responses)
        exchanges = harness.upstream.calls("/api/accounts/oauth/token")
        assert len(exchanges) == 2
        data = parse_qs(exchanges[-1].content.decode())
        assert data["grant_type"] == ["refresh_token"]
        assert data["client_id"] == ["issued-client"]
        assert data["refresh_token"] == ["refresh-1"]
        assert data["resource"] == ["https://api.openai.com/v1"]
        assert all(
            request.headers["authorization"] == "Bearer access-2"
            for request in harness.upstream.calls("/v1/models")
        )
        await harness.restart()
        assert (await harness.models()).status_code == 200
        assert len(harness.upstream.calls("/api/accounts/oauth/token")) == 2
    finally:
        await other.client.aclose()


async def test_terminal_refresh_error_requires_login_not_new_registration(harness):
    await near_expiry(harness)

    async def invalid_grant(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    harness.upstream.on_token = invalid_grant
    assert (await harness.models()).status_code == 401
    assert (await harness.models()).status_code == 503
    status = await harness.status()
    assert status["registered"] and not status["connected"]
    _, params = await harness.prepare()
    assert params["client_id"] == ["issued-client"]
    assert len(harness.upstream.calls("/api/accounts/oauth/token")) == 2


@pytest.mark.parametrize("failure", ["network", "invalid_response", "changed_identity"])
async def test_unknown_refresh_outcome_is_not_retried(harness, failure):
    await near_expiry(harness)

    async def uncertain(request):
        if failure == "network":
            raise httpx.ReadTimeout("lost response", request=request)
        if failure == "changed_identity":
            harness.upstream.claims = {"sub": "other"}
            return httpx.Response(200, json=harness.upstream.token_data())
        return httpx.Response(200, json={"access_token": "incomplete"})

    harness.upstream.on_token = uncertain
    assert (await harness.models()).status_code == 503
    assert (await harness.models()).status_code == 401
    assert len(harness.upstream.calls("/api/accounts/oauth/token")) == 2
    assert (await harness.status())["renewal_uncertain"]
    await harness.restart()
    assert (await harness.models()).status_code == 401
    assert len(harness.upstream.calls("/api/accounts/oauth/token")) == 2


async def test_temporary_refresh_error_preserves_session_and_backs_off(harness):
    await near_expiry(harness)

    async def unavailable(request):
        return httpx.Response(503, json={"error": "temporarily_unavailable"})

    harness.upstream.on_token = unavailable
    response = await harness.models()
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    await harness.models()
    assert len(harness.upstream.calls("/api/accounts/oauth/token")) == 2
    status = await harness.status()
    assert status["connected"] and not status["renewal_uncertain"]
