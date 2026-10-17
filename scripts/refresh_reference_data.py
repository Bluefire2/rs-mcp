"""Rebuild the static reference data in src/osrs_mcp/reference/data/ from the OSRS Wiki.

    uv run python scripts/refresh_reference_data.py

WikiSync reports *what* a player has done (quest states, diary task booleans, combat achievement
IDs) but not names or requirements. This script pulls those from the Wiki:

- quest_reqs.json: Module:Questreq/data (a Lua table of quest and diary prerequisites) plus
  difficulty/length from the Quest bucket, the skills each quest page's {{Quest rewards}} gives
  experience in, whether it's a miniquest (Category:Miniquests) and its position in the
  "Optimal quest guide" table.
- quest_points.json: quest -> quest points, from the qp= of each quest page's {{Quest rewards}}
  (the same field the Wiki's Quests/List tables show), checked against the Wiki's total.
- diary_tasks.json: task text and requirements from each "<Region> Diary" page, in the same
  order as WikiSync's task arrays.
- combat_achievements.json: the combat_achievement bucket (id -> name, tier, monster, task).
- item_names.json: every item id -> name from the infobox_item bucket. The GE prices mapping
  covers tradeable items only; this names untradeables (Marks of grace, quest items, ...) in
  the bank and gear tools.

Run it after game updates that add quests, diary changes or combat achievements, then run the
tests (they cross-check this data against a WikiSync fixture).
"""

import json
import re
import sys
from pathlib import Path

import httpx

from osrs_mcp.config import get_settings

OUT = Path(__file__).resolve().parent.parent / "src" / "osrs_mcp" / "reference" / "data"
WIKI = "https://oldschool.runescape.wiki"
API = f"{WIKI}/api.php"

DIARY_REGIONS = [
    "Ardougne",
    "Desert",
    "Falador",
    "Fremennik",
    "Kandarin",
    "Karamja",
    "Kourend & Kebos",
    "Lumbridge & Draynor",
    "Morytania",
    "Varrock",
    "Western Provinces",
    "Wilderness",
]
DIARY_TIERS = ["Easy", "Medium", "Hard", "Elite"]


# --- Lua table parsing (just enough for Module:Questreq/data) ---------------------------------

