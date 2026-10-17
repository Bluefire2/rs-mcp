"""get_stats and get_activity_scores, from the official hiscores (levels topped up from
WikiSync where the hiscores lag)."""

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.hiscores import Hiscores
from osrs_mcp.config import AccountType
from osrs_mcp.errors import SourceError
from osrs_mcp.game import COMBAT_SKILLS, MAX_LEVEL, XP_FOR_LEVEL, combat_level, xp_to_level
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY, AccountTypeArg, Rsn

HISCORES_SOURCE = "Official OSRS hiscores (cached up to 5 minutes)"
MERGED_SOURCE = (
    "Official OSRS hiscores (cached up to 5 minutes); skills marked level_source='wikisync' "
    "come from WikiSync (the RuneLite plugin, synced on login), where they're higher than the "
    "hiscores show"
)


class SkillStat(BaseModel):
    skill: str
    level: int | None = Field(description="None only if no source has data")
    xp: int | None
    xp_is_minimum: bool = Field(
        default=False,
        description="True when the level came from WikiSync: xp is the minimum for that level, "
        "so the XP-to-go figures are upper bounds",
    )
    level_source: Literal["hiscores", "wikisync"] = "hiscores"
    rank: int | None = Field(description="None if outside the ranked range")
    xp_to_next_level: int | None
    xp_to_99: int | None = Field(description="0 once the skill is 99")


class Stats(BaseModel):
    rsn: str
    account_type: AccountType
    source: str
    fetched_at: datetime
    total_level: int | None
    total_xp: int | None
    combat_level: int | None = Field(description="None if a combat skill has no data")
    closest_99s: list[str] = Field(description="Up to five non-99 skills, least XP remaining first")
    skills: list[SkillStat]


def _with_xp(stat: SkillStat, xp: int) -> SkillStat:
    level = stat.level or 1
    next_level = level + 1 if level < len(XP_FOR_LEVEL) - 1 else None
    stat.xp = xp
    stat.xp_to_next_level = xp_to_level(xp, next_level) if next_level else None
    stat.xp_to_99 = xp_to_level(xp, MAX_LEVEL)
    return stat


def build_stats(hiscores: Hiscores, wikisync_levels: Mapping[str, int] | None = None) -> Stats:
    """Hiscore stats, with any skill WikiSync reports higher taken from WikiSync.

    Seen on a real account: the hiscores list an unranked account's lower skills as level 1 with
    0 XP while WikiSync has the real level (Agility 25). Levels never go down, so the higher of
    the two is the more current.
    """
    wikisync_levels = wikisync_levels or {}
    skills = []
    for s in hiscores.skills:
        synced = wikisync_levels.get(s.name)
        if synced is not None and (s.level is None or synced > s.level):
            stat = SkillStat(
                skill=s.name,
                level=synced,
                xp=None,
                xp_is_minimum=True,
                level_source="wikisync",
                rank=s.rank,
                xp_to_next_level=None,
                xp_to_99=None,
            )
            skills.append(_with_xp(stat, XP_FOR_LEVEL[min(synced, len(XP_FOR_LEVEL) - 1)]))
            continue
        if s.level is None or s.xp is None:
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
    merged = any(s.level_source == "wikisync" for s in skills)
    total_level = hiscores.overall.level
    if merged and all(s.level is not None for s in skills):
        total_level = sum(s.level for s in skills if s.level is not None)
    return Stats(
        rsn=hiscores.rsn,
        account_type=hiscores.account_type,
        source=MERGED_SOURCE if merged else HISCORES_SOURCE,
        fetched_at=hiscores.fetched_at,
        total_level=total_level,
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
        if (include_unranked or (a.score or 0) > 0)
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
        Hiscore data updates when the player logs out or hops worlds; any skill WikiSync reports
        higher (the hiscores can lag for unranked skills) is taken from WikiSync and marked."""
        name = sources.rsn(rsn)
        hiscores, synced = await asyncio.gather(
            sources.hiscores.fetch(name, sources.account_type(account_type)),
            _wikisync_levels(sources, name),
        )
        return build_stats(hiscores, synced)

    @mcp.tool(annotations=READ_ONLY)
    async def get_activity_scores(
        rsn: Rsn = None,
        account_type: AccountTypeArg = None,
        category: Annotated[
            Category, Field(description="bosses (kill counts), clues, minigames, or all")
        ] = "all",
        include_unranked: Annotated[
            bool, Field(description="Also list activities with a score of 0 or below the minimum")
        ] = False,
    ) -> ActivityScores:
        """Boss kill counts, clue scroll completions and minigame scores from the official
        hiscores. By default only activities with a score above 0 are returned."""
        hiscores = await sources.hiscores.fetch(
            sources.rsn(rsn), sources.account_type(account_type)
        )
        return build_activities(hiscores, category, include_unranked)


async def _wikisync_levels(sources: Sources, rsn: str) -> dict[str, int]:
    """WikiSync levels, or none if the player doesn't use WikiSync (it's optional here)."""
    try:
        return (await sources.wikisync.fetch(rsn)).levels
    except SourceError:
        return {}
