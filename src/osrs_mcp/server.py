"""The FastMCP app (tools, RuneLite ingest routes, pages, health check), the ASGI app that serves
it, optionally under a URL prefix, and the entry point."""

from urllib.parse import urlsplit

import uvicorn
from fastmcp import FastMCP
from key_value.aio.protocols.key_value import AsyncKeyValue
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from osrs_mcp import ingest, site
from osrs_mcp.auth import make_auth
from osrs_mcp.config import Settings, get_settings
from osrs_mcp.snapshots import SnapshotStore
from osrs_mcp.sources import Sources
from osrs_mcp.store import make_store
from osrs_mcp.tools import register_all

INSTRUCTIONS = """\
Tools for one Old School RuneScape account: skills and boss kill counts from the official
hiscores, quests / achievement diaries / combat achievements from WikiSync, recent gains from
Wise Old Man, Grand Exchange prices from the OSRS Wiki, and bank / inventory / gear snapshots
pushed by the player's RuneLite client. Every result says where its data came from and how fresh
it is; mention staleness when it matters to the answer.
If no player name is given, tools use the account owner's default name."""


def create_app(
    settings: Settings | None = None,
    sources: Sources | None = None,
    store: AsyncKeyValue | None = None,
) -> FastMCP:
    settings = settings or get_settings()
    sources = sources or Sources.create(settings)
    # One store for OAuth state and snapshots; they live in separate collections.
    store = store if store is not None else make_store(settings)
    snapshots = SnapshotStore(store)
    # mask_error_details: unexpected exceptions reach the client as a generic error (and are
    # logged here); SourceError/ToolError messages are written for the client and pass through.
    mcp = FastMCP(
        "osrs-mcp",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        auth=make_auth(settings, store),
        mask_error_details=True,
    )
    register_all(mcp, sources, snapshots)
    ingest.register(mcp, settings, snapshots)
    site.register(mcp, settings)

    # /health for Cloud Run, whose front end reserves /healthz and never forwards it.
    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp


MCP_PATH = "/mcp"


def http_app(settings: Settings, mcp: FastMCP | None = None) -> ASGIApp:
    """The ASGI app, served under PUBLIC_BASE_URL's path if it has one.

    With PUBLIC_BASE_URL=https://rs.example.com/osrs everything (the MCP endpoint, OAuth
    endpoints, ingest, pages) moves under /osrs, because a Cloud Run domain mapping can only map
    a whole hostname. OAuth discovery documents stay at the domain root, where RFC 8414 and
    RFC 9728 put them (/.well-known/oauth-authorization-server/osrs and
    /.well-known/oauth-protected-resource/osrs/mcp).
    """
    mcp = mcp or create_app(settings)
    inner = mcp.http_app(path=MCP_PATH)
    prefix = urlsplit(settings.public_base_url).path.rstrip("/")
    if not prefix:
        return inner

    async def to_prefix(request: Request) -> RedirectResponse:
        return RedirectResponse(f"{prefix}/")

    routes = [*mcp.auth.get_well_known_routes(mcp_path=MCP_PATH)] if mcp.auth else []
    routes += [Route("/", to_prefix), Mount(prefix, app=inner)]
    # The inner app's lifespan runs the MCP session manager and the server's own lifespan.
    return Starlette(routes=routes, lifespan=inner.lifespan)


def main() -> None:
    settings = get_settings()
    uvicorn.run(http_app(settings), host=settings.host, port=settings.port, lifespan="on")
