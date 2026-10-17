"""Google sign-in for the MCP endpoint, restricted to one email address.

Google supports neither Dynamic Client Registration nor Client ID Metadata Documents, which
Claude needs. FastMCP's OAuthProxy fills the gap: it is the authorization server Claude talks
to (offering CIMD and DCR), and it sends the user to Google to log in. On every MCP request it
re-validates the stored Google token through our verifier, which also checks the email.
"""

import logging

from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.google import GoogleTokenVerifier
from key_value.aio.protocols.key_value import AsyncKeyValue
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from osrs_mcp.config import Settings

logger = logging.getLogger(__name__)

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]

# Where MCP clients may ask to be redirected after sign-in: Claude's hosted apps (claude.com is
# listed by Anthropic as a possible future callback host) and loopback ports for Claude Code and
# the MCP Inspector.
ALLOWED_CLIENT_REDIRECTS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]


class AllowlistGoogleVerifier(GoogleTokenVerifier):
    """Google token verification that only accepts one verified email address."""

    def __init__(self, *, allowed_email: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.allowed_email = allowed_email.strip().lower()

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        if email_allowed(access.claims, self.allowed_email):
            return access
        logger.warning("Rejected sign-in from a Google account that isn't ALLOWED_EMAIL")
        return None


def email_allowed(claims: dict, allowed_email: str) -> bool:
    email = str(claims.get("email") or "").strip().lower()
    # tokeninfo reports email_verified as the string "true"; userinfo as a boolean.
    verified = str(claims.get("email_verified")).lower() == "true"
    return bool(allowed_email) and verified and email == allowed_email


class AuthConfigError(RuntimeError):
    pass


def make_auth(settings: Settings, store: AsyncKeyValue) -> OAuthProxy | None:
    if settings.auth_mode == "none":
        logger.warning("AUTH_MODE=none: the MCP endpoint is open to anyone who can reach it")
        return None

    missing = [
        name
        for name, value in [
            ("GOOGLE_CLIENT_ID", settings.google_client_id),
            ("GOOGLE_CLIENT_SECRET", settings.google_client_secret),
            ("ALLOWED_EMAIL", settings.allowed_email),
            ("JWT_SIGNING_KEY", settings.jwt_signing_key),
            ("STORAGE_ENCRYPTION_KEY", settings.storage_encryption_key),
        ]
        if not value.strip()
    ]
    if missing:
        raise AuthConfigError(
            f"AUTH_MODE=google needs {', '.join(missing)} (see .env.example), "
            "or set AUTH_MODE=none for local development with public data only."
        )

    verifier = AllowlistGoogleVerifier(
        allowed_email=settings.allowed_email,
        required_scopes=SCOPES,
        audience=settings.google_client_id,
    )
    return OAuthProxy(
        upstream_authorization_endpoint=GOOGLE_AUTHORIZE,
        upstream_token_endpoint=GOOGLE_TOKEN,
        upstream_revocation_endpoint=GOOGLE_REVOKE,
        upstream_client_id=settings.google_client_id,
        upstream_client_secret=settings.google_client_secret,
        token_verifier=verifier,
        base_url=settings.public_base_url,
        issuer_url=settings.public_base_url,
        allowed_client_redirect_uris=ALLOWED_CLIENT_REDIRECTS,
        valid_scopes=SCOPES,
        client_storage=FernetEncryptionWrapper(
            store, fernet=Fernet(settings.storage_encryption_key.encode())
        ),
        jwt_signing_key=settings.jwt_signing_key,
        # access_type=offline gets a Google refresh token, so Claude stays signed in;
        # prompt=consent makes Google return it on every sign-in, not just the first.
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        enable_cimd=True,
    )
