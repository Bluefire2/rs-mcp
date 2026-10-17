"""Endpoints compatible with the Group Ironmen Tracker RuneLite plugin.

The plugin (github.com/christoabrown/group-ironmen-tracker, on the Plugin Hub) has a "Server base
URL override" setting, so pointing it at this server makes the player's own client push bank,
inventory and gear here. It speaks this protocol (from DataManager.java / HttpRequestService.java):

- ``GET {base}/api/group/{group}/am-i-in-group?member_name=<rsn>``: any 2xx means "you are a
  member, start sending"; anything else and it backs off for ~10 attempts.
- ``POST {base}/api/group/{group}/update-group-member`` about once a second, with a JSON object
  holding ``name`` plus only the fields that changed since the last successful POST.
- Both send ``Authorization: <token>`` raw (no "Bearer"). A 401 on POST makes it re-check
  membership.

These routes are registered with ``custom_route``, which FastMCP does not wrap in its OAuth
``RequireAuthMiddleware`` (only the MCP endpoint is). The ingest token protects them instead.

There is no group here, just an allowlist of RuneScape names that may push data. Ingest is off
(every request refused) unless both GIM_GROUP and INGEST_TOKEN are configured.
"""

import hmac
import logging
from typing import Any

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from osrs_mcp.config import Settings
from osrs_mcp.game import normalize_rsn
from osrs_mcp.snapshots import SnapshotStore, pairs

logger = logging.getLogger(__name__)

# A full bank is at most a few thousand [id, qty] pairs (~60 KB of JSON); the collection log
# a few thousand more. 2 MB leaves ample room while bounding what an anonymous caller can make
# us buffer.
MAX_BODY_BYTES = 2 * 1024 * 1024

# Fields whose value is a flat [item_id, quantity, item_id, quantity, ...] list.
ITEM_PAIR_FIELDS = (
    "inventory",
    "equipment",
    "bank",
    "shared_bank",
    "rune_pouch",
    "quiver",
    "seed_vault",
    "potion_storage",
    "deposited",
    "collection_log_v2",
)
# Stored as-is, one document each. Item containers keep the plugin's flat list; skills, quests
# and diary_vars are positional int arrays (see the fixtures for their layouts).
REPLACE_KINDS = (
    "inventory",
    "equipment",
    "bank",
    "shared_bank",
    "rune_pouch",
    "quiver",
    "seed_vault",
    "potion_storage",
    "skills",
    "quests",
    "diary_vars",
)


