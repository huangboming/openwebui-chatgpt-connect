"""Small host/network boundary fixtures, not an Open WebUI implementation."""

import json
import re
import sys
import time
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

REAL_ASYNC_CLIENT = httpx.AsyncClient
KEY = "internal-key-" + "k" * 32
WEBUI_URL = "https://webui.example.test:8080"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


def install_modules(monkeypatch, bindings):
    for name, values in bindings.items():
        module = sys.modules.get(name)
        if module is None:
            module = ModuleType(name)
            monkeypatch.setitem(sys.modules, name, module)
        for key, value in values.items():
            monkeypatch.setattr(module, key, value, raising=False)
        if "." in name:
            parent, child = name.rsplit(".", 1)
            monkeypatch.setattr(sys.modules[parent], child, module, raising=False)


def events(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


class WorkspaceModelForm(BaseModel):
    id: str
    base_model_id: str | None = None
    name: str
    params: dict
    meta: dict
    access_grants: list[dict] | None = None
    is_active: bool = True


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content
        self.closed = False

    async def __aiter__(self):
        for byte in self.content:
            yield bytes([byte])

    async def aclose(self):
        self.closed = True


@dataclass
class Host:
    env: SimpleNamespace
    active: bool = True
    valves: dict = field(default_factory=lambda: {"INTERNAL_API_KEY": KEY, "SSH_TARGET": "user@ct"})
    users: dict = field(
        default_factory=lambda: {
            "admin": SimpleNamespace(id="admin", role="admin"),
            "user": SimpleNamespace(id="user", role="user"),
        }
    )
    workspace_models: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    async def get(self, key, default=None):
        return self.config.get(key, default)

    async def has_permission(self, user_id, permission, permissions):
        category, name = permission.split(".")
        return user_id in self.users and permissions.get(category, {}).get(name, False)

    async def get_function_by_id(self, _id):
        return SimpleNamespace(is_active=self.active)

    async def get_function_valves_by_id(self, _id):
        return self.valves

    async def get_user_by_id(self, user_id):
        return self.users.get(user_id)

    async def get_model_by_id(self, model_id):
        return self.workspace_models.get(model_id)

    async def insert_new_model(self, form, user_id):
        if form.id in self.workspace_models:
            return None
        data = form.model_dump()
        data["access_grants"] = data["access_grants"] or []
        model = SimpleNamespace(**data, user_id=user_id)
        self.workspace_models[form.id] = model
        return model

    async def update_model_by_id(self, model_id, form):
        existing = self.workspace_models.get(model_id)
        if existing is None:
            return None
        data = form.model_dump()
        data["access_grants"] = data["access_grants"] or existing.access_grants
        model = SimpleNamespace(**data, user_id=existing.user_id)
        self.workspace_models[model_id] = model
        return model

    async def get_admin_user(self, request: Request):
        # Only the dependency contract is supplied; WebUI owns real session verification.
        user = self.users.get(request.cookies.get("token"))
        if user is None:
            raise HTTPException(401, "Not authenticated")
        if user.role != "admin":
            raise HTTPException(403, "Admin required")
        return user


class OpenAI:
    def __init__(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        key.update(kid="test-key", alg="RS256", use="sig")
        self.key = key
        self.requests: list[httpx.Request] = []
        self.nonce = ""
        self.claims: dict = {}
        self.token_overrides: dict = {}
        self.expires_in = 3600
        self.on_token = None
        self.response_status = 200
        self.response_error = {"error": {"code": "usage_limit", "message": "quota"}}
        self.model_status = 200
        self.models = [
            {
                "slug": "catalog-model",
                "display_name": "Catalog Model",
                "visibility": "list",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "medium"},
                ],
            },
            {"slug": "hidden-model", "visibility": "hidden"},
        ]
        self.events = [
            {"type": "response.output_text.delta", "delta": "Price: \u20ac5"},
            {
                "type": "response.completed",
                "response": {
                    "id": "response-test",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "Price: \u20ac5"},
                            ],
                        }
                    ],
                },
            },
        ]
        self.streams: list[ByteStream] = []

    def calls(self, path: str):
        return [request for request in self.requests if request.url.path == path]

    def token_data(self):
        sequence = len(self.calls("/api/accounts/oauth/token"))
        claims = {
            "iss": "https://auth.openai.com",
            "aud": "issued-client",
            "sub": "account",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "nonce": self.nonce,
            "email": "account@example.test",
            **self.claims,
        }
        return {
            "access_token": f"access-{sequence}",
            "refresh_token": f"refresh-{sequence}",
            "id_token": jwt.encode(
                claims, self.private_key, algorithm="RS256", headers={"kid": "test-key"}
            ),
            "expires_in": self.expires_in,
            "token_type": "Bearer",
            "scope": SCOPES,
            **self.token_overrides,
        }

    async def handle(self, request: httpx.Request):
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "auth.openai.com":
            if path == "/.well-known/openid-configuration":
                return httpx.Response(
                    200,
                    json={
                        "issuer": "https://auth.openai.com",
                        "jwks_uri": "https://auth.openai.com/jwks",
                        "revocation_endpoint": "https://auth.openai.com/revoke",
                    },
                )
            if path == "/jwks":
                return httpx.Response(200, json={"keys": [self.key]})
            if path == "/api/accounts/oauth/token":
                if self.on_token is not None:
                    return await self.on_token(request)
                return httpx.Response(200, json=self.token_data())
            if path == "/revoke":
                return httpx.Response(200)
        elif request.url.host == "api.openai.com":
            if path == "/v1/models":
                if self.model_status != 200:
                    return httpx.Response(self.model_status, json=self.response_error)
                return httpx.Response(200, json={"models": self.models})
            if path == "/v1/responses":
                if self.response_status != 200:
                    return httpx.Response(
                        self.response_status,
                        json=self.response_error,
                        headers={
                            "x-request-id": "request-test",
                            "retry-after": "30",
                        },
                    )
                content = "".join(
                    f"event: {event['type']}\r\ndata: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"
                    for event in self.events
                ).encode()
                stream = ByteStream(content)
                self.streams.append(stream)
                return httpx.Response(
                    200,
                    stream=stream,
                    headers={
                        "content-type": "text/event-stream",
                        "x-request-id": "request-test",
                    },
                )
        raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")


