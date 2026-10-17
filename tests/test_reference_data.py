"""Cross-check the committed reference data against a real WikiSync payload, and unit-test the
parsers in scripts/refresh_reference_data.py."""

import importlib.util
import json
from pathlib import Path

import pytest

from osrs_mcp.reference import (
    collection_log,
    combat_achievements,
    diary_tasks,
    quest_points,
    quest_reqs,
)
from osrs_mcp.requirements import player_quest_points
from tests.conftest import fixture_json

SCRIPT = Path(__file__).parent.parent / "scripts" / "refresh_reference_data.py"
DATA = Path(__file__).parent.parent / "src" / "osrs_mcp" / "reference" / "data"
spec = importlib.util.spec_from_file_location("refresh_reference_data", SCRIPT)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


@pytest.fixture(scope="module")
def wikisync():
    return fixture_json("wikisync_b0aty.json")


def test_diary_task_counts_match_wikisync(wikisync):
    data = diary_tasks()
    for region, tiers in wikisync["achievement_diaries"].items():
        for tier, state in tiers.items():
            assert len(data[region][tier]) == len(state["tasks"]), (region, tier)


def test_every_wikisync_quest_has_requirements(wikisync):
    reqs = quest_reqs()
    missing = [q for q in wikisync["quests"] if q not in reqs and q != "."]
    assert missing == []


def test_every_prerequisite_is_a_known_quest(wikisync):
    reqs = quest_reqs()
    known = set(reqs) | set(wikisync["quests"])
    unknown = {
        p.removeprefix("Started:")
        for entry in reqs.values()
        for p in entry.quests
        if p.removeprefix("Started:") not in known
    }
    assert unknown == set()


def test_every_wikisync_quest_has_quest_points(wikisync):
    points = quest_points()
    assert [q for q in wikisync["quests"] if q not in points and q != "."] == []
    reqs = quest_reqs()
    miniquests = [q for q, state in wikisync["quests"].items() if state != 2 and q != "."]
    assert miniquests and all(points[q] == 0 for q in miniquests)  # B0aty: only miniquests left
    assert {q for q in points if q not in reqs} <= {"Tutorial Island"}


def test_quest_points_spot_values():
    points = quest_points()
    assert points["Cook's Assistant"] == 1
    assert points["Dragon Slayer I"] == 2
    assert points["Recipe for Disaster"] == 0  # its 10 points come from the subquests
    assert sum(v for q, v in points.items() if q.startswith("Recipe for Disaster - ")) == 10
    assert points["Barbarian Training"] == 0  # miniquest
    assert points["Tutorial Island"] == 1


def test_quest_cape_total(wikisync):
    # B0aty has every quest done: the Wiki's total (343 in Module:Globals/data) + Tutorial Island.
    assert player_quest_points(wikisync["quests"]) == sum(quest_points().values()) == 344


def test_every_completed_combat_achievement_id_is_known(wikisync):
    known = combat_achievements()
    assert [i for i in wikisync["combat_achievements"] if i not in known] == []


# --- parser unit tests -------------------------------------------------------------------------

QUESTREQ_SAMPLE = r"""
-- comment
--[[ block
comment ]]
local questReqs = {
    ['Legends\' Quest'] = {
        ['quests'] = {
            'Family Crest',
            'Started:Waterfall Quest ',
        },
        ['skills'] = {
            {'Agility', 50},
            {'Prayer', 42, 'ironman', 'boostable'}
        }
    },
    ['Cook\'s Assistant'] = {
        ['quests'] = {},
        ['skills'] = {}
    },
}
return questReqs
"""


def test_parse_questreq():
    parsed = refresh.parse_questreq(QUESTREQ_SAMPLE)
    legends = parsed["Legends' Quest"]
    assert legends["quests"] == ["Family Crest", "Started:Waterfall Quest "]
    assert legends["skills"][1] == {
        "skill": "Prayer",
        "level": 42,
        "ironman_only": True,
        "boostable": True,
    }
    assert parsed["Cook's Assistant"] == {"quests": [], "skills": []}


def test_normalise_prereq():
    assert refresh.normalise_prereq("Started:Waterfall Quest ") == "Started:Waterfall Quest"
    assert (
        refresh.normalise_prereq("Recipe for Disaster/Freeing Evil Dave")
        == "Recipe for Disaster - Evil Dave"
    )


def test_plain_strips_wikitext():
    text = "2. Steal a [[cake]] from the [[Ardougne Baker's Stall.|market stalls]]{{sic}}."
    assert refresh.plain(text) == "2. Steal a cake from the market stalls."


DIARY_SAMPLE = """\
{| class="wikitable diary-table" data-diary-name="Ardougne" data-diary-tier="Easy"
! Task
! Requirements
|-
|1. Have [[Wizard Cromperty]] teleport you.
''Note: right-click him.''
|
*{{SCP|Quest}} Completion of [[Rune Mysteries]]
|-
|2. Steal a [[cake]].
|
*{{SCP|Thieving|5|link=y}}
*{{SCP|Thieving|3}}
|-
|3. Use the altar.
|{{NA|None}}
|}
"""


