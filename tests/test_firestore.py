"""The Firestore storage backend against the real Firestore emulator.

Skipped unless FIRESTORE_EMULATOR_HOST is set. To run:

    gcloud emulators firestore start --host-port=127.0.0.1:8088
    FIRESTORE_EMULATOR_HOST=127.0.0.1:8088 uv run pytest tests/test_firestore.py
"""

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from osrs_mcp.config import Settings
from osrs_mcp.ingest.gim import GimUpdate, apply_update
from osrs_mcp.snapshots import SnapshotStore
from osrs_mcp.store import make_store
from tests.conftest import fixture_json

pytestmark = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_EMULATOR_HOST"), reason="needs the Firestore emulator"
)


def firestore_store():
    # A fresh project id per test keeps runs independent within one emulator.
    settings = Settings(
        _env_file=None,
        store_backend="firestore",
        firestore_project=f"test-{uuid.uuid4().hex[:12]}",
    )
    return make_store(settings)


async def test_snapshots_round_trip_with_slashes_in_keys():
    snapshots = SnapshotStore(firestore_store())
    update = GimUpdate.model_validate(fixture_json("gim_update_full.json")["body"])
    await apply_update(snapshots, update)

    # Keys are "<rsn>/<kind>" and the rsn has a non-breaking space: both need sanitising.
    bank = await snapshots.get("Iron" + chr(0xA0) + "Tester", "bank")
    assert bank is not None and bank.data == update.bank
    assert (await snapshots.get("iron tester", "inventory")).data == update.inventory
    assert await snapshots.get("someone else", "bank") is None


async def test_encrypted_values_round_trip():
    # How the OAuth proxy stores client registrations and upstream tokens.
    store = FernetEncryptionWrapper(firestore_store(), fernet=Fernet(Fernet.generate_key()))
    value = {"client_id": "abc", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}
    await store.put("https://claude.ai/oauth/claude-code-client-metadata", value, collection="c")
    got = await store.get("https://claude.ai/oauth/claude-code-client-metadata", collection="c")
    assert got == value
