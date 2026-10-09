"""ChatGPT identity, session persistence, token renewal, and revocation."""

import asyncio
import fcntl
import hmac
import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import HTTPException

from .settings import ISSUER, PLAN_SCOPE, RESOURCE, TERMINAL_REFRESH_ERRORS, TOKEN_URL


class CredentialStore:
    """One protected record; file locks also serialize rotating refresh tokens."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "credentials.json"

    @asynccontextmanager
    async def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise HTTPException(503, "The credentials directory must not be a symbolic link.")
        os.chmod(self.directory, 0o700)
        fd = os.open(
            self.directory / "credentials.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        acquired = False
        try:
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + 90
            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise HTTPException(
                            503, "Another credentials operation is still in progress."
                        ) from None
                    await asyncio.sleep(0.05)
            yield self.read()
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self) -> dict:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            state = {
                "version": 1,
                "ext_agent_host_id": f"urn:uuid:{uuid.uuid4()}",
                "revision": 0,
                "registration": None,
                "tokens": None,
            }
            self.save(state)
            return state
        try:
            with os.fdopen(fd) as stream:
                state = json.load(stream)
            if (
                not isinstance(state, dict)
                or state.get("version") != 1
                or not isinstance(state.get("revision"), int)
                or not isinstance(state.get("ext_agent_host_id"), str)
                or not state["ext_agent_host_id"].startswith("urn:uuid:")
                or not isinstance(state.get("registration"), (dict, type(None)))
                or not isinstance(state.get("tokens"), (dict, type(None)))
            ):
                raise ValueError("invalid state")
            return state
        except (ValueError, TypeError):
            raise HTTPException(
                503, "The credentials file is corrupt or incompatible and was not overwritten."
            ) from None

    def save(self, state: dict):
        fd, name = tempfile.mkstemp(prefix=".credentials-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def timestamp(value: Any) -> float:
    if value is None:
        return 0
    try:
        number = float(value)
    except (ValueError, TypeError):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp requires a timezone") from None
        number = parsed.timestamp()
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid timestamp")
    return number / 1000 if number > 100_000_000_000 else number


def token_record(data: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    expires = float(data.get("expires_in", 0))
    scope = data.get("scope")
    if scope is None and previous:
        scopes = previous["scopes"]
    elif isinstance(scope, str):
        scopes = scope.split()
    else:
        raise ValueError("missing granted scopes")
    if (
        not isinstance(access, str)
        or not access
        or (refresh is not None and (not isinstance(refresh, str) or not refresh))
        or ("offline_access" in scopes and not refresh)
        or str(data.get("token_type", "")).lower() != "bearer"
        or not math.isfinite(expires)
        or not 0 < expires <= 86400
    ):
        raise ValueError("invalid token response")
    now = time.time()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": data.get("id_token") or previous.get("id_token"),
        "token_type": "Bearer",
        "scopes": scopes,
        "saved_at": now,
        "expires_at": now + expires,
        "earliest_refresh_at": timestamp(data.get("earliest_refresh_at")),
    }


def oauth_error_code(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", "unknown_oauth_error")
        value = (
            error.get("code", error.get("type", "unknown_oauth_error"))
            if isinstance(error, dict)
            else error
        )
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", value)
            else "unknown_oauth_error"
        )
    except (ValueError, AttributeError):
        return "unknown_oauth_error"


class ChatGPTAuth:
    def __init__(self, store: CredentialStore):
        self.store = store
        self._metadata: dict | None = None
        self._jwks: list[dict] = []
        self._jwks_at = 0.0

    async def discovery(self) -> dict:
        if self._metadata is None:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                result = await client.get(f"{ISSUER}/.well-known/openid-configuration")
                result.raise_for_status()
                data = result.json()
            if not isinstance(data, dict) or data.get("issuer") != ISSUER:
                raise ValueError("unexpected issuer")
            for name in ("jwks_uri", "revocation_endpoint"):
                uri = urlsplit(data.get(name, ""))
                if (
                    uri.scheme != "https"
                    or uri.netloc != "auth.openai.com"
                    or uri.query
                    or uri.fragment
                ):
                    raise ValueError("unexpected discovery endpoint")
            self._metadata = data
        return self._metadata

    async def verify_identity(self, encoded: str, client_id: str, nonce: str | None = None) -> dict:
        header = jwt.get_unverified_header(encoded)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("invalid ID token algorithm")
        metadata = await self.discovery()
        keys = self._jwks
        if time.time() - self._jwks_at > 3600 or not any(
            key.get("kid") == header["kid"] for key in keys
        ):
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                result = await client.get(metadata["jwks_uri"])
                result.raise_for_status()
                keys = result.json()["keys"]
            if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
                raise ValueError("invalid JWKS")
            self._jwks, self._jwks_at = keys, time.time()
        key = next(
            (
                key
                for key in keys
                if (
                    key.get("kid") == header["kid"]
                    and key.get("kty") == "RSA"
                    and key.get("use") in (None, "sig")
                    and key.get("alg") in (None, "RS256")
                )
            ),
            None,
        )
        if key is None:
            raise ValueError("unknown ID token key")
        claims = jwt.decode(
            encoded,
            jwt.PyJWK.from_dict(key).key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUER,
            leeway=30,
            options={"require": ["iss", "aud", "sub", "exp", "iat"]},
        )
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError("missing subject")
        if claims.get("azp") not in (None, client_id) or (
            isinstance(claims["aud"], list)
            and len(claims["aud"]) > 1
            and claims.get("azp") != client_id
        ):
            raise ValueError("invalid authorized party")
        if nonce is not None and not hmac.compare_digest(
            str(claims.get("nonce", "")).encode(), nonce.encode()
        ):
            raise ValueError("invalid nonce")
        return claims

    async def request_token(self, data: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            return await client.post(TOKEN_URL, data=data)

    async def access_token(self) -> str:
        async with self.store.locked() as state:
            registration, tokens = state.get("registration"), state.get("tokens")
            if not registration or not registration.get("subject") or not tokens:
                raise HTTPException(503, "ChatGPT is not connected. Open /siwc/admin to sign in.")
            if PLAN_SCOPE not in tokens["scopes"] or "resource.invoke" not in tokens["scopes"]:
                raise HTTPException(
                    403, "The account is signed in but has not granted subscription access."
                )
            if tokens.get("refresh_in_flight"):
                raise HTTPException(
                    401,
                    "Token renewal could not be confirmed. Sign in again before making another request.",
                )
            now = time.time()
            if tokens["expires_at"] > now + 30:
                return tokens["access_token"]
            if not tokens.get("refresh_token"):
                if tokens["expires_at"] > now:
                    return tokens["access_token"]
                raise HTTPException(
                    401, "This session cannot be renewed. Sign in to ChatGPT again."
                )
            next_try = max(
                tokens.get("earliest_refresh_at", 0), tokens.get("refresh_retry_after", 0)
            )
            if now < next_try:
                if tokens["expires_at"] > now:
                    return tokens["access_token"]
                raise HTTPException(
                    503,
                    "Token renewal is not available yet. Try again later.",
                    headers={"Retry-After": str(max(1, int(next_try - now)))},
                )
            tokens["refresh_in_flight"] = True
            self.store.save(state)
            try:
                result = await self.request_token(
                    {
                        "grant_type": "refresh_token",
                        "client_id": registration["client_id"],
                        "refresh_token": tokens["refresh_token"],
                        "resource": RESOURCE,
                    }
                )
            except httpx.HTTPError:
                raise HTTPException(
                    503,
                    "Token renewal was interrupted and its result is unknown. Sign in to ChatGPT again.",
                ) from None
            if result.status_code != 200:
                code = oauth_error_code(result)
                if code in TERMINAL_REFRESH_ERRORS:
                    state["tokens"] = None
                    state["revision"] += 1
                    self.store.save(state)
                    raise HTTPException(401, f"The ChatGPT session cannot be renewed ({code}).")
                tokens.pop("refresh_in_flight", None)
                tokens["refresh_retry_after"] = time.time() + 30
                self.store.save(state)
                raise HTTPException(
                    503,
                    f"Token renewal failed temporarily (HTTP {result.status_code}, {code}).",
                    headers={"Retry-After": "30"},
                )
            try:
                data = result.json()
                replacement = token_record(data, tokens)
                if data.get("id_token"):
                    identity = await self.verify_identity(
                        data["id_token"], registration["client_id"]
                    )
                    if identity["sub"] != registration["subject"]:
                        raise ValueError("refresh changed identity")
            except (ValueError, KeyError, TypeError, jwt.PyJWTError, httpx.HTTPError):
                raise HTTPException(
                    503,
                    "The token renewal response could not be verified. Sign in to ChatGPT again.",
                ) from None
            granted = (
                PLAN_SCOPE in replacement["scopes"] and "resource.invoke" in replacement["scopes"]
            )
            registration["plan_permission"] = granted
            state["registration"] = registration
            state["tokens"] = replacement
            self.store.save(state)
            if not granted:
                raise HTTPException(403, "The renewed session no longer has subscription access.")
            return replacement["access_token"]

    async def revoke(self, refresh: str, client_id: str) -> bool:
        try:
            metadata = await self.discovery()
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                result = await client.post(
                    metadata["revocation_endpoint"],
                    data={
                        "token": refresh,
                        "token_type_hint": "refresh_token",
                        "client_id": client_id,
                    },
                )
            return result.status_code == 200
        except (httpx.HTTPError, ValueError, KeyError):
            return False
