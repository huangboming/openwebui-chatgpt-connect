"""Protocol constants and administrator configuration."""

from pydantic import BaseModel, Field

APP_NAME = "Open WebUI ChatGPT Connect"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_PATH = "/api/v1/siwc/oauth/callback"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}
ERROR_STATUS = {
    "subscription_sharing_usage_limit_exceeded": 429,
    "subscription_sharing_usage_unavailable": 503,
    "subscription_sharing_user_unavailable": 503,
    "subscription_sharing_user_not_eligible": 403,
    "subscription_sharing_route_not_supported": 403,
    "subscription_sharing_invalid_user": 401,
    "subscription_sharing_unsupported_capability": 400,
    "chatpass_v2_scope_not_authorized": 403,
    "chatpass_v2_invalid_authorization_context": 403,
}


class ConnectionSettings(BaseModel):
    INTERNAL_API_KEY: str = Field(
        default="",
        description=(
            "A private key of at least 32 characters, shared only with the native Responses "
            "connection. Never enter a ChatGPT or OpenAI token here. "
            "[Open ChatGPT Connect](/siwc/admin)"
        ),
    )
    SSH_TARGET: str = Field(
        default="",
        description=(
            "The SSH host that runs Open WebUI, such as user@example.com or an alias from "
            "~/.ssh/config. Do not enter a password or private key."
        ),
    )
    CALLBACK_PORT: int = Field(
        default=1455,
        ge=1024,
        le=65535,
        description="An available loopback port on the computer used to sign in.",
    )