def test_parse_diary_page():
    tiers = refresh.parse_diary_page("Ardougne", DIARY_SAMPLE)
    easy = tiers["Easy"]
    assert [t["task"] for t in easy] == [
        "Have Wizard Cromperty teleport you.",
        "Steal a cake.",
        "Use the altar.",
    ]
    assert easy[0]["quests"] == ["Rune Mysteries"]
    assert easy[1]["skills"] == [{"skill": "Thieving", "level": 5}]
    assert easy[2] == {"task": "Use the altar.", "skills": [], "quests": []}


def test_parse_quest_rewards_qp():
    parse = refresh.parse_quest_rewards_qp
    assert parse("{{Quest rewards\n|name=Dragon Slayer I|qp=2\n}}") == 2
    assert parse("{{Quest rewards\n|qp = 0\n|rewards=x}}") == 0
    assert parse("{{Quest rewards|rewards=x}}") == 1  # the template's default
    assert parse("{{Infobox Quest|qp=5}}") is None


def test_parse_bucket_requirements():
    html = (
        "*Completion of the following quests:\n"
        "** [[The Heart of Darkness]]\n"
        "*** [[Twilight's Promise]]\n"
        "**[[Perilous Moons]]\n"
        '*<span class="scp" data-skill="Quest points" data-level="20">x</span>\n'
        '*<span class="scp" data-skill="Thieving" data-level="66">x</span>\n'
    )
    parsed = refresh.parse_bucket_requirements(html)
    assert parsed["quests"] == ["The Heart of Darkness", "Perilous Moons"]
    assert [(s["skill"], s["level"]) for s in parsed["skills"]] == [
        ("Quest point", 20),
        ("Thieving", 66),
    ]


def test_most_wikisync_quests_have_an_optimal_order(wikisync):
    reqs = quest_reqs()
    quests = [q for q in wikisync["quests"] if q != "."]
    ordered = [q for q in quests if reqs[q].optimal_order is not None]
    assert len(ordered) >= 0.9 * len(quests)
    # Only miniquests (and Recipe for Disaster, listed by subquest) are missing from the guide.
    assert {q for q in quests if q not in ordered and not reqs[q].miniquest} == {
        "Recipe for Disaster"
    }
    positions = [reqs[q].optimal_order for q in ordered]
    assert len(positions) == len(set(positions))
    assert reqs["Cook's Assistant"].optimal_order < reqs["Dragon Slayer II"].optimal_order
    # The guide links "Natural history quiz"; names match whatever their case.
    assert reqs["Natural History Quiz"].optimal_order is not None


def test_miniquests_are_marked(wikisync):
    reqs = quest_reqs()
    assert reqs["Barbarian Training"].miniquest
    assert reqs["Barbarian Firemaking"].miniquest  # a route step that redirects to it
    assert not reqs["Cook's Assistant"].miniquest
    points = quest_points()
    assert all(points[q] == 0 for q in wikisync["quests"] if q in reqs and reqs[q].miniquest)


def test_xp_reward_spot_values():
    reqs = quest_reqs()
    assert reqs["The Grand Tree"].xp_rewards == {"Agility": 7900, "Attack": 18400, "Magic": 2150}
    assert reqs["Hazeel Cult"].xp_rewards == {"Thieving": 1500}
    assert reqs["The Tourist Trap"].xp_rewards["Agility"] is None  # a choice of skills
    assert reqs["Rune Mysteries"].xp_rewards == {}
    assert reqs["Desert Treasure I"].xp_rewards == {"Magic": 20006}  # {{SCP|Magic|20,006.9}}


def test_xp_amounts_are_none_only_without_a_fixed_number():
    # Checked against each page's {{Quest rewards}}: these offer lamps or a choice of skills,
    # or (While Guthix Sleeps) XP that scales with level. Any other None is a parser miss, like
    # the decimal amounts ({{SCP|Magic|20,006.9}}) that once came out as None.
    no_fixed_amount = {
        "A Night at the Theatre",
        "Contact!",
        "Desert Treasure II - The Fallen Empire",
        "Legends' Quest",
        "Monkey Madness I",
        "Observatory Quest",
        "The Fremennik Isles",
        "The Tourist Trap",
        "While Guthix Sleeps",
    }
    with_none = {q for q, e in quest_reqs().items() if None in e.xp_rewards.values()}
    assert with_none == no_fixed_amount


def test_every_quest_entry_has_the_new_keys():
    raw = json.loads(DATA.joinpath("quest_reqs.json").read_text("utf-8"))
    keys = {"xp_rewards", "miniquest", "optimal_order"}
    assert [quest for quest, entry in raw.items() if keys - entry.keys()] == []