class Harness:
    def __init__(self, module, host: Host, upstream: OpenAI):
        self.module = module
        self.host = host
        self.upstream = upstream
        self.app = None
        self.event = None
        self.client = None
        self.csrf = None

    async def restart(self):
        if self.client is not None:
            await self.client.aclose()
        self.app = FastAPI()
        self.event = self.module.Event()
        await self.event.event(
            {}, __event_name__="system.startup.completed", __id__="connect", __app__=self.app
        )
        self.client = REAL_ASYNC_CLIENT(
            transport=httpx.ASGITransport(app=self.app), base_url=WEBUI_URL
        )
        self.client.cookies.set("token", "admin", domain="webui.example.test")
        self.csrf = None

    async def page(self):
        response = await self.client.get("/siwc/admin")
        assert response.status_code == 200
        self.csrf = re.search(r'<meta name="siwc-csrf" content="([^"]+)">', response.text)[1]
        return response

    async def post_admin(self, path: str, **kwargs):
        if self.csrf is None:
            await self.page()
        return await self.client.post(
            "/siwc/admin/" + path, headers={"x-siwc-csrf": self.csrf}, **kwargs
        )

    async def prepare(self, consent=False):
        response = await self.post_admin("start", json={"request_consent": consent})
        assert response.status_code == 200
        path = response.json()["status"]["pending"]["authorize_path"]
        entry = await self.client.get(path, follow_redirects=False)
        assert entry.status_code == 303
        params = parse_qs(urlsplit(entry.headers["location"]).query)
        self.upstream.nonce = params["nonce"][0]
        return response.json(), params

    async def callback(self, params, **changes):
        query = {
            "code": "authorization-code",
            "client_id": "issued-client",
            "state": params["state"][0],
            **changes,
        }
        async with REAL_ASYNC_CLIENT(
            transport=httpx.ASGITransport(app=self.app)
        ) as callback_client:
            return await callback_client.get(params["redirect_uri"][0], params=query)

    async def connect(self):
        _, params = await self.prepare()
        response = await self.callback(params)
        assert response.status_code == 200
        return params

    async def status(self):
        return (await self.client.get("/siwc/admin/status")).json()

    async def models(self):
        return await self.client.get("/siwc/v1/models", headers={"Authorization": f"Bearer {KEY}"})

    async def respond(self, **changes):
        return await self.client.post(
            "/siwc/v1/responses",
            headers={"Authorization": f"Bearer {KEY}"},
            json={
                "model": "catalog-model",
                "input": "hello",
                **changes,
            },
        )
