"""The Group Ironmen Tracker ingest endpoints: who may push, size limit, partial-update merging,
and that the routes sit outside the OAuth gate. Request bodies come from tests/fixtures/gim_*.json,
which are derived from the plugin's source (see their _note), not captured live."""

import json
import logging

import httpx
import pytest
from key_value.aio.stores.memory import MemoryStore
from starlette.testclient import TestClient

from osrs_mcp.config import Settings
from osrs_mcp.ingest import gim
from osrs_mcp.server import MCP_PATH, create_app
from osrs_mcp.snapshots import SnapshotStore
from tests.conftest import fixture_json
from tests.test_auth import INIT, MCP_HEADERS, auth_settings

BASE = "http://localhost:8000"
TOKEN = "test-ingest-token-0123456789abcdefghijklmnop"
GROUP = "My Group"
MEMBER = "/api/group/My%20Group/am-i-in-group"
UPDATE = "/api/group/My%20Group/update-group-member"
AUTH = {"authorization": TOKEN}


def body(name: str) -> dict:
    return fixture_json(f"gim_update_{name}.json")["body"]


def ingest_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "auth_mode": "none",
        "store_backend": "memory",
        "default_rsn": "Iron Tester",
        "gim_group": GROUP,
        "ingest_token": TOKEN,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def store():
    return MemoryStore()


def make_client(store, **overrides) -> httpx.AsyncClient:
    app = create_app(ingest_settings(**overrides), store=store).http_app(path=MCP_PATH)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@pytest.fixture
async def client(store):
    async with make_client(store) as c:
        yield c


# --- am-i-in-group ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member_name",
    ["Iron Tester", "iron_tester", "IRON\u00a0TESTER", "Iron  Tester "],
)
async def test_member_check_accepts_owner_name_variants(client, member_name):
    response = await client.get(MEMBER, params={"member_name": member_name}, headers=AUTH)
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("path", "params", "headers"),
    [
        (MEMBER, {"member_name": "Iron Tester"}, {}),  # no token
        (MEMBER, {"member_name": "Iron Tester"}, {"authorization": "wrong"}),
        (MEMBER, {"member_name": "Iron Tester"}, {"authorization": f"Bearer {TOKEN}"}),
        ("/api/group/other/am-i-in-group", {"member_name": "Iron Tester"}, AUTH),
        (MEMBER, {"member_name": "Someone Else"}, AUTH),
        (MEMBER, {}, AUTH),
    ],
)
async def test_member_check_rejections(client, path, params, headers):
    response = await client.get(path, params=params, headers=headers)
    assert response.status_code == 401


async def test_group_name_is_case_insensitive(client):
    path = "/api/group/my%20group/am-i-in-group"
    response = await client.get(path, params={"member_name": "Iron Tester"}, headers=AUTH)
    assert response.status_code == 200


async def test_allowlist_overrides_default_rsn(store):
    async with make_client(store, ingest_allowed_rsns="Main Acct, Alt_Acct") as c:
        for name, expected in [("alt acct", 200), ("Main Acct", 200), ("Iron Tester", 401)]:
            response = await c.get(MEMBER, params={"member_name": name}, headers=AUTH)
            assert response.status_code == expected, name


@pytest.mark.parametrize(
    "overrides",
    [{"ingest_token": ""}, {"gim_group": ""}, {"ingest_token": "  ", "gim_group": "  "}],
)
async def test_ingest_disabled_without_config(store, overrides):
    async with make_client(store, **overrides) as c:
        member = await c.get(MEMBER, params={"member_name": "Iron Tester"}, headers=AUTH)
        # An empty Authorization header must not match an empty token.
        update = await c.post(UPDATE, json=body("full"), headers={"authorization": ""})
    assert member.status_code == 404
    assert update.status_code == 404
    assert await SnapshotStore(store).get("Iron Tester", "bank") is None


async def test_no_allowed_names_rejects_everyone(store):
    async with make_client(store, default_rsn="") as c:
        response = await c.get(MEMBER, params={"member_name": ""}, headers=AUTH)
    assert response.status_code == 401


# --- update-group-member ----------------------------------------------------------------------


