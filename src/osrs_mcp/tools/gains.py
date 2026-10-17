"""get_gains and refresh_gains_tracking, from Wise Old Man."""

from datetime import datetime
from typing import Annotated

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from osrs_mcp.clients.wom import Gained, Period
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY, Rsn

WOM_SOURCE = "Wise Old Man (snapshots taken when the player is updated; cached up to 5 minutes)"


class SkillGain(BaseModel):
    skill: str
    xp_gained: int
    levels_gained: int
    level_now: int


class CountGain(BaseModel):
    name: str
    gained: int
    now: int


class Gains(BaseModel):
    rsn: str
    period: Period
    source: str
    window_start: datetime | None = Field(description="First snapshot in the period")
    window_end: datetime | None = Field(description="Latest snapshot in the period")
    total_xp_gained: int
    skills: list[SkillGain]
    bosses: list[CountGain]
    activities: list[CountGain]
    ehp_gained: float | None = Field(description="Efficient hours played gained")
    ehb_gained: float | None = Field(description="Efficient hours bossed gained")
    note: str


def _label(metric: str) -> str:
    return metric.replace("_", " ").title()


def build_gains(rsn: str, period: Period, gained: Gained) -> Gains:
    data = gained.data
    skills = sorted(
        (
            SkillGain(
                skill=_label(name),
                xp_gained=int(s.experience.gained),
                levels_gained=int(s.level.gained),
                level_now=int(s.level.end),
            )
            for name, s in data.skills.items()
            if name != "overall" and s.experience.gained > 0
        ),
        key=lambda s: -s.xp_gained,
    )
    bosses = sorted(
        (
            CountGain(name=_label(name), gained=int(b.kills.gained), now=int(b.kills.end))
            for name, b in data.bosses.items()
            if b.kills.gained > 0
        ),
        key=lambda b: -b.gained,
    )
    activities = sorted(
        (
            CountGain(name=_label(name), gained=int(a.score.gained), now=int(a.score.end))
            for name, a in data.activities.items()
            if a.score.gained > 0
        ),
        key=lambda a: -a.gained,
    )
    overall = data.skills.get("overall")
    ehp, ehb = data.computed.get("ehp"), data.computed.get("ehb")
    return Gains(
        rsn=rsn,
        period=period,
        source=WOM_SOURCE,
        window_start=gained.startsAt,
        window_end=gained.endsAt,
        total_xp_gained=int(overall.experience.gained) if overall else 0,
        skills=skills,
        bosses=bosses,
        activities=activities,
        ehp_gained=round(ehp.value.gained, 2) if ehp else None,
        ehb_gained=round(ehb.value.gained, 2) if ehb else None,
        note=(
            "Gains are measured between Wise Old Man snapshots, so anything since the latest "
            "snapshot (window_end) is missing. refresh_gains_tracking takes a new snapshot."
        ),
    )


class RefreshResult(BaseModel):
    rsn: str
    updated_at: datetime | None
    message: str


def register(mcp: FastMCP, sources: Sources) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_gains(
        rsn: Rsn = None,
        period: Annotated[Period, Field(description="Time window to measure")] = "week",
    ) -> Gains:
        """XP, levels, boss kills and activity scores gained over the last day, week, month
        or year, from Wise Old Man. Only non-zero gains are listed, biggest first."""
        name = sources.rsn(rsn)
        return build_gains(name, period, await sources.wom.gained(name, period))

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, idempotent_hint=True, open_world_hint=True
        )
    )
    async def refresh_gains_tracking(rsn: Rsn = None) -> RefreshResult:
        """Ask Wise Old Man to take a fresh snapshot of the player from the hiscores, so
        get_gains includes recent progress (also starts tracking a new player). Wise Old Man is
        rate-limited (20 requests/minute) and asks for updates at most every hour or so: only
        call this when the user wants up-to-the-minute gains."""
        name = sources.rsn(rsn)
        player = await sources.wom.update(name)
        return RefreshResult(
            rsn=player.displayName,
            updated_at=player.updatedAt,
            message="Snapshot taken. get_gains now reflects the current hiscores.",
        )
