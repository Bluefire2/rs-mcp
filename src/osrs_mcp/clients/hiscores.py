"""Official OSRS hiscores (JSON variant of index_lite)."""

from datetime import UTC, datetime

from pydantic import BaseModel

from osrs_mcp.config import AccountType
from osrs_mcp.errors import SourceError
from osrs_mcp.http import HttpClient

BASE = "https://secure.runescape.com"
TTL = 300

HISCORE_PATHS: dict[AccountType, str] = {
    "regular": "hiscore_oldschool",
    "ironman": "hiscore_oldschool_ironman",
    "hardcore_ironman": "hiscore_oldschool_hardcore_ironman",
    "ultimate": "hiscore_oldschool_ultimate",
    "deadman": "hiscore_oldschool_deadman",
    "seasonal": "hiscore_oldschool_seasonal",
}


class Skill(BaseModel):
    name: str
    # Rank -1 only means "outside the ranked range": level and XP are still real. They are
    # None only if the hiscores report XP as -1 (no data).
    level: int | None
    xp: int | None
    rank: int | None


class Activity(BaseModel):
    name: str
    score: int | None  # None = below the hiscores' minimum score (-1); 0 is a real score
    rank: int | None


class Hiscores(BaseModel):
    rsn: str
    account_type: AccountType
    overall: Skill
    skills: list[Skill]
    activities: list[Activity]
    fetched_at: datetime


def _ranked(value: int) -> int | None:
    return None if value < 0 else value


def parse(payload: dict, account_type: AccountType, fetched_at: datetime) -> Hiscores:
    skills = [
        Skill(
            name=s["name"],
            level=s["level"] if s["xp"] >= 0 else None,
            xp=s["xp"] if s["xp"] >= 0 else None,
            rank=_ranked(s["rank"]),
        )
        for s in payload["skills"]
    ]
    overall, *rest = skills
    activities = [
        Activity(name=a["name"], score=_ranked(a["score"]), rank=_ranked(a["rank"]))
        for a in payload["activities"]
    ]
    return Hiscores(
        rsn=payload["name"],
        account_type=account_type,
        overall=overall,
        skills=rest,
        activities=activities,
        fetched_at=fetched_at,
    )


class HiscoresClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def fetch(self, rsn: str, account_type: AccountType) -> Hiscores:
        url = f"{BASE}/m={HISCORE_PATHS[account_type]}/index_lite.json"
        response = await self._http.get(url, params={"player": rsn}, ttl=TTL)
        if response.status_code == 404:
            hint = (
                "try account_type='regular'"
                if account_type != "regular"
                else "if this is an ironman account, pass its account_type"
            )
            raise SourceError(
                f"'{rsn}' isn't on the {account_type} hiscores. Check the spelling or a recent "
                f"name change; {hint}."
            )
        if response.status_code != 200:
            raise SourceError(
                f"The OSRS hiscores returned HTTP {response.status_code}; try again shortly."
            )
        fetched_at = datetime.fromtimestamp(response.fetched_at, UTC)
        return parse(response.json(), account_type, fetched_at)
