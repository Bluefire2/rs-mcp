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
