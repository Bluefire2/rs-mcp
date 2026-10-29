"""The public endpoint (<PUBLIC_BASE_URL>/public/mcp): static bearer tokens, a fixed set of
public-data tools, and no owner defaults or private data, whatever the owner's settings say."""

import json
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from key_value.aio.stores.memory import MemoryStore
from mcp.shared.exceptions import MCPError
from starlette.testclient import TestClient

from osrs_mcp.config import Settings
from osrs_mcp.http import HttpClient
from osrs_mcp.public_auth import StaticBearerVerifier
from osrs_mcp.server import create_public_app, http_app
from osrs_mcp.snapshots import SnapshotStore
from osrs_mcp.sources import Sources
from tests.conftest import fixture_text
from tests.test_auth import INIT, MCP_HEADERS, auth_settings
from tests.test_tools import make_client, wikisync_collection_log

ORIGIN = "https://rs.example.com"
BASE = f"{ORIGIN}/osrs"
TOKEN = "public-test-token-0123456789abcdef"
OTHER_TOKEN = "second-consumer-token-fedcba9876543210"

# Exactly these: a private tool added later must not appear on the public endpoint by accident.
PUBLIC_TOOLS = {
    "get_stats",
    "get_activity_scores",
    "get_quest_progress",
    "get_diary_progress",
    "get_combat_achievements",
    "get_collection_log",
    "get_gains",
    "get_item_price",
    "get_price_history",
    "search_wiki",
    "get_wiki_page",
}
PLAYER_TOOLS = sorted(
    PUBLIC_TOOLS - {"get_item_price", "get_price_history", "search_wiki", "get_wiki_page"}
)


def public_settings(**overrides) -> Settings:
    values = {
        "public_base_url": BASE,
        "public_api_tokens": f"{TOKEN}, {OTHER_TOKEN}",
        "default_rsn": "Lynx Titan",
    }
    return auth_settings(**(values | overrides))


@pytest.fixture
def client():
    with TestClient(http_app(public_settings()), base_url=ORIGIN) as test_client:
        yield test_client


# --- HTTP and auth ----------------------------------------------------------------------------


def test_public_endpoint_needs_a_token(client):
    response = client.post("/osrs/public/mcp", json=INIT, headers=MCP_HEADERS)
    assert response.status_code == 401
    # A static token can't be obtained by OAuth, so don't send clients to the Google sign-in.
    assert "resource_metadata" not in response.headers["www-authenticate"]


def test_public_endpoint_rejects_a_wrong_token(client):
    for token in ("wrong", f"{TOKEN}x", TOKEN[:-1], f"{TOKEN},{OTHER_TOKEN}"):
        headers = MCP_HEADERS | {"authorization": f"Bearer {token}"}
        response = client.post("/osrs/public/mcp", json=INIT, headers=headers)
        assert response.status_code == 401, token
        assert "resource_metadata" not in response.headers["www-authenticate"]


def test_each_configured_token_is_accepted(client):
    for token in (TOKEN, OTHER_TOKEN):
        headers = MCP_HEADERS | {"authorization": f"Bearer {token}"}
        assert client.post("/osrs/public/mcp", json=INIT, headers=headers).status_code == 200


def test_owner_endpoint_unchanged_and_closed_to_public_tokens(client):
    headers = MCP_HEADERS | {"authorization": f"Bearer {TOKEN}"}
    response = client.post("/osrs/mcp", json=INIT, headers=headers)
    assert response.status_code == 401
    response = client.post("/osrs/mcp", json=INIT, headers=MCP_HEADERS)
    pointer = f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/osrs/mcp"'
    assert pointer in response.headers["www-authenticate"]
    assert client.get("/osrs/health").json() == {"status": "ok"}


def test_not_mounted_without_tokens():
    settings = public_settings(public_api_tokens=" , ")
    assert not settings.public_enabled
    with TestClient(http_app(settings), base_url=ORIGIN) as test_client:
        headers = MCP_HEADERS | {"authorization": f"Bearer {TOKEN}"}
        assert test_client.post("/osrs/public/mcp", json=INIT, headers=headers).status_code == 404


