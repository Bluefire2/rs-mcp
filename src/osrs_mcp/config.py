"""Runtime configuration, read from environment variables (and a local `.env` file)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Key-value storage for OAuth state (and later, RuneLite snapshots).
    store_backend: Literal["file", "memory", "firestore"] = "file"
    store_dir: str = "var/store"

    @property
    def full_user_agent(self) -> str:
        if self.contact:
            return f"{self.user_agent} (contact: {self.contact})"
        return self.user_agent


@lru_cache
def get_settings() -> Settings:
    return Settings()
