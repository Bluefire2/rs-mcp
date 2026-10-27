"""The FastMCP app (tools, RuneLite ingest routes, pages, health check), the ASGI app that serves
it, optionally under a URL prefix, and the entry point."""

from contextlib import AsyncExitStack, asynccontextmanager
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
from osrs_mcp.public_auth import StaticBearerVerifier
from osrs_mcp.snapshots import SnapshotStore
from osrs_mcp.sources import Sources
from osrs_mcp.store import make_store
from osrs_mcp.tools import public, register_all

INSTRUCTIONS = """\
Tools for one Old School RuneScape account: skills and boss kill counts from the official
hiscores, quests / achievement diaries / combat achievements from WikiSync, recent gains from
Wise Old Man, Grand Exchange prices from the OSRS Wiki, and bank / inventory / gear snapshots
pushed by the player's RuneLite client. Every result says where its data came from and how fresh
it is; mention staleness when it matters to the answer.
If no player name is given, tools use the account owner's default name."""

PUBLIC_INSTRUCTIONS = """\
Public data for any Old School RuneScape player: skills and boss kill counts from the official
hiscores, quests / achievement diaries / combat achievements / collection log from WikiSync,
recent gains from Wise Old Man, and Grand Exchange prices from the OSRS Wiki. Always pass the
player's name. If the user says "my" stats or account and you don't know their RuneScape name,
ask for it rather than guessing. For ironman accounts pass account_type to the hiscores tools.
Every result says where its data came from and how fresh it is; mention staleness when it
matters to the answer."""


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


def create_public_app(settings: Settings, sources: Sources) -> FastMCP:
    """The public endpoint: public-data tools for any player, behind PUBLIC_API_TOKENS. No
    ingest, pages or health check; those stay on the owner's app."""
    mcp = FastMCP(
        "osrs-mcp-public",
        instructions=PUBLIC_INSTRUCTIONS,
        version="0.1.0",
        auth=StaticBearerVerifier(settings.public_tokens),
        mask_error_details=True,
    )
    public.register(mcp, sources)
    return mcp


MCP_PATH = "/mcp"
PUBLIC_PATH = "/public"


def http_app(settings: Settings, sources: Sources | None = None) -> ASGIApp:
    """The ASGI app, served under PUBLIC_BASE_URL's path if it has one.

    With PUBLIC_BASE_URL=https://rs.example.com/osrs everything (the MCP endpoint, OAuth
    endpoints, ingest, pages) moves under /osrs, because a Cloud Run domain mapping can only map
    a whole hostname. OAuth discovery documents stay at the domain root, where RFC 8414 and
    RFC 9728 put them (/.well-known/oauth-authorization-server/osrs and
    /.well-known/oauth-protected-resource/osrs/mcp).

    With PUBLIC_API_TOKENS set, the public endpoint is a second FastMCP app mounted at
    <prefix>/public (MCP at <prefix>/public/mcp), sharing the same sources and so the same HTTP
    cache. Each app has its own auth middleware and session manager.
    """
    sources = sources or Sources.create(settings)
    mcp = create_app(settings, sources)
    inner = mcp.http_app(path=MCP_PATH)
    prefix = urlsplit(settings.public_base_url).path.rstrip("/")
    if not prefix and not settings.public_enabled:
        return inner

    apps = [inner]
    routes = []
    if settings.public_enabled:
        public_inner = create_public_app(settings, sources).http_app(path=MCP_PATH)
        apps.append(public_inner)
        # Before the owner's mount, which would otherwise match <prefix>/public too.
        routes.append(Mount(f"{prefix}{PUBLIC_PATH}", app=public_inner))
    if prefix:

        async def to_prefix(request: Request) -> RedirectResponse:
            return RedirectResponse(f"{prefix}/")

        if mcp.auth:
            routes += mcp.auth.get_well_known_routes(mcp_path=MCP_PATH)
        routes.append(Route("/", to_prefix))
    routes.append(Mount(prefix, app=inner))

    # Each inner app's lifespan runs its MCP session manager and its server's own lifespan.
    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            for sub in apps:
                await stack.enter_async_context(sub.lifespan(app))
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    settings = get_settings()
    uvicorn.run(http_app(settings), host=settings.host, port=settings.port, lifespan="on")
