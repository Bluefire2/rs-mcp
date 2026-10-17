"""Serving under a URL prefix (PUBLIC_BASE_URL=https://rs.example.com/osrs), as behind a Cloud Run
domain mapping, which can only map a whole hostname. Every URL Claude, Google and the RuneLite
plugin use must line up under the prefix, with OAuth discovery at the domain root."""

import pytest
from starlette.testclient import TestClient

from osrs_mcp.server import http_app
from tests.test_auth import INIT, MCP_HEADERS, auth_settings

ORIGIN = "https://rs.example.com"
BASE = f"{ORIGIN}/osrs"
TOKEN = "prefix-test-ingest-token-0123456789abcdef"
CLAUDE_CODE_CIMD = "https%3A%2F%2Fclaude.ai%2Foauth%2Fclaude-code-client-metadata"


@pytest.fixture
def client():
    settings = auth_settings(
        public_base_url=BASE, gim_group="osrs-mcp", ingest_token=TOKEN, default_rsn="Test Player"
    )
    with TestClient(http_app(settings), base_url=ORIGIN) as test_client:
        yield test_client


def test_mcp_endpoint_challenges_with_root_metadata_pointer(client):
    response = client.post("/osrs/mcp", json=INIT, headers=MCP_HEADERS)
    assert response.status_code == 401
    pointer = f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/osrs/mcp"'
    assert pointer in response.headers["www-authenticate"]


def test_protected_resource_matches_connector_url(client):
    metadata = client.get("/.well-known/oauth-protected-resource/osrs/mcp").json()
    # Must equal the URL entered in claude.ai exactly.
    assert metadata["resource"] == f"{BASE}/mcp"
    assert metadata["authorization_servers"][0].rstrip("/") == BASE


def test_authorization_server_metadata_at_rfc8414_location(client):
    metadata = client.get("/.well-known/oauth-authorization-server/osrs").json()
    assert metadata["issuer"].rstrip("/") == BASE
    for endpoint in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert metadata[endpoint].startswith(f"{BASE}/"), endpoint
    assert metadata["client_id_metadata_document_supported"] is True


def test_advertised_oauth_endpoints_are_served_under_the_prefix(client):
    response = client.get(
        "/osrs/authorize?response_type=code&client_id="
        + CLAUDE_CODE_CIMD
        + "&redirect_uri=http%3A%2F%2Flocalhost%3A3000%2Fcallback&state=x"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256",
        follow_redirects=False,
    )
    # Valid request: on to the consent page, still under the prefix. (Fetching Claude Code's
    # CIMD document needs the network; anything but a 404 proves the route is mounted.)
    assert response.status_code != 404
    if response.status_code == 302:
        assert response.headers["location"].startswith(f"{BASE}/consent")
    assert client.post("/osrs/token", data={}).status_code != 404
    assert client.post("/osrs/register", json={}).status_code != 404


def test_plugin_ingest_resolves_under_the_prefix(client):
    # The plugin appends /api/group/... to its base URL, which is set to BASE.
    url = "/osrs/api/group/osrs-mcp/am-i-in-group?member_name=Test Player"
    assert client.get(url, headers={"authorization": TOKEN}).status_code == 200
    assert client.get(url, headers={"authorization": "nope"}).status_code == 401


def test_pages_and_health_under_the_prefix(client):
    home = client.get("/osrs/")
    assert home.status_code == 200 and f"{BASE}/mcp" in home.text
    assert 'href="privacy"' in home.text  # relative, so it stays under the prefix
    assert client.get("/osrs/privacy").status_code == 200
    assert client.get("/osrs/health").json() == {"status": "ok"}


def test_bare_domain_redirects_to_the_prefix(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/osrs/"


def test_no_prefix_serves_at_the_root():
    with TestClient(http_app(auth_settings()), base_url="http://localhost:8000") as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["resource"] == "http://localhost:8000/mcp"
