"""get_bank and get_equipment_and_inventory, from snapshots pushed by the Group Ironmen Tracker
RuneLite plugin (see osrs_mcp.ingest.gim), named and valued with the OSRS Wiki prices API."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.prices import ItemInfo, LatestPrice
from osrs_mcp.errors import SourceError
from osrs_mcp.reference import item_names
from osrs_mcp.snapshots import Snapshot, SnapshotStore, pairs
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY, Rsn

SNAPSHOT_SOURCE = (
    "Pushed from the player's RuneLite client by the Group Ironmen Tracker plugin; current as "
    "of updated_at."
)
BANK_SOURCE = SNAPSHOT_SOURCE + " The bank only refreshes when the bank is opened in game."
PRICE_NOTE = (
    "unit_price is the midpoint of the latest GE instant-buy and instant-sell prices (OSRS "
    "Wiki, cached up to 60 s). Untradeable items have no price and count as 0 in total_value."
)

# Not on the GE, so absent from the prices API, but with a fixed gp value.
FIXED_VALUE_ITEMS = {995: ("Coins", 1), 13204: ("Platinum token", 1000)}

# RuneLite EquipmentInventorySlot indices. The plugin sends all 14 slots in order; 6, 8 and 11
# (arms, hair, jaw) are body-model slots that never hold an item.
EQUIPMENT_SLOTS = {
    0: "head",
    1: "cape",
    2: "amulet",
    3: "weapon",
    4: "body",
    5: "shield",
    7: "legs",
    9: "gloves",
    10: "boots",
    12: "ring",
    13: "ammo",
}


class Items:
    """Item names and GE prices by item id. The plugin canonicalises ids (noted items arrive
    as the unnoted id), so tradeables match the prices mapping; untradeables are named from
    the Wiki's item list (reference/data/item_names.json) and have no price."""

    def __init__(self, mapping: list[ItemInfo], latest: dict[int, LatestPrice]) -> None:
        self._names = {item.id: item.name for item in mapping}
        self._latest = latest

    def name(self, item_id: int) -> str:
        if item_id in FIXED_VALUE_ITEMS:
            return FIXED_VALUE_ITEMS[item_id][0]
        return self._names.get(item_id) or item_names().get(item_id) or f"unknown item {item_id}"

    def unit_price(self, item_id: int) -> int | None:
        if item_id in FIXED_VALUE_ITEMS:
            return FIXED_VALUE_ITEMS[item_id][1]
        price = self._latest.get(item_id)
        if price is None:
            return None
        quotes = [p for p in (price.high, price.low) if p]
        return sum(quotes) // len(quotes) if quotes else None


class BankItem(BaseModel):
    name: str
    id: int
    quantity: int
    unit_price: int | None = Field(description="GE price each, gp; null if untradeable")
    value: int | None = Field(description="quantity x unit_price, gp; null if untradeable")


class Bank(BaseModel):
    rsn: str
    source: str
    updated_at: datetime
    price_note: str
    total_value: int = Field(description="Whole bank's GE value, gp (untradeables count as 0)")
    item_count: int = Field(description="Distinct items in the whole bank")
    matching_count: int = Field(description="Distinct items matching search/min_value")
    items: list[BankItem]


class Slot(BaseModel):
    name: str
    id: int
    quantity: int


class InventorySlot(Slot):
    slot: int = Field(description="Inventory slot, 0-27, left to right then top to bottom")


class Section[T](BaseModel):
    updated_at: datetime | None = Field(description="null if the plugin never sent this")
    age_minutes: int | None = Field(
        description="Minutes since updated_at. The plugin only sends while the player is logged "
        "in, so a large age means a past session, not what they're carrying now"
    )
    items: T


class EquipmentAndInventory(BaseModel):
    rsn: str
    source: str
    equipment: Section[dict[str, Slot]]
    inventory: Section[list[InventorySlot]]
    free_inventory_slots: int | None
    rune_pouch: Section[list[Slot]]
    quiver: Section[list[Slot]]


def no_snapshot_error(sources: Sources, rsn: str, what: str) -> SourceError:
    settings = sources.settings
    if not settings.ingest_enabled:
        return SourceError(
            f"No {what} data: RuneLite ingest isn't configured on this server. The owner needs "
            "to set GIM_GROUP and INGEST_TOKEN, then set up the Group Ironmen Tracker plugin."
        )
    return SourceError(
        f"No {what} data for {rsn} yet. In RuneLite, install the Group Ironmen Tracker plugin "
        f"and set: Server base URL override = {settings.public_base_url}, Group Name = "
        f"{settings.gim_group}, Group Token = the server's INGEST_TOKEN. Then log in as {rsn} "
        "and open the bank once."
    )


