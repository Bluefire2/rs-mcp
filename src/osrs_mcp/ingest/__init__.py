"""HTTP endpoints that receive game state pushed from the player's RuneLite client."""

from osrs_mcp.ingest.gim import register

__all__ = ["register"]
