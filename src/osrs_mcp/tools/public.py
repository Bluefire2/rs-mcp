"""The public endpoint's tools: public data for any player, for bots (see public_auth.py).

These are thin wrappers over the owner endpoint's build_* functions rather than a flag on each
module, so a private tool can't appear here by a missed flag. Differences from the owner's tools:
- `rsn` is required: no fallback to DEFAULT_RSN, so "my stats" can't silently mean the owner's.
- `account_type` defaults to regular, not DEFAULT_ACCOUNT_TYPE; quest and diary requirements are
  checked as for a regular account.
- get_collection_log reads WikiSync only, never the owner's RuneLite snapshots.
- No get_bank, get_equipment_and_inventory or refresh_gains_tracking.
"""

import asyncio
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from osrs_mcp.clients.wom import NotTrackedError, Period
from osrs_mcp.config import AccountType
from osrs_mcp.errors import SourceError
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY
from osrs_mcp.tools.achievements import (
    CaTierName,
    CombatAchievementProgress,
    DiaryProgress,
    DiaryTierName,
    build_combat_achievements,
    build_diary_progress,
)
from osrs_mcp.tools.collection_log import CollectionLog, TabName, build_collection_log
from osrs_mcp.tools.gains import Gains, build_gains
from osrs_mcp.tools.prices import ItemPrices, build_prices
from osrs_mcp.tools.quests import QuestProgress, StatusFilter, build_quest_progress
from osrs_mcp.tools.stats import (
    ActivityScores,
    Category,
    Stats,
    _wikisync_levels,
    build_activities,
    build_stats,
)

Rsn = Annotated[str, Field(min_length=1, max_length=12, description="The player's RuneScape name")]
AccountTypeArg = Annotated[
    AccountType,
    Field(description="Which hiscores to read; pass it for ironman accounts"),
]

# Quest and diary requirements are checked as for a regular account (some are ironman-only).
IS_IRONMAN = False

# The owner's wording points at refresh_gains_tracking, which isn't on this endpoint.
GAINS_NOTE = (
    "Gains are measured between Wise Old Man snapshots, so anything since the latest snapshot "
    "(window_end) is missing. Updating the player on wiseoldman.net takes a new snapshot."
)


def _name(rsn: str) -> str:
    name = rsn.strip()
    if not name:
        raise SourceError("Pass the player's RuneScape name as rsn.")
    return name


