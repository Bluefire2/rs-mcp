"""get_quest_progress, from WikiSync plus Wiki quest requirements."""

from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process, utils

from osrs_mcp.clients.wikisync import QUEST_STATES, WikiSyncData
from osrs_mcp.errors import SourceError
from osrs_mcp.reference import quest_reqs
from osrs_mcp.requirements import STARTED, Checker, MissingSkill, player_quest_points
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
        description="Requirements to start met? None = only unverifiable requirements remain "
        "(miniquests WikiSync doesn't track). Omitted for completed quests. Starting isn't "
        "finishing: some quests need more partway through (see the quest's Wiki page).",
    )
    missing_quests: list[str] = []
    missing_skills: list[MissingSkill] = []
    unverified: list[str] = []


class QuestProgress(BaseModel):
    rsn: str
    source: str
    data_as_of: datetime
    quest_points: int = Field(
        description="Total quest points: completed quests (Wiki QP values) plus Tutorial Island's 1"
    )
    counts: dict[str, int]
    note: str
    quests: list[QuestEntry]
    route: list[QuestEntry] | None = Field(
        default=None,
        description="With `quest`: every unfinished prerequisite at any depth, in an order that "
        "can be done (each before the quests that need it), ending with the quest itself",
    )
    route_skill_targets: list[MissingSkill] | None = Field(
        default=None,
        description="With `quest`: the highest level each skill needs anywhere on the route, "
        "where the player is below it, biggest gap first",
    )


ROMAN = {"1": "i", "2": "ii", "3": "iii"}


def match_quests(names: list[str], query: str) -> list[str]:
    """Exact (case-insensitive) name, else names containing the query, else close spellings.
    Standalone 1-3 become Roman numerals, as in quest names ("fairytale 2")."""
    q = " ".join(ROMAN.get(word, word) for word in query.strip().lower().split())
    exact = [n for n in names if n.lower() == q]
    if exact:
        return exact
    partial = [n for n in names if q in n.lower()]
    if partial:
        return partial
    # Plain edit-distance similarity: catches typos ("recipie for disaster") without matching
    # every quest that merely shares a word ("... quest").
    matches = process.extract(
        q, names, scorer=fuzz.ratio, processor=utils.default_process, limit=3, score_cutoff=80
    )
    # Keep only near-ties with the best match, so 'dragon slyer ii' isn't also Dragon Slayer I.
    best = matches[0][1] if matches else 0
    return [name for name, score, _index in matches if score >= best - 2]


def unfinished_route(target: str, states: dict[str, int]) -> list[str]:
    """Unfinished prerequisites of `target` at any depth, each before the quests that need it
    (depth-first post-order), ending with the target itself."""
    reqs = quest_reqs()
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        req = reqs.get(name)
        for prereq in req.quests if req else []:
            prereq_name = prereq.removeprefix(STARTED)
            needed = 1 if prereq.startswith(STARTED) else 2
            if states.get(prereq_name, 0) < needed:
                visit(prereq_name)
        order.append(name)

    visit(target)
    return order


def build_quest_progress(
    data: WikiSyncData,
    status: StatusFilter,
    startable_only: bool,
    is_ironman: bool,
    quest: str | None = None,
) -> QuestProgress:
    reqs = quest_reqs()
    qp = player_quest_points(data.quests)
    checker = Checker(data.levels, data.quests, is_ironman, quest_points=qp)
    # WikiSync includes a junk "." key; keep real quest names only.
    states = {name: state for name, state in data.quests.items() if name[:1].isalnum()}

    def entry_for(name: str) -> QuestEntry:
        quest_status = QUEST_STATES[states.get(name, 0)]
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
        return entry

    counts = {"completed": 0, "in_progress": 0, "not_started": 0}
    for state in states.values():
        counts[QUEST_STATES[state]] += 1

    route = route_targets = None
    if quest:
        names = match_quests(sorted(states), quest)
        if not names:
            raise SourceError(
                f"No quest matches '{quest}'. Use the quest's full name, e.g. 'Dragon Slayer II'."
            )
        entries = [entry_for(name) for name in names]
        if len(names) == 1:
            route = [entry_for(name) for name in unfinished_route(names[0], states)]
            targets: dict[str, MissingSkill] = {}
            for step in route:
                for missing in step.missing_skills:
                    best = targets.get(missing.skill)
                    if best is None or missing.need > best.need:
                        targets[missing.skill] = missing
            route_targets = sorted(targets.values(), key=lambda m: m.need - m.have, reverse=True)
    else:
        entries = []
        for name, state in sorted(states.items()):
            quest_status = QUEST_STATES[state]
            wanted = (
                status == "all"
                or status == quest_status
                or (status == "incomplete" and quest_status != "completed")
            )
            if not wanted:
                continue
            entry = entry_for(name)
            if startable_only and entry.can_start is False:
                continue
            entries.append(entry)

    return QuestProgress(
        rsn=data.username,
        source=WIKISYNC_SOURCE + "; requirements from the OSRS Wiki",
        data_as_of=data.timestamp,
        quest_points=qp,
        counts=counts,
        note=(
            "Requirement checks cover skills and prerequisite quests only (not items or "
            "combat ability). Skill levels are WikiSync's, so boostable requirements may be "
            "reachable a few levels early."
        ),
        quests=entries,
        route=route,
        route_skill_targets=route_targets,
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
        quest: Annotated[
            str | None,
            Field(
                description="One quest by name (e.g. 'Dragon Slayer II', 'Recipe for "
                "Disaster'): returns just that quest plus its full route of unfinished "
                "prerequisites and the skill targets along it. Ignores status/startable_only."
            ),
        ] = None,
    ) -> QuestProgress:
        """Quest completion and quest points, with, for each unfinished quest, whether it can
        be started now and exactly which skills or prerequisite quests are missing. Use
        `quest` for "can I do X?" or "how far am I from X?" (small answer, includes the full
        prerequisite route); use `startable_only` for "what quests can I do next?". Counts
        cover all quests regardless of filters. Requires the WikiSync RuneLite plugin."""
        data = await sources.wikisync.fetch(sources.rsn(rsn))
        return build_quest_progress(data, status, startable_only, sources.is_ironman, quest)