OQG_SAMPLE = """\
{| class="wikitable oqg-table" data-tableid="OQG"
! Quest/Activity
|- data-rowid="Tutorial Island"
|[[Learning the Ropes]]
|''N/A''
|- data-rowid="Cook's Assistant"
|[[Cook's Assistant]]
|[[Cook's Assistant/Quick guide|QG]]
|- data-rowid="Stronghold of Security"
|Unlock: [[Stronghold of Security]]
|- data-rowid="Easy Ardougne Diary"
| Easy [[Ardougne Diary]]
|- data-rowid="The Restless Ghost"
| data-sort-value="Restless Ghost, The" |[[The Restless Ghost]]
|- data-rowid="Evil Dave"
|[[Recipe for Disaster/Freeing Evil Dave|RFD: Evil Dave]]
|- data-rowid="Barcrawl"
|[[Alfred Grimhand's Barcrawl]] (miniquest)
|- data-rowid="Natural history quiz"
|[[Natural history quiz]] (miniquest)
|- data-rowid="Cook's Assistant again"
|[[Cook's Assistant]]
|}
"""


def test_parse_optimal_order():
    known = {
        "Cook's Assistant",
        "The Restless Ghost",
        "Recipe for Disaster - Evil Dave",
        "Alfred Grimhand's Barcrawl",
        "Natural History Quiz",
        "Stronghold of Security",  # even if known, an Unlock: row isn't a quest step
    }
    assert refresh.parse_optimal_order(OQG_SAMPLE, known) == [
        "Cook's Assistant",
        "The Restless Ghost",
        "Recipe for Disaster - Evil Dave",
        "Alfred Grimhand's Barcrawl",
        "Natural History Quiz",  # the guide's link differs in case
    ]


def test_parse_xp_rewards():
    wikitext = (
        "==Rewards==\n{{Quest rewards\n|name=X|qp=2|rewards=*{{SCP|Prayer|8,000}} [[Prayer]] "
        "experience\n*{{SCP|thieving|1,000}} [[Thieving]] [[experience]]\n"
        "*4,650 experience in your choice of {{SCP|Agility}} [[Agility]] or {{SCP|Smithing}}\n"
        "*{{SCP|Magic|20,006.9}} [[Magic]] experience\n"
        "*Use the shortcut (with {{SCP|Agility|37}} [[Agility]])\n"
        "*An [[antique lamp]] giving 2,500 XP in any skill\n*[[Robe of Elidinis]]}}\n"
        "*{{SCP|Mining|9,000}} experience outside the template\n"
    )
    assert refresh.parse_xp_rewards(wikitext) == {
        "Agility": None,
        "Magic": 20006,  # decimals truncate, as in game
        "Prayer": 8000,
        "Smithing": None,
        "Thieving": 1000,
    }
    assert refresh.parse_xp_rewards("no rewards here") == {}


def test_collection_log_reference():
    pages = collection_log()
    assert len({p.page for p in pages}) == len(pages)
    assert {p.tab for p in pages} == {"Bosses", "Raids", "Clues", "Minigames", "Other"}
    assert all(p.items for p in pages)
    assert len({item.id for p in pages for item in p.items}) > 1700
    vorkath = next(p for p in pages if p.page == "Vorkath")
    assert vorkath.tab == "Bosses" and "Vorki" in {item.name for item in vorkath.items}
    assert [p.tab for p in pages][:1] == ["Bosses"]  # game order: Bosses first


COLLECTION_LOG_PAGE = """==Ranks==
===Bronze===
==Bosses==
===Abyssal Sire===
===Crazy archaeologist===
==Clues==
===Hard Treasure Trail Rewards (Rare)===
==Duplicate entries==
===Abyssal Sire===
"""


def test_build_collection_log_from_wiki_shapes():
    headings = refresh.parse_collection_log_headings(COLLECTION_LOG_PAGE)
    assert headings == [
        ("Bosses", "Abyssal Sire"),
        ("Bosses", "Crazy archaeologist"),
        ("Clues", "Hard Treasure Trail Rewards (Rare)"),
    ]
    items = [
        {"id": 1, "name": "Pet", "tabs": ["Hard Treasure Trails (Rare)", "Abyssal Sire"]},
        {"id": 2, "name": "Fedora", "tabs": ["Crazy Archaeologist"]},
    ]
    log = refresh.build_collection_log(items, {"1": 2.5}, headings)
    assert [(p["page"], p["tab"]) for p in log] == [
        ("Abyssal Sire", "Bosses"),
        ("Crazy Archaeologist", "Bosses"),
        ("Hard Treasure Trails (Rare)", "Clues"),
    ]
    assert log[0]["items"] == [{"id": 1, "name": "Pet", "completion": 2.5}]
    assert log[1]["items"][0]["completion"] is None
    with pytest.raises(RuntimeError, match="no heading"):
        refresh.build_collection_log([{"id": 3, "name": "X", "tabs": ["Nowhere"]}], {}, headings)
