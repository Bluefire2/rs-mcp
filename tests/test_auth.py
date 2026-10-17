"""OAuth wiring: what Claude's OAuth client needs to discover and use the server, the email
allowlist, and failing closed on missing configuration."""

import pytest
from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleTokenVerifier
from starlette.testclient import TestClient

from osrs_mcp.auth import AllowlistGoogleVerifier, AuthConfigError, email_allowed, make_auth
from osrs_mcp.config import Settings
from osrs_mcp.server import MCP_PATH, create_app
from osrs_mcp.store import make_store

BASE = "http://localhost:8000"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def auth_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "auth_mode": "google",
        "google_client_id": "test-client.apps.googleusercontent.com",
        "google_client_secret": "test-secret",
        "allowed_email": "Owner@Example.com",
        "jwt_signing_key": "x" * 48,
        "storage_encryption_key": Fernet.generate_key().decode(),
        "store_backend": "memory",
        "public_base_url": BASE,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def client():
    app = create_app(auth_settings()).http_app(path=MCP_PATH)
    with TestClient(app, base_url=BASE) as test_client:
        yield test_client


def test_unauthenticated_mcp_request_gets_401_with_metadata_pointer(client):
    response = client.post(MCP_PATH, json=INIT, headers=MCP_HEADERS)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource{MCP_PATH}"' in (
        challenge
    )


def test_garbage_bearer_token_is_rejected(client):
    headers = MCP_HEADERS | {"authorization": "Bearer not-a-real-token"}
    response = client.post(MCP_PATH, json=INIT, headers=headers)
    assert response.status_code == 401


def test_protected_resource_metadata_matches_connector_url(client):
    metadata = client.get(f"/.well-known/oauth-protected-resource{MCP_PATH}").json()
    # Claude requires `resource` to equal the URL entered in the connector dialog exactly,
    # and only uses the first authorization server.
    assert metadata["resource"] == f"{BASE}{MCP_PATH}"
    assert metadata["authorization_servers"][0].rstrip("/") == BASE


def test_authorization_server_metadata_supports_claude(client):
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"].rstrip("/") == BASE
    # Claude uses CIMD only when both of these are advertised; otherwise it falls back to DCR.
    assert metadata["client_id_metadata_document_supported"] is True
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    assert "registration_endpoint" in metadata
    assert "S256" in metadata["code_challenge_methods_supported"]
    assert "refresh_token" in metadata["grant_types_supported"]


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("missing", ["google_client_id", "allowed_email", "jwt_signing_key"])
def test_google_mode_fails_closed_without_config(missing):
    settings = auth_settings(**{missing: ""})
    with pytest.raises(AuthConfigError, match=missing.upper()):
        make_auth(settings, make_store(settings))


def test_auth_mode_none_disables_auth():
    settings = auth_settings(auth_mode="none")
    assert make_auth(settings, make_store(settings)) is None


# --- email allowlist --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "allowed"),
    [
        ({"email": "owner@example.com", "email_verified": "true"}, True),
        ({"email": "OWNER@example.com", "email_verified": True}, True),
        ({"email": "owner@example.com", "email_verified": "false"}, False),
        ({"email": "owner@example.com"}, False),
        ({"email": "someone@example.com", "email_verified": "true"}, False),
        ({}, False),
    ],
)
def test_email_allowed(claims, allowed):
    assert email_allowed(claims, "owner@example.com") is allowed


def test_email_allowed_rejects_everyone_when_unset():
    assert email_allowed({"email": "", "email_verified": "true"}, "") is False


async def test_verifier_rejects_other_accounts(monkeypatch):
    claims = {"email": "someone@example.com", "email_verified": "true"}

    async def fake_verify(self, token):
        return AccessToken(token=token, client_id="sub", scopes=[], claims=claims)

    monkeypatch.setattr(GoogleTokenVerifier, "verify_token", fake_verify)
    verifier = AllowlistGoogleVerifier(allowed_email="Owner@Example.com", audience="client")
    assert await verifier.verify_token("t") is None

    claims["email"] = "owner@example.com"
    assert (await verifier.verify_token("t")).claims["email"] == "owner@example.com"


async def test_verifier_passes_through_invalid_tokens(monkeypatch):
    async def fake_verify(self, token):
        return None

    monkeypatch.setattr(GoogleTokenVerifier, "verify_token", fake_verify)
    verifier = AllowlistGoogleVerifier(allowed_email="owner@example.com", audience="client")
    assert await verifier.verify_token("t") is None
