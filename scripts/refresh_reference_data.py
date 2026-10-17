"""Rebuild the static reference data in src/osrs_mcp/reference/data/ from the OSRS Wiki.

    uv run python scripts/refresh_reference_data.py

WikiSync reports *what* a player has done (quest states, diary task booleans, combat achievement
IDs) but not names or requirements. This script pulls those from the Wiki:

- quest_reqs.json: Module:Questreq/data (a Lua table of quest and diary prerequisites) plus
  difficulty/length from the Quest bucket.
- diary_tasks.json: task text and requirements from each "<Region> Diary" page, in the same
  order as WikiSync's task arrays.
- combat_achievements.json: the combat_achievement bucket (id -> name, tier, monster, task).

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
        for row in quest_rows:
            if row["page_name"].endswith("/Full guide"):
                continue
            name = normalise_prereq(row["page_name"]).removesuffix(" (miniquest)")
            if name not in reqs:
                entry = parse_bucket_requirements(row.get("requirements") or "")
                entry["quests"] = [normalise_prereq(q) for q in entry["quests"]]
                entry["source"] = "Quest bucket"
                reqs[name] = entry
            reqs[name]["difficulty"] = row.get("official_difficulty")
            reqs[name]["length"] = row.get("official_length")
        write("quest_reqs.json", dict(sorted(reqs.items())))

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
    return 0


def write(name: str, data: object) -> None:
    path = OUT / name
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(OUT.parent.parent.parent.parent)} ({path.stat().st_size} B)")


if __name__ == "__main__":
    sys.exit(main())