def build_bank(
    rsn: str,
    snapshot: Snapshot,
    items: Items,
    search: str | None,
    min_value: int | None,
    sort: str,
    limit: int,
) -> Bank:
    rows = []
    for item_id, qty in pairs(snapshot.data):
        if item_id <= 0 or qty <= 0:  # empty slots / placeholders
            continue
        unit = items.unit_price(item_id)
        rows.append(
            BankItem(
                name=items.name(item_id),
                id=item_id,
                quantity=qty,
                unit_price=unit,
                value=unit * qty if unit is not None else None,
            )
        )
    total = sum(row.value or 0 for row in rows)

    matching = rows
    if search:
        needle = search.strip().lower()
        matching = [row for row in matching if needle in row.name.lower()]
    if min_value is not None:
        matching = [row for row in matching if row.value is not None and row.value >= min_value]
    match sort:
        case "quantity":
            matching.sort(key=lambda row: -row.quantity)
        case "name":
            matching.sort(key=lambda row: row.name.lower())
        case _:
            matching.sort(key=lambda row: (row.value is None, -(row.value or 0)))

    return Bank(
        rsn=snapshot.rsn or rsn,
        source=BANK_SOURCE,
        updated_at=snapshot.updated_at,
        price_note=PRICE_NOTE,
        total_value=total,
        item_count=len(rows),
        matching_count=len(matching),
        items=matching[:limit],
    )


def slots(flat: list[int], items: Items) -> list[tuple[int, Slot]]:
    """(position, item) for each non-empty position of a positional container."""
    return [
        (index, Slot(name=items.name(item_id), id=item_id, quantity=qty))
        for index, (item_id, qty) in enumerate(pairs(flat))
        if item_id > 0 and qty > 0
    ]


def build_equipment_and_inventory(
    rsn: str,
    equipment: Snapshot | None,
    inventory: Snapshot | None,
    rune_pouch: Snapshot | None,
    quiver: Snapshot | None,
    items: Items,
    now: datetime | None = None,
) -> EquipmentAndInventory:
    now = now or datetime.now(UTC)

    def section(snapshot: Snapshot | None) -> tuple[datetime | None, list[tuple[int, Slot]]]:
        if snapshot is None:
            return None, []
        return snapshot.updated_at, slots(snapshot.data, items)

    def age(at: datetime | None) -> int | None:
        return None if at is None else max(int((now - at).total_seconds() // 60), 0)

    eq_at, eq = section(equipment)
    inv_at, inv = section(inventory)
    pouch_at, pouch = section(rune_pouch)
    quiver_at, ammo = section(quiver)
    known = next((s.rsn for s in (equipment, inventory) if s is not None), rsn)
    return EquipmentAndInventory(
        rsn=known,
        source=SNAPSHOT_SOURCE,
        equipment=Section(
            updated_at=eq_at,
            age_minutes=age(eq_at),
            items={EQUIPMENT_SLOTS.get(i, f"slot {i}"): slot for i, slot in eq},
        ),
        inventory=Section(
            updated_at=inv_at,
            age_minutes=age(inv_at),
            items=[InventorySlot(slot=i, **slot.model_dump()) for i, slot in inv],
        ),
        free_inventory_slots=28 - len(inv) if inventory is not None else None,
        rune_pouch=Section(
            updated_at=pouch_at, age_minutes=age(pouch_at), items=[slot for _, slot in pouch]
        ),
        quiver=Section(
            updated_at=quiver_at, age_minutes=age(quiver_at), items=[slot for _, slot in ammo]
        ),
    )


def register(mcp: FastMCP, sources: Sources, snapshots: SnapshotStore) -> None:
    async def items() -> Items:
        return Items(await sources.prices.mapping(), await sources.prices.latest())

    @mcp.tool(annotations=READ_ONLY)
    async def get_bank(
        search: Annotated[
            str | None, Field(description="Only items whose name contains this text")
        ] = None,
        min_value: Annotated[
            int | None, Field(ge=0, description="Only stacks worth at least this many gp")
        ] = None,
        sort: Literal["value", "quantity", "name"] = "value",
        limit: Annotated[int, Field(ge=1, le=500, description="Max items to list")] = 50,
        rsn: Rsn = None,
    ) -> Bank:
        """The player's bank as last seen by RuneLite: items with quantities and GE values,
        the whole bank's total value, and when the snapshot was taken (updated_at). The bank
        only refreshes when the player opens it in game, so mention updated_at when it is old.
        Untradeable items are listed with no price."""
        name = sources.rsn(rsn)
        snapshot = await snapshots.get(name, "bank")
        if snapshot is None:
            raise no_snapshot_error(sources, name, "bank")
        return build_bank(name, snapshot, await items(), search, min_value, sort, limit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_equipment_and_inventory(rsn: Rsn = None) -> EquipmentAndInventory:
        """What the player is wearing (by slot), carrying in the inventory (by slot), and has in
        the rune pouch and Dizana's quiver, as last reported by RuneLite. Each section has its
        own updated_at and age_minutes; the plugin sends changes within seconds while the player
        is logged in, so an age of hours means a past session, not a trip in progress."""
        name = sources.rsn(rsn)
        equipment = await snapshots.get(name, "equipment")
        inventory = await snapshots.get(name, "inventory")
        if equipment is None and inventory is None:
            raise no_snapshot_error(sources, name, "equipment or inventory")
        return build_equipment_and_inventory(
            name,
            equipment=equipment,
            inventory=inventory,
            rune_pouch=await snapshots.get(name, "rune_pouch"),
            quiver=await snapshots.get(name, "quiver"),
            items=await items(),
        )
