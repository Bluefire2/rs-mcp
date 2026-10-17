"""Exercise every tool through a real FastMCP client (in-memory transport), with outbound HTTP
mocked from captured fixtures. This covers registration, input/output schemas and error text."""

import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from key_value.aio.stores.memory import MemoryStore

from osrs_mcp.config import Settings
from osrs_mcp.http import HttpClient
from osrs_mcp.ingest.gim import GimUpdate, apply_update
from osrs_mcp.reference import SkillReq
from osrs_mcp.requirements import Checker
from osrs_mcp.server import create_app
from osrs_mcp.snapshots import SnapshotStore
from osrs_mcp.sources import Sources
from tests.conftest import fixture_json, fixture_text

EXPECTED_TOOLS = {
    "get_stats",
    "get_activity_scores",
    "get_quest_progress",
    "get_diary_progress",
    "get_combat_achievements",
    "get_gains",
    "refresh_gains_tracking",
    "get_item_price",
    "get_bank",
    "get_equipment_and_inventory",
}


@pytest.fixture
def mock_sources():
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://secure\.runescape\.com/m=hiscore_oldschool/.*").respond(
            200, text=fixture_text("hiscores_lynx_titan.json")
        )
        router.get(url__regex=r"https://sync\.runescape\.wiki/.*").respond(
            200, text=fixture_text("wikisync_b0aty.json")
        )
        router.get(url__regex=r"https://api\.wiseoldman\.net/v2/players/.*/gained.*").respond(
            200, text=fixture_text("wom_gained_week.json")
        )
        router.post(url__regex=r"https://api\.wiseoldman\.net/v2/players/.*").respond(
            200, text=fixture_text("wom_player.json")
        )
        router.get("https://prices.runescape.wiki/api/v1/osrs/mapping").respond(
            200, text=fixture_text("prices_mapping.json")
        )
        router.get("https://prices.runescape.wiki/api/v1/osrs/latest").respond(
            200, text=fixture_text("prices_latest.json")
        )
        yield router


def make_client(default_rsn: str = "Lynx Titan", store=None, **settings_overrides) -> Client:
    settings = Settings(
        _env_file=None,
        default_rsn=default_rsn,
        auth_mode="none",
        store_backend="memory",
        **settings_overrides,
    )
    sources = Sources.create(settings, HttpClient("tests/0.1", backoff_base=0))
    return Client(create_app(settings, sources, store))


async def call(name: str, args: dict | None = None, default_rsn: str = "Lynx Titan") -> dict:
    async with make_client(default_rsn) as client:
        result = await client.call_tool(name, args or {})
        return result.structured_content


async def test_tool_list_and_annotations(mock_sources):
    async with make_client() as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.description and len(tool.description) > 40, name
        assert tool.output_schema, name
        expected_read_only = name != "refresh_gains_tracking"
        assert tool.annotations.read_only_hint is expected_read_only, name


async def test_get_stats(mock_sources):
    stats = await call("get_stats")
    assert stats["total_level"] == 2278
    assert stats["combat_level"] == 126
    sailing = next(s for s in stats["skills"] if s["skill"] == "Sailing")
    # Unranked (rank -1) but the level is still real.
    assert (sailing["level"], sailing["rank"]) == (1, None)
    attack = next(s for s in stats["skills"] if s["skill"] == "Attack")
    assert attack["xp_to_99"] == 0
    assert stats["closest_99s"] == ["Sailing"]
    assert "hiscores" in stats["source"].lower()


async def test_get_activity_scores_filters(mock_sources):
    result = await call("get_activity_scores", {"category": "clues"})
    assert result["activities"]
    assert all(a["name"].startswith("Clue Scrolls") for a in result["activities"])
    assert all(a["score"] is not None for a in result["activities"])


async def test_get_quest_progress(mock_sources):
    result = await call("get_quest_progress", {"status": "all"})
    counts = result["counts"]
    assert sum(counts.values()) == len(result["quests"])
    assert counts["completed"] > 150
    completed = next(q for q in result["quests"] if q["status"] == "completed")
    assert completed["can_start"] is None and completed["missing_skills"] == []

    incomplete = await call("get_quest_progress")
    assert all(q["status"] != "completed" for q in incomplete["quests"])


async def test_get_diary_progress_summary_and_region(mock_sources):
    summary = await call("get_diary_progress")
    assert len(summary["summary"]) == 12
    assert summary["details"] == []
    ardougne_easy = summary["summary"]["Ardougne"]["Easy"]
    assert ardougne_easy["total"] == 10

    detail = await call("get_diary_progress", {"region": "kourend", "tier": "Elite"})
    assert [d["region"] for d in detail["details"]] == ["Kourend & Kebos"]


