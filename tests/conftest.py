import json
from pathlib import Path

import pytest
import respx

from osrs_mcp.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


@pytest.fixture
def http():
    return HttpClient("osrs-mcp-tests/0.1", backoff_base=0)


@pytest.fixture
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        yield router


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
