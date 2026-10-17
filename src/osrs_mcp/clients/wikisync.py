"""WikiSync: quest, diary, combat-achievement and level data uploaded by the WikiSync RuneLite
plugin. The plugin uploads when the player logs in and as things change in game."""

from datetime import datetime
from urllib.parse import quote

from pydantic import BaseModel

from osrs_mcp.errors import SourceError
from osrs_mcp.http import HttpClient

BASE = "https://sync.runescape.wiki/runelite/player"
TTL = 300

QUEST_STATES = {0: "not_started", 1: "in_progress", 2: "completed"}


class DiaryTier(BaseModel):
    complete: bool
    tasks: list[bool]


class WikiSyncData(BaseModel):
    username: str
    timestamp: datetime
    quests: dict[str, int]
    achievement_diaries: dict[str, dict[str, DiaryTier]]
    levels: dict[str, int]
    combat_achievements: list[int]


class WikiSyncClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def fetch(self, rsn: str) -> WikiSyncData:
        response = await self._http.get(f"{BASE}/{quote(rsn)}/STANDARD", ttl=TTL)
        if response.status_code == 400 and "NO_USER_DATA" in response.text:
            raise SourceError(
                f"WikiSync has no data for '{rsn}'. Enable the WikiSync plugin in RuneLite and "
                "log in once; it uploads quests, diaries and combat achievements automatically."
            )
        if response.status_code != 200:
            raise SourceError(f"WikiSync returned HTTP {response.status_code}; try again shortly.")
        return WikiSyncData.model_validate(response.json())