def test_mounted_at_root_without_a_prefix():
    settings = public_settings(public_base_url="http://localhost:8000")
    with TestClient(http_app(settings), base_url="http://localhost:8000") as test_client:
        assert test_client.post("/public/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        headers = MCP_HEADERS | {"authorization": f"Bearer {TOKEN}"}
        assert test_client.post("/public/mcp", json=INIT, headers=headers).status_code == 200
        assert test_client.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        assert test_client.get("/health").json() == {"status": "ok"}


async def test_verifier_names_the_matching_token():
    verifier = StaticBearerVerifier([TOKEN, OTHER_TOKEN])
    first, second = await verifier.verify_token(TOKEN), await verifier.verify_token(OTHER_TOKEN)
    assert first.client_id.startswith("public-") and first.client_id != second.client_id
    assert TOKEN not in first.client_id  # loggable: the id doesn't reveal the token
    assert (await verifier.verify_token(TOKEN)).client_id == first.client_id
    assert await verifier.verify_token("") is None
    assert await StaticBearerVerifier([]).verify_token("") is None


@contextmanager
def serve_public():
    """The full ASGI app on a real port; yields the public MCP URL for a real MCP client over
    Streamable HTTP."""
    settings = public_settings(public_base_url="http://127.0.0.1")
    config = uvicorn.Config(http_app(settings), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "server didn't start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/public/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def live_server():
    with serve_public() as url:
        yield url


async def test_tool_list_over_http_is_exactly_the_public_set(live_server):
    async with Client(StreamableHttpTransport(live_server, auth=TOKEN)) as mcp:
        tools = await mcp.list_tools()
        assert {tool.name for tool in tools} == PUBLIC_TOOLS
        assert all(tool.annotations.read_only_hint for tool in tools)
        assert mcp.server_info.name == "osrs-mcp-public"
        assert "Always pass the player's name" in " ".join(mcp.instructions.split())


async def test_wrong_token_over_http_fails(live_server):
    # The client reports a 401 as a generic error; the raw status is checked above.
    with pytest.raises(MCPError):
        async with Client(StreamableHttpTransport(live_server, auth="wrong")) as mcp:
            await mcp.list_tools()


# --- tools (in memory) ------------------------------------------------------------------------


def make_public_client(**overrides) -> Client:
    settings = Settings(
        _env_file=None,
        auth_mode="none",
        store_backend="memory",
        default_rsn="Lynx Titan",
        public_api_tokens=TOKEN,
        **overrides,
    )
    sources = Sources.create(settings, HttpClient("tests/0.1", backoff_base=0))
    return Client(create_public_app(settings, sources))


async def test_tool_list_and_schemas_require_rsn():
    async with make_public_client() as mcp:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert set(tools) == PUBLIC_TOOLS
    for name in PLAYER_TOOLS:
        assert "rsn" in tools[name].input_schema["required"], name


@pytest.mark.parametrize("tool", PLAYER_TOOLS)
async def test_player_tools_need_rsn_despite_default_rsn(mock_sources, tool):
    async with make_public_client() as mcp:
        with pytest.raises(ToolError):
            await mcp.call_tool(tool, {})
        with pytest.raises(ToolError):
            await mcp.call_tool(tool, {"rsn": "   "})
    assert not mock_sources.calls  # nothing was looked up for the owner's default name


async def test_hiscores_tools_read_regular_hiscores_despite_ironman_default(mock_sources):
    async with make_public_client(default_account_type="ironman") as mcp:
        result = (await mcp.call_tool("get_stats", {"rsn": "Lynx Titan"})).structured_content
        assert result["account_type"] == "regular"
        args = {"rsn": "Lynx Titan"}
        result = (await mcp.call_tool("get_activity_scores", args)).structured_content
        assert result["account_type"] == "regular"
    hiscores = [str(c.request.url) for c in mock_sources.calls if "hiscore" in str(c.request.url)]
    assert hiscores and all("/m=hiscore_oldschool/" in url for url in hiscores)


async def test_account_type_can_still_be_chosen(mock_sources):
    mock_sources.get(
        url__regex=r"https://secure\.runescape\.com/m=hiscore_oldschool_ironman/.*"
    ).respond(200, text=fixture_text("hiscores_lynx_titan.json"))
    async with make_public_client() as mcp:
        args = {"rsn": "Lynx Titan", "account_type": "ironman"}
        result = (await mcp.call_tool("get_stats", args)).structured_content
    assert result["account_type"] == "ironman"


async def test_quest_requirements_ignore_ironman_default(mock_sources):
    # Doric's Quest needs 15 Mining for ironmen only.
    payload = json.loads(fixture_text("wikisync_b0aty.json"))
    payload["quests"]["Doric's Quest"] = 0
    payload["levels"]["Mining"] = 1
    mock_sources.get(url__regex=r"https://sync\.runescape\.wiki/.*").respond(200, json=payload)
    args = {"rsn": "B0aty", "quest": "Doric's Quest"}
    # Control: the owner's tool does change with the account type for this player, so equal
    # public results below mean the public tool ignores it, not that the fixture can't tell.
    owner = []
    for account_type in ("regular", "ironman"):
        async with make_client(default_account_type=account_type) as mcp:
            owner.append((await mcp.call_tool("get_quest_progress", args)).structured_content)
    assert owner[0]["quests"] != owner[1]["quests"]
    public = []
    for account_type in ("regular", "ironman"):
        async with make_public_client(default_account_type=account_type) as mcp:
            public.append((await mcp.call_tool("get_quest_progress", args)).structured_content)
    assert public[0]["quests"] == public[1]["quests"] == owner[0]["quests"]


async def test_collection_log_ignores_runelite_snapshot(mock_sources, monkeypatch):
    # The owner's endpoint would merge this snapshot in (2 slots); the public one must not.
    mock_sources.get(url__regex=r"https://sync\.runescape\.wiki/.*").respond(
        200, json=wikisync_collection_log([21907])
    )
    store = MemoryStore()
    await SnapshotStore(store).put("Lynx Titan", "collection_log", {"11286": 1, "21907": 3})
    args = {"rsn": "Lynx Titan", "page": "Vorkath"}
    # Control: the owner's tool sees the seeded snapshot.
    async with make_client(store=store) as mcp:
        owner = (await mcp.call_tool("get_collection_log", args)).structured_content
    assert owner["obtained"] == 2
    # The full app, with the seeded store as the one the server itself would use.
    monkeypatch.setattr("osrs_mcp.server.make_store", lambda settings: store)
    with serve_public() as url:
        async with Client(StreamableHttpTransport(url, auth=TOKEN)) as mcp:
            result = (await mcp.call_tool("get_collection_log", args)).structured_content
    assert result["obtained"] == 1 and result["runelite_as_of"] is None
    items = {item["name"]: item for item in result["page_details"][0]["items"]}
    assert not items["Draconic visage"]["obtained"]
    assert "quantity" not in items["Vorkath's head"]


async def test_collection_log_without_wikisync_data_fails(mock_sources):
    mock_sources.get(url__regex=r"https://sync\.runescape\.wiki/.*").respond(
        400, text=fixture_text("wikisync_no_data.json")
    )
    async with make_public_client() as mcp:
        with pytest.raises(ToolError):
            await mcp.call_tool("get_collection_log", {"rsn": "Lynx Titan"})


async def test_public_tools_answer(mock_sources):
    async with make_public_client() as mcp:
        quests = await mcp.call_tool("get_quest_progress", {"rsn": "B0aty"})
        assert quests.structured_content["rsn"]
        gains = await mcp.call_tool("get_gains", {"rsn": "Lynx Titan", "period": "week"})
        assert gains.structured_content["total_xp_gained"] > 0
        price = await mcp.call_tool("get_item_price", {"query": "whip"})
        assert price.structured_content["matches"][0]["name"] == "Abyssal whip"


async def test_gains_wording_fits_the_public_endpoint(mock_sources):
    async with make_public_client() as mcp:
        gains = await mcp.call_tool("get_gains", {"rsn": "Lynx Titan"})
        assert "refresh_gains_tracking" not in gains.structured_content["note"]
        mock_sources.get(url__regex=r"https://api\.wiseoldman\.net/v2/players/.*/gained.*").respond(
            404, text=fixture_text("wom_not_found.json")
        )
        with pytest.raises(ToolError, match="isn't tracking") as error:
            await mcp.call_tool("get_gains", {"rsn": "Nobody Here"})
    assert "refresh_gains_tracking" not in str(error.value)