class GimUpdate(BaseModel):
    """One update-group-member body. Every field but ``name`` is optional because the plugin
    only sends what changed. Unknown fields are kept aside (and logged), not rejected, so a
    plugin update that adds a field doesn't break ingest."""

    model_config = ConfigDict(extra="allow")

    name: str
    inventory: list[int] | None = None
    equipment: list[int] | None = None
    bank: list[int] | None = None
    shared_bank: list[int] | None = None
    rune_pouch: list[int] | None = None
    quiver: list[int] | None = None
    seed_vault: list[int] | None = None
    potion_storage: list[int] | None = None
    deposited: list[int] | None = None
    collection_log_v2: list[int] | None = None
    skills: list[int] | None = None
    quests: list[int] | None = None
    diary_vars: list[int] | None = None
    # Sent every second while playing (HP/prayer/energy/world, position, current target). They
    # are accepted but not stored: nothing reads them, and they would dominate store writes.
    stats: list[int] | None = None
    coordinates: list[int] | None = None
    interacting: dict[str, Any] | None = None

    @field_validator(*ITEM_PAIR_FIELDS)
    @classmethod
    def _item_pairs(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and len(value) % 2:
            raise ValueError("item list must be [id, quantity, ...] pairs")
        return value


_logged_unknown_fields: set[str] = set()


def _log_unknown_fields(update: GimUpdate) -> None:
    new = set(update.model_extra or {}) - _logged_unknown_fields
    if new:
        _logged_unknown_fields.update(new)
        logger.info("Ignoring unknown Group Ironmen Tracker fields: %s", ", ".join(sorted(new)))


def add_items(base: list[int], extra: list[int]) -> list[int]:
    """Add quantities from ``extra`` into ``base`` (both flat pair lists), keeping base order."""
    totals: dict[int, int] = {}
    for item_id, qty in pairs(base) + pairs(extra):
        totals[item_id] = totals.get(item_id, 0) + qty
    return [v for item_id, qty in totals.items() if item_id > 0 and qty > 0 for v in (item_id, qty)]


async def apply_update(snapshots: SnapshotStore, update: GimUpdate) -> list[str]:
    """Merge one partial update into the stored snapshots; returns the kinds written."""
    rsn = update.name
    written = []
    for kind in REPLACE_KINDS:
        value = getattr(update, kind)
        if value is not None:
            await snapshots.put(rsn, kind, value)
            written.append(kind)

    # Items put in a bank deposit box while the bank is closed. The plugin resets this when the
    # bank is next opened and sends the real bank, so adding them to the stored bank keeps it
    # right in between (the official server does the same).
    if update.deposited and update.bank is None:
        bank = await snapshots.get(rsn, "bank")
        if bank is not None:
            await snapshots.put(rsn, "bank", add_items(bank.data, update.deposited))
            written.append("bank")

    # The plugin sends only the collection log items it saw since the last successful POST, as
    # [id, qty] pairs, so merge them into what we already have.
    if update.collection_log_v2:
        existing = await snapshots.get(rsn, "collection_log")
        items: dict[str, int] = dict(existing.data) if existing else {}
        items.update({str(item_id): qty for item_id, qty in pairs(update.collection_log_v2)})
        await snapshots.put(rsn, "collection_log", items)
        written.append("collection_log")
    return written


def register(mcp: FastMCP, settings: Settings, snapshots: SnapshotStore) -> None:
    def check_access(request: Request) -> Response | None:
        """Shared checks for both endpoints, before any body is read. None means allowed."""
        if not settings.ingest_enabled:
            return PlainTextResponse("Ingest is not configured on this server", status_code=404)
        token = request.headers.get("authorization", "").strip()
        expected = settings.ingest_token.strip()
        if not hmac.compare_digest(token.encode(), expected.encode()):
            logger.warning("Rejected ingest request: wrong or missing token")
            return PlainTextResponse("Unauthorized", status_code=401)
        group = request.path_params["group"].strip()
        if group.casefold() != settings.gim_group.strip().casefold():
            logger.warning("Rejected ingest request for group %r: not GIM_GROUP", group)
            return PlainTextResponse("Unauthorized", status_code=401)
        return None

    def member_allowed(name: str) -> bool:
        if normalize_rsn(name) in settings.ingest_rsns:
            return True
        logger.warning("Rejected ingest request for player %r: not in INGEST_ALLOWED_RSNS", name)
        return False

    @mcp.custom_route("/api/group/{group}/am-i-in-group", methods=["GET"], include_in_schema=False)
    async def am_i_in_group(request: Request) -> Response:
        if (denied := check_access(request)) is not None:
            return denied
        if not member_allowed(request.query_params.get("member_name", "")):
            return PlainTextResponse("Player is not a member of this group", status_code=401)
        return JSONResponse({})

    @mcp.custom_route(
        "/api/group/{group}/update-group-member", methods=["POST"], include_in_schema=False
    )
    async def update_group_member(request: Request) -> Response:
        if (denied := check_access(request)) is not None:
            return denied
        body = await read_limited(request, MAX_BODY_BYTES)
        if body is None:
            return PlainTextResponse("Request body too large", status_code=413)
        try:
            update = GimUpdate.model_validate_json(body)
        except ValidationError as exc:
            logger.warning("Rejected malformed ingest update: %s", exc.errors(include_input=False))
            return PlainTextResponse("Malformed update", status_code=400)
        if not member_allowed(update.name):
            return PlainTextResponse("Player is not a member of this group", status_code=401)
        _log_unknown_fields(update)
        written = await apply_update(snapshots, update)
        if written:
            logger.info("Stored %s for %s", ", ".join(written), update.name)
        return JSONResponse({})


async def read_limited(request: Request, limit: int) -> bytes | None:
    """The request body, or None if it is larger than ``limit`` bytes. Checks Content-Length
    first, then counts while streaming (the header can be absent or wrong)."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)