async def test_get_diary_progress_tolerates_null_complete(mock_sources):
    # Seen live: WikiSync can report "complete": null for a whole region.
    import json

    payload = json.loads(fixture_text("wikisync_b0aty.json"))
    for tier in payload["achievement_diaries"]["Western Provinces"].values():
        tier["complete"] = None
    mock_sources.get(url__regex=r"https://sync\.runescape\.wiki/.*").respond(200, json=payload)
    result = await call("get_diary_progress", {"region": "Western Provinces"})
    western = result["summary"]["Western Provinces"]["Easy"]
    assert western["complete"] is None and western["done"] == western["total"]


async def test_unexpected_wikisync_shape_is_a_clean_error(mock_sources):
    mock_sources.get(url__regex=r"https://sync\.runescape\.wiki/.*").respond(
        200, json={"username": "x"}
    )
    async with make_client() as client:
        with pytest.raises(ToolError, match="format this server doesn't understand"):
            await client.call_tool("get_quest_progress", {})


async def test_get_diary_progress_unknown_region(mock_sources):
    async with make_client() as client:
        with pytest.raises(ToolError, match="Unknown diary region"):
            await client.call_tool("get_diary_progress", {"region": "Atlantis"})


async def test_get_combat_achievements(mock_sources):
    result = await call("get_combat_achievements", {"status": "all", "limit": 300})
    total = sum(t["total"] for t in result["by_tier"].values())
    assert total == 655
    assert result["points"] > 0
    assert len(result["tasks"]) == 300
    assert result["matching_tasks"] == 655

    zulrah = await call("get_combat_achievements", {"monster": "zulrah", "status": "all"})
    assert zulrah["tasks"] and all("Zulrah" in t["monster"] for t in zulrah["tasks"])


async def test_get_gains(mock_sources):
    result = await call("get_gains", {"period": "week"})
    assert result["total_xp_gained"] > 0
    assert all(s["xp_gained"] > 0 for s in result["skills"])
    xp = [s["xp_gained"] for s in result["skills"]]
    assert xp == sorted(xp, reverse=True)


async def test_refresh_gains_tracking(mock_sources):
    result = await call("refresh_gains_tracking")
    assert result["updated_at"]


async def test_get_item_price(mock_sources):
    result = await call("get_item_price", {"query": "whip"})
    whip = result["matches"][0]
    assert whip["name"] == "Abyssal whip"
    assert whip["instant_buy"] and whip["buy_limit"] == 70


async def test_missing_default_rsn_is_actionable(mock_sources):
    async with make_client(default_rsn="") as client:
        with pytest.raises(ToolError, match="DEFAULT_RSN"):
            await client.call_tool("get_stats", {})


# --- requirement checker ----------------------------------------------------------------------


def test_checker_outcomes():
    levels = {
        "Attack": 60,
        "Strength": 60,
        "Defence": 60,
        "Hitpoints": 60,
        "Ranged": 60,
        "Prayer": 43,
        "Magic": 60,
        "Agility": 50,
    }
    quests = {"Family Crest": 2, "Waterfall Quest": 1, "Biohazard": 0}
    checker = Checker(levels, quests, is_ironman=False)

    ok = checker.check([SkillReq(skill="Agility", level=50)], ["Family Crest"])
    assert ok.met is True

    started = checker.check([], ["Started:Waterfall Quest"])
    assert started.met is True
    not_done = checker.check([], ["Waterfall Quest", "Biohazard"])
    assert not_done.met is False
    assert not_done.missing_quests == ["Waterfall Quest", "Biohazard"]

    low = checker.check([SkillReq(skill="Agility", level=60, boostable=True)], [])
    assert low.met is False
    assert low.missing_skills[0].model_dump() == {
        "skill": "Agility",
        "need": 60,
        "have": 50,
        "boostable": True,
    }

    unknown = checker.check([SkillReq(skill="Quest point", level=200)], ["Barbarian Firemaking"])
    assert unknown.met is None
    assert unknown.unverified == ["200 Quest point", "Barbarian Firemaking"]

    ironman_only = SkillReq(skill="Agility", level=90, ironman_only=True)
    assert checker.check([ironman_only], []).met is True
    assert Checker(levels, quests, is_ironman=True).check([ironman_only], []).met is False

    combat = checker.check([SkillReq(skill="Combat", level=85)], [])
    assert combat.met is False and combat.missing_skills[0].have < 85


# --- bank and gear (snapshots from the Group Ironmen Tracker plugin) --------------------------

INGEST = {"gim_group": "My Group", "ingest_token": "secret-token"}


