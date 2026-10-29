"""get_item_price and get_price_history, from the OSRS Wiki real-time prices API."""

import math
import time
from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.prices import (
    ItemInfo,
    LatestPrice,
    PricePoint,
    Timestep,
    search,
    to_datetime,
)
from osrs_mcp.errors import SourceError
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY

PRICES_SOURCE = (
    "OSRS Wiki real-time Grand Exchange prices (from RuneLite trade data; cached up to 60 s)"
)
HISTORY_SOURCE = "OSRS Wiki real-time Grand Exchange prices (from RuneLite trade data)"

PricePeriod = Literal["day", "week", "month", "year"]
# The timestep that covers each period, and the period's length in seconds.
PERIODS: dict[PricePeriod, tuple[Timestep, int]] = {
    "day": ("5m", 24 * 3600),
    "week": ("1h", 7 * 24 * 3600),
    "month": ("6h", 30 * 24 * 3600),
    "year": ("24h", 365 * 24 * 3600),
}
# Enough for a chart; longer periods are averaged down to this many points.
MAX_POINTS = 120


class ItemPrice(BaseModel):
    name: str
    id: int
    members: bool
    instant_buy: int | None = Field(description="Latest high (insta-buy) price, gp")
    instant_buy_at: datetime | None
    instant_sell: int | None = Field(description="Latest low (insta-sell) price, gp")
    instant_sell_at: datetime | None
    buy_limit: int | None = Field(description="GE buy limit per 4 hours")
    high_alch: int | None


class ItemPrices(BaseModel):
    query: str
    source: str
    matches: list[ItemPrice]


class PriceHistoryPoint(BaseModel):
    at: datetime = Field(description="End of the interval")
    price: int | None = Field(description="Average traded price (mid of buy and sell), gp")
    volume: int = Field(description="Items traded in the interval")


class PriceHistory(BaseModel):
    name: str
    id: int
    period: PricePeriod
    source: str
    start_price: int | None
    end_price: int | None
    lowest: int | None
    highest: int | None
    change_percent: float | None = Field(description="From start_price to end_price")
    total_volume: int
    points: list[PriceHistoryPoint] = Field(description="Oldest first")


def _mid(point: PricePoint) -> float | None:
    prices = [p for p in (point.avgHighPrice, point.avgLowPrice) if p is not None]
    return sum(prices) / len(prices) if prices else None


def build_price_history(
    item: ItemInfo, period: PricePeriod, series: list[PricePoint], now: float
) -> PriceHistory:
    _timestep, length = PERIODS[period]
    recent = [p for p in series if p.timestamp >= now - length]
    size = max(1, math.ceil(len(recent) / MAX_POINTS))
    points = []
    for i in range(0, len(recent), size):
        chunk = recent[i : i + size]
        mids = [m for m in map(_mid, chunk) if m is not None]
        points.append(
            PriceHistoryPoint(
                at=to_datetime(chunk[-1].timestamp),
                price=round(sum(mids) / len(mids)) if mids else None,
                volume=sum(p.highPriceVolume + p.lowPriceVolume for p in chunk),
            )
        )
    prices = [p.price for p in points if p.price is not None]
    start, end = (prices[0], prices[-1]) if prices else (None, None)
    return PriceHistory(
        name=item.name,
        id=item.id,
        period=period,
        source=HISTORY_SOURCE,
        start_price=start,
        end_price=end,
        lowest=min(prices, default=None),
        highest=max(prices, default=None),
        change_percent=round((end - start) / start * 100, 1) if start else None,
        total_volume=sum(p.volume for p in points),
        points=points,
    )


async def price_history(sources: Sources, query: str, period: PricePeriod) -> PriceHistory:
    """The best-matching item's price history; shared with the public endpoint."""
    matches = search(await sources.prices.mapping(), query, 1)
    if not matches:
        raise SourceError(
            f"No tradeable item matches '{query}'. Check the spelling, or use the item's full "
            "in-game name."
        )
    series = await sources.prices.timeseries(matches[0].id, PERIODS[period][0])
    return build_price_history(matches[0], period, series, time.time())


def build_prices(
    query: str, items: list[ItemInfo], latest: dict[int, LatestPrice], limit: int
) -> ItemPrices:
    matches = search(items, query, limit)
    if not matches:
        raise SourceError(
            f"No tradeable item matches '{query}'. Check the spelling, or use the item's full "
            "in-game name."
        )
    rows = []
    for item in matches:
        price = latest.get(item.id, LatestPrice())
        rows.append(
            ItemPrice(
                name=item.name,
                id=item.id,
                members=item.members,
                instant_buy=price.high,
                instant_buy_at=to_datetime(price.highTime),
                instant_sell=price.low,
                instant_sell_at=to_datetime(price.lowTime),
                buy_limit=item.limit,
                high_alch=item.highalch,
            )
        )
    return ItemPrices(query=query, source=PRICES_SOURCE, matches=rows)


def register(mcp: FastMCP, sources: Sources) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_item_price(
        query: Annotated[str, Field(description="Item name; partial names and typos are OK")],
        limit: Annotated[int, Field(ge=1, le=20, description="Max matching items")] = 5,
    ) -> ItemPrices:
        """Current Grand Exchange prices for an item: latest instant-buy and instant-sell
        prices with when they last traded, the GE buy limit, and high alchemy value. Matches
        exact names first, then partial names, then close spellings."""
        items = await sources.prices.mapping()
        latest = await sources.prices.latest()
        return build_prices(query, items, latest, limit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_price_history(
        query: Annotated[str, Field(description="Item name; partial names and typos are OK")],
        period: Annotated[PricePeriod, Field(description="How far back to look")] = "month",
    ) -> PriceHistory:
        """Grand Exchange price history for the best-matching item over the last day, week,
        month or year: start and end price, change, low and high, trade volume, and up to 120
        points for a chart. Use get_item_price for the current price."""
        return await price_history(sources, query, period)
