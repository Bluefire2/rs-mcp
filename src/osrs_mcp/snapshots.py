"""Snapshots of a player's game state pushed from RuneLite, kept in the key-value store.

One document per (player, kind), so a partial update (the plugin only sends what changed)
replaces just the kinds it carries and leaves the rest as they were. Each document records when
it was last written, which the tools pass on so Claude can say how stale an answer is.
"""

from datetime import UTC, datetime
from typing import Any

from key_value.aio.protocols.key_value import AsyncKeyValue
from pydantic import BaseModel

from osrs_mcp.game import normalize_rsn

COLLECTION = "snapshots"


def pairs(flat: list[int]) -> list[tuple[int, int]]:
    """[id, qty, id, qty, ...] (the plugin's item container format) as (id, qty) tuples."""
    return list(zip(flat[::2], flat[1::2], strict=True))


class Snapshot(BaseModel):
    rsn: str  # the name as the plugin last reported it, for display
    kind: str
    updated_at: datetime
    data: Any


class SnapshotStore:
    def __init__(self, store: AsyncKeyValue) -> None:
        self._store = store

    @staticmethod
    def _key(rsn: str, kind: str) -> str:
        return f"{normalize_rsn(rsn)}/{kind}"

    async def get(self, rsn: str, kind: str) -> Snapshot | None:
        raw = await self._store.get(self._key(rsn, kind), collection=COLLECTION)
        return Snapshot.model_validate(raw) if raw is not None else None

    async def put(self, rsn: str, kind: str, data: Any, at: datetime | None = None) -> Snapshot:
        snapshot = Snapshot(rsn=rsn, kind=kind, updated_at=at or datetime.now(UTC), data=data)
        await self._store.put(
            self._key(rsn, kind), snapshot.model_dump(mode="json"), collection=COLLECTION
        )
        return snapshot
