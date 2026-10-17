"""get_quest_progress, from WikiSync plus Wiki quest requirements."""

from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.wikisync import QUEST_STATES, WikiSyncData
from osrs_mcp.reference import quest_reqs
from osrs_mcp.requirements import Checker, MissingSkill
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY, Rsn

WIKISYNC_SOURCE = (
    "WikiSync (uploaded by the RuneLite plugin on login and as things change; see data_as_of)"
)

StatusFilter = Literal["completed", "in_progress", "not_started", "incomplete", "all"]


class QuestEntry(BaseModel):
    name: str
    status: Literal["completed", "in_progress", "not_started"]
    difficulty: str | None = None
    length: str | None = None
    can_start: bool | None = Field(
        default=None,
        description="Requirements met? None = only unverifiable requirements remain "
        "(quest points, miniquests WikiSync doesn't track). Omitted for completed quests.",
    )
    missing_quests: list[str] = []
    missing_skills: list[MissingSkill] = []
    unverified: list[str] = []


class QuestProgress(BaseModel):
    rsn: str
    source: str
    data_as_of: datetime
    counts: dict[str, int]
    note: str
    quests: list[QuestEntry]


def build_quest_progress(
    data: WikiSyncData, status: StatusFilter, startable_only: bool, is_ironman: bool
) -> QuestProgress:
    reqs = quest_reqs()
    checker = Checker(data.levels, data.quests, is_ironman)
    counts = {"completed": 0, "in_progress": 0, "not_started": 0}
    entries = []
    for name, state in sorted(data.quests.items()):
        if name not in reqs and not name[:1].isalnum():
            continue  # WikiSync includes a junk "." key
        quest_status = QUEST_STATES[state]
        counts[quest_status] += 1
        wanted = (
            status == "all"
            or status == quest_status
            or (status == "incomplete" and quest_status != "completed")
        )
        if not wanted:
            continue
        req = reqs.get(name)
        entry = QuestEntry(
            name=name,
            status=quest_status,
            difficulty=req.difficulty if req else None,
            length=req.length if req else None,
        )
        if quest_status != "completed":
            if req:
                result = checker.check(req.skills, req.quests)
                entry.can_start = result.met
                entry.missing_quests = result.missing_quests
                entry.missing_skills = result.missing_skills
                entry.unverified = result.unverified
            else:
                entry.can_start = True  # no requirement data: the Wiki lists none
        if startable_only and entry.can_start is False:
            continue
        entries.append(entry)
    return QuestProgress(
        rsn=data.username,
        source=WIKISYNC_SOURCE + "; requirements from the OSRS Wiki",
        data_as_of=data.timestamp,
        counts=counts,
        note=(
            "Requirement checks cover skills and prerequisite quests only (not items or "
            "combat ability). Skill levels are WikiSync's, so boostable requirements may be "
            "reachable a few levels early."
        ),
        quests=entries,
    )


def register(mcp: FastMCP, sources: Sources) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_quest_progress(
        rsn: Rsn = None,
        status: Annotated[
            StatusFilter,
            Field(description="Which quests to list; 'incomplete' = in progress + not started"),
        ] = "incomplete",
        startable_only: Annotated[
            bool, Field(description="Only quests whose requirements are met (or unverifiable)")
        ] = False,
    ) -> QuestProgress:
        """Quest completion (completed / in progress / not started) with, for each unfinished
        quest, whether it can be started now and exactly which skills or prerequisite quests
        are missing. Use this for "what quests can I do next?". Counts cover all quests
        regardless of the filter. Requires the WikiSync RuneLite plugin."""
        data = await sources.wikisync.fetch(sources.rsn(rsn))
        return build_quest_progress(data, status, startable_only, sources.is_ironman)
