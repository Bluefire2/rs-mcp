"""Capture real API responses into tests/fixtures/ (run once, re-run when a format changes).

    uv run python scripts/capture_fixtures.py

Uses well-known public accounts so fixtures don't depend on the owner's RSN. The prices mapping
is trimmed to a handful of items to keep the repo small.
"""

import json
import sys
from pathlib import Path

import httpx

from osrs_mcp.config import get_settings

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

HISCORES = "https://secure.runescape.com/m=hiscore_oldschool/index_lite.json"
WIKISYNC = "https://sync.runescape.wiki/runelite/player/{rsn}/STANDARD"
WOM = "https://api.wiseoldman.net/v2"
PRICES = "https://prices.runescape.wiki/api/v1/osrs"

HISCORE_PLAYER = "Lynx Titan"
WIKISYNC_PLAYER = "B0aty"
MAPPING_KEEP = {4151, 11832, 11834, 12924, 13576, 22325, 2, 561, 1513, 20997}  # whip, bandos, etc.


def save(name: str, response: httpx.Response, body: object | None = None) -> None:
    path = FIXTURES / name
    if body is None:
        path.write_text(response.text, encoding="utf-8")
    else:
        path.write_text(json.dumps(body, indent=1), encoding="utf-8")
    print(f"{response.status_code} {name} ({path.stat().st_size} B)")


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": get_settings().full_user_agent}, timeout=20) as http:
        save("hiscores_lynx_titan.json", http.get(HISCORES, params={"player": HISCORE_PLAYER}))
        save(
            "hiscores_not_found.html",
            http.get(HISCORES, params={"player": "zzqqxxnotreal123"}),
        )
        save("wikisync_b0aty.json", http.get(WIKISYNC.format(rsn=WIKISYNC_PLAYER)))
        save("wikisync_no_data.json", http.get(WIKISYNC.format(rsn="zzqqxxnotreal123")))
        save("wom_player.json", http.get(f"{WOM}/players/{HISCORE_PLAYER}"))
        save(
            "wom_gained_week.json",
            http.get(f"{WOM}/players/{WIKISYNC_PLAYER}/gained", params={"period": "week"}),
        )
        save("wom_not_found.json", http.get(f"{WOM}/players/zzqqxxnotreal123"))

        mapping = http.get(f"{PRICES}/mapping")
        trimmed = [item for item in mapping.json() if item["id"] in MAPPING_KEEP]
        save("prices_mapping.json", mapping, trimmed)

        latest = http.get(f"{PRICES}/latest")
        data = latest.json()["data"]
        save(
            "prices_latest.json",
            latest,
            {"data": {k: v for k, v in data.items() if int(k) in MAPPING_KEEP}},
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
