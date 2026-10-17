"""Runtime configuration, read from environment variables (and a local `.env` file)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from osrs_mcp.game import normalize_rsn

AccountType = Literal[
    "regular",
    "ironman",
    "hardcore_ironman",
    "ultimate",
    "deadman",
    "seasonal",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Player defaults: every tool accepts an `rsn` override.
    default_rsn: str = ""
    default_account_type: AccountType = "regular"

    # Outbound HTTP. The OSRS Wiki blocks generic user agents, so this must stay descriptive.
    user_agent: str = "osrs-mcp-personal/0.1"
    contact: str = ""
    wom_api_key: str = ""

    # Server
    host: str = "127.0.0.1"
    port: int = Field(default=8000, validation_alias="PORT")
    # The URL clients reach this server at (no trailing slash, no /mcp). It is the OAuth
    # issuer and must match the Google redirect URI: <public_base_url>/auth/callback.
    public_base_url: str = "http://localhost:8000"

    # Auth. "google" (default) requires Google sign-in restricted to ALLOWED_EMAIL; "none" is
    # for local development against public data only.
    auth_mode: Literal["google", "none"] = "google"
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_email: str = ""
    jwt_signing_key: str = ""  # signs the tokens this server issues to Claude
    storage_encryption_key: str = ""  # Fernet key; encrypts stored upstream Google tokens

    # Key-value storage for OAuth state and RuneLite snapshots.
    store_backend: Literal["file", "memory", "firestore"] = "file"
    store_dir: str = "var/store"

    # Ingest from the Group Ironmen Tracker RuneLite plugin (bank, inventory, gear). Disabled
    # (every ingest request refused) unless both GIM_GROUP and INGEST_TOKEN are set.
    gim_group: str = ""  # the plugin's "Group Name" setting
    ingest_token: str = ""  # the plugin's "Group Token" setting; a long random secret
    # Comma-separated RuneScape names allowed to push data. Blank means DEFAULT_RSN only.
    ingest_allowed_rsns: str = ""

    @property
    def ingest_enabled(self) -> bool:
        return bool(self.gim_group.strip() and self.ingest_token.strip())

    @property
    def ingest_rsns(self) -> frozenset[str]:
        """Normalised names allowed to push snapshots (empty means nobody can)."""
        names = self.ingest_allowed_rsns.split(",") if self.ingest_allowed_rsns.strip() else []
        names = names or [self.default_rsn]
        return frozenset(n for n in (normalize_rsn(name) for name in names) if n)

    @property
    def full_user_agent(self) -> str:
        if self.contact:
            return f"{self.user_agent} (contact: {self.contact})"
        return self.user_agent


@lru_cache
def get_settings() -> Settings:
    return Settings()
