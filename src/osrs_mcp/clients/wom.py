"""Wise Old Man: XP and kill-count tracking over time.

Rate limit: 20 requests/minute without an API key (100 with one). Responses are cached and
updates are only triggered on explicit request.
"""

import contextlib
from datetime import datetime
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel

from osrs_mcp.errors import SourceError
from osrs_mcp.http import CachedResponse, HttpClient

BASE = "https://api.wiseoldman.net/v2"
TTL = 300

Period = Literal["day", "week", "month", "year"]


class Delta(BaseModel):
    gained: float
    start: float
    end: float


class SkillGain(BaseModel):
    metric: str
    experience: Delta
    level: Delta


class BossGain(BaseModel):
    metric: str
    kills: Delta


class ActivityGain(BaseModel):
    metric: str
    score: Delta


class ComputedGain(BaseModel):
    metric: str
    value: Delta


class GainedData(BaseModel):
    skills: dict[str, SkillGain]
    bosses: dict[str, BossGain]
    activities: dict[str, ActivityGain]
    computed: dict[str, ComputedGain]


class Gained(BaseModel):
    startsAt: datetime | None
    endsAt: datetime | None
    data: GainedData


class UpdatedPlayer(BaseModel):
    displayName: str
    updatedAt: datetime | None


class NotTrackedError(SourceError):
    """Wise Old Man doesn't track this player (HTTP 404)."""


class WomClient:
    def __init__(self, http: HttpClient, api_key: str = "") -> None:
        self._http = http
        self._headers = {"x-api-key": api_key} if api_key else None

    async def gained(self, rsn: str, period: Period) -> Gained:
        response = await self._http.get(
            f"{BASE}/players/{quote(rsn)}/gained",
            params={"period": period},
            headers=self._headers,
            ttl=TTL,
        )
        self._raise_for(response, rsn)
        return Gained.model_validate(response.json())

    async def update(self, rsn: str) -> UpdatedPlayer:
        response = await self._http.post(f"{BASE}/players/{quote(rsn)}", headers=self._headers)
        self._raise_for(response, rsn)
        self._http.clear_cache()
        return UpdatedPlayer.model_validate(response.json())

    @staticmethod
    def _raise_for(response: CachedResponse, rsn: str) -> None:
        if response.status_code == 404:
            raise NotTrackedError(
                f"Wise Old Man isn't tracking '{rsn}' yet. Call refresh_gains_tracking once to "
                "start tracking; gains appear after a second update later on."
            )
        if response.status_code == 429:
            raise SourceError(
                "Wise Old Man rate limit hit (20 requests/minute). Wait a minute and try again."
            )
        if response.status_code >= 400:
            message = ""
            with contextlib.suppress(ValueError):
                message = response.json().get("message", "")
            raise SourceError(
                f"Wise Old Man returned HTTP {response.status_code}. {message}".strip()
            )
