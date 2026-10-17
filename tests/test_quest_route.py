"""get_quest_progress(quest=...): one quest plus its full route of unfinished prerequisites, so
"can I do X?" doesn't need the whole 200-quest list (55 KB, unusable in a phone chat)."""

import json

import pytest

from osrs_mcp.clients.wikisync import WikiSyncData
from osrs_mcp.errors import SourceError
from osrs_mcp.reference import quest_reqs
from osrs_mcp.requirements import STARTED
from osrs_mcp.tools.quests import build_quest_progress
from tests.conftest import fixture_text


def account(done: tuple[str, ...] = (), started: tuple[str, ...] = (), level: int = 30):
    """A WikiSync payload for a low-level account: every quest not started unless listed."""
    payload = json.loads(fixture_text("wikisync_b0aty.json"))
    payload["quests"] = {
        name: 2 if name in done else 1 if name in started else 0 for name in payload["quests"]
    }
    payload["levels"] = {skill: level for skill in payload["levels"]}
    return WikiSyncData.model_validate(payload)


def progress(data: WikiSyncData, quest: str):
    return build_quest_progress(data, "incomplete", False, False, quest)


def test_single_quest_returns_just_that_quest_and_its_route():
    result = progress(account(), "dragon slayer ii")  # case-insensitive exact match
    assert [q.name for q in result.quests] == ["Dragon Slayer II"]
    route = [step.name for step in result.route]
    assert route[-1] == "Dragon Slayer II"
    assert "Legends' Quest" in route and "Dragon Slayer I" in route
    assert len(route) == len(set(route))


def test_route_puts_every_prerequisite_before_the_quests_that_need_it():
    route = [step.name for step in progress(account(), "Dragon Slayer II").route]
    position = {name: i for i, name in enumerate(route)}
    reqs = quest_reqs()
    for name in route:
        for prereq in reqs[name].quests if name in reqs else []:
            prereq_name = prereq.removeprefix(STARTED)
            if prereq_name in position:
                assert position[prereq_name] < position[name], (prereq_name, name)


def test_route_skips_finished_and_already_started_prerequisites():
    data = account(done=("Dragon Slayer I",), started=("Legends' Quest",))
    route = [step.name for step in progress(data, "Dragon Slayer II").route]
    assert "Dragon Slayer I" not in route
    # Legends' Quest is a full prerequisite of DS2, so a started one stays on the route.
    assert "Legends' Quest" in route


def test_route_skill_targets_take_the_highest_need_per_skill():
    result = progress(account(level=30), "Dragon Slayer II")
    targets = {t.skill: t for t in result.route_skill_targets}
    assert targets["Magic"].need == 75 and targets["Magic"].have == 30
    gaps = [t.need - t.have for t in result.route_skill_targets]
    assert gaps == sorted(gaps, reverse=True)


def test_partial_name_lists_matches_without_a_route():
    result = progress(account(), "dragon slayer")
    assert {q.name for q in result.quests} >= {"Dragon Slayer I", "Dragon Slayer II"}
    assert result.route is None


def test_close_spelling_matches():
    result = progress(account(), "dragon slyer ii")
    assert [q.name for q in result.quests] == ["Dragon Slayer II"]


def test_unknown_quest_is_an_actionable_error():
    with pytest.raises(SourceError, match="full name"):
        progress(account(), "Totally Made Up Quest")


def test_quest_filter_still_reports_account_totals():
    data = account(done=("Cook's Assistant",))
    result = progress(data, "Dragon Slayer II")
    assert result.counts["completed"] == 1
    assert result.quest_points >= 2  # Cook's Assistant (1) + Tutorial Island (1)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("recipie for disaster", ["Recipe for Disaster"]),
        ("legends quest", ["Legends' Quest"]),
        ("desert treasure 1", ["Desert Treasure I"]),
        ("fairytale 2", ["Fairytale II - Cure a Queen"]),
    ],
)
def test_typos_and_arabic_numerals_match(query, expected):
    assert [q.name for q in progress(account(), query).quests] == expected


def test_start_only_prerequisite_does_not_count_its_completion_skills():
    # Seen in a skill test: RFD's Sir Amik Varze subquest only needs Legends' Quest *started*,
    # but the route counted Legends' Quest's completion skills (45 Herblore, 50 Thieving, ...).
    result = progress(account(level=1), "Recipe for Disaster")
    steps = {step.name: step for step in result.route}
    assert steps["Legends' Quest"].needed == "start"
    assert steps["Recipe for Disaster"].needed == "finish"
    targets = {t.skill: t.need for t in result.route_skill_targets}
    assert targets.get("Herblore", 0) < 45  # 45 is only needed to finish Legends' Quest
    assert targets["Quest point"] == 175  # QP gates still count


def test_finish_wins_when_a_quest_is_needed_both_ways():
    # Legends' Quest is a full prerequisite of Dragon Slayer II.
    steps = {s.name: s for s in progress(account(), "Dragon Slayer II").route}
    assert steps["Legends' Quest"].needed == "finish"


def test_route_counts_remaining_quests():
    result = progress(account(), "Dragon Slayer II")
    assert result.route_quests_remaining == len(result.route)


def listing(data: WikiSyncData, startable_only: bool = True, rewards_skill: str | None = None):
    return build_quest_progress(data, "incomplete", startable_only, False, None, rewards_skill)


def test_startable_quests_follow_the_optimal_quest_guide():
    quests = listing(account(level=1)).quests
    orders = [q.optimal_order for q in quests if q.optimal_order is not None]
    assert orders == sorted(orders) and len(orders) > 10
    assert [q.name for q in quests[:2]] == ["Learning the Ropes", "Cook's Assistant"]
    unlisted = [i for i, q in enumerate(quests) if q.optimal_order is None]
    assert unlisted == list(range(len(quests) - len(unlisted), len(quests)))  # all at the end


def test_rewards_skill_lists_quests_giving_that_xp():
    result = listing(account(level=1), rewards_skill="agility")  # case-insensitive
    assert result.quests
    assert all("Agility" in q.xp_rewards for q in result.quests)
    assert all(q.can_start is not False for q in result.quests)
    everything = listing(account(level=1), startable_only=False, rewards_skill="Agility")
    assert "Dragon Slayer II" in {q.name for q in everything.quests}
    assert listing(account(level=1)).quests[0].xp_rewards is None  # only with rewards_skill


def test_unknown_rewards_skill_is_an_actionable_error():
    with pytest.raises(SourceError, match=r"Use one of: .*Agility"):
        listing(account(), rewards_skill="Sneaking")


def test_miniquests_are_flagged():
    [entry] = progress(account(), "Barbarian Training").quests
    assert entry.miniquest
    assert not progress(account(), "Cook's Assistant").quests[0].miniquest
