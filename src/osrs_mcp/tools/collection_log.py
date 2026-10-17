"""get_collection_log: obtained items from WikiSync and from the RuneLite (Group Ironmen
Tracker) snapshot, laid out on the Wiki's collection log pages."""

from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from osrs_mcp.clients.wikisync import WikiSyncData
from osrs_mcp.errors import SourceError
from osrs_mcp.reference import collection_log
from osrs_mcp.snapshots import Snapshot, SnapshotStore
from osrs_mcp.sources import Sources
from osrs_mcp.tools import READ_ONLY, Rsn
from osrs_mcp.tools.quests import match_quests

TABS = ("Bosses", "Raids", "Clues", "Minigames", "Other")
TabName = Literal["Bosses", "Raids", "Clues", "Minigames", "Other"]

NOT_SYNCED = (
    "No collection log data for '{rsn}'. WikiSync doesn't upload it automatically: open the "
    "collection log in game and press the WikiSync button in its top right, then ask again."
)


class Count(BaseModel):
    obtained: int
    total: int


class PageSummary(BaseModel):
    page: str
    tab: str
    obtained: int
    total: int


class MissingItem(BaseModel):
    name: str
    pages: list[str]
    completion_pct: float | None = Field(
        description="Share of WikiSync collection log users who have it"
    )


class ItemStatus(BaseModel):
    name: str
    obtained: bool
    quantity: int | None = Field(None, exclude_if=lambda v: v is None)
    completion_pct: float | None


class PageDetail(BaseModel):
    page: str
    tab: str
    obtained: int
    total: int
    items: list[ItemStatus]


class CollectionLog(BaseModel):
    rsn: str
    source: str
    wikisync_as_of: datetime | None = Field(
        description="Latest WikiSync upload; None if WikiSync has no collection log"
    )
    runelite_as_of: datetime | None = Field(
        description="Latest collection log update from the RuneLite plugin; None if none"
    )
    note: str
    obtained: int = Field(description="Unique slots filled")
    total: int
    by_tab: dict[str, Count]
    pages: list[PageSummary] = Field(description="Every page in scope, in game order")
    most_common_missing: list[MissingItem] = Field(
        description="Missing items in scope that the most players have: often the easiest next"
    )
    page_details: list[PageDetail] = Field(
        [], description="Every item on the requested page(s)", exclude_if=lambda v: not v
    )


def build_collection_log(
    rsn: str,
    wikisync: WikiSyncData | None,
    runelite: Snapshot | None,
    tab: TabName | None,
    page: str | None,
    limit: int,
) -> CollectionLog:
    """WikiSync has the whole log as of the player's last button press; the RuneLite snapshot
    has quantities, but only for items the plugin has seen since it was set up. Use both."""
    synced = wikisync is not None and bool(wikisync.collection_log)
    quantities = {int(item_id): qty for item_id, qty in (runelite.data if runelite else {}).items()}
    if not synced and not quantities:
        raise SourceError(NOT_SYNCED.format(rsn=rsn))
    have = set(wikisync.collection_log or []) if synced else set()
    have |= set(quantities)
    pages = collection_log()

    wanted_pages: set[str] = set()
    if page:
        matched = match_quests([p.page for p in pages], page)
        if not matched:
            names = ", ".join(p.page for p in pages if tab is None or p.tab == tab)
            raise SourceError(f"No collection log page matches '{page}'. Pages: {names}.")
        wanted_pages = set(matched)

    tab_items: dict[str, set[int]] = {t: set() for t in TABS}
    summaries, details = [], []
    missing: dict[int, MissingItem] = {}
    for p in pages:
        tab_items.setdefault(p.tab, set()).update(item.id for item in p.items)
        if wanted_pages and p.page not in wanted_pages:
            continue
        if not wanted_pages and tab is not None and p.tab != tab:
            continue
        obtained = sum(1 for item in p.items if item.id in have)
        summaries.append(PageSummary(page=p.page, tab=p.tab, obtained=obtained, total=len(p.items)))
        for item in p.items:
            if item.id not in have:
                entry = missing.setdefault(
                    item.id, MissingItem(name=item.name, pages=[], completion_pct=item.completion)
                )
                entry.pages.append(p.page)
        if wanted_pages:
            items = [
                ItemStatus(
                    name=item.name,
                    obtained=item.id in have,
                    quantity=quantities.get(item.id),
                    completion_pct=item.completion,
                )
                for item in p.items
            ]
            details.append(
                PageDetail(
                    page=p.page, tab=p.tab, obtained=obtained, total=len(p.items), items=items
                )
            )
    # An item on two pages (a pet on its boss's page and on All Pets) fills one slot, so tabs
    # count unique items.
    by_tab = {t: Count(obtained=len(ids & have), total=len(ids)) for t, ids in tab_items.items()}

    sources = []
    if synced:
        sources.append("WikiSync (as of the player's last WikiSync button press)")
    if quantities:
        sources.append("the RuneLite plugin (only items it has seen since it was set up)")
    reference_total = len({item.id for p in pages for item in p.items})
    ranked = sorted(missing.values(), key=lambda m: -(m.completion_pct or 0))
    return CollectionLog(
        rsn=wikisync.username if wikisync else (runelite.rsn if runelite else rsn),
        source=" and ".join(sources) + "; pages and completion rates from the OSRS Wiki",
        wikisync_as_of=wikisync.timestamp if synced else None,
        runelite_as_of=runelite.updated_at if quantities else None,
        note=(
            "WikiSync's collection log is only as fresh as the player's last press of its "
            "WikiSync button. completion_pct is the share of WikiSync users with that item: a "
            "rough guide to how easy it is, not a drop rate."
        ),
        obtained=len(have),
        total=(wikisync.collection_log_item_count if wikisync else None) or reference_total,
        by_tab=by_tab,
        pages=summaries,
        most_common_missing=ranked[:limit],
        page_details=details,
    )


def register(mcp: FastMCP, sources: Sources, snapshots: SnapshotStore) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_collection_log(
        rsn: Rsn = None,
        tab: Annotated[TabName | None, Field(description="Only pages on this tab")] = None,
        page: Annotated[
            str | None,
            Field(description="A page such as 'Vorkath' or 'Barrows'; lists all its items"),
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=100, description="Max entries in most_common_missing")
        ] = 15,
    ) -> CollectionLog:
        """Collection log progress: slots filled overall and per tab and page, and the missing
        items most other players have (good "what's an easy log slot?" candidates). Pass a page
        to see every item on it. Data comes from WikiSync (after the player presses the WikiSync
        button in the in-game collection log) and from the RuneLite plugin's updates."""
        name = sources.rsn(rsn)
        runelite = await snapshots.get(name, "collection_log")
        try:
            wikisync = await sources.wikisync.fetch(name)
        except SourceError:
            if runelite is None:
                raise
            wikisync = None  # the RuneLite snapshot alone still answers
        return build_collection_log(name, wikisync, runelite, tab, page, limit)