LUA_TOKEN = re.compile(
    r"""\s*(?:
        (?P<comment>--\[\[.*?\]\]|--[^\n]*)
      | (?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<punct>[{}\[\]=,])
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE | re.DOTALL,
)


def lua_tokens(source: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(source):
        match = LUA_TOKEN.match(source, pos)
        if not match:
            if source[pos:].strip() == "":
                break
            raise ValueError(f"Unexpected Lua at {pos}: {source[pos : pos + 40]!r}")
        pos = match.end()
        kind = match.lastgroup
        if kind != "comment":
            tokens.append((kind, match.group(kind)))
    return tokens


def lua_string(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw[1:-1])


def parse_lua_table(tokens: list[tuple[str, str]], i: int) -> tuple[object, int]:
    """Parse a table starting at tokens[i] == '{'. Returns a dict if it has keys, else a list."""
    assert tokens[i] == ("punct", "{"), tokens[i]
    i += 1
    keyed: dict[str, object] = {}
    items: list[object] = []
    while tokens[i] != ("punct", "}"):
        if tokens[i] == ("punct", "["):
            key = lua_string(tokens[i + 1][1])
            assert tokens[i + 2] == ("punct", "]") and tokens[i + 3] == ("punct", "=")
            keyed[key], i = parse_lua_value(tokens, i + 4)
        else:
            value, i = parse_lua_value(tokens, i)
            items.append(value)
        if tokens[i] == ("punct", ","):
            i += 1
    return (keyed if keyed else items), i + 1


def parse_lua_value(tokens: list[tuple[str, str]], i: int) -> tuple[object, int]:
    kind, text = tokens[i]
    if kind == "punct" and text == "{":
        return parse_lua_table(tokens, i)
    if kind == "string":
        return lua_string(text), i + 1
    if kind == "number":
        return (float(text) if "." in text else int(text)), i + 1
    if kind == "word" and text in ("true", "false"):
        return text == "true", i + 1
    raise ValueError(f"Unexpected token {tokens[i]}")


def parse_questreq(source: str) -> dict[str, dict]:
    tokens = lua_tokens(source)
    start = tokens.index(("word", "questReqs"))
    assert tokens[start + 1] == ("punct", "=")
    table, _ = parse_lua_table(tokens, start + 2)
    result = {}
    for name, entry in table.items():
        skills = []
        for skill in entry.get("skills", []):
            skill_name, level, *flags = skill
            skills.append(
                {
                    "skill": skill_name,
                    "level": level,
                    "ironman_only": "ironman" in flags,
                    "boostable": "boostable" in flags,
                }
            )
        result[name] = {"quests": list(entry.get("quests", [])), "skills": skills}
    return result


# --- wikitext helpers -------------------------------------------------------------------------


def plain(wikitext: str) -> str:
    """Rough wikitext -> plain text for task descriptions."""
    text = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", "", wikitext, flags=re.DOTALL)
    text = re.sub(r"\{\{SCP\|([^|}]+)\|(\d+)[^}]*\}\}", r"\2 \1", text)
    text = re.sub(r"\{\{(?:sic|NA|\*)[^}]*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+ ([^\]]+)\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", text).strip()


SCP_SKILL = re.compile(r"\{\{SCP\|([A-Za-z ]+)\|(\d+)")
QUEST_LINK = re.compile(r"(?:Completion of|Started|Partial completion of)[^\n]*?\[\[([^\]|]+)")


def parse_diary_page(region: str, wikitext: str) -> dict[str, list[dict]]:
    tiers: dict[str, list[dict]] = {}
    pattern = re.compile(
        r'data-diary-name="' + re.escape(region) + r'" data-diary-tier="(\w+)"\s*\n(.*?)\n\|\}',
        re.DOTALL,
    )
    for match in pattern.finditer(wikitext):
        tier, body = match.group(1), match.group(2)
        rows = re.split(r"\n\|-\s*\n", body)[1:]  # first chunk is the header row
        tasks = []
        for row in rows:
            cells = re.split(r"\n\|(?!\-)", "\n" + row)
            cells = [c for c in cells if c.strip()]
            task_cell = cells[0]
            req_cell = "\n".join(cells[1:])
            first_line = task_cell.strip().splitlines()[0]
            text = re.sub(r"^\d+\.\s*", "", plain(first_line))
            levels: dict[str, int] = {}
            for skill, level in SCP_SKILL.findall(req_cell):
                if skill not in ("Quest", "Combat"):
                    levels[skill] = max(levels.get(skill, 0), int(level))
            skills = [{"skill": skill, "level": level} for skill, level in levels.items()]
            quests = [q.strip() for q in QUEST_LINK.findall(req_cell)]
            tasks.append({"task": text, "skills": skills, "quests": quests})
        tiers[tier] = tasks
    return tiers


# Module:Questreq/data names some subquests differently from WikiSync.
QUESTREQ_ALIASES = {
    "Recipe for Disaster/Another Cook's Quest": "Recipe for Disaster - Another Cook's Quest",
    "Recipe for Disaster/Defeating the Culinaromancer": "Recipe for Disaster - Culinaromancer",
    "Recipe for Disaster/Freeing Evil Dave": "Recipe for Disaster - Evil Dave",
    "Recipe for Disaster/Freeing King Awowogei": "Recipe for Disaster - King Awowogei",
    "Recipe for Disaster/Freeing Pirate Pete": "Recipe for Disaster - Pirate Pete",
    "Recipe for Disaster/Freeing Sir Amik Varze": "Recipe for Disaster - Sir Amik Varze",
    "Recipe for Disaster/Freeing Skrach Uglogwee": "Recipe for Disaster - Skrach Uglogwee",
    "Recipe for Disaster/Freeing the Goblin generals": "Recipe for Disaster - Wartface & Bentnoze",
    "Recipe for Disaster/Freeing the Lumbridge Guide": "Recipe for Disaster - Lumbridge Guide",
    "Recipe for Disaster/Freeing the Mountain Dwarf": "Recipe for Disaster - Mountain Dwarf",
}

BUCKET_SKILL = re.compile(r'data-skill="([^"]+)" data-level="(\d+)"')
BUCKET_DIRECT_QUEST = re.compile(r"^\*\*(?!\*)\s*\[\[([^\]|]+)", re.MULTILINE)


def normalise_prereq(name: str) -> str:
    """Questreq marks "must be started" as 'Started:Quest'; keep the marker, fix aliases."""
    name = name.strip()
    prefix = ""
    if name.startswith("Started:"):
        prefix, name = "Started:", name.removeprefix("Started:").strip()
    return prefix + QUESTREQ_ALIASES.get(name, name)


def parse_bucket_requirements(html: str) -> dict:
    """Fallback for quests missing from Module:Questreq/data: the Quest bucket's rendered
    requirements list. Direct prerequisites are the second-level list items."""
    skills = []
    for skill, level in BUCKET_SKILL.findall(html):
        if skill == "Quest points":
            skill = "Quest point"
        skills.append(
            {"skill": skill, "level": int(level), "ironman_only": False, "boostable": False}
        )
    quests = [q.strip() for q in BUCKET_DIRECT_QUEST.findall(html)]
    return {"quests": quests, "skills": skills}


# --- fetching ---------------------------------------------------------------------------------


def bucket(http: httpx.Client, query: str) -> list[dict]:
    response = http.get(API, params={"action": "bucket", "format": "json", "query": query})
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["bucket"]


def item_names(http: httpx.Client, page_size: int = 5000) -> dict[str, str]:
    """Every item id -> name, paging through the infobox_item bucket."""
    names: dict[int, str] = {}
    offset = 0
    while True:
        rows = bucket(
            http,
            f"bucket('infobox_item').select('item_id','item_name')"
            f".limit({page_size}).offset({offset}).run()",
        )
        for row in rows:
            name = row.get("item_name")
            ids = row.get("item_id") or []
            for item_id in [ids] if isinstance(ids, str) else ids:
                if name and str(item_id).strip().isdigit():
                    names.setdefault(int(item_id), name)
        if len(rows) < page_size:
            return {str(item_id): names[item_id] for item_id in sorted(names)}
        offset += page_size


QUEST_REWARDS_QP = re.compile(r"\{\{\s*Quest rewards\b([^{}]*)", re.IGNORECASE)
QP_PARAM = re.compile(r"\|\s*qp\s*=\s*(\d+)")
# Categories the Wiki's own quest lists (the DPL on Quests/Members) leave out.
UNRELEASED_CATEGORIES = {
    "Category:Future content",
    "Category:Sailing alpha content",
    "Category:Sailing beta content",
}
# Since the 22 October 2025 update every account's first quest point comes from Tutorial Island,
# a miniquest page with no {{Quest rewards}} (see its changelog). WikiSync doesn't track it.
TUTORIAL_ISLAND_QP = {"Tutorial Island": 1}
MINIQUEST_CATEGORY = "Category:Miniquests"


def parse_quest_rewards_qp(wikitext: str) -> int | None:
    """qp= of the page's {{Quest rewards}}; the template defaults to 1. None if there isn't one."""
    match = QUEST_REWARDS_QP.search(wikitext)
    if not match:
        return None
    qp = QP_PARAM.search(match.group(1))
    return int(qp.group(1)) if qp else 1


def wikisync_name(title: str) -> str:
    """Wiki page title -> the name WikiSync and Module:Questreq/data use."""
    return normalise_prereq(title).removesuffix(" (miniquest)")


def query(http: httpx.Client, **params: str) -> dict:
    base = {"action": "query", "format": "json", "formatversion": "2"}
    response = http.get(API, params=base | params)
    response.raise_for_status()
    return response.json()["query"]


def quest_pages(http: httpx.Client, titles: list[str]) -> dict[str, tuple[set[str], str]]:
    """Released quest and miniquest pages: title -> (categories, wikitext)."""
    pages: dict[str, tuple[set[str], str]] = {}
    for start in range(0, len(titles), 50):
        result = query(
            http,
            prop="revisions|categories",
            rvprop="content",
            rvslots="main",
            cllimit="max",
            titles="|".join(titles[start : start + 50]),
        )
        for page in result["pages"]:
            categories = {c["title"] for c in page.get("categories", [])}
            if categories & UNRELEASED_CATEGORIES:
                continue
            pages[page["title"]] = (categories, page["revisions"][0]["slots"]["main"]["content"])
    return pages


def page_quest_points(title: str, categories: set[str], wikitext: str) -> int:
    """Quest points for one released page (miniquests give 0)."""
    if MINIQUEST_CATEGORY in categories:
        return 0
    qp = parse_quest_rewards_qp(wikitext)
    if qp is None:
        raise RuntimeError(f"{title}: released quest without {{{{Quest rewards}}}}")
    return qp


def wiki_total_quest_points(http: httpx.Client) -> int:
    match = re.search(
        r"\['quest points'\]\s*=\s*\{[^}]*?'(\d+)'", raw_page(http, "Module:Globals/data")
    )
    if not match:
        raise RuntimeError("Module:Globals/data: no 'quest points' total")
    return int(match.group(1))


def build_quest_points(
    http: httpx.Client, pages: dict[str, tuple[set[str], str]]
) -> dict[str, int]:
    """Quest points keyed by WikiSync quest name, plus Tutorial Island."""
    points = {
        wikisync_name(title): page_quest_points(title, categories, wikitext)
        for title, (categories, wikitext) in pages.items()
    }
    # The Recipe for Disaster page lists all 10 points; the game awards (and WikiSync
    # tracks) them one per subquest, so the parent quest itself counts for nothing.
    subquests = sum(qp for name, qp in points.items() if name.startswith("Recipe for Disaster - "))
    if points.get("Recipe for Disaster") != subquests:
        raise RuntimeError(
            f"Recipe for Disaster QP {points.get('Recipe for Disaster')} != {subquests}"
        )
    points["Recipe for Disaster"] = 0
    total, expected = sum(points.values()), wiki_total_quest_points(http)
    if total != expected:
        raise RuntimeError(f"Quest points add up to {total}, the Wiki's total is {expected}")
    points.update(TUTORIAL_ISLAND_QP)
    return dict(sorted(points.items()))


SKILLS = {
    "Agility", "Attack", "Construction", "Cooking", "Crafting", "Defence", "Farming",
    "Firemaking", "Fishing", "Fletching", "Herblore", "Hitpoints", "Hunter", "Magic", "Mining",
    "Prayer", "Ranged", "Runecraft", "Sailing", "Slayer", "Smithing", "Strength", "Thieving",
    "Woodcutting",
}  # fmt: skip
QUEST_REWARDS_START = re.compile(r"\{\{\s*Quest rewards\b", re.IGNORECASE)
TEMPLATE_BRACES = re.compile(r"\{\{|\}\}")
REWARD_SCP = re.compile(
    r"\{\{\s*SCP\s*\|\s*([A-Za-z ]+?)\s*(?:\|\s*([\d,]+(?:\.\d+)?)\s*)?(?:\|[^{}]*)?\}\}"
)
XP_WORDS = re.compile(r"experience|\bxp\b", re.IGNORECASE)


def quest_rewards_body(wikitext: str) -> str:
    """The text of the page's {{Quest rewards ...}}, up to its matching }} ("" if none)."""
    start = QUEST_REWARDS_START.search(wikitext)
    if not start:
        return ""
    depth = 1
    for brace in TEMPLATE_BRACES.finditer(wikitext, start.end()):
        depth += 1 if brace.group() == "{{" else -1
        if depth == 0:
            return wikitext[start.end() : brace.start()]
    return wikitext[start.end() :]


def parse_xp_rewards(wikitext: str) -> dict[str, int | None]:
    """Skills the page's {{Quest rewards}} gives experience in: skill -> fixed XP, or None when
    there's no plain number next to the skill (a choice, "35,000 experience in {{SCP|Attack}}").
    Only reward lines that mention experience count, so "(with {{SCP|Agility|37}})" unlocks
    don't. Lamps for "any skill" name no skill and aren't listed."""
    rewards: dict[str, int | None] = {}
    for line in quest_rewards_body(wikitext).splitlines():
        if not XP_WORDS.search(line):
            continue
        for name, amount in REWARD_SCP.findall(line):
            skill = name.capitalize()  # {{SCP|thieving|1,500}} on Hazeel Cult
            if skill not in SKILLS:
                continue
            if amount:
                # Some amounts have decimals ({{SCP|Magic|20,006.9}}); the game truncates.
                xp = int(float(amount.replace(",", "")))
                rewards[skill] = max(rewards.get(skill) or 0, xp)
            else:
                rewards.setdefault(skill, None)
    return dict(sorted(rewards.items()))


def miniquest_names(http: httpx.Client, names: list[str]) -> set[str]:
    """Which of `names` are miniquests: pages in Category:Miniquests, directly or through a
    redirect ("Barbarian Firemaking" -> Barbarian Training#Firemaking)."""
    members = query(
        http, list="categorymembers", cmtitle=MINIQUEST_CATEGORY, cmnamespace="0", cmlimit="max"
    )["categorymembers"]
    pages = {m["title"] for m in members if "/" not in m["title"]}
    result = {wikisync_name(title) for title in pages} & set(names)
    unresolved = [name for name in names if name not in result]
    for start in range(0, len(unresolved), 50):
        found = query(http, titles="|".join(unresolved[start : start + 50]), redirects="1")
        for redirect in found.get("redirects", []):
            if redirect["to"] in pages:
                result.add(redirect["from"])
    return result


OQG_ROW = re.compile(r"^\|-[^\n]*\bdata-rowid=[^\n]*\n\|([^\n]*)", re.MULTILINE)
SORT_VALUE = re.compile(r'^\s*data-sort-value="[^"]*"\s*\|')
FIRST_LINK = re.compile(r"\[\[([^\]|#]+)")


def parse_optimal_order(wikitext: str, known: set[str]) -> list[str]:
    """Quests in the order of the Optimal quest guide's table. Rows for diaries, unlocks and
    other activities are skipped (their first link isn't a known quest); first appearance wins."""
    known_by_case = {name.lower(): name for name in known}
    order: list[str] = []
    for match in OQG_ROW.finditer(wikitext):
        cell = SORT_VALUE.sub("", match.group(1)).strip()
        link = FIRST_LINK.search(cell)
        if cell.startswith("Unlock:") or not link:
            continue
        name = wikisync_name(link.group(1))
        name = known_by_case.get(name.lower())  # "Natural history quiz" -> "...History Quiz"
        if name and name not in order:
            order.append(name)
    return order


def raw_page(http: httpx.Client, title: str) -> str:
    response = http.get(f"{WIKI}/w/{title.replace(' ', '_')}", params={"action": "raw"})
    response.raise_for_status()
    return response.text


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": get_settings().full_user_agent}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as http:
        parsed = parse_questreq(raw_page(http, "Module:Questreq/data"))
        reqs = {}
        for name, entry in parsed.items():
            if name.endswith(" Diary"):
                continue  # diary requirements come from the diary pages instead
            entry["quests"] = [normalise_prereq(q) for q in entry["quests"]]
            entry["source"] = "Module:Questreq/data"
            reqs[QUESTREQ_ALIASES.get(name, name)] = entry
        quest_rows = bucket(
            http,
            "bucket('quest').select('page_name','official_difficulty','official_length',"
            "'requirements').limit(5000).run()",
        )
        titles = {row["page_name"] for row in quest_rows}
        pages = quest_pages(http, sorted(t for t in titles if not t.endswith("/Full guide")))
        for row in quest_rows:
            if row["page_name"].endswith("/Full guide"):
                continue
            name = wikisync_name(row["page_name"])
            if name not in reqs:
                entry = parse_bucket_requirements(row.get("requirements") or "")
                entry["quests"] = [normalise_prereq(q) for q in entry["quests"]]
                entry["source"] = "Quest bucket"
                reqs[name] = entry
            reqs[name]["difficulty"] = row.get("official_difficulty")
            reqs[name]["length"] = row.get("official_length")
            page = pages.get(row["page_name"])  # None if unreleased
            reqs[name]["xp_rewards"] = parse_xp_rewards(page[1]) if page else {}
        miniquests = miniquest_names(http, sorted(reqs))
        order = parse_optimal_order(raw_page(http, "Optimal quest guide"), set(reqs))
        for name, entry in reqs.items():
            entry.setdefault("xp_rewards", {})  # Questreq-only entries have no quest page
            entry["miniquest"] = name in miniquests
            entry["optimal_order"] = order.index(name) + 1 if name in order else None
        write("quest_reqs.json", dict(sorted(reqs.items())))

        write("quest_points.json", build_quest_points(http, pages))

        diaries = {}
        for region in DIARY_REGIONS:
            tiers = parse_diary_page(region, raw_page(http, f"{region} Diary"))
            missing = [t for t in DIARY_TIERS if t not in tiers]
            if missing:
                raise RuntimeError(f"{region} Diary: no task table for {missing}")
            diaries[region] = {tier: tiers[tier] for tier in DIARY_TIERS}
        write("diary_tasks.json", diaries)

        cas = bucket(
            http,
            "bucket('combat_achievement').select('id','name','tier','monster','task','type')"
            ".limit(5000).run()",
        )
        cas = sorted(
            (
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "tier": row.get("tier"),
                    "monster": row.get("monster"),
                    "type": row.get("type"),
                    "task": plain(row.get("task") or ""),
                }
                for row in cas
                if row.get("id") is not None
            ),
            key=lambda ca: ca["id"],
        )
        write("combat_achievements.json", cas)
        write("item_names.json", item_names(http), compact=True)
    return 0


def write(name: str, data: object, *, compact: bool = False) -> None:
    path = OUT / name
    if compact:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(data, indent=1, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(OUT.parent.parent.parent.parent)} ({path.stat().st_size} B)")


if __name__ == "__main__":
    sys.exit(main())
