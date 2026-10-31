# osrs-mcp

Remote [MCP](https://modelcontextprotocol.io) servers that let Claude (or any MCP client) read
Old School RuneScape data: skills, boss kill counts, quests, achievement diaries, combat
achievements, collection log, recent gains, Grand Exchange prices and the OSRS Wiki. They run
locally first, then on Google Cloud Run.

They never handle Jagex credentials. Everything comes from public APIs, or (bank, inventory and
gear) from the owner's own RuneLite client pushing data out.

- Design and build order: [docs/PLAN.md](docs/PLAN.md)
- Research behind the design (with sources): [RESEARCH.md](RESEARCH.md)

## Two MCP servers

One process serves two separate MCP servers. Each has its own endpoint, auth and tool set, so
you can connect a client to either one, or to both.

| | **Private** | **Public** |
|---|---|---|
| Endpoint | `<PUBLIC_BASE_URL>/mcp` | `<PUBLIC_BASE_URL>/public/mcp` |
| For | The owner, from claude.ai (web, Desktop, mobile) or Claude Code | Headless consumers that can't do a browser sign-in, such as a Discord bot |
| Auth | Google sign-in (OAuth), restricted to `ALLOWED_EMAIL` | Static bearer token from `PUBLIC_API_TOKENS`, one per consumer |
| Players | The owner by default (`DEFAULT_RSN`); `rsn` is optional. Other players are read with the owner's account type and ironman rules unless `account_type` is passed | Any player; `rsn` is required on every player tool. Every player is treated as a regular account unless `account_type` is passed |
| Private data | Bank, inventory and gear pushed by the owner's RuneLite client | None |
| Enabled | Always | Only when `PUBLIC_API_TOKENS` is set (`deploy/deploy.sh` always sets it) |

**Use them independently**: connect claude.ai to the private server for your own account, and
give a bot a token for the public server. Neither needs the other's credentials.

**Use them together**: add both to one client, for example Claude Code, to look up your own
account (with bank and gear) next to other players'. See
[Using both together](#using-both-together).

They share one process, so they share the HTTP cache too: the same upstream request made on one
is a cache hit on the other, within that source's cache time (see [Data sources](#data-sources)). The private server's endpoint is always mounted,
because the RuneLite ingest routes, sign-in pages and health check live on it.

## Data sources

| Source | What it provides | Freshness |
|---|---|---|
| [Official hiscores](https://secure.runescape.com/m=hiscore_oldschool/overall) (`index_lite.json`) | Levels, XP, ranks, boss KCs, clues, minigames | Updates on logout/hop. Cached 5 min |
| [WikiSync](https://oldschool.runescape.wiki/w/RuneScape:WikiSync) (RuneLite plugin) | Quest states, diary tasks, combat achievements, levels, collection log | Uploaded on login and as things change; the collection log only when its WikiSync button is pressed. Cached 5 min |
| OSRS Wiki (build time) | Quest requirements, diary task text, CA names, collection log pages. See `scripts/refresh_reference_data.py` | Refreshed manually |
| [Wise Old Man](https://wiseoldman.net) | Gains over a day/week/month/year | Per WOM snapshot. Cached 5 min |
| [OSRS Wiki prices](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices) | GE instant-buy/sell, buy limits, alch values, price history | Cached 60 s (mapping 24 h, history 5-60 min) |
| [OSRS Wiki](https://oldschool.runescape.wiki) (live, MediaWiki API) | Page search, and page text: infoboxes, drop tables, quest details | Cached 1 h |
| [Group Ironmen Tracker](https://github.com/christoabrown/group-ironmen-tracker) (RuneLite plugin) | Bank, inventory, equipment, rune pouch, quiver, new collection log items. **Private server only** | Pushed within seconds of a change; the bank only when it's opened |

Every outbound request sends a descriptive User-Agent with contact info. The Wiki requires this.

## Tools

| Tool | What Claude uses it for | Private | Public |
|---|---|:-:|:-:|
| `get_stats` | Levels, XP, ranks, combat level, XP to next level and to 99, closest 99s | ✓ | ✓ |
| `get_activity_scores` | Boss KCs, clue counts, minigame scores (ranked entries only by default) | ✓ | ✓ |
| `get_quest_progress` | Quest states, plus what each unfinished quest still needs | ✓ | ✓ |
| `get_diary_progress` | Diary progress per region/tier; remaining tasks and their missing requirements | ✓ | ✓ |
| `get_combat_achievements` | CA points and completion per tier; filtered task list | ✓ | ✓ |
| `get_collection_log` | Log slots per tab and page, the missing items most players have, and every item on a page | ✓ | ✓ ¹ |
| `get_gains` | XP, levels and KCs gained over a period | ✓ | ✓ |
| `refresh_gains_tracking` | Ask WOM for a fresh snapshot (rate-limited, so used sparingly) | ✓ | — ² |
| `get_item_price` | GE prices for an item. Partial names and typos are OK | ✓ | ✓ |
| `get_price_history` | An item's price over the last day/week/month/year: change, low/high, volume, up to 120 chart points | ✓ | ✓ |
| `search_wiki` | Find OSRS Wiki pages by name or topic | ✓ | ✓ |
| `get_wiki_page` | A Wiki page as plain text: the intro and infobox, or one section such as Drops or Details | ✓ | ✓ |
| `get_bank` | Bank contents with GE values and total; search, minimum value, sort | ✓ | — |
| `get_equipment_and_inventory` | Worn gear by slot, inventory, rune pouch, quiver | ✓ | — |

1. On the public server it reads WikiSync only, never the owner's RuneLite snapshots.
2. It writes to Wise Old Man, using the server's rate limit (its IP, or `WOM_API_KEY`).

Other differences on the public server: `account_type` defaults to `regular` rather than
`DEFAULT_ACCOUNT_TYPE`, and quest and diary requirements are checked as for a regular account.
Anything a bot can call, any of its users can make it call, so "my stats" must never quietly
mean the owner's.

Each result includes its `source` and a `fetched_at` / `data_as_of` timestamp, so Claude can say
how stale its answer is.

## Running the server locally

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
python -m pip install --user uv
```

```bash
cp .env.example .env
```

Edit `.env` and set `DEFAULT_RSN` (your RuneScape name), `DEFAULT_ACCOUNT_TYPE` and `CONTACT`.
Then generate the server's keys and tokens:

```bash
python -m uv run python -m osrs_mcp.keys >> .env
```

This appends `JWT_SIGNING_KEY` and `STORAGE_ENCRYPTION_KEY` (private server), `INGEST_TOKEN`
(RuneLite ingest) and `PUBLIC_API_TOKENS` (public server). If you don't want the public server,
delete the appended `PUBLIC_API_TOKENS` line: when a key appears twice in `.env`, the last line
wins.

Then set up Google sign-in for the private server (below), or set `AUTH_MODE=none` to skip it
during development. Start the server:

```bash
python -m uv run python -m osrs_mcp
```

| Endpoint | URL |
|---|---|
| Private MCP | `http://localhost:8000/mcp` |
| Public MCP | `http://localhost:8000/public/mcp` (only with `PUBLIC_API_TOKENS` set) |
| Health check | `http://localhost:8000/healthz` or `/health` (on Cloud Run only `/health` gets through) |

With a path in `PUBLIC_BASE_URL` (for example `https://rs.example.com/osrs`), all of these move
under that path: `/osrs/mcp`, `/osrs/public/mcp`, `/osrs/health`.

`AUTH_MODE=none` opens the **private** endpoint to anyone who can reach it, bank and gear
included. Only use it on localhost. It doesn't affect the public endpoint, which always
requires its bearer token.

### Google sign-in (private server)

With `AUTH_MODE=google` (the default), only the Google account in `ALLOWED_EMAIL` can use the
private server. FastMCP's OAuth proxy acts as the authorization server Claude talks to (it
supports Client ID Metadata Documents and Dynamic Client Registration) and sends you to Google
to log in. Every MCP request re-checks the Google token and the email. The server refuses to
start in this mode if any setting is missing.

One-time setup in the Google Cloud console:

1. **OAuth consent screen**:
   - User type **External**.
   - Scopes: only `openid` and `.../auth/userinfo.email`.
   - **Publish** the app so its status is *In production*. In *Testing*, Google expires refresh
     tokens after 7 days.
2. **Clients → Create client**:
   - Type: Web application.
   - Authorized redirect URI: `<PUBLIC_BASE_URL>/auth/callback`, for example
     `http://localhost:8000/auth/callback`.
3. Put `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `ALLOWED_EMAIL` in `.env`.

OAuth state (client registrations, encrypted Google tokens) is kept in `STORE_DIR`, so sign-ins
survive restarts.

For quests, diaries and combat achievements (on either server), the player needs the
**WikiSync** plugin enabled in RuneLite and to have logged in once since.

### Bank, inventory and gear (private server)

The [Group Ironmen Tracker](https://github.com/christoabrown/group-ironmen-tracker) RuneLite
plugin can send its data to a self-hosted server instead of groupiron.men. This server speaks
the same protocol, so you don't need a group ironman account or a groupiron.men group: the
"group" is just a name and a secret that you make up.

1. Generate a token (or reuse the `INGEST_TOKEN` line from `python -m osrs_mcp.keys`) and set in
   `.env`:
   - `GIM_GROUP`: any group name, for example `osrs-mcp`. Avoid spaces.
   - `INGEST_TOKEN`: the long random secret.
   - `INGEST_ALLOWED_RSNS` (optional): comma-separated RuneScape names allowed to push data.
     Blank means `DEFAULT_RSN` only.

   Restart the server. Ingest stays off until both `GIM_GROUP` and `INGEST_TOKEN` are set.
2. In RuneLite, install **Group Ironmen Tracker** from the Plugin Hub and open its settings:
   - **Group Name**: the `GIM_GROUP` value.
   - **Group Token**: the `INGEST_TOKEN` value.
   - **Self Hosted Config > Server base URL override**: `PUBLIC_BASE_URL`, for example
     `http://localhost:8000` (no trailing slash, no `/mcp`).
3. Log in and **open your bank once**. Inventory, equipment, rune pouch and quiver are sent within
   a few seconds of changing; the bank only when you open it (items put in a deposit box are
   added to the stored bank in between).

The plugin calls `GET /api/group/<group>/am-i-in-group` and then `POST
/api/group/<group>/update-group-member` with `Authorization: <token>`. These routes are outside
the Google sign-in (RuneLite can't do OAuth). Instead they check the token in constant time, the
group name and the player name, and limit bodies to 2 MB. Snapshots are stored per player and
kind in `STORE_DIR` (collection `snapshots`). Position, HP/prayer and the current target are
sent every second and are accepted but not stored.

### Public server tokens

Give each consumer its own token, so one can be revoked alone. To add one, generate a token
(the `PUBLIC_API_TOKENS` line from `python -m osrs_mcp.keys`) and append it to
`PUBLIC_API_TOKENS`, comma-separated, then restart. To revoke one, remove it and restart. Each
request's client ID is the start of its token's SHA-256 (`public-1a2b3c4d`), never the token
itself.

A missing or wrong token gets a plain `401` with no OAuth metadata, so clients aren't sent to
the Google sign-in. In Cloud Run the value lives in the `osrs-mcp-public-api-tokens` secret; see
[Deploying to Cloud Run](#deploying-to-cloud-run).

## Integration guide: private server

The private server uses OAuth, so the client must be able to open a browser for the Google
sign-in. Sign in with the `ALLOWED_EMAIL` account; any other account is refused.

### claude.ai (web, Desktop and mobile)

claude.ai connects from Anthropic's cloud, so it needs a public HTTPS URL: it **can't reach
localhost**. Once deployed:

1. In claude.ai, go to **Customize > Connectors > Add custom connector**.
2. URL: `<PUBLIC_BASE_URL>/mcp`, for example `https://osrs-mcp-123456789.us-west1.run.app/mcp`.
   Leave the OAuth client ID and secret blank; the server registers Claude itself.
3. Choose **Connect** and sign in with Google.

The connector then works in claude.ai on the web, in Claude Desktop and in the mobile apps.

To try claude.ai before deploying, expose the local server temporarily with a Cloudflare quick
tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
```

Then:
1. Set `PUBLIC_BASE_URL` to the `https://<random>.trycloudflare.com` URL it prints, and restart
   the server.
2. Add `<that URL>/auth/callback` as a redirect URI on the Google client.
3. In claude.ai, add `<that URL>/mcp` as a custom connector, as above.

The random URL changes every time the tunnel starts, so this is for one-off checks only.

### Claude Code

```bash
claude mcp add --transport http osrs http://localhost:8000/mcp
```

Then run `/mcp` in Claude Code and choose **Authenticate** for `osrs`. Use `localhost`, not
`127.0.0.1`: the URL must match `PUBLIC_BASE_URL`. For a deployed server, use
`<PUBLIC_BASE_URL>/mcp` instead.

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

In the browser UI it opens, choose transport **Streamable HTTP**, enter
`http://localhost:8000/mcp` and connect; the Inspector runs the OAuth flow for you.

## Integration guide: public server

The public server takes a static bearer token in the `Authorization` header. Every request
needs it:

```
Authorization: Bearer <token>
```

Its MCP instructions tell the model to always pass the player's name, and to ask for it rather
than guess when a user says "my stats". A bot that already knows its user's RuneScape name (for
example from a `/link` command) should put it in the prompt or pass it as `rsn` directly.

claude.ai custom connectors sign in with OAuth and can't send a static bearer token, so use the
private server there.

### Claude Code

```bash
claude mcp add --transport http osrs-public http://localhost:8000/public/mcp --header "Authorization: Bearer <token>"
```

For a deployed server, use `<PUBLIC_BASE_URL>/public/mcp`. The default scope keeps the token in
your local Claude Code config; `--scope user` makes it available in every project. Avoid
`--scope project`, which writes the token into `.mcp.json` in the repo.

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

Choose transport **Streamable HTTP**, enter `http://localhost:8000/public/mcp`, and under
**Authentication** set the bearer token, then connect.

### A bot or script (Python)

Any MCP client library works. With the [FastMCP](https://gofastmcp.com) client:

```python
import asyncio
import os

from fastmcp import Client
from fastmcp.client.auth import BearerAuth

URL = "https://osrs-mcp-123456789.us-west1.run.app/public/mcp"


async def main() -> None:
    async with Client(URL, auth=BearerAuth(os.environ["OSRS_MCP_TOKEN"])) as client:
        tools = await client.list_tools()
        print([tool.name for tool in tools])
        result = await client.call_tool("get_stats", {"rsn": "Lynx Titan"})
        print(result.structured_content)


asyncio.run(main())
```

A bot that lets an LLM answer questions can pass the same URL and token to its model's MCP
support (for example the Claude API's MCP connector) instead of calling tools itself. The Claude
API connects from Anthropic's cloud, so like claude.ai it needs the deployed URL, not localhost.

### Checking a token with curl

```bash
curl -i -X POST http://localhost:8000/public/mcp -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

`200` means the token is valid; `401` means it is missing or wrong.

## Using both together

In Claude Code, add both servers under different names:

```bash
claude mcp add --transport http osrs http://localhost:8000/mcp
```

```bash
claude mcp add --transport http osrs-public http://localhost:8000/public/mcp --header "Authorization: Bearer <token>"
```

The tools have the same names on both servers; Claude Code prefixes each with its server's
name, so they don't clash. Questions about your own account,
bank or gear go to `osrs`. Lookups of other players work on either, but differ: `osrs` reads them
with your `DEFAULT_ACCOUNT_TYPE` and checks quest and diary requirements with your ironman rules,
while `osrs-public` treats them as regular accounts unless `account_type` is passed. For an
ironman owner, the public server gives the right answers for other, non-ironman players.
Connecting both is also the easiest way to see exactly what a bot will see.

## Development

```bash
python -m uv run pytest
```

```bash
python -m uv run ruff check .
```

The tests run every private-server tool through a real FastMCP client. Outbound HTTP is mocked
with responses captured from the live APIs (`tests/fixtures/`). `tests/test_public.py` covers
the public server: its exact tool list (so a private tool can't appear on it by accident), its
token auth, the required `rsn`, and where its answers differ from the private server's. To
re-capture the fixtures after an API changes:

```bash
python -m uv run python scripts/capture_fixtures.py
```

After game updates that add quests, diary tasks or combat achievements, rebuild the reference
data. The tests cross-check it against a WikiSync payload.

```bash
python -m uv run python scripts/refresh_reference_data.py
```

GitHub Actions runs lint, format and tests on every push and pull request
(`.github/workflows/ci.yml`). A separate workflow checks weekly, and whenever the skill
changes, that the Wiki pages the osrs-coach skill names still exist (`wiki-links.yml`), so a
Wiki outage can't fail a code change.

### Layout

```
src/osrs_mcp/
  server.py         both FastMCP apps (private and public), /healthz, entry point
  auth.py           private server: Google sign-in via FastMCP's OAuth proxy + email allowlist
  public_auth.py    public server: static bearer tokens
  ingest/gim.py     Group Ironmen Tracker-compatible endpoints the RuneLite plugin pushes to
  snapshots.py      per-player snapshots (bank, inventory, ...) in the key-value store
  store.py          key-value storage (file tree / memory / Firestore)
  config.py         settings from env / .env
  http.py           shared HTTP client: User-Agent, timeout, retry, TTL cache
  sources.py        wires the data-source clients together
  clients/          hiscores, WikiSync, Wise Old Man, prices, Wiki pages
  reference/        Wiki reference data (JSON) and loaders
  requirements.py   quest/diary requirement checking
  game.py           XP table, combat level
  tools/            the private server's tools, and the build_* functions both servers share
  tools/public.py   the public server's tools (wrappers over the build_* functions, rsn required)
scripts/            fixture capture, reference-data refresh
tests/
```

## Deploying to Cloud Run

`deploy/deploy.sh` deploys both servers as one Cloud Run service and is safe to re-run. It:
- enables the APIs and creates the Firestore `(default)` database, an Artifact Registry repo,
  and a dedicated service account (Firestore user, plus read access to its own secrets only);
- creates five Secret Manager secrets if they're missing. `GOOGLE_CLIENT_SECRET` and
  `INGEST_TOKEN` are copied from `.env`; the signing and encryption keys and the public
  server's first bearer token are generated fresh. Existing secrets are never overwritten, and
  values are never printed;
- builds the image with Cloud Build, tagged with the git commit, and refuses to run with
  uncommitted build inputs;
- deploys with `--update-env-vars` / `--update-secrets`, which merge, so settings changed by
  hand on the service survive;
- prints the result for you to check (it doesn't fail on a mismatch): container count, image
  and service account, env var names, the `/health` status, the status from `/mcp` and
  `/public/mcp` without a token (expect 401 from both), and the OAuth protected-resource
  document.

```bash
bash deploy/deploy.sh --dry-run
```

```bash
bash deploy/deploy.sh
```

The project and public address come from `.env`: `GCP_PROJECT` (required), and optionally
`DEPLOY_DOMAIN` and `DEPLOY_PREFIX` for a custom domain such as `https://rs.example.com/osrs`
(see `.env.example`). Region `us-west1` and service `osrs-mcp` are defaults; override any of
them with `PROJECT=… REGION=… DOMAIN=… PREFIX=…`. The Firestore location is permanent once
created. Run it from Git Bash; it calls the POSIX `gcloud`, because `gcloud.cmd` breaks
arguments containing spaces.

`deploy.sh` stops unless `.env` sets `GCP_PROJECT`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`ALLOWED_EMAIL`, `DEFAULT_RSN`, `CONTACT`, `GIM_GROUP` and `INGEST_TOKEN`. It always deploys with
`AUTH_MODE=google` and a public-server token, so in Cloud Run both servers are always on.

The **public URL** below is the `PUBLIC_BASE_URL` the script deploys:
`https://<DEPLOY_DOMAIN><DEPLOY_PREFIX>` with a custom domain, otherwise the service's run.app URL
(`https://osrs-mcp-<project number>.<region>.run.app`). The script prints it at the end, along
with the exact redirect URI, connector URL, plugin base URL and public endpoint.

After the first deploy, for the private server:
1. Add `<public URL>/auth/callback` to the Google OAuth client's **Authorized redirect URIs**.
2. In the Group Ironmen Tracker plugin, set **Server base URL override** to the public URL.
3. In claude.ai, add `<public URL>/mcp` as a custom connector (see
   [Integration guide: private server](#integration-guide-private-server)).
4. To publish the Google OAuth app (so sign-ins don't expire after 7 days), use `<public URL>/`
   as the homepage and `<public URL>/privacy` as the privacy policy on the Branding page.

And for the public server, read the generated token and give it to the consumer, with
`<public URL>/public/mcp`. Pass the same project as `GCP_PROJECT`, since the script doesn't use
gcloud's active project:

```bash
gcloud secrets versions access latest --secret=osrs-mcp-public-api-tokens --project=<GCP_PROJECT>
```

To add or revoke a consumer:

1. Read the current list with the command above. The new version replaces the whole list.
2. Add a new secret version with the full comma-separated list you want:

   ```bash
   printf '%s' "<token1>,<token2>" | gcloud secrets versions add osrs-mcp-public-api-tokens --data-file=- --project=<GCP_PROJECT>
   ```

3. Roll out a new revision so the service reads it. Running instances keep the old list until
   then, so a revoked token keeps working until this step. Re-running the deploy script does
   this, but also rebuilds the image; this only makes a new revision:

   ```bash
   gcloud run services update osrs-mcp --region=us-west1 --project=<GCP_PROJECT> --update-secrets=PUBLIC_API_TOKENS=osrs-mcp-public-api-tokens:latest
   ```

   Check that `latestReadyRevisionName` changed, with `gcloud run services describe osrs-mcp
   --region=us-west1 --project=<GCP_PROJECT>`.
4. When revoking, disable the old version so the revoked token can't be read back from it:

   ```bash
   gcloud secrets versions disable <old version number> --secret=osrs-mcp-public-api-tokens --project=<GCP_PROJECT>
   ```

See [docs/PLAN.md](docs/PLAN.md).
