"""get_stats and get_activity_scores, from the official hiscores."""

from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.hiscores import Hiscores
from osrs_mcp.config import AccountType
from osrs_mcp.game import COMBAT_SKILLS, MAX_LEVEL, XP_FOR_LEVEL, combat_level, xp_to_level
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY, AccountTypeArg, Rsn

HISCORES_SOURCE = "Official OSRS hiscores (cached up to 5 minutes)"


class SkillStat(BaseModel):
    skill: str
    level: int | None = Field(description="None if the skill is unranked on the hiscores")
    xp: int | None
    rank: int | None
    xp_to_next_level: int | None
    xp_to_99: int | None = Field(description="0 once the skill is 99")


class Stats(BaseModel):
    rsn: str
    account_type: AccountType
    source: str
    fetched_at: datetime
    total_level: int
    total_xp: int
    combat_level: int | None = Field(description="None if a combat skill is unranked")
    closest_99s: list[str] = Field(description="Up to five non-99 skills, least XP remaining first")
    skills: list[SkillStat]


def build_stats(hiscores: Hiscores) -> Stats:
    skills = []
    for s in hiscores.skills:
        if s.rank is None:
            skills.append(
                SkillStat(
                    skill=s.name,
                    level=None,
                    xp=None,
                    rank=None,
                    xp_to_next_level=None,
                    xp_to_99=None,
                )
            )
            continue
        next_level = s.level + 1 if s.level < len(XP_FOR_LEVEL) - 1 else None
        skills.append(
            SkillStat(
                skill=s.name,
                level=s.level,
                xp=s.xp,
                rank=s.rank,
                xp_to_next_level=xp_to_level(s.xp, next_level) if next_level else None,
                xp_to_99=xp_to_level(s.xp, MAX_LEVEL),
            )
        )
    levels = {s.skill: s.level for s in skills}
    combat = None
    if all(levels.get(name) is not None for name in COMBAT_SKILLS):
        combat = combat_level(levels)
    remaining = sorted((s for s in skills if s.xp_to_99), key=lambda s: s.xp_to_99 or 0)
    return Stats(
        rsn=hiscores.rsn,
        account_type=hiscores.account_type,
        source=HISCORES_SOURCE,
        fetched_at=hiscores.fetched_at,
        total_level=hiscores.overall.level,
        total_xp=hiscores.overall.xp,
        combat_level=combat,
        closest_99s=[s.skill for s in remaining[:5]],
        skills=skills,
    )


# Activities that aren't bosses; anything not listed here (including newly added bosses)
# counts as a boss.
CLUE_PREFIX = "Clue Scrolls"
MINIGAMES = frozenset(
    {
        "Grid Points",
        "League Points",
        "Deadman Points",
        "Bounty Hunter - Hunter",
        "Bounty Hunter - Rogue",
        "Bounty Hunter (Legacy) - Hunter",
        "Bounty Hunter (Legacy) - Rogue",
        "LMS - Rank",
        "PvP Arena - Rank",
        "Soul Wars Zeal",
        "Rifts closed",
        "Colosseum Glory",
        "Collections Logged",
    }
)

Category = Literal["bosses", "clues", "minigames", "all"]


def category_of(name: str) -> Category:
    if name.startswith(CLUE_PREFIX):
        return "clues"
    if name in MINIGAMES:
        return "minigames"
    return "bosses"


class ActivityScore(BaseModel):
    name: str
    category: Category
    score: int | None
    rank: int | None


class ActivityScores(BaseModel):
    rsn: str
    account_type: AccountType
    source: str
    fetched_at: datetime
    note: str
    activities: list[ActivityScore]


def build_activities(
    hiscores: Hiscores, category: Category, include_unranked: bool
) -> ActivityScores:
    rows = [
        ActivityScore(name=a.name, category=category_of(a.name), score=a.score, rank=a.rank)
        for a in hiscores.activities
        if (include_unranked or a.score is not None)
        and (category == "all" or category_of(a.name) == category)
    ]
    return ActivityScores(
        rsn=hiscores.rsn,
        account_type=hiscores.account_type,
        source=HISCORES_SOURCE,
        fetched_at=hiscores.fetched_at,
        note=(
            "Hiscores only rank an activity past a minimum score (for example 5 boss kills), "
            "so a missing entry may still mean a few kills."
        ),
        activities=rows,
    )


def register(mcp: FastMCP, sources: Sources) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_stats(rsn: Rsn = None, account_type: AccountTypeArg = None) -> Stats:
        """Skill levels, XP and ranks from the official hiscores, plus total level, computed
        combat level, XP to the next level and to 99 for every skill, and the closest 99s.
        Hiscore data updates when the player logs out or hops worlds."""
        hiscores = await sources.hiscores.fetch(
            sources.rsn(rsn), sources.account_type(account_type)
        )
        return build_stats(hiscores)

    @mcp.tool(annotations=READ_ONLY)
    async def get_activity_scores(
        rsn: Rsn = None,
        account_type: AccountTypeArg = None,
        category: Annotated[
            Category, Field(description="bosses (kill counts), clues, minigames, or all")
        ] = "all",
        include_unranked: Annotated[
            bool, Field(description="Also list activities with no hiscore entry")
        ] = False,
    ) -> ActivityScores:
        """Boss kill counts, clue scroll completions and minigame scores from the official
        hiscores. By default only activities the player is ranked in are returned."""
        hiscores = await sources.hiscores.fetch(
            sources.rsn(rsn), sources.account_type(account_type)
        )
        return build_activities(hiscores, category, include_unranked)
