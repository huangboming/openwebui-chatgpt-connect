"""Open WebUI lifecycle, route ownership, and internal connection authentication."""

import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from open_webui import env as webui_env
from open_webui.models.functions import Functions
from starlette.routing import Mount

from .admin import AdminRoutes, install_callback_log_filter
from .auth import ChatGPTAuth, CredentialStore
from .responses import ResponsesAPI
from .settings import CALLBACK_PATH, ConnectionSettings

BEARER = HTTPBearer(auto_error=False)


class Event:
    Valves = ConnectionSettings

    def __init__(self):
        self.valves = self.Valves()
        self._function_id: str | None = None
        store = CredentialStore(Path(webui_env.DATA_DIR) / "siwc-connect")
        auth = ChatGPTAuth(store)
        self.responses_api = ResponsesAPI(auth)
        self.admin = AdminRoutes(auth, self._settings, self.responses_api.management_models)

    async def event(
        self, event: dict, __event_name__: str = None, __id__: str = None, __app__=None
    ):
        if __app__ is None or __id__ is None:
            return
        self._function_id = __id__
        own_event = (event.get("subject") or {}).get("id") == __id__
        if __event_name__ == "system.startup.completed" or (
            __event_name__ in {"function.enable_started", "function.updated"} and own_event
        ):
            self._install_routes(__app__)
        elif __event_name__ == "system.shutdown.started" or (
            __event_name__ == "function.disable_started" and own_event
        ):
            self.admin.cancel_pending()
            self._remove_routes(__app__)

    def _route_prefix(self):
        return f"openwebui-chatgpt-connect:{self._function_id}:"

    def _remove_routes(self, app):
        prefix = self._route_prefix()
        app.router.routes = [
            route
            for route in app.router.routes
            if not (getattr(route, "name", "") or "").startswith(prefix)
        ]
        app.openapi_schema = None

    def _install_routes(self, app):
        specs = [
            ("/siwc/admin", self.admin.admin_page, "GET"),
            ("/siwc/admin/status", self.admin.status, "GET"),
            ("/siwc/admin/start", self.admin.start, "POST"),
            ("/siwc/admin/cancel", self.admin.cancel, "POST"),
            ("/siwc/admin/models", self.admin.list_models, "GET"),
            ("/siwc/admin/models", self.admin.save_models, "POST"),
            ("/siwc/admin/sign-out", self.admin.sign_out, "POST"),
            ("/siwc/authorize/{attempt_id}", self.admin.authorize, "GET"),
            (CALLBACK_PATH, self.admin.callback, "GET"),
            ("/siwc/v1/models", self.responses_api.models, "GET"),
            ("/siwc/v1/responses", self.responses_api.responses, "POST"),
        ]
        paths = {spec[0] for spec in specs}
        prefix = self._route_prefix()
        for route in app.router.routes:
            if getattr(route, "path", None) in paths and not (
                getattr(route, "name", "") or ""
            ).startswith(prefix):
                raise RuntimeError("Another Function already owns a required /siwc route.")
        self._remove_routes(app)
        router = APIRouter()
        for path, endpoint, method in specs:
            dependencies = [Depends(self._require_key)] if path.startswith("/siwc/v1/") else []
            router.add_api_route(
                path,
                endpoint,
                methods=[method],
                dependencies=dependencies,
                name=f"{prefix}{endpoint.__name__}",
                include_in_schema=False,
            )
        routes = list(app.router.routes)
        index = next(
            (
                i
                for i, route in enumerate(routes)
                if isinstance(route, Mount) and route.path in ("", "/")
            ),
            len(routes),
        )
        routes[index:index] = router.routes
        app.router.routes = routes
        app.openapi_schema = None
        install_callback_log_filter()

    async def _settings(self) -> ConnectionSettings:
        function = await Functions.get_function_by_id(self._function_id)
        if function is None or not function.is_active:
            raise HTTPException(404, "This Function is not active.")
        valves = await Functions.get_function_valves_by_id(self._function_id)
        return self.Valves(**(valves or {}))

    async def _require_key(
        self, credentials: HTTPAuthorizationCredentials | None = Depends(BEARER)
    ):
        settings = await self._settings()
        expected = settings.INTERNAL_API_KEY
        if len(expected) < 32:
            raise HTTPException(503, "Set INTERNAL_API_KEY to at least 32 characters.")
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), expected.encode()
        ):
            raise HTTPException(
                401, "Invalid internal API key.", headers={"WWW-Authenticate": "Bearer"}
            )
