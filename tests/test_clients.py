import httpx
import pytest
import respx

from osrs_mcp.clients.hiscores import HiscoresClient
from osrs_mcp.clients.prices import PricesClient, search
from osrs_mcp.clients.wikisync import WikiSyncClient
from osrs_mcp.clients.wom import WomClient
from osrs_mcp.errors import SourceError
from tests.conftest import fixture_json, fixture_text

HISCORES_URL = "https://secure.runescape.com/m=hiscore_oldschool/index_lite.json"


# --- hiscores ---------------------------------------------------------------------------------


async def test_hiscores_parses_skills_and_activities(http, mock_api: respx.Router):
    mock_api.get(HISCORES_URL).respond(200, text=fixture_text("hiscores_lynx_titan.json"))
    result = await HiscoresClient(http).fetch("Lynx Titan", "regular")

    assert result.rsn == "Lynx Titan"
    assert result.overall.name == "Overall"
    assert len(result.skills) == 24  # includes Sailing
    names = [s.name for s in result.skills]
    assert names[0] == "Attack" and "Sailing" in names
    attack = result.skills[0]
    assert (attack.level, attack.xp) == (99, 200_000_000)
    assert attack.rank is not None
    # Unranked entries become None rather than -1.
    assert any(a.score is None and a.rank is None for a in result.activities)
    assert any(a.name == "Zulrah" for a in result.activities)


async def test_hiscores_account_type_selects_path(http, mock_api: respx.Router):
    route = mock_api.get(
        "https://secure.runescape.com/m=hiscore_oldschool_ironman/index_lite.json"
    ).respond(200, text=fixture_text("hiscores_lynx_titan.json"))
    await HiscoresClient(http).fetch("Lynx Titan", "ironman")
    assert route.called


async def test_hiscores_not_found_is_actionable(http, mock_api: respx.Router):
    mock_api.get(HISCORES_URL).respond(404, text=fixture_text("hiscores_not_found.html"))
    with pytest.raises(SourceError, match="isn't on the regular hiscores"):
        await HiscoresClient(http).fetch("nobody", "regular")


# --- WikiSync ---------------------------------------------------------------------------------


async def test_wikisync_parses(http, mock_api: respx.Router):
    mock_api.get("https://sync.runescape.wiki/runelite/player/B0aty/STANDARD").respond(
        200, text=fixture_text("wikisync_b0aty.json")
    )
    data = await WikiSyncClient(http).fetch("B0aty")
    assert data.username == "B0aty"
    assert set(data.quests.values()) <= {0, 1, 2}
    assert "Ardougne" in data.achievement_diaries
    assert len(data.achievement_diaries["Ardougne"]["Easy"].tasks) > 0
    assert all(isinstance(i, int) for i in data.combat_achievements)


async def test_wikisync_no_data_is_actionable(http, mock_api: respx.Router):
    mock_api.get(url__regex=r"https://sync\.runescape\.wiki/.*").respond(
        400, text=fixture_text("wikisync_no_data.json")
    )
    with pytest.raises(SourceError, match="Enable the WikiSync plugin"):
        await WikiSyncClient(http).fetch("nobody")


async def test_wikisync_escapes_spaces(http, mock_api: respx.Router):
    route = mock_api.get("https://sync.runescape.wiki/runelite/player/Lynx%20Titan/STANDARD")
    route.respond(200, text=fixture_text("wikisync_b0aty.json"))
    await WikiSyncClient(http).fetch("Lynx Titan")
    assert route.called


# --- Wise Old Man -----------------------------------------------------------------------------


async def test_wom_gained_parses(http, mock_api: respx.Router):
    mock_api.get("https://api.wiseoldman.net/v2/players/B0aty/gained").respond(
        200, text=fixture_text("wom_gained_week.json")
    )
    gained = await WomClient(http).gained("B0aty", "week")
    assert gained.data.skills["overall"].experience.gained > 0
    assert "zulrah" in gained.data.bosses
    assert "ehp" in gained.data.computed


async def test_wom_sends_api_key(http, mock_api: respx.Router):
    route = mock_api.get("https://api.wiseoldman.net/v2/players/B0aty/gained").respond(
        200, text=fixture_text("wom_gained_week.json")
    )
    await WomClient(http, api_key="k").gained("B0aty", "week")
    assert route.calls.last.request.headers["x-api-key"] == "k"


async def test_wom_not_found(http, mock_api: respx.Router):
    mock_api.get(url__regex=r"https://api\.wiseoldman\.net/.*").respond(
        404, text=fixture_text("wom_not_found.json")
    )
    with pytest.raises(SourceError, match="isn't tracking"):
        await WomClient(http).gained("nobody", "week")


async def test_wom_update_parses_player(http, mock_api: respx.Router):
    mock_api.post("https://api.wiseoldman.net/v2/players/Lynx%20Titan").respond(
        200, text=fixture_text("wom_player.json")
    )
    player = await WomClient(http).update("Lynx Titan")
    assert player.displayName.lower() == "lynx titan"
    assert player.updatedAt is not None


async def test_wom_update_rate_limited(http, mock_api: respx.Router):
    mock_api.post(url__regex=r"https://api\.wiseoldman\.net/.*").mock(
        return_value=httpx.Response(429, json={"message": "Too many requests"})
    )
    with pytest.raises(SourceError, match="rate limit"):
        await WomClient(http).update("x")


# --- prices -----------------------------------------------------------------------------------


async def test_prices_mapping_and_latest(http, mock_api: respx.Router):
    mock_api.get("https://prices.runescape.wiki/api/v1/osrs/mapping").respond(
        200, text=fixture_text("prices_mapping.json")
    )
    mock_api.get("https://prices.runescape.wiki/api/v1/osrs/latest").respond(
        200, text=fixture_text("prices_latest.json")
    )
    client = PricesClient(http)
    items = await client.mapping()
    latest = await client.latest()
    whip = next(i for i in items if i.name == "Abyssal whip")
    assert whip.id == 4151 and whip.limit == 70
    assert latest[4151].high is not None


def test_search_prefers_exact_then_substring_then_fuzzy():
    from osrs_mcp.clients.prices import ItemInfo

    items = [ItemInfo.model_validate(i) for i in fixture_json("prices_mapping.json")]
    assert [i.name for i in search(items, "abyssal WHIP", 5)] == ["Abyssal whip"]
    assert search(items, "bandos", 5)[0].name.startswith("Bandos")
    assert search(items, "abysal wip", 5)[0].name == "Abyssal whip"
    assert search(items, "qqqqqq", 5) == []
