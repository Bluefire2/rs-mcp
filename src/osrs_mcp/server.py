"""The FastMCP app: tool registration, health check, and the entry point."""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from osrs_mcp.auth import make_auth
from osrs_mcp.config import Settings, get_settings
from osrs_mcp.sources import Sources
from osrs_mcp.store import make_store
from osrs_mcp.tools import register_all

INSTRUCTIONS = """\
Tools for one Old School RuneScape account: skills and boss kill counts from the official
hiscores, quests / achievement diaries / combat achievements from WikiSync, recent gains from
Wise Old Man, and Grand Exchange prices from the OSRS Wiki. Every result says where its data
came from and how fresh it is; mention staleness when it matters to the answer.
If no player name is given, tools use the account owner's default name."""


def create_app(settings: Settings | None = None, sources: Sources | None = None) -> FastMCP:
    settings = settings or get_settings()
    sources = sources or Sources.create(settings)
    store = make_store(settings)
    # mask_error_details: unexpected exceptions reach the client as a generic error (and are
    # logged here); SourceError/ToolError messages are written for the client and pass through.
    mcp = FastMCP(
        "osrs-mcp",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        auth=make_auth(settings, store),
        mask_error_details=True,
    )
    register_all(mcp, sources)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp


MCP_PATH = "/mcp"


def main() -> None:
    settings = get_settings()
    create_app(settings).run(
        transport="http", host=settings.host, port=settings.port, path=MCP_PATH
    )
