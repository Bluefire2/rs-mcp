"""The two public pages Google requires before an OAuth app can be published: a homepage and a
privacy policy. They describe what this server actually does with data."""

from html import escape

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse

from osrs_mcp.config import Settings

STYLE = """
body { font: 16px/1.55 system-ui, sans-serif; max-width: 42rem; margin: 2rem auto;
       padding: 0 1rem; color: #1f2328; background: #fff; }
h1 { font-size: 1.6rem; } h2 { font-size: 1.15rem; margin-top: 1.6rem; }
code { background: #f3f4f6; padding: 0.1rem 0.3rem; border-radius: 4px; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e6e6; background: #16181c; } code { background: #2a2d33; } a { color: #8ab4f8; }
}
"""


def page(title: str, body: str) -> HTMLResponse:
    html = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )
    return HTMLResponse(html)


def register(mcp: FastMCP, settings: Settings) -> None:
    contact = escape(settings.contact or "the operator of this server")
    base = escape(settings.public_base_url.rstrip("/"))

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def home(request: Request) -> HTMLResponse:
        return page(
            "osrs-mcp",
            f"""
<h1>osrs-mcp</h1>
<p>A personal <a href="https://modelcontextprotocol.io">Model Context Protocol</a> server that
lets Claude read one Old School RuneScape account: skills, quests, achievement diaries, combat
achievements, recent gains, Grand Exchange prices, and bank and gear snapshots sent from the
owner's RuneLite client.</p>
<p>It is a private tool for a single person. Signing in is restricted to one Google account;
nobody else can use it.</p>
<p>MCP endpoint: <code>{base}/mcp</code><br>
<a href="/privacy">Privacy policy</a></p>
""",
        )

    @mcp.custom_route("/privacy", methods=["GET"], include_in_schema=False)
    async def privacy(request: Request) -> HTMLResponse:
        return page(
            "osrs-mcp privacy policy",
            f"""
<h1>Privacy policy</h1>
<p>osrs-mcp is a personal tool operated by and for one person. This page describes all the data
it handles.</p>
<h2>Google sign-in</h2>
<p>Sign-in uses Google OAuth with only the <code>openid</code> and <code>email</code> scopes. The
server uses your email address solely to check that you are the one account allowed to use it.
It stores the Google tokens needed to keep you signed in, encrypted, and uses them for nothing
else. It does not access your Gmail, Drive, contacts or any other Google data.</p>
<h2>Game data</h2>
<p>Skills, quests, diaries and similar data come from public sources (the official Old School
RuneScape hiscores, the OSRS Wiki and WikiSync, and Wise Old Man). Bank, inventory and equipment
snapshots are sent by the owner's own RuneLite client and stored so Claude can answer questions
about them. No game account credentials are ever collected or stored.</p>
<h2>Sharing</h2>
<p>Nothing is sold or shared. Data leaves the server only in answers to the signed-in owner's
own requests through Claude.</p>
<h2>Storage and deletion</h2>
<p>Data is stored in Google Cloud (Firestore) in the owner's project. Revoking access at
<a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a> ends
the sign-in; contact {contact} to have stored data deleted.</p>
""",
        )
