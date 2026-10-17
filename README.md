# osrs-mcp

A personal remote [MCP](https://modelcontextprotocol.io) server that lets Claude read one Old
School RuneScape account: skills, boss kill counts, quests, achievement diaries, combat
achievements, recent gains and Grand Exchange prices. It's built to be added to claude.ai as a
custom connector (web and mobile). It runs locally first, then on Google Cloud Run.

It never handles Jagex credentials. Everything comes from public APIs, or (Phase 3) from the
player's own RuneLite client pushing data out.

- Design and build order: [docs/PLAN.md](docs/PLAN.md)
- Research behind the design (with sources): [RESEARCH.md](RESEARCH.md)

## Data sources

| Source | What it provides | Freshness |
|---|---|---|
| [Official hiscores](https://secure.runescape.com/m=hiscore_oldschool/overall) (`index_lite.json`) | Levels, XP, ranks, boss KCs, clues, minigames | Updates on logout/hop. Cached 5 min |
| [WikiSync](https://oldschool.runescape.wiki/w/RuneScape:WikiSync) (RuneLite plugin) | Quest states, diary tasks, combat achievements, levels | Uploaded on login and as things change. Cached 5 min |
| OSRS Wiki (build time) | Quest requirements, diary task text, CA names. See `scripts/refresh_reference_data.py` | Refreshed manually |
| [Wise Old Man](https://wiseoldman.net) | Gains over a day/week/month/year | Per WOM snapshot. Cached 5 min |
| [OSRS Wiki prices](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices) | GE instant-buy/sell, buy limits, alch values | Cached 60 s (mapping 24 h) |

Every outbound request sends a descriptive User-Agent with contact info. The Wiki requires this.

## Tools

| Tool | What Claude uses it for |
|---|---|
| `get_stats` | Levels, XP, ranks, combat level, XP to next level and to 99, closest 99s |
| `get_activity_scores` | Boss KCs, clue counts, minigame scores (ranked entries only by default) |
| `get_quest_progress` | Quest states, plus what each unfinished quest still needs |
| `get_diary_progress` | Diary progress per region/tier; remaining tasks and their missing requirements |
| `get_combat_achievements` | CA points and completion per tier; filtered task list |
| `get_gains` | XP, levels and KCs gained over a period |
| `refresh_gains_tracking` | Ask WOM for a fresh snapshot (rate-limited, so used sparingly) |
| `get_item_price` | GE prices for an item. Partial names and typos are OK |

Each result includes its `source` and a `fetched_at` / `data_as_of` timestamp, so Claude can say
how stale its answer is.

## Running locally

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
python -m pip install --user uv
```

```bash
cp .env.example .env
```

Edit `.env` and set `DEFAULT_RSN` (your RuneScape name), `DEFAULT_ACCOUNT_TYPE` and `CONTACT`.
Then generate the server's own signing and encryption keys:

```bash
python -m uv run python -m osrs_mcp.keys >> .env
```

Then set up Google sign-in (below), or set `AUTH_MODE=none` to skip auth while you only serve
public data. Start the server:

```bash
python -m uv run python -m osrs_mcp
```

The MCP endpoint is `http://localhost:8000/mcp`, and the health check is `/healthz`.

### Google sign-in

With `AUTH_MODE=google` (the default), only the Google account in `ALLOWED_EMAIL` can use the
connector. FastMCP's OAuth proxy acts as the authorization server Claude talks to (it supports
Client ID Metadata Documents and Dynamic Client Registration) and sends you to Google to log in.
Every MCP request re-checks the Google token and the email. The server refuses to start in this
mode if any setting is missing.

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

For quests, diaries and combat achievements, enable the **WikiSync** plugin in RuneLite and log
in once.

### Trying it from Claude

claude.ai (web, Desktop and mobile) connects from Anthropic's cloud, so it **can't reach
localhost**. Locally, use Claude Code or the MCP Inspector.

Claude Code:

```bash
claude mcp add --transport http osrs http://localhost:8000/mcp
```

Then run `/mcp` in Claude Code and choose **Authenticate** for `osrs`. Use `localhost`, not
`127.0.0.1`: the URL must match `PUBLIC_BASE_URL`.

MCP Inspector (opens a browser UI):

```bash
npx @modelcontextprotocol/inspector
```

To try claude.ai before deploying, expose the server temporarily with a Cloudflare quick tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
```

Then:
1. Set `PUBLIC_BASE_URL` to the `https://<random>.trycloudflare.com` URL it prints, and restart
   the server.
2. Add `<that URL>/auth/callback` as a redirect URI on the Google client.
3. In claude.ai, add `<that URL>/mcp` under **Customize > Connectors > Add custom connector**.

The random URL changes every time the tunnel starts, so this is for one-off checks only.

## Development

```bash
python -m uv run pytest
```

```bash
python -m uv run ruff check .
```

The tests run every tool through a real FastMCP client. Outbound HTTP is mocked with responses
captured from the live APIs (`tests/fixtures/`). To re-capture them after an API changes:

```bash
python -m uv run python scripts/capture_fixtures.py
```

After game updates that add quests, diary tasks or combat achievements, rebuild the reference
data. The tests cross-check it against a WikiSync payload.

```bash
python -m uv run python scripts/refresh_reference_data.py
```

### Layout

```
src/osrs_mcp/
  server.py         FastMCP app, /healthz, entry point
  auth.py           Google sign-in via FastMCP's OAuth proxy + email allowlist
  store.py          key-value storage (file tree / memory / Firestore)
  config.py         settings from env / .env
  http.py           shared HTTP client: User-Agent, timeout, retry, TTL cache
  sources.py        wires the data-source clients together
  clients/          hiscores, WikiSync, Wise Old Man, prices
  reference/        Wiki reference data (JSON) and loaders
  requirements.py   quest/diary requirement checking
  game.py           XP table, combat level
  tools/            the MCP tools
scripts/            fixture capture, reference-data refresh
tests/
```

## Roadmap

- **Phase 3:** bank, inventory and gear, pushed by the
  [Group Ironmen Tracker](https://github.com/christoabrown/group-ironmen-tracker) plugin to this
  server (it supports a self-hosted base URL).
- **Phase 4:** deploy to Cloud Run.

See [docs/PLAN.md](docs/PLAN.md).
