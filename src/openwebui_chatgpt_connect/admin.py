"""Administrator UI, authorization transaction, and callback protection."""

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import shlex
import time
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from open_webui import env as webui_env
from open_webui.models.models import ModelForm, Models
from open_webui.models.users import Users
from open_webui.utils.auth import get_admin_user
from pydantic import BaseModel, Field, StrictBool

from .auth import ChatGPTAuth, oauth_error_code, token_record
from .settings import (
    APP_NAME,
    AUTHORIZE_URL,
    CALLBACK_PATH,
    ISSUER,
    NO_STORE,
    PLAN_SCOPE,
    RESOURCE,
    SCOPES,
    ConnectionSettings,
)

ADMIN_HTML = (Path(__file__).parent / "assets" / "admin.html").read_text(encoding="utf-8")
MODEL_AVATAR = base64.b64encode(
    (Path(__file__).parent / "assets" / "chatgpt.png").read_bytes()
).decode()
MODEL_AVATAR_URL = f"data:image/png;base64,{MODEL_AVATAR}"
MODEL_AVATAR_FINGERPRINT = hashlib.sha256(base64.b64decode(MODEL_AVATAR)).hexdigest()
APPEARANCE_META_KEY = "openwebui_chatgpt_connect"


class CallbackLogFilter(logging.Filter):
    _siwc_callback_filter = True
    pattern = re.compile(r"(/(?:api/v1/)?siwc/oauth/callback)\?[^\s\"<>]*")

    def filter(self, record):
        def redact(value):
            return self.pattern.sub(r"\1", value) if isinstance(value, str) else value

        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            args = {key: redact(value) for key, value in record.args.items()}
            if CALLBACK_PATH in str(args.get("U", "")):
                args["q"] = ""
            record.args = args
        return True


def install_callback_log_filter():
    # Keep this harmless redactor after disable: late callbacks must not leak codes.
    for name in ("uvicorn.access", "gunicorn.access"):
        logger = logging.getLogger(name)
        if not any(getattr(item, "_siwc_callback_filter", False) for item in logger.filters):
            logger.addFilter(CallbackLogFilter())


def require_safe_callback_audit():
    if getattr(webui_env, "AUDIT_LOG_LEVEL", "NONE") == "NONE" or not getattr(
        webui_env, "ENABLE_AUDIT_GET_REQUESTS", False
    ):
        return
    included = getattr(webui_env, "AUDIT_INCLUDED_PATHS", [])
    excluded = getattr(webui_env, "AUDIT_EXCLUDED_PATHS", [])

    def matches(paths):
        # v0.11.4 audit patterns are regexes relative to /api[/v1], not URL prefixes.
        values = [item.strip().lstrip("/") for item in paths if item.strip().lstrip("/")]
        return bool(
            values and re.match(r"^/api(?:/v1)?/(" + "|".join(values) + r")\b", CALLBACK_PATH)
        )

    audited = matches(included) if included else not matches(excluded)
    if audited:
        raise HTTPException(
            503,
            "Exclude the OAuth callback from GET audit logging. If no audit allowlist is set, "
            "add siwc/oauth/callback to AUDIT_EXCLUDED_PATHS and restart Open WebUI.",
        )


class StartRequest(BaseModel):
    request_consent: StrictBool = False


class ModelSelectionRequest(BaseModel):
    enabled: list[str] = Field(max_length=100)


