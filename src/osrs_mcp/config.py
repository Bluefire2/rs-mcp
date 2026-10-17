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

    @property
    def full_user_agent(self) -> str:
        if self.contact:
            return f"{self.user_agent} (contact: {self.contact})"
        return self.user_agent


@lru_cache
def get_settings() -> Settings:
    return Settings()
