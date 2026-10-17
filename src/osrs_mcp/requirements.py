"""Check quest and diary requirements against a player's WikiSync levels and quest states."""

from collections.abc import Iterable, Mapping

from pydantic import BaseModel

from osrs_mcp.game import COMBAT_SKILLS, combat_level
from osrs_mcp.reference import SkillReq, quest_points

STARTED = "Started:"
QUEST_POINT = "Quest point"  # the skill name Questreq uses for QP requirements


def player_quest_points(quest_states: Mapping[str, int]) -> int:
    """Quest points from completed quests, plus Tutorial Island's point: every account has
    finished it, and WikiSync doesn't track it."""
    points = quest_points()
    total = sum(points.get(name, 0) for name, state in quest_states.items() if state == 2)
    if "Tutorial Island" not in quest_states:
        total += points["Tutorial Island"]
    return total


class MissingSkill(BaseModel):
    skill: str
    need: int
    have: int
    boostable: bool


class RequirementCheck(BaseModel):
    met: bool | None
    """True if every checkable requirement is met and nothing is unverifiable; None if only
    unverifiable requirements remain; False if something is definitely missing."""
    missing_skills: list[MissingSkill] = []
    missing_quests: list[str] = []
    unverified: list[str] = []


class Checker:
    def __init__(
        self,
        levels: Mapping[str, int],
        quest_states: Mapping[str, int],
        is_ironman: bool,
        quest_points: int | None = None,
    ) -> None:
        self.levels = dict(levels)
        if all(skill in levels for skill in COMBAT_SKILLS):
            self.levels["Combat"] = combat_level(levels)
        if quest_points is not None:
            self.levels[QUEST_POINT] = quest_points
        self.quest_states = quest_states
        self.is_ironman = is_ironman

    def check(self, skills: Iterable[SkillReq], quests: Iterable[str]) -> RequirementCheck:
        missing_skills: list[MissingSkill] = []
        missing_quests: list[str] = []
        unverified: list[str] = []

        for req in skills:
            if req.ironman_only and not self.is_ironman:
                continue
            have = self.levels.get(req.skill)
            if have is None:
                # Barbarian Assault ranks, Kudos, ...: not reported by WikiSync.
                unverified.append(f"{req.level} {req.skill}")
            elif have < req.level:
                missing_skills.append(
                    MissingSkill(
                        skill=req.skill, need=req.level, have=have, boostable=req.boostable
                    )
                )

        for prereq in quests:
            started_only = prereq.startswith(STARTED)
            name = prereq.removeprefix(STARTED)
            state = self.quest_states.get(name)
            if state is None:
                unverified.append(f"{'start ' if started_only else ''}{name}")
            elif state < (1 if started_only else 2):
                missing_quests.append(f"{name} (start it)" if started_only else name)

        met: bool | None
        if missing_skills or missing_quests:
            met = False
        elif unverified:
            met = None
        else:
            met = True
        return RequirementCheck(
            met=met,
            missing_skills=missing_skills,
            missing_quests=missing_quests,
            unverified=unverified,
        )