class AdminRoutes:
    def __init__(
        self,
        auth: ChatGPTAuth,
        settings: Callable[[], Awaitable[ConnectionSettings]],
        model_catalog: Callable[[], Awaitable[list[dict]]],
    ):
        self.auth = auth
        self.store = auth.store
        self._settings = settings
        self._model_catalog = model_catalog
        self._csrf_secret = secrets.token_bytes(32)
        self._pending: dict | None = None

    def cancel_pending(self):
        self._pending = None

    def _status_payload(self, state: dict) -> dict:
        registration, tokens = state.get("registration") or {}, state.get("tokens") or {}
        if self._pending and self._pending["expires_at"] <= time.time():
            self._pending = None
        connected = bool(
            tokens
            and PLAN_SCOPE in tokens.get("scopes", [])
            and "resource.invoke" in tokens.get("scopes", [])
            and not tokens.get("refresh_in_flight")
        )
        return {
            "connected": connected,
            "registered": bool(registration.get("client_id")),
            "email": registration.get("email") if connected else None,
            "revision": state["revision"],
            "renewal_uncertain": bool(tokens.get("refresh_in_flight")),
            "pending": {
                "phase": self._pending["phase"],
                "expires_at": self._pending["expires_at"],
                "ssh_command": self._pending["ssh_command"]
                if self._pending["phase"] == "waiting" and not self._pending["consumed"]
                else None,
                "authorize_path": self._pending["authorize_path"]
                if self._pending["phase"] == "waiting" and not self._pending["consumed"]
                else None,
            }
            if self._pending
            else None,
        }

    def _csrf(self, request: Request, user, issued: str) -> str:
        session = request.headers.get("authorization") or request.cookies.get("token", "")
        message = f"{user.id}\n{session}\n{issued}".encode()
        return hmac.new(self._csrf_secret, message, hashlib.sha256).hexdigest()

    async def _admin_request(self, request: Request, user, write=False) -> ConnectionSettings:
        settings = await self._settings()
        origin = request.headers.get("origin")
        if origin:
            supplied = urlsplit(origin)
            expected = urlsplit(str(request.base_url))

            def authority(parts):
                return (
                    parts.scheme,
                    parts.hostname,
                    parts.port or (443 if parts.scheme == "https" else 80),
                )

            if supplied.scheme not in ("http", "https") or authority(supplied) != authority(
                expected
            ):
                raise HTTPException(
                    403,
                    "Origin mismatch. Check the reverse proxy Host and X-Forwarded-Proto headers.",
                )
        if write:
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "Cross-site administration requests are not allowed.")
            token = request.headers.get("x-siwc-csrf", "")
            issued, separator, signature = token.partition(".")
            if (
                not separator
                or not issued.isdigit()
                or not 0 <= time.time() - int(issued) <= 7200
                or not hmac.compare_digest(
                    signature.encode(), self._csrf(request, user, issued).encode()
                )
            ):
                raise HTTPException(403, "This page has expired. Reload it and try again.")
        return settings

    @staticmethod
    def _backend_port(request: Request) -> int:
        server = request.scope.get("server")
        if not server or not isinstance(server[1], int) or not 0 < server[1] <= 65535:
            raise HTTPException(
                503,
                "Could not determine the Open WebUI TCP port. Unix-socket-only deployments are not supported.",
            )
        return server[1]

    @staticmethod
    def _ssh_command(settings: ConnectionSettings, backend_port: int) -> str:
        target = settings.SSH_TARGET.strip()
        if not target or target.startswith("-") or any(char in target for char in "\r\n\x00"):
            raise HTTPException(400, "Set SSH_TARGET in the Function valves before connecting.")
        return (
            "ssh -N -o ExitOnForwardFailure=yes "
            f"-L 127.0.0.1:{settings.CALLBACK_PORT}:127.0.0.1:{backend_port} {shlex.quote(target)}"
        )

    async def start(self, request: Request, body: StartRequest, user=Depends(get_admin_user)):
        settings = await self._admin_request(request, user, write=True)
        require_safe_callback_audit()
        backend_port = self._backend_port(request)
        attempt_id = secrets.token_urlsafe(32)
        command = self._ssh_command(settings, backend_port)
        async with self.store.locked() as state:
            registration = state.get("registration") or {}
            tokens = state.get("tokens") or {}
            plan_permission = registration.get("plan_permission")
            if plan_permission is None and tokens:
                plan_permission = bool(
                    PLAN_SCOPE in tokens.get("scopes", [])
                    and "resource.invoke" in tokens.get("scopes", [])
                )
            verifier = secrets.token_urlsafe(48)
            pending = {
                "id": attempt_id,
                "owner_id": user.id,
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "verifier": verifier,
                "client_id": registration.get("client_id"),
                "subject": registration.get("subject"),
                "revision": state["revision"],
                "redirect_uri": f"http://127.0.0.1:{settings.CALLBACK_PORT}{CALLBACK_PATH}",
                "host_id": state["ext_agent_host_id"],
                "expires_at": time.time() + 600,
                "request_consent": body.request_consent or plan_permission is False,
                "consumed": False,
                "phase": "waiting",
                "ssh_command": command,
                "authorize_path": f"/siwc/authorize/{attempt_id}",
            }
            self._pending = pending
            status = self._status_payload(state)
        return JSONResponse(
            {
                "ssh_command": command,
                "authorize_path": pending["authorize_path"],
                "expires_at": pending["expires_at"],
                "status": status,
            },
            headers=NO_STORE,
        )

    async def authorize(self, attempt_id: str, request: Request, user=Depends(get_admin_user)):
        await self._admin_request(request, user)
        pending = self._pending
        if (
            not pending
            or pending["id"] != attempt_id
            or pending["owner_id"] != user.id
            or pending["consumed"]
            or pending["expires_at"] <= time.time()
        ):
            raise HTTPException(
                400, "This authorization attempt has expired. Start again from the admin page."
            )
        async with self.store.locked() as state:
            if state["revision"] != pending["revision"]:
                raise HTTPException(
                    409, "The connection changed. Start a new authorization attempt."
                )
            tokens = state.get("tokens") or {}
            registration = state.get("registration") or {}
            params = {
                "client_id": pending["client_id"] or "dynamic_agent_client",
                "ext_agent_host_id": pending["host_id"],
                "response_type": "code",
                "redirect_uri": pending["redirect_uri"],
                "scope": SCOPES,
                "resource": RESOURCE,
                "state": pending["state"],
                "nonce": pending["nonce"],
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(
                    hashlib.sha256(pending["verifier"].encode()).digest()
                )
                .decode()
                .rstrip("="),
            }
            if pending["client_id"]:
                if tokens.get("id_token"):
                    params["id_token_hint"] = tokens["id_token"]
                if registration.get("email"):
                    params["login_hint"] = registration["email"]
            else:
                params["agent_name_hint"] = APP_NAME
            if pending["request_consent"]:
                params["prompt"] = "consent"
        return RedirectResponse(
            f"{AUTHORIZE_URL}?{urlencode(params)}", status_code=303, headers=NO_STORE
        )

    async def callback(self, request: Request):
        query = request.query_params
        # Uvicorn logs after the response; leave no query string on the request scope.
        request.scope["query_string"] = b""
        await self._settings()
        async with self.store.locked() as state:
            pending = self._pending
            valid = (
                pending is not None
                and not pending["consumed"]
                and pending["expires_at"] > time.time()
                and len(query.getlist("state")) == 1
                and hmac.compare_digest(query.get("state", "").encode(), pending["state"].encode())
                and request.headers.get("host") == urlsplit(pending["redirect_uri"]).netloc
                and request.url.scheme == "http"
            )
            if not valid:
                return self._callback_page(
                    "This authorization callback is invalid, expired, or already used.", 400
                )
            if any(len(query.getlist(name)) > 1 for name in ("code", "client_id", "error")):
                return self._callback_page(
                    "The authorization callback contains duplicate parameters.", 400
                )
            pending["consumed"] = True
            pending["phase"] = "exchanging"
            candidate = None
            client_id = None
            committed = False
            try:
                if state["revision"] != pending["revision"]:
                    raise HTTPException(409, "The connection changed during authorization.")
                owner = await Users.get_user_by_id(pending["owner_id"])
                if owner is None or owner.role != "admin":
                    raise HTTPException(
                        403, "The administrator who started this flow no longer has access."
                    )
                if query.get("error"):
                    raise HTTPException(400, "ChatGPT authorization was cancelled or denied.")
                code = query.get("code")
                supplied_client = query.get("client_id")
                client_id = pending["client_id"] or supplied_client
                if not code or not client_id or client_id == "dynamic_agent_client":
                    raise HTTPException(
                        400, "The callback is missing the authorization code or issued client ID."
                    )
                if pending["client_id"] and supplied_client not in (None, pending["client_id"]):
                    raise HTTPException(
                        400, "The returned client ID does not match this registration."
                    )
                if not pending["client_id"]:
                    # Retain an issued ID even if code exchange fails; don't create another client.
                    state["registration"] = {
                        "client_id": client_id,
                        "subject": None,
                        "email": None,
                        "issuer": ISSUER,
                    }
                    self.store.save(state)
                result = await self.auth.request_token(
                    {
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": code,
                        "code_verifier": pending["verifier"],
                        "redirect_uri": pending["redirect_uri"],
                        "resource": RESOURCE,
                    }
                )
                if result.status_code != 200:
                    raise HTTPException(
                        400,
                        f"Authorization code exchange failed (HTTP {result.status_code}, {oauth_error_code(result)}).",
                    )
                candidate = result.json()
                identity = await self.auth.verify_identity(
                    candidate["id_token"], client_id, pending["nonce"]
                )
                if pending["subject"] and identity["sub"] != pending["subject"]:
                    raise HTTPException(
                        400, "The signed-in identity does not match this registration."
                    )
                tokens = token_record(candidate)
                if self._pending is not pending:
                    raise HTTPException(
                        409, "Authorization was cancelled before the credentials were saved."
                    )
                await self._settings()
                owner = await Users.get_user_by_id(pending["owner_id"])
                if owner is None or owner.role != "admin":
                    raise HTTPException(
                        403, "The administrator who started this flow no longer has access."
                    )
                if self._pending is not pending:
                    raise HTTPException(
                        409, "Authorization was cancelled before the credentials were saved."
                    )
                granted = PLAN_SCOPE in tokens["scopes"] and "resource.invoke" in tokens["scopes"]
                state["registration"] = {
                    "client_id": client_id,
                    "subject": identity["sub"],
                    "email": identity.get("email"),
                    "issuer": identity["iss"],
                    "plan_permission": granted,
                }
                state["tokens"] = tokens
                state["revision"] += 1
                self.store.save(state)
                committed = True
                message = (
                    "ChatGPT is connected. You can close this tab and stop the SSH tunnel."
                    if granted
                    else "Your identity was verified, but subscription access was not granted. Return to Open WebUI and try connecting again."
                )
                return self._callback_page(message)
            except HTTPException as error:
                return self._callback_page(str(error.detail), error.status_code)
            except (ValueError, KeyError, TypeError, jwt.PyJWTError, httpx.HTTPError):
                return self._callback_page(
                    "The authorization response could not be verified. Existing credentials were not changed.",
                    400,
                )
            finally:
                self._pending = None
                if not committed and isinstance(candidate, dict):
                    refresh = candidate.get("refresh_token")
                    previous_refresh = (state.get("tokens") or {}).get("refresh_token")
                    if isinstance(refresh, str) and refresh and refresh != previous_refresh:
                        await self.auth.revoke(refresh, client_id)

    @staticmethod
    def _callback_page(message: str, status=200):
        tone = "success" if status < 400 else "error"
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>ChatGPT Connect</title>
  <style>
    :root {{ color-scheme: light dark; font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; }}
    body {{ display: grid; min-height: 100vh; margin: 0; place-items: center; background: #f7f7f5; color: #171717; }}
    main {{ width: min(420px, calc(100% - 32px)); text-align: center; }}
    img {{ width: 64px; height: 64px; border-radius: 18px; }}
    h1 {{ margin: 18px 0 8px; font-size: 24px; }}
    p {{ margin: 0; color: #6b6b66; }}
    .success {{ color: #0d8c6d; }}
    .error {{ color: #c43d3d; }}
    .hint {{ margin-top: 12px; font-size: 13px; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #161616; color: #f4f4f0; }}
      p {{ color: #aaa9a2; }}
    }}
  </style>
</head>
<body>
  <main>
    <img src="{MODEL_AVATAR_URL}" alt="">
    <h1 class="{tone}">{"Connected" if status < 400 else "Sign-in failed"}</h1>
    <p>{escape(message)}</p>
    <p class="hint">You can close this tab and return to Open WebUI.</p>
  </main>
</body>
</html>"""
        return HTMLResponse(
            html,
            status_code=status,
            headers={
                **NO_STORE,
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def status(self, request: Request, user=Depends(get_admin_user)):
        await self._admin_request(request, user)
        async with self.store.locked() as state:
            return JSONResponse(self._status_payload(state), headers=NO_STORE)

    async def cancel(self, request: Request, user=Depends(get_admin_user)):
        await self._admin_request(request, user, write=True)
        async with self.store.locked() as state:
            pending = self._pending
            if pending and pending["owner_id"] != user.id:
                raise HTTPException(409, "This sign-in was started by another administrator.")
            if pending and pending["phase"] != "waiting":
                raise HTTPException(409, "ChatGPT is already finishing this sign-in.")
            self._pending = None
            return JSONResponse({"status": self._status_payload(state)}, headers=NO_STORE)

    async def list_models(self, request: Request, user=Depends(get_admin_user)):
        await self._admin_request(request, user)
        catalog = await self._model_catalog()
        models = []
        for item in catalog:
            existing = await Models.get_model_by_id(item["id"])
            manageable = existing is None or existing.base_model_id is None
            _, _, appearance_ready = self._avatar_state(existing)
            models.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "enabled": existing.is_active if existing and manageable else True,
                    "manageable": manageable,
                    "synchronized": not manageable or appearance_ready,
                    "reasoning_efforts": item["reasoning_efforts"],
                }
            )
        return JSONResponse({"models": models}, headers=NO_STORE)

    async def save_models(
        self, request: Request, body: ModelSelectionRequest, user=Depends(get_admin_user)
    ):
        await self._admin_request(request, user, write=True)
        if not body.enabled:
            raise HTTPException(400, "Select at least one ChatGPT model.")
        selected = set(body.enabled)
        if len(selected) != len(body.enabled):
            raise HTTPException(400, "The model selection contains duplicates.")

        catalog = await self._model_catalog()
        available = {item["id"] for item in catalog}
        unknown = selected - available
        if unknown:
            raise HTTPException(400, "The model selection contains an unavailable model.")

        updated = 0
        preserved = 0
        for item in catalog:
            existing = await Models.get_model_by_id(item["id"])
            if existing is not None and existing.base_model_id is not None:
                if item["id"] not in selected:
                    raise HTTPException(
                        409,
                        f"Model {item['id']} is managed by an existing Workspace Model.",
                    )
                continue

            enabled = item["id"] in selected
            meta, custom_avatar, _ = self._avatar_state(existing)
            marker = meta.get(APPEARANCE_META_KEY)
            current_avatar = meta.get("profile_image_url")
            avatar_changed = False
            if custom_avatar:
                preserved += 1
            else:
                expected_marker = {"avatar": MODEL_AVATAR_FINGERPRINT}
                avatar_changed = current_avatar != MODEL_AVATAR_URL or marker != expected_marker
                meta["profile_image_url"] = MODEL_AVATAR_URL
                meta[APPEARANCE_META_KEY] = expected_marker

            if existing and existing.is_active == enabled and not avatar_changed:
                continue
            form = ModelForm(
                id=item["id"],
                base_model_id=None,
                name=existing.name if existing else item["name"],
                params=self._model_data(existing.params) if existing else {},
                meta=meta,
                is_active=enabled,
            )
            result = (
                await Models.update_model_by_id(item["id"], form)
                if existing
                else await Models.insert_new_model(form, user.id)
            )
            if result is None:
                raise HTTPException(500, f"Could not save model {item['id']}.")
            updated += 1
        return JSONResponse(
            {
                "total": len(catalog),
                "enabled": len(selected),
                "updated": updated,
                "custom_avatars_preserved": preserved,
            },
            headers=NO_STORE,
        )

    @staticmethod
    def _model_data(value) -> dict:
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return {}

    def _avatar_state(self, existing) -> tuple[dict, bool, bool]:
        meta = self._model_data(existing.meta) if existing else {}
        marker = meta.get(APPEARANCE_META_KEY)
        managed = isinstance(marker, dict) and isinstance(marker.get("avatar"), str)
        current = meta.get("profile_image_url")
        custom = bool(existing and current and current != MODEL_AVATAR_URL and not managed)
        ready = bool(
            custom
            or (current == MODEL_AVATAR_URL and marker == {"avatar": MODEL_AVATAR_FINGERPRINT})
        )
        return meta, custom, ready

    async def sign_out(self, request: Request, user=Depends(get_admin_user)):
        await self._admin_request(request, user, write=True)
        async with self.store.locked() as state:
            tokens, registration = state.get("tokens") or {}, state.get("registration") or {}
            confirmed = not tokens
            if tokens.get("refresh_token"):
                confirmed = await self.auth.revoke(
                    tokens["refresh_token"], registration["client_id"]
                )
                if tokens.get("refresh_in_flight"):
                    confirmed = (
                        False  # The saved token might no longer be the latest rotated token.
                    )
            if registration and tokens:
                registration["plan_permission"] = bool(
                    PLAN_SCOPE in tokens.get("scopes", [])
                    and "resource.invoke" in tokens.get("scopes", [])
                )
                state["registration"] = registration
            state["tokens"] = None
            state["revision"] += 1
            self.store.save(state)
            self._pending = None
            status = self._status_payload(state)
        return JSONResponse(
            {"session_revocation_confirmed": confirmed, "status": status}, headers=NO_STORE
        )

    async def admin_page(self, request: Request, user=Depends(get_admin_user)):
        settings = await self._admin_request(request, user)
        port = self._backend_port(request)
        try:
            ssh_command = self._ssh_command(settings, port)
        except HTTPException:
            ssh_command = ""
        issued = str(int(time.time()))
        csrf = f"{issued}.{self._csrf(request, user, issued)}"
        nonce = secrets.token_urlsafe(24)
        async with self.store.locked() as state:
            initial_state = escape(
                json.dumps(self._status_payload(state), separators=(",", ":")), quote=True
            )
        html = (
            ADMIN_HTML.replace("__CSRF__", csrf)
            .replace("__NONCE__", nonce)
            .replace("__INITIAL_STATE__", initial_state)
            .replace("__BASE_URL__", escape(f"http://127.0.0.1:{port}/siwc/v1", quote=True))
            .replace("__SSH_COMMAND__", escape(ssh_command, quote=True))
            .replace('src="chatgpt.png"', f'src="{MODEL_AVATAR_URL}"')
        )
        return HTMLResponse(
            html,
            headers={
                **NO_STORE,
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'",
            },
        )