def register(mcp: FastMCP, sources: Sources) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_stats(rsn: Rsn, account_type: AccountTypeArg = "regular") -> Stats:
        """Skill levels, XP and ranks from the official hiscores, plus total level, computed
        combat level, XP to the next level and to 99 for every skill, and the closest 99s.
        Hiscore data updates when the player logs out or hops worlds; any skill WikiSync reports
        higher is taken from WikiSync and marked."""
        name = _name(rsn)
        hiscores, synced = await asyncio.gather(
            sources.hiscores.fetch(name, account_type), _wikisync_levels(sources, name)
        )
        return build_stats(hiscores, synced)

    @mcp.tool(annotations=READ_ONLY)
    async def get_activity_scores(
        rsn: Rsn,
        account_type: AccountTypeArg = "regular",
        category: Annotated[
            Category, Field(description="bosses (kill counts), clues, minigames, or all")
        ] = "all",
        include_unranked: Annotated[
            bool, Field(description="Also list activities with a score of 0 or below the minimum")
        ] = False,
    ) -> ActivityScores:
        """Boss kill counts, clue scroll completions and minigame scores from the official
        hiscores. By default only activities with a score above 0 are returned."""
        hiscores = await sources.hiscores.fetch(_name(rsn), account_type)
        return build_activities(hiscores, category, include_unranked)

    @mcp.tool(annotations=READ_ONLY)
    async def get_quest_progress(
        rsn: Rsn,
        status: Annotated[
            StatusFilter,
            Field(description="Which quests to list; 'incomplete' = in progress + not started"),
        ] = "incomplete",
        startable_only: Annotated[
            bool,
            Field(
                description="Only quests whose requirements are met (or unverifiable), best "
                "next quest first"
            ),
        ] = False,
        rewards_skill: Annotated[
            str | None,
            Field(description="Only quests whose rewards include XP in this skill"),
        ] = None,
        quest: Annotated[
            str | None,
            Field(
                description="One quest by name: returns just that quest plus its unfinished "
                "prerequisites. Ignores status, startable_only and rewards_skill."
            ),
        ] = None,
    ) -> QuestProgress:
        """Quest completion and quest points, with, for each unfinished quest, whether it can
        be started now and which skills or prerequisite quests are missing. Use `quest` for
        "has X done / can X do <quest>?". Requires the player to use the WikiSync RuneLite
        plugin."""
        data = await sources.wikisync.fetch(_name(rsn))
        return build_quest_progress(data, status, startable_only, IS_IRONMAN, quest, rewards_skill)

    @mcp.tool(annotations=READ_ONLY)
    async def get_diary_progress(
        rsn: Rsn,
        region: Annotated[
            str | None,
            Field(description="e.g. 'Ardougne', 'Kourend', 'Western Provinces'. Omit for summary"),
        ] = None,
        tier: Annotated[
            DiaryTierName | None, Field(description="Limit details to one tier")
        ] = None,
    ) -> DiaryProgress:
        """Achievement diary progress for every region and tier (done/total, and how many
        remaining tasks are doable with current skills and quests). Pass a region to list the
        remaining tasks with their missing requirements. Requires the WikiSync RuneLite
        plugin."""
        data = await sources.wikisync.fetch(_name(rsn))
        return build_diary_progress(data, region, tier, IS_IRONMAN)

    @mcp.tool(annotations=READ_ONLY)
    async def get_combat_achievements(
        rsn: Rsn,
        tier: Annotated[CaTierName | None, Field(description="Only this tier")] = None,
        monster: Annotated[
            str | None, Field(description="Only tasks for this boss/monster (substring match)")
        ] = None,
        status: Annotated[
            Literal["completed", "incomplete", "all"], Field(description="Which tasks to list")
        ] = "incomplete",
        limit: Annotated[int, Field(ge=1, le=300, description="Max tasks to list")] = 50,
    ) -> CombatAchievementProgress:
        """Combat achievement points and completion per tier, plus a filtered task list with
        each task's boss, type and description. Requires the WikiSync RuneLite plugin."""
        data = await sources.wikisync.fetch(_name(rsn))
        return build_combat_achievements(data, tier, monster, status, limit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_collection_log(
        rsn: Rsn,
        tab: Annotated[TabName | None, Field(description="Only pages on this tab")] = None,
        page: Annotated[
            str | None,
            Field(description="A page such as 'Vorkath' or 'Barrows'; lists all its items"),
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=100, description="Max entries in most_common_missing")
        ] = 15,
    ) -> CollectionLog:
        """Collection log progress: slots filled overall and per tab and page, and the missing
        items most other players have. Pass a page to see every item on it. Data comes from
        WikiSync, after the player presses the WikiSync button in the in-game collection
        log."""
        name = _name(rsn)
        wikisync = await sources.wikisync.fetch(name)
        return build_collection_log(name, wikisync, None, tab, page, limit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_gains(
        rsn: Rsn,
        period: Annotated[Period, Field(description="Time window to measure")] = "week",
    ) -> Gains:
        """XP, levels, boss kills and activity scores gained over the last day, week, month
        or year, from Wise Old Man (only for players Wise Old Man tracks). Only non-zero gains
        are listed, biggest first."""
        name = _name(rsn)
        try:
            gained = await sources.wom.gained(name, period)
        except NotTrackedError:
            raise SourceError(
                f"Wise Old Man isn't tracking '{name}', so there are no gains to show. The player "
                "can start tracking by searching for their name on wiseoldman.net."
            ) from None
        return build_gains(name, period, gained).model_copy(update={"note": GAINS_NOTE})

    @mcp.tool(annotations=READ_ONLY)
    async def get_item_price(
        query: Annotated[str, Field(description="Item name; partial names and typos are OK")],
        limit: Annotated[int, Field(ge=1, le=20, description="Max matching items")] = 5,
    ) -> ItemPrices:
        """Current Grand Exchange prices for an item: latest instant-buy and instant-sell
        prices with when they last traded, the GE buy limit, and high alchemy value."""
        items = await sources.prices.mapping()
        latest = await sources.prices.latest()
        return build_prices(query, items, latest, limit)
