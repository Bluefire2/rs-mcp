"""Static reference data built from the OSRS Wiki by scripts/refresh_reference_data.py."""

import json
from functools import cache
from importlib import resources

from pydantic import BaseModel


class SkillReq(BaseModel):
    skill: str
    level: int
    ironman_only: bool = False
    boostable: bool = False


class QuestReqs(BaseModel):
    quests: list[str]  # direct prerequisites; "Started:<name>" means it only has to be started
    skills: list[SkillReq]
    difficulty: str | None = None
    length: str | None = None
    miniquest: bool = False
    optimal_order: int | None = None  # position in the Wiki's "Optimal quest guide", 1 = first
    xp_rewards: dict[str, int | None] = {}  # skill -> fixed XP, None if chosen or variable


class DiaryTask(BaseModel):
    task: str
    skills: list[SkillReq]
    quests: list[str]


class CombatAchievement(BaseModel):
    id: int
    name: str
    tier: str | None
    monster: str | None
    type: str | None
    task: str


def _load(name: str):
    text = resources.files("osrs_mcp.reference").joinpath("data", name).read_text("utf-8")
    return json.loads(text)


@cache
def quest_reqs() -> dict[str, QuestReqs]:
    return {name: QuestReqs.model_validate(v) for name, v in _load("quest_reqs.json").items()}


@cache
def quest_points() -> dict[str, int]:
    """Quest points per WikiSync quest name (miniquests 0), plus "Tutorial Island", which
    WikiSync doesn't report."""
    return _load("quest_points.json")


@cache
def diary_tasks() -> dict[str, dict[str, list[DiaryTask]]]:
    return {
        region: {
            tier: [DiaryTask.model_validate(task) for task in tasks]
            for tier, tasks in tiers.items()
        }
        for region, tiers in _load("diary_tasks.json").items()
    }


@cache
def item_names() -> dict[int, str]:
    """Every item id the Wiki knows, tradeable or not."""
    return {int(item_id): name for item_id, name in _load("item_names.json").items()}


@cache
def combat_achievements() -> dict[int, CombatAchievement]:
    rows = _load("combat_achievements.json")
    return {row["id"]: CombatAchievement.model_validate(row) for row in rows}


class CollectionLogItem(BaseModel):
    id: int
    name: str
    completion: float | None  # % of WikiSync collection log users who have it


class CollectionLogPage(BaseModel):
    page: str
    tab: str  # Bosses, Raids, Clues, Minigames or Other
    items: list[CollectionLogItem]


@cache
def collection_log() -> list[CollectionLogPage]:
    """Every collection log page in game order. An item can be on more than one page."""
    return [CollectionLogPage.model_validate(page) for page in _load("collection_log.json")]