async def test_update_stores_each_kind(client, store):
    response = await client.post(UPDATE, json=body("full"), headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {}

    snapshots = SnapshotStore(store)
    bank = await snapshots.get("iron tester", "bank")
    assert bank.rsn == "Iron\u00a0Tester"
    assert bank.data[:2] == [995, 1234567]
    assert bank.updated_at.tzinfo is not None
    for kind in ("inventory", "equipment", "rune_pouch", "quiver", "seed_vault", "skills"):
        assert await snapshots.get("Iron Tester", kind) is not None, kind
    assert len((await snapshots.get("Iron Tester", "equipment")).data) == 28
    assert (await snapshots.get("Iron Tester", "collection_log")).data == {"4151": 1, "11832": 2}
    # Per-second fields are accepted but not stored.
    for kind in ("stats", "coordinates", "interacting"):
        assert await snapshots.get("Iron Tester", kind) is None, kind


async def test_partial_updates_merge(client, store):
    await client.post(UPDATE, json=body("full"), headers=AUTH)
    snapshots = SnapshotStore(store)
    bank_before = await snapshots.get("Iron Tester", "bank")

    response = await client.post(UPDATE, json=body("inventory"), headers=AUTH)
    assert response.status_code == 200

    inventory = await snapshots.get("Iron Tester", "inventory")
    assert inventory.data == body("inventory")["inventory"]
    bank_after = await snapshots.get("Iron Tester", "bank")
    assert bank_after == bank_before  # untouched by an inventory-only update
    assert inventory.updated_at >= bank_after.updated_at


async def test_inventory_then_bank_keeps_both(client, store):
    await client.post(UPDATE, json=body("inventory"), headers=AUTH)
    await client.post(UPDATE, json={"name": "Iron Tester", "bank": [4151, 2]}, headers=AUTH)
    snapshots = SnapshotStore(store)
    assert (await snapshots.get("Iron Tester", "inventory")).data == body("inventory")["inventory"]
    assert (await snapshots.get("Iron Tester", "bank")).data == [4151, 2]


async def test_deposited_items_are_added_to_bank(client, store):
    await client.post(UPDATE, json=body("full"), headers=AUTH)
    await client.post(UPDATE, json=body("deposited"), headers=AUTH)
    bank = dict(gim.pairs((await SnapshotStore(store).get("Iron Tester", "bank")).data))
    assert bank[1513] == 300 + 25
    assert bank[995] == 1234567


async def test_collection_log_items_accumulate(client, store):
    await client.post(UPDATE, json=body("full"), headers=AUTH)
    update = {"name": "Iron Tester", "collection_log_v2": [11832, 3, 20997, 1]}
    await client.post(UPDATE, json=update, headers=AUTH)
    clog = await SnapshotStore(store).get("Iron Tester", "collection_log")
    assert clog.data == {"4151": 1, "11832": 3, "20997": 1}


async def test_update_rejects_other_players_and_bad_auth(client, store):
    other = body("full") | {"name": "Someone Else"}
    assert (await client.post(UPDATE, json=other, headers=AUTH)).status_code == 401
    wrong = {"authorization": "nope"}
    assert (await client.post(UPDATE, json=body("full"), headers=wrong)).status_code == 401
    other_group = "/api/group/other/update-group-member"
    assert (await client.post(other_group, json=body("full"), headers=AUTH)).status_code == 401
    assert await SnapshotStore(store).get("Someone Else", "bank") is None
    assert await SnapshotStore(store).get("Iron Tester", "bank") is None


async def test_unknown_fields_are_ignored_and_logged_once(client, store, caplog, monkeypatch):
    monkeypatch.setattr(gim, "_logged_unknown_fields", set())
    update = body("inventory") | {"sailing_boat": [1, 2, 3], "new_thing": {"a": 1}}
    with caplog.at_level(logging.INFO, logger="osrs_mcp.ingest.gim"):
        assert (await client.post(UPDATE, json=update, headers=AUTH)).status_code == 200
        assert (await client.post(UPDATE, json=update, headers=AUTH)).status_code == 200
    unknown = [r.message for r in caplog.records if "unknown" in r.message]
    assert unknown == ["Ignoring unknown Group Ironmen Tracker fields: new_thing, sailing_boat"]
    assert await SnapshotStore(store).get("Iron Tester", "inventory") is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"bank": [1, 2, 3]},  # not [id, qty] pairs
        {"name": "Iron Tester", "bank": [1, 2, 3]},
        {"name": "Iron Tester", "inventory": "nope"},
    ],
)
async def test_malformed_updates_are_rejected(client, store, payload):
    response = await client.post(UPDATE, json=payload, headers=AUTH)
    assert response.status_code == 400
    assert await SnapshotStore(store).get("Iron Tester", "bank") is None


async def test_invalid_json_is_rejected(client):
    response = await client.post(UPDATE, content=b"{not json", headers=AUTH)
    assert response.status_code == 400


async def test_oversized_body_is_rejected_before_parsing(client, store):
    huge = json.dumps({"name": "Iron Tester", "bank": [4151, 1] * 300_000}).encode()
    assert len(huge) > gim.MAX_BODY_BYTES
    response = await client.post(UPDATE, content=huge, headers=AUTH)
    assert response.status_code == 413
    assert await SnapshotStore(store).get("Iron Tester", "bank") is None


async def test_oversized_streamed_body_without_length_is_rejected(client):
    async def chunks():
        for _ in range(3):
            yield b" " * (gim.MAX_BODY_BYTES // 2 + 1)

    response = await client.post(UPDATE, content=chunks(), headers=AUTH)
    assert response.status_code == 413


# --- outside the OAuth gate -------------------------------------------------------------------


def test_ingest_routes_bypass_oauth_but_mcp_does_not():
    settings = auth_settings(default_rsn="Iron Tester", gim_group=GROUP, ingest_token=TOKEN)
    app = create_app(settings).http_app(path=MCP_PATH)
    with TestClient(app, base_url=BASE) as client:
        member = client.get(MEMBER, params={"member_name": "Iron Tester"}, headers=AUTH)
        assert member.status_code == 200
        update = client.post(UPDATE, json=body("inventory"), headers=AUTH)
        assert update.status_code == 200
        # The ingest token is not an OAuth token: the MCP endpoint still demands sign-in.
        mcp = client.post(MCP_PATH, json=INIT, headers=MCP_HEADERS | AUTH)
        assert mcp.status_code == 401
        # And OAuth being on doesn't loosen the ingest checks.
        assert client.get(MEMBER, params={"member_name": "Iron Tester"}).status_code == 401


async def test_snapshots_round_trip_through_file_store(tmp_path):
    from osrs_mcp.store import make_store

    settings = ingest_settings(store_backend="file", store_dir=str(tmp_path))
    snapshots = SnapshotStore(make_store(settings))
    await snapshots.put("Iron\u00a0Tester", "bank", [4151, 1])
    stored = await snapshots.get("iron_tester", "bank")
    assert stored.data == [4151, 1]
    assert stored.rsn == "Iron\u00a0Tester"
