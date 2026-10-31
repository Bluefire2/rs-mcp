"""Hiscores edge cases seen on real accounts."""

import json
from datetime import UTC, datetime

from osrs_mcp.clients.hiscores import parse
from osrs_mcp.clients.wikisync import WikiSyncData
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


def wikisync(levels: dict[str, int] | None = None) -> WikiSyncData:
    data = WikiSyncData.model_validate(json.loads(fixture_text("wikisync_b0aty.json")))
    return data if levels is None else data.model_copy(update={"levels": levels})


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


def test_wikisync_levels_that_dont_fit_the_hiscores_xp_are_ignored():
    # Seen live on "Zezima": WikiSync had 99 in every skill, the hiscores 28M total XP.
    payload = payload_outside_ranked_range()
    payload["skills"][4].update(level=1, xp=-1)  # Hitpoints, not listed
    maxed = {s["name"]: 99 for s in payload["skills"][1:]}
    stats = build_stats(parse(payload, "regular", datetime.now(UTC)), wikisync(maxed))
    assert all(s.level_source == "hiscores" for s in stats.skills)
    assert next(s for s in stats.skills if s.skill == "Attack").level == 50
    assert stats.total_level == 1200
    assert "ignored" in stats.source
    # WikiSync's quests, diaries and combat achievements aren't trusted either.
    assert stats.progress.quest_points is None
    assert stats.progress.diary_tiers_completed is None
    assert stats.progress.combat_achievement_points is None


def test_unlisted_skills_share_the_rest_of_the_total_xp():
    payload = payload_outside_ranked_range()
    payload["skills"][4].update(level=1, xp=-1)  # Hitpoints: 2.5M - 23 x 101,333 = 169,341 XP
    hiscores = parse(payload, "regular", datetime.now(UTC))
    fits = build_stats(hiscores, wikisync({"Hitpoints": 60}))  # 273,742 XP: a session's worth more
    assert next(s for s in fits.skills if s.skill == "Hitpoints").level == 60
    too_much = build_stats(hiscores, wikisync({"Hitpoints": 99}))  # 13M XP
    assert next(s for s in too_much.skills if s.skill == "Hitpoints").level is None


def test_progress_from_wikisync_and_hiscores():
    lynx = parse(json.loads(fixture_text("hiscores_lynx_titan.json")), "regular", datetime.now(UTC))
    # No levels: the fixtures are different accounts, so the levels wouldn't fit.
    progress = build_stats(lynx, wikisync({})).progress
    assert progress.quest_points == 344
    assert (progress.diary_tiers_completed, progress.diary_tiers_total) == (48, 48)
    assert progress.combat_achievement_points == 2697
    assert progress.clue_scrolls == 22
    assert progress.collections_logged is None  # below the hiscores' minimum


def test_progress_without_wikisync():
    stats = build_stats(hiscores())
    assert stats.progress.quest_points is None
    assert stats.progress.diary_tiers_total == 48
