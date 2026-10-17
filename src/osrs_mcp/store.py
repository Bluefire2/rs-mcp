"""Key-value storage: a file tree locally, Firestore on Cloud Run, memory in tests."""

from pathlib import Path

from key_value.aio.protocols.key_value import AsyncKeyValue

from osrs_mcp.config import Settings


def make_store(settings: Settings) -> AsyncKeyValue:
    match settings.store_backend:
        case "memory":
            from key_value.aio.stores.memory import MemoryStore

            return MemoryStore()
        case "file":
            from key_value.aio.stores.filetree import (
                FileTreeStore,
                FileTreeV1CollectionSanitizationStrategy,
                FileTreeV1KeySanitizationStrategy,
            )

            directory = Path(settings.store_dir)
            directory.mkdir(parents=True, exist_ok=True)
            return FileTreeStore(
                data_directory=directory,
                key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
                collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
                    directory
                ),
            )
        case "firestore":
            from key_value.aio.stores.firestore import (
                FirestoreStore,
                FirestoreV1CollectionSanitizationStrategy,
                FirestoreV1KeySanitizationStrategy,
            )

            # Credentials come from the environment: the service account on Cloud Run, or
            # FIRESTORE_EMULATOR_HOST locally. Keys like "zezima/bank" contain characters
            # Firestore document ids can't, hence the sanitization strategies.
            return FirestoreStore(
                project=settings.firestore_project or None,
                database=settings.firestore_database,
                key_sanitization_strategy=FirestoreV1KeySanitizationStrategy(),
                collection_sanitization_strategy=FirestoreV1CollectionSanitizationStrategy(),
            )
