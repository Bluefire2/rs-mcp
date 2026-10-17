"""Cross-check the committed reference data against a real WikiSync payload, and unit-test the
parsers in scripts/refresh_reference_data.py."""

import importlib.util
from pathlib import Path

import pytest

from osrs_mcp.reference import combat_achievements, diary_tasks, quest_points, quest_reqs
from osrs_mcp.requirements import player_quest_points
from tests.conftest import fixture_json

SCRIPT = Path(__file__).parent.parent / "scripts" / "refresh_reference_data.py"
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