async def seeded_store(*fixtures: str) -> MemoryStore:
    store = MemoryStore()
    snapshots = SnapshotStore(store)
    for name in fixtures:
        update = GimUpdate.model_validate(fixture_json(f"gim_update_{name}.json")["body"])
        await apply_update(snapshots, update)
    return store


async def call_snapshot_tool(name: str, args: dict, store, **overrides) -> dict:
    settings = INGEST | overrides
    async with make_client("Iron Tester", store, **settings) as client:
        result = await client.call_tool(name, args)
        return result.structured_content


async def test_get_bank_values_and_sorts(mock_sources):
    store = await seeded_store("full")
    bank = await call_snapshot_tool("get_bank", {}, store)

    assert bank["rsn"] == "Iron\u00a0Tester"
    assert bank["updated_at"]
    assert "Group Ironmen Tracker" in bank["source"]
    assert bank["item_count"] == 8
    names = [item["name"] for item in bank["items"]]
    assert names[0] == "Twisted bow"  # highest value first
    # Untradeables are listed (last, unpriced) with a placeholder name.
    assert bank["items"][-1] == {
        "name": "unknown item 6570",
        "id": 6570,
        "quantity": 1,
        "unit_price": None,
        "value": None,
    }
    coins = next(i for i in bank["items"] if i["id"] == 995)
    assert coins == {
        "name": "Coins",
        "id": 995,
        "quantity": 1234567,
        "unit_price": 1,
        "value": 1234567,
    }
    whip = next(i for i in bank["items"] if i["name"] == "Abyssal whip")
    assert whip["unit_price"] == (784892 + 769675) // 2
    assert bank["total_value"] == sum(i["value"] or 0 for i in bank["items"])


async def test_get_bank_filters(mock_sources):
    store = await seeded_store("full")
    runes = await call_snapshot_tool("get_bank", {"search": "RUNE"}, store)
    assert [i["name"] for i in runes["items"]] == ["Nature rune"]
    assert runes["matching_count"] == 1
    assert runes["item_count"] == 8  # whole-bank figures are unaffected by filters

    rich = await call_snapshot_tool("get_bank", {"min_value": 1_000_000, "limit": 2}, store)
    assert rich["matching_count"] == 3  # twisted bow, bandos chestplate, coins (whip < 1m)
    assert len(rich["items"]) == 2

    by_qty = await call_snapshot_tool("get_bank", {"sort": "quantity"}, store)
    assert by_qty["items"][0]["name"] == "Coins"
    by_name = await call_snapshot_tool("get_bank", {"sort": "name"}, store)
    assert by_name["items"][0]["name"] == "Abyssal whip"


async def test_get_bank_includes_deposit_box_items(mock_sources):
    store = await seeded_store("full", "deposited")
    bank = await call_snapshot_tool("get_bank", {"search": "magic logs"}, store)
    assert bank["items"][0]["quantity"] == 325


async def test_get_equipment_and_inventory(mock_sources):
    store = await seeded_store("full", "inventory")
    gear = await call_snapshot_tool("get_equipment_and_inventory", {}, store)

    equipment = gear["equipment"]["items"]
    assert set(equipment) == {"cape", "weapon", "body", "legs", "ammo"}
    assert equipment["weapon"] == {"name": "Abyssal whip", "id": 4151, "quantity": 1}
    assert equipment["cape"]["name"] == "unknown item 6570"
    assert gear["equipment"]["updated_at"]

    inventory = gear["inventory"]["items"]
    assert [(i["slot"], i["name"]) for i in inventory] == [
        (0, "Toxic blowpipe (empty)"),
        (2, "Magic logs"),
        (5, "unknown item 6570"),
    ]
    assert gear["free_inventory_slots"] == 25

    assert [(r["name"], r["quantity"]) for r in gear["rune_pouch"]["items"]] == [
        ("Nature rune", 1000),
        ("unknown item 554", 2500),
    ]
    assert gear["quiver"]["items"] == []
    assert gear["quiver"]["updated_at"]


async def test_snapshot_tools_explain_setup_when_empty(mock_sources):
    for tool in ("get_bank", "get_equipment_and_inventory"):
        async with make_client("Iron Tester", MemoryStore(), **INGEST) as client:
            with pytest.raises(ToolError, match="Group Ironmen Tracker") as error:
                await client.call_tool(tool, {})
        message = str(error.value)
        assert "Server base URL override = http://localhost:8000" in message
        assert "Group Name = My Group" in message
        assert "secret-token" not in message


async def test_snapshot_tools_say_when_ingest_is_off(mock_sources):
    async with make_client("Iron Tester", MemoryStore()) as client:
        with pytest.raises(ToolError, match="GIM_GROUP and INGEST_TOKEN"):
            await client.call_tool("get_bank", {})
