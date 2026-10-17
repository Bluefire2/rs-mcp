from typing import Annotated

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from osrs_mcp.config import AccountType
from osrs_mcp.sources import Sources

Rsn = Annotated[
    str | None,
    Field(description="RuneScape name. Omit to use the account owner's default name."),
]
AccountTypeArg = Annotated[
    AccountType | None,
    Field(description="Which hiscores to read. Omit to use the owner's account type."),
]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def register_all(mcp: FastMCP, sources: Sources) -> None:
    from osrs_mcp.tools import achievements, gains, prices, quests, stats

    for module in (stats, quests, achievements, gains, prices):
        module.register(mcp, sources)
