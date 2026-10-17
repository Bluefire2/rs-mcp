"""Hiscores edge cases seen on real accounts."""

import json
from datetime import UTC, datetime

from osrs_mcp.clients.hiscores import parse
from osrs_mcp.tools.stats import build_activities, build_stats
from tests.conftest import fixture_text


def payload_outside_ranked_range() -> dict:
    """Seen live: a mid-level account outside the top ~2M has rank -1 on every skill, but real
    levels and XP, and score 0 (not -1) on activities it has never done."""
    payload = json.loads(fixture_text("hiscores_lynx_titan.json"))
    for skill in payload["skills"]:
        skill["rank"] = -1
        skill["level"], skill["xp"] = 50, 101_333
    payload["skills"][0].update(level=1200, xp=2_500_000)  # Overall
    for activity in payload["activities"]:
        activity["rank"], activity["score"] = -1, 0
    payload["activities"][8].update(score=6)  # Clue Scrolls (beginner)
    return payload


def hiscores():
    return parse(payload_outside_ranked_range(), "regular", datetime.now(UTC))


def test_rank_minus_one_keeps_real_levels():
    stats = build_stats(hiscores())
    assert all(s.level == 50 and s.rank is None for s in stats.skills)
    assert stats.combat_level is not None
    assert len(stats.closest_99s) == 5
    assert stats.total_level == 1200


def test_zero_scores_are_not_listed_by_default():
    result = build_activities(hiscores(), "all", include_unranked=False)
    assert [(a.name, a.score) for a in result.activities] == [("Clue Scrolls (beginner)", 6)]
    everything = build_activities(hiscores(), "all", include_unranked=True)
    assert len(everything.activities) == len(hiscores().activities)


def test_xp_minus_one_means_no_data():
    payload = payload_outside_ranked_range()
    payload["skills"][1].update(level=1, xp=-1)  # Attack
    stats = build_stats(parse(payload, "regular", datetime.now(UTC)))
    attack = next(s for s in stats.skills if s.skill == "Attack")
    assert attack.level is None and attack.xp is None
    assert stats.combat_level is None
