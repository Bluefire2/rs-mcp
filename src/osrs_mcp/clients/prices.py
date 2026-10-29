"""OSRS Wiki real-time Grand Exchange prices (prices.runescape.wiki)."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from rapidfuzz import fuzz, process, utils

from osrs_mcp.errors import SourceError
from osrs_mcp.http import HttpClient

BASE = "https://prices.runescape.wiki/api/v1/osrs"
MAPPING_TTL = 24 * 3600
LATEST_TTL = 60

# The API returns the latest 365 points at each step: about 30 hours at 5m, 15 days at 1h,
# 3 months at 6h and a year at 24h.
Timestep = Literal["5m", "1h", "6h", "24h"]
TIMESERIES_TTL: dict[Timestep, int] = {"5m": 300, "1h": 600, "6h": 1800, "24h": 3600}


class ItemInfo(BaseModel):
    id: int
    name: str
    members: bool
    limit: int | None = None
    highalch: int | None = None
    lowalch: int | None = None
    value: int | None = None
    examine: str | None = None


class LatestPrice(BaseModel):
    high: int | None = None
    highTime: int | None = None
    low: int | None = None
    lowTime: int | None = None


class PricePoint(BaseModel):
    """Average prices and volumes over one timestep; a side with no trades is null."""

    timestamp: int
    avgHighPrice: int | None = None
    avgLowPrice: int | None = None
    highPriceVolume: int = 0
    lowPriceVolume: int = 0


def to_datetime(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts, UTC) if ts else None


def search(items: list[ItemInfo], query: str, limit: int) -> list[ItemInfo]:
    """Exact (case-insensitive) match first, then substring matches (shortest names first),
    then fuzzy matches for typos."""
    q = query.strip().lower()
    exact = [i for i in items if i.name.lower() == q]
    if exact:
        return exact[:limit]
    substring = sorted((i for i in items if q in i.name.lower()), key=lambda i: len(i.name))
    if substring:
        return substring[:limit]
    by_name = {i.name: i for i in items}
    matches = process.extract(
        q,
        by_name.keys(),
        scorer=fuzz.WRatio,
        processor=utils.default_process,  # case-insensitive
        limit=limit,
        score_cutoff=70,
    )
    return [by_name[name] for name, _score, _ in matches]


class PricesClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def mapping(self) -> list[ItemInfo]:
        response = await self._http.get(f"{BASE}/mapping", ttl=MAPPING_TTL)
        if response.status_code != 200:
            raise SourceError(
                f"OSRS Wiki prices returned HTTP {response.status_code}; try again shortly."
            )
        return [ItemInfo.model_validate(item) for item in response.json()]

    async def latest(self) -> dict[int, LatestPrice]:
        """All items' latest prices in one call (cached 60 s), used for price lookups and for
        valuing a whole bank."""
        response = await self._http.get(f"{BASE}/latest", ttl=LATEST_TTL)
        if response.status_code != 200:
            raise SourceError(
                f"OSRS Wiki prices returned HTTP {response.status_code}; try again shortly."
            )
        return {
            int(item_id): LatestPrice.model_validate(price)
            for item_id, price in response.json()["data"].items()
        }

    async def timeseries(self, item_id: int, timestep: Timestep) -> list[PricePoint]:
        """The latest 365 points for one item at this timestep, oldest first."""
        response = await self._http.get(
            f"{BASE}/timeseries",
            params={"id": str(item_id), "timestep": timestep},
            ttl=TIMESERIES_TTL[timestep],
        )
        if response.status_code != 200:
            raise SourceError(
                f"OSRS Wiki prices returned HTTP {response.status_code}; try again shortly."
            )
        return [PricePoint.model_validate(point) for point in response.json()["data"]]
