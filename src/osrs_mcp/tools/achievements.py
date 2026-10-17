"""get_diary_progress and get_combat_achievements, from WikiSync plus Wiki task data."""

from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.wikisync import WikiSyncData
from osrs_mcp.errors import SourceError
from osrs_mcp.reference import combat_achievements, diary_tasks
from osrs_mcp.requirements import Checker, MissingSkill, player_quest_points
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY, Rsn
from osrs_mcp.tools.quests import WIKISYNC_SOURCE

DIARY_TIERS = ("Easy", "Medium", "Hard", "Elite")
DiaryTierName = Literal["Easy", "Medium", "Hard", "Elite"]


# --- diaries -----------------------------------------------------------------------------------


class TierSummary(BaseModel):
    complete: bool | None = Field(
        description="Reward claimed in game. None = not reported by WikiSync"
    )
    done: int
    total: int
    remaining_doable_now: int = Field(
        description="Remaining tasks whose skill and quest requirements are already met"
    )


class DiaryTaskStatus(BaseModel):
    task: str
    doable_now: bool | None
    missing_skills: list[MissingSkill] = []
    missing_quests: list[str] = []
    unverified: list[str] = []


class DiaryTierDetail(BaseModel):
    region: str
    tier: str
    complete: bool | None
    remaining: list[DiaryTaskStatus]


class DiaryProgress(BaseModel):
    rsn: str
    source: str
    data_as_of: datetime
    note: str
    summary: dict[str, dict[str, TierSummary]]
    details: list[DiaryTierDetail] = Field(
        description="Remaining tasks, only when a region (and optionally tier) is requested"
    )


def _resolve_region(region: str) -> str:
    wanted = region.strip().lower()
    regions = list(diary_tasks())
    for name in regions:
        if name.lower() == wanted or name.lower().startswith(wanted):
            return name
    raise SourceError(f"Unknown diary region '{region}'. Regions: {', '.join(regions)}.")


def build_diary_progress(
    data: WikiSyncData, region: str | None, tier: DiaryTierName | None, is_ironman: bool
) -> DiaryProgress:
    checker = Checker(
        data.levels, data.quests, is_ironman, quest_points=player_quest_points(data.quests)
    )
    reference = diary_tasks()
    wanted_region = _resolve_region(region) if region else None

    summary: dict[str, dict[str, TierSummary]] = {}
    details: list[DiaryTierDetail] = []
    for region_name, tiers in data.achievement_diaries.items():
        summary[region_name] = {}
        for tier_name in DIARY_TIERS:
            state = tiers.get(tier_name)
            tasks = reference.get(region_name, {}).get(tier_name, [])
            if state is None:
                continue
            remaining = []
            for done, task in zip(state.tasks, tasks, strict=False):
                if done:
                    continue
                result = checker.check(task.skills, task.quests)
                remaining.append(
                    DiaryTaskStatus(
                        task=task.task,
                        doable_now=result.met,
                        missing_skills=result.missing_skills,
                        missing_quests=result.missing_quests,
                        unverified=result.unverified,
                    )
                )
            summary[region_name][tier_name] = TierSummary(
                complete=state.complete,
                done=sum(1 for done in state.tasks if done),
                total=len(state.tasks),
                remaining_doable_now=sum(1 for t in remaining if t.doable_now),
            )
            if region_name == wanted_region and (tier is None or tier == tier_name):
                details.append(
                    DiaryTierDetail(
                        region=region_name,
                        tier=tier_name,
                        complete=state.complete,
                        remaining=remaining,
                    )
                )
    return DiaryProgress(
        rsn=data.username,
        source=WIKISYNC_SOURCE + "; task text and requirements from the OSRS Wiki",
        data_as_of=data.timestamp,
        note=(
            "doable_now checks skills and quests only; tasks can also need items, kills or "
            "unlocks. 'complete' means the reward was claimed in game; done == total means all "
            "tasks are done."
        ),
        summary=summary,
        details=details,
    )


# --- combat achievements ----------------------------------------------------------------------

CA_TIERS = ("Easy", "Medium", "Hard", "Elite", "Master", "Grandmaster")
CA_POINTS = {"Easy": 1, "Medium": 2, "Hard": 3, "Elite": 4, "Master": 5, "Grandmaster": 6}
CaTierName = Literal["Easy", "Medium", "Hard", "Elite", "Master", "Grandmaster"]


class CaTierSummary(BaseModel):
    done: int
    total: int


class CaTask(BaseModel):
    name: str
    tier: str | None
    monster: str | None
    type: str | None
    description: str
    completed: bool


class CombatAchievementProgress(BaseModel):
    rsn: str
    source: str
    data_as_of: datetime
    points: int
    by_tier: dict[str, CaTierSummary]
    matching_tasks: int
    tasks: list[CaTask] = Field(description="Filtered tasks, easiest tier first, up to limit")


def build_combat_achievements(
    data: WikiSyncData,
    tier: CaTierName | None,
    monster: str | None,
    status: Literal["completed", "incomplete", "all"],
    limit: int,
) -> CombatAchievementProgress:
    reference = combat_achievements()
    completed = set(data.combat_achievements)
    by_tier = {t: CaTierSummary(done=0, total=0) for t in CA_TIERS}
    points = 0
    tasks = []
    for ca in reference.values():
        is_done = ca.id in completed
        if ca.tier in by_tier:
            by_tier[ca.tier].total += 1
            if is_done:
                by_tier[ca.tier].done += 1
                points += CA_POINTS[ca.tier]
        if tier and ca.tier != tier:
            continue
        if monster and monster.lower() not in (ca.monster or "").lower():
            continue
        if (status == "completed" and not is_done) or (status == "incomplete" and is_done):
            continue
        tasks.append(
            CaTask(
                name=ca.name,
                tier=ca.tier,
                monster=ca.monster,
                type=ca.type,
                description=ca.task,
                completed=is_done,
            )
        )
    order = {t: i for i, t in enumerate(CA_TIERS)}
    tasks.sort(key=lambda t: (order.get(t.tier or "", 99), t.monster or "", t.name))
    return CombatAchievementProgress(
        rsn=data.username,
        source=WIKISYNC_SOURCE + "; task details from the OSRS Wiki",
        data_as_of=data.timestamp,
        points=points,
        by_tier=by_tier,
        matching_tasks=len(tasks),
        tasks=tasks[:limit],
    )


def register(mcp: FastMCP, sources: Sources) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_diary_progress(
        rsn: Rsn = None,
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
        remaining tasks with their missing requirements; use this for "which diaries can I
        finish now?". Requires the WikiSync RuneLite plugin."""
        data = await sources.wikisync.fetch(sources.rsn(rsn))
        return build_diary_progress(data, region, tier, sources.is_ironman)

    @mcp.tool(annotations=READ_ONLY)
    async def get_combat_achievements(
        rsn: Rsn = None,
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
        data = await sources.wikisync.fetch(sources.rsn(rsn))
        return build_combat_achievements(data, tier, monster, status, limit)
