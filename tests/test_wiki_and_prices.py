"""search_wiki, get_wiki_page and get_price_history."""

import pytest
from fastmcp.exceptions import ToolError

from osrs_mcp.clients.prices import ItemInfo, PricePoint
from osrs_mcp.clients.wiki import Section
from osrs_mcp.tools.prices import MAX_POINTS, build_price_history
from osrs_mcp.tools.wiki import find_section, html_to_text, truncate
from tests.conftest import fixture_text
from tests.test_tools import call, make_client

WIKI_API = "https://oldschool.runescape.wiki/api.php"
TIMESERIES = "https://prices.runescape.wiki/api/v1/osrs/timeseries"


@pytest.fixture
def mock_wiki(mock_sources):
    mock_sources.get(WIKI_API, params={"list": "search"}).respond(
        200, text=fixture_text("wiki_search_dragon_warhammer.json")
    )
    mock_sources.get(WIKI_API, params={"page": "Qqzzx", "prop": "sections"}).respond(
        200, text=fixture_text("wiki_missing_page.json")
    )
    mock_sources.get(WIKI_API, params={"prop": "sections"}).respond(
        200, text=fixture_text("wiki_sections_dragon_warhammer.json")
    )
    mock_sources.get(WIKI_API, params={"prop": "text", "section": "0"}).respond(
        200, text=fixture_text("wiki_section0_dragon_warhammer.json")
    )
    mock_sources.get(WIKI_API, params={"prop": "text", "section": "3"}).respond(
        200, text=fixture_text("wiki_section3_dragon_warhammer.json")
    )
    return mock_sources


# --- HTML to text -----------------------------------------------------------------------------


def test_html_to_text_keeps_headings_lists_and_table_rows():
    html = """
    <div class="mw-parser-output">
      <h2 id="Drops">Drops</h2>
      <p>Drop rates are <b>estimated</b>.<sup class="reference">[1]</sup></p>
      <ul><li>First</li><li>Second<br>line</li></ul>
      <table class="wikitable">
        <tr><th></th><th>Item</th><th>Rarity</th></tr>
        <tr><td><img src="x.png"></td><td><a href="/w/X">Dragon warhammer</a></td>
            <td><span data-drop-fraction="1/3,000">1/3,000</span></td></tr>
      </table>
      <div class="navbox">Every item in the game</div>
      <script>ignored()</script>
      <p>After &amp; done</p>
    </div>
    """
    assert html_to_text(html).splitlines() == [
        "## Drops",
        "Drop rates are estimated.",
        "- First",
        "- Second line",
        "Item | Rarity",
        "Dragon warhammer | 1/3,000",
        "After & done",
    ]


def test_truncate_cuts_at_a_line_break():
    text = "\n".join(f"line {i}" for i in range(100))
    short, cut = truncate(text, 50)
    assert cut and short.endswith("line 6")
    assert truncate("short", 50) == ("short", False)


def test_find_section_prefers_exact_then_prefix_then_substring():
    sections = [
        Section(index="1", line="Item sources", level="2"),
        Section(index="2", line="Drops", level="2"),
        Section(index="3", line="Rare drop table", level="3"),
    ]
    assert find_section(sections, "drops").index == "2"
    assert find_section(sections, "item").index == "1"
    assert find_section(sections, "rare drop").index == "3"
    assert find_section(sections, "trivia") is None


# --- wiki tools --------------------------------------------------------------------------------


async def test_search_wiki(mock_wiki):
    result = await call("search_wiki", {"query": "dragon warhammer"})
    first = result["results"][0]
    assert first["title"] == "Dragon warhammer"
    assert first["url"] == "https://oldschool.runescape.wiki/w/Dragon_warhammer"
    assert "<span" not in first["snippet"] and "warhammer" in first["snippet"]


async def test_get_wiki_page_intro_includes_infobox_and_section_list(mock_wiki):
    result = await call("get_wiki_page", {"title": "dwh"})
    assert result["title"] == "Dragon warhammer"  # the redirect, resolved
    assert result["section"] is None
    assert "Item sources" in result["sections"]
    assert "High alch | 72,000 coins" in result["text"]
    assert "extremely rare drop from Lizardman shamans" in result["text"]
    assert not result["truncated"]


async def test_get_wiki_page_section_has_the_drop_table(mock_wiki):
    result = await call("get_wiki_page", {"title": "Dragon warhammer", "section": "item sources"})
    assert result["section"] == "Item sources"
    assert "Lizardman shaman | 150 | 1 | 1/3,000" in result["text"]


async def test_get_wiki_page_unknown_section_lists_the_real_ones(mock_wiki):
    async with make_client() as client:
        with pytest.raises(ToolError, match=r"no section 'Drops'.*Item sources"):
            await client.call_tool("get_wiki_page", {"title": "dwh", "section": "Drops"})


async def test_get_wiki_page_missing_page_suggests_search_results(mock_wiki):
    async with make_client() as client:
        with pytest.raises(ToolError, match=r"No OSRS Wiki page.*Closest pages: 'Dragon war"):
            await client.call_tool("get_wiki_page", {"title": "Qqzzx"})


# --- price history -----------------------------------------------------------------------------

WHIP = ItemInfo(id=4151, name="Abyssal whip", members=True)
NOW = 1_800_000_000


def point(ts: int, high: int | None, low: int | None, volume: int = 10) -> PricePoint:
    return PricePoint(
        timestamp=ts,
        avgHighPrice=high,
        avgLowPrice=low,
        highPriceVolume=volume if high is not None else 0,
        lowPriceVolume=volume if low is not None else 0,
    )


def test_price_history_keeps_only_the_period_and_summarises_it():
    day = 24 * 3600
    series = [
        point(NOW - 3 * day, 1, 1),  # before the period
        point(NOW - day + 300, 1000, 900),
        point(NOW - day + 600, None, None),  # no trades
        point(NOW - 600, 1200, None),
        point(NOW - 300, 1100, 1050),
    ]
    history = build_price_history(WHIP, "day", series, NOW)
    assert [p.price for p in history.points] == [950, None, 1200, 1075]
    assert history.start_price == 950 and history.end_price == 1075
    assert history.lowest == 950 and history.highest == 1200
    assert history.change_percent == 13.2
    assert history.total_volume == 20 + 0 + 10 + 20


def test_price_history_averages_long_series_down():
    series = [point(NOW - 365 * 86400 + i * 86400, 100 + i, 100 + i) for i in range(365)]
    history = build_price_history(WHIP, "year", series, NOW)
    # 365 days in buckets of 4: 92 points, the first averaging 100..103.
    assert len(history.points) == 92 <= MAX_POINTS
    assert history.points[0].price == round((100 + 101 + 102 + 103) / 4)
    assert history.points[-1].at.timestamp() == series[-1].timestamp
    assert history.total_volume == 365 * 20


async def test_get_price_history(mock_sources):
    series = [point(NOW - 3600 * i, 2_000_000, 1_900_000).model_dump() for i in range(5, 0, -1)]
    route = mock_sources.get(TIMESERIES).respond(200, json={"data": series, "itemId": 4151})
    result = await call("get_price_history", {"query": "whip", "period": "year"})
    assert route.calls.last.request.url.params["timestep"] == "24h"
    assert route.calls.last.request.url.params["id"] == "4151"
    assert result["name"] == "Abyssal whip"
    assert result["end_price"] == 1_950_000
    assert len(result["points"]) == 5


async def test_get_price_history_unknown_item(mock_sources):
    async with make_client() as client:
        with pytest.raises(ToolError, match="No tradeable item matches"):
            await client.call_tool("get_price_history", {"query": "qqzzx"})
