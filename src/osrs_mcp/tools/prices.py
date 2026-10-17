"""get_item_price, from the OSRS Wiki real-time prices API."""

from datetime import datetime
from typing import Annotated

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.prices import ItemInfo, LatestPrice, search, to_datetime
from osrs_mcp.errors import SourceError
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY

PRICES_SOURCE = (
    "OSRS Wiki real-time Grand Exchange prices (from RuneLite trade data; cached up to 60 s)"
)


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
