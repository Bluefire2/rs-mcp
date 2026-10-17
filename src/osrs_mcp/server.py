"""The FastMCP app: tool registration, health check, and the entry point."""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from osrs_mcp.config import get_settings

INSTRUCTIONS = """\
Tools for one Old School RuneScape account: skills and boss kill counts from the official
hiscores, quests / achievement diaries / combat achievements from WikiSync, recent gains from
Wise Old Man, and Grand Exchange prices from the OSRS Wiki. Every result says where its data
came from and how fresh it is; mention staleness when it matters to the answer.
If no player name is given, tools use the account owner's default name."""


def create_app() -> FastMCP:
    mcp = FastMCP("osrs-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp


mcp = create_app()


def main() -> None:
    settings = get_settings()
    mcp.run(transport="http", host=settings.host, port=settings.port, path="/mcp")
