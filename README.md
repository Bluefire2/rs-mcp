# osrs-mcp

A personal remote [MCP](https://modelcontextprotocol.io) server that lets Claude read one Old
School RuneScape account: skills, boss kill counts, quests, achievement diaries, combat
achievements, collection log, recent gains and Grand Exchange prices. It's built to be added
to claude.ai as a custom connector (web and mobile). It runs locally first, then on Google
Cloud Run.

It never handles Jagex credentials. Everything comes from public APIs, or (bank, inventory and
gear) from the player's own RuneLite client pushing data out.

- Design and build order: [docs/PLAN.md](docs/PLAN.md)
- Research behind the design (with sources): [RESEARCH.md](RESEARCH.md)

## Data sources

| Source | What it provides | Freshness |
|---|---|---|
| [Official hiscores](https://secure.runescape.com/m=hiscore_oldschool/overall) (`index_lite.json`) | Levels, XP, ranks, boss KCs, clues, minigames | Updates on logout/hop. Cached 5 min |
| [WikiSync](https://oldschool.runescape.wiki/w/RuneScape:WikiSync) (RuneLite plugin) | Quest states, diary tasks, combat achievements, levels, collection log | Uploaded on login and as things change; the collection log only when its WikiSync button is pressed. Cached 5 min |
| OSRS Wiki (build time) | Quest requirements, diary task text, CA names, collection log pages. See `scripts/refresh_reference_data.py` | Refreshed manually |
| [Wise Old Man](https://wiseoldman.net) | Gains over a day/week/month/year | Per WOM snapshot. Cached 5 min |
| [OSRS Wiki prices](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices) | GE instant-buy/sell, buy limits, alch values | Cached 60 s (mapping 24 h) |
| [Group Ironmen Tracker](https://github.com/christoabrown/group-ironmen-tracker) (RuneLite plugin) | Bank, inventory, equipment, rune pouch, quiver, new collection log items | Pushed within seconds of a change; the bank only when it's opened |

Every outbound request sends a descriptive User-Agent with contact info. The Wiki requires this.

## Tools

| Tool | What Claude uses it for |
|---|---|
| `get_stats` | Levels, XP, ranks, combat level, XP to next level and to 99, closest 99s |
| `get_activity_scores` | Boss KCs, clue counts, minigame scores (ranked entries only by default) |
| `get_quest_progress` | Quest states, plus what each unfinished quest still needs |
| `get_diary_progress` | Diary progress per region/tier; remaining tasks and their missing requirements |
| `get_combat_achievements` | CA points and completion per tier; filtered task list |
| `get_collection_log` | Log slots per tab and page, the missing items most players have, and every item on a page |
| `get_gains` | XP, levels and KCs gained over a period |
| `refresh_gains_tracking` | Ask WOM for a fresh snapshot (rate-limited, so used sparingly) |
| `get_item_price` | GE prices for an item. Partial names and typos are OK |
| `get_bank` | Bank contents with GE values and total; search, minimum value, sort |
| `get_equipment_and_inventory` | Worn gear by slot, inventory, rune pouch, quiver |

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

### Bank, inventory and gear (Group Ironmen Tracker)

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

GitHub Actions runs lint, format and tests on every push and pull request
(`.github/workflows/ci.yml`). A separate workflow checks weekly, and whenever the skill
changes, that the Wiki pages the osrs-coach skill names still exist (`wiki-links.yml`), so a
Wiki outage can't fail a code change.

### Layout

```
src/osrs_mcp/
  server.py         FastMCP app, /healthz, entry point
  auth.py           Google sign-in via FastMCP's OAuth proxy + email allowlist
  ingest/gim.py     Group Ironmen Tracker-compatible endpoints the RuneLite plugin pushes to
  snapshots.py      per-player snapshots (bank, inventory, ...) in the key-value store
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

## Deploying to Cloud Run

`deploy/deploy.sh` does the whole deploy and is safe to re-run. It:
- enables the APIs and creates the Firestore `(default)` database, an Artifact Registry repo,
  and a dedicated service account (Firestore user, plus read access to its own secrets only);
- creates four Secret Manager secrets if they're missing. `GOOGLE_CLIENT_SECRET` and
  `INGEST_TOKEN` are copied from `.env`; the signing and encryption keys are generated fresh.
  Existing secrets are never overwritten, and values are never printed;
- builds the image with Cloud Build, tagged with the git commit, and refuses to run with
  uncommitted build inputs;
- deploys with `--update-env-vars` / `--update-secrets`, which merge, so settings changed by
  hand on the service survive;
- checks the result: one container, the right image and service account, env var names,
  `/health`, and a 401 from `/mcp`.

```bash
bash deploy/deploy.sh --dry-run
```

```bash
bash deploy/deploy.sh
```

The project and public address come from `.env`: `GCP_PROJECT` (required), and optionally
`DEPLOY_DOMAIN` and `DEPLOY_PREFIX` for a custom domain such as `https://rs.example.com/osrs`
(see `.env.example`). Region `us-west1` and service `osrs-mcp` are defaults; override any of
them with `PROJECT=… REGION=… DOMAIN=… PREFIX=…`. The Firestore location is permanent once created. Run it from Git
Bash; it calls the POSIX `gcloud`, because `gcloud.cmd` breaks arguments containing spaces.

After the first deploy:
1. Add `https://<service URL>/auth/callback` to the Google OAuth client's **Authorized redirect
   URIs**. The service URL is printed, and is `https://osrs-mcp-<project number>.<region>.run.app`.
2. In the Group Ironmen Tracker plugin, set **Server base URL override** to the service URL.
3. In claude.ai, **Customize > Connectors > Add custom connector** with `<service URL>/mcp`.
4. To publish the Google OAuth app (so sign-ins don't expire after 7 days), use `<service URL>/`
   as the homepage and `<service URL>/privacy` as the privacy policy on the Branding page.

See [docs/PLAN.md](docs/PLAN.md).
