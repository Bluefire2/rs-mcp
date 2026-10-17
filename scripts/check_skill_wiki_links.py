"""Check that every OSRS Wiki page and category the osrs-coach skill names still exists.

    uv run python scripts/check_skill_wiki_links.py

The skill names only directory pages and categories (hubs), so this list is short. Missing pages
are errors (exit 1). Redirects still work for readers but mean the page was renamed, so they're
reported to update the skill to the current title.
"""

import re
import sys
from pathlib import Path

import httpx

from osrs_mcp.config import get_settings

SKILL = Path(__file__).resolve().parent.parent / "skills" / "osrs-coach" / "SKILL.md"
API = "https://oldschool.runescape.wiki/api.php"

# In the skill, a backticked token starting with a capital letter is a wiki title
# (`Skill_training_guides`, `Quests/List`, `Category:Strategies`, `Calculator:Armoury`); tool
# names and arguments are lowercase. Keep it that way so this check stays complete. Placeholders
# like `<Monster>/Strategies` don't start with a capital letter, so they're skipped.
TITLE = re.compile(r"`((?:[A-Z][a-z]+:)?[A-Z][A-Za-z0-9_()/'-]*)`")


def titles_in_skill() -> list[str]:
    text = SKILL.read_text(encoding="utf-8")
    return sorted({m.group(1) for m in TITLE.finditer(text)})


def main() -> int:
    titles = titles_in_skill()
    with httpx.Client(headers={"User-Agent": get_settings().full_user_agent}, timeout=30) as http:
        response = http.get(
            API,
            params={
                "action": "query",
                "titles": "|".join(t.replace("_", " ") for t in titles),
                "redirects": 1,
                "prop": "categoryinfo",
                "format": "json",
            },
        )
        response.raise_for_status()
    query = response.json()["query"]
    redirects = {r["from"]: r["to"] for r in query.get("redirects", [])}
    normalized = {n["from"]: n["to"] for n in query.get("normalized", [])}
    pages = {p["title"]: p for p in query["pages"].values()}

    errors = 0
    for title in titles:
        name = normalized.get(title.replace("_", " "), title.replace("_", " "))
        target = redirects.get(name, name)
        page = pages.get(target, {})
        empty_category = name.startswith("Category:") and not page.get("categoryinfo")
        if "missing" in page or not page or empty_category:
            print(f"MISSING   {title}")
            errors += 1
        elif target != name:
            print(f"RENAMED   {title}  ->  {target.replace(' ', '_')}  (update the skill)")
        else:
            members = page.get("categoryinfo", {}).get("pages")
            print(f"ok        {title}" + (f"  ({members} pages)" if members else ""))
    print(f"\n{len(titles)} titles checked, {errors} missing")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
