# OSRS MCP Connector: research findings and implementation plan

## Context

The design doc ("OSRS_MCP_PLAN.md", Google Doc `10DJYSpd…`) describes a personal remote MCP server. It exposes one Old School RuneScape account (stats, quests, diaries, CAs, gains, prices, and later bank and gear) to claude.ai on web and mobile as a custom connector. It's private to the user's Google account, never touches Jagex credentials, and stays cheap. The doc asked for research threads R1–R10 to be resolved before any code. This plan records those findings, the stack decisions the user approved, and the build order. The user wants it **hosted locally first, then on GCP Cloud Run**. The repo is `C:\Users\chern\Documents\rs-mcp`, an empty git repo.

**Decisions approved by the user (2026-10-16):**
- **Python + FastMCP**, not TypeScript.
- **Group Ironmen Tracker plugin** for Phase 3, not a custom RuneLite plugin.
- Local testing with **MCP Inspector + Claude Code**, plus an on-demand **Cloudflare quick tunnel** for claude.ai.
- **Google OAuth** through FastMCP's OAuth proxy. The static-header beta isn't assumed.

---

## Research findings (written to `RESEARCH.md` as step 1, with sources)

**R1 claude.ai connectors** ([claude.com/docs/connectors/building](https://claude.com/docs/connectors/building), [.../authentication](https://claude.com/docs/connectors/building/authentication), [.../custom/remote-mcp](https://claude.com/docs/connectors/custom/remote-mcp))
- Custom connectors work on Free (one connector), Pro, Max, Team and Enterprise. On Pro/Max you add them under Customize > Connectors > Add custom connector. They work in web, Desktop and mobile, which share the same backend. Calls come from `160.79.104.0/21`.
- Transport: Streamable HTTP (legacy SSE also supported). The URL has to be public HTTPS.
- Auth: authless; OAuth via **CIMD** ("Use Claude's published identity", recommended), **DCR** ("Register automatically") or your own client ID/secret. Static request headers are a limited beta. Claude's OAuth client follows the **2025-03-26, 2025-06-18 and 2025-11-25** auth specs.
- OAuth specifics:
  - Callback is `https://claude.ai/api/mcp/auth_callback`.
  - PKCE S256 is required.
  - An unauthenticated request must get `401` + `WWW-Authenticate: Bearer resource_metadata=…`. Claude ignores that header on a 200.
  - Claude uses only `authorization_servers[0]`.
  - CIMD is used only when the AS advertises `client_id_metadata_document_supported: true` **and** `none` in `token_endpoint_auth_methods_supported`.
  - Refresh happens on a 401 or within 5 minutes of expiry. Return `invalid_grant` for a dead refresh token. `/token` must accept form-urlencoded.
  - OAuth endpoints get 10 s to respond; refresh gets 30 s.
- Limits: tool result about **150,000 characters** (Claude Code: 25k tokens); tool call timeout **240 s**. Resource subscriptions and sampling aren't supported. Auth settings can't be edited after adding a connector (remove and re-add).
- Projects: not stated. Connectors are toggled per chat with **+ > Connectors**. Verify in-Project use during Phase 1 E2E.

**R2 MCP spec + SDKs**
- Current spec: **2026-07-28**. It's stateless: no `initialize`, no `Mcp-Session-Id`, new `server/discover`, `resultType` on results, and DCR deprecated in favour of CIMD. claude.ai's client still speaks 2025-era revisions, so the server **must answer both**.
- TS SDK v2 (`@modelcontextprotocol/server` 2.3.1) answers legacy clients statelessly, but its authorization-server helpers (`mcpAuthRouter`, `ProxyOAuthServerProvider`) are frozen in `server-legacy`, with advice to "use a dedicated identity provider". That's the main reason for switching stacks.
- **FastMCP 4.1.0** (Python, 2026-10-08, built on Python SDK v2) "answers both the sessionless 2026-07-28 protocol and the older session-based handshake". Its `GoogleProvider`/`OAuthProxy` offers CIMD (on by default) and DCR to clients, issues its own refresh tokens mapped to Google's, and persists state through `client_storage` (py-key-value: FileTree/Disk, Redis, Firestore, and others, wrapped with Fernet encryption) plus a fixed `jwt_signing_key`.

**R3 Tool design** ([Anthropic, "Writing effective tools for agents"](https://www.anthropic.com/engineering/writing-tools-for-agents))
- Use few, task-shaped tools rather than API passthrough.
- Return human-meaningful names, not IDs.
- Filter and paginate by default and keep output concise.
- Write descriptions that state the source and how fresh the data is.
- Return errors as actionable text.
- Namespacing isn't needed because there's a single server.

**R4 Cloud Run**
- claude.ai can't present Google IAM tokens, so the service needs `--allow-unauthenticated` with in-app OAuth.
- The 240 s tool timeout makes min-instances=0 fine; a Python cold start is a few seconds. Start at min=0 and measure.
- The free tier is roughly 180k vCPU-s, 360k GiB-s and 2M requests a month (re-check the pricing page at deploy time). No custom domain is needed.
- **Outbound blocking is unconfirmed:** the hiscores sit behind Cloudflare (the 404 page carries a CF challenge script). Probe from a GCP IP before relying on it (see the open items).

**R5 Hiscores (verified live)**
- **JSON exists**: `https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player=<rsn>` returns `{name, skills:[{id,name,rank,level,xp}], activities:[{id,name,rank,score}]}`. It includes **Sailing** (24 skills + Overall) and 91 named activities, so there's no positional CSV parsing.
- Unranked is shown as `rank:-1` / `score:-1`. Unknown player gives an HTML **404**.
- Account types use the path segment: `hiscore_oldschool`, `_ironman`, `_hardcore_ironman`, `_ultimate`, `_deadman`, `_seasonal`, plus `_fresh_start` and `_tournament` (to verify).

**R6 WikiSync / Wiki (verified live)**
- `GET https://sync.runescape.wiki/runelite/player/<rsn>/STANDARD` returns `{username, timestamp, quests:{name:0|1|2}, achievement_diaries:{Region:{Tier:{complete,tasks:[bool…]}}}, levels:{Skill:int}, music_tracks, combat_achievements:[completed task ids], collection_log:[], sea_charting:[ids], league_tasks, …}`.
- If there's no data it returns `400 {"code":"NO_USER_DATA"}`.
- Quest states 0/1/2 are confirmed by `weirdgloop/wikisync-api` `QuestCompletionState.ts`. Allowed profiles are `STANDARD` and the current league.
- **Requirement data exists:**
  - `Module:Questreq/data` (`?action=raw`) is a structured Lua table of quest prerequisites and skill requirements, about 47 KB.
  - The Bucket API (`api.php?action=bucket&query=bucket('combat_achievement').select('id','name','tier','monster','task','type').run()`) maps CA ids to name, tier, monster and description.
  - Diary task text is in each `<Region>_Diary?action=raw` table (`data-diary-name/tier`), in task order, with skill requirements in `{{DiarySkillStats}}`.
- Wiki policy: no explicit rate limit, but a descriptive User-Agent with contact info is required. Default UAs (python-requests, curl, Java…) are blocked.

**R7 Wise Old Man (verified live)**
- Base: `https://api.wiseoldman.net/v2`. Endpoints: `GET /players/<rsn>`, `GET /players/<rsn>/gained?period=day|week|month|year`, `POST /players/<rsn>` (update).
- Rate limit: **20 requests / 60 s** without a key (`ratelimit-*` headers seen), 100 with an `x-api-key` from their Discord. Send a UA, and keep player updates to every 1–6 hours.

**R8 Existing export tools**
- **Group Ironmen Tracker** (`christoabrown/group-ironmen-tracker`, on the Plugin Hub, pushed 2026-07-20) has a `baseUrlOverride` setting.
  - Every few seconds it POSTs only the changed fields to `{base}/api/group/{group}/update-group-member`, with header `Authorization: <token>` (raw, no "Bearer").
  - Fields: `name, inventory, bank, equipment, shared_bank, rune_pouch, quiver, seed_vault, potion_storage, skills, quests, diary_vars, stats, coordinates, interacting, deposited, collection_log_v2`. Item containers are a flat `[id, qty, id, qty, ...]` list; inventory (28 slots) and equipment (14) are positional with `0,0` for empty slots, rune_pouch is always 4 pairs, quiver 1 pair. Ids are canonicalised (noted becomes unnoted) and bank placeholders are dropped. Shapes are pinned in `tests/fixtures/gim_update_*.json` (derived from source at plugin commit 343af95).
  - Before POSTing it calls `GET …/am-i-in-group?member_name=<rsn>`, and a 2xx means "member". There's no account-type check.
- Others (Dink = event webhooks only; Data Exporter, Bank Memory, Claude Export = local files; Bank Ledger Uploader = third-party site) are worse fits.

**R9 RuneLite rules**
- Using a Plugin Hub plugin pointed at our own server avoids building and running a developer-mode client (and the Jagex-account dev-mode hassle).
- We need no Jagex credentials. The plugin only transmits the client's own game state to a URL the user chooses.

**R10 OAuth design**
- Google supports neither DCR nor CIMD.
- FastMCP `GoogleProvider` sits in between as the AS: Claude sees CIMD/DCR, and users log in at Google with the scopes `openid` and `userinfo.email`.
- We enforce a **single-email allowlist** ourselves on every authenticated request; the provider has no built-in allowlist.
- The Google OAuth consent screen must be **External + In production**. "Testing" status expires refresh tokens after 7 days. Basic scopes need no verification, so confirm in the console.

---

## Stack

- **Python 3.14**, **uv** for the project and lockfile.
- **fastmcp ~=4.1**: tools, Streamable HTTP, and `@mcp.custom_route` for `/healthz` and the ingest endpoints.
- **httpx** (async) for outbound calls, **pydantic v2** for output models and ingest validation, **pydantic-settings** for config.
- **rapidfuzz** for item-name matching.
- **py-key-value-aio**: `FileTreeStore` locally, `FirestoreStore` on GCP. One `Store` abstraction backs both OAuth client storage and snapshots.
- **pytest + pytest-asyncio + respx**, with fixtures captured from real responses. **ruff** for lint and format.
- Docker (python:3.14-slim + uv) → Artifact Registry → Cloud Run (us-west1). Secret Manager for secrets, Firestore Native for state.

## Repo layout

```
rs-mcp/
  pyproject.toml  uv.lock  .env.example  README.md  RESEARCH.md  docs/PLAN.md
  src/osrs_mcp/
    server.py        # FastMCP app, auth wiring, /healthz, ingest routes; `python -m osrs_mcp`
    config.py        # pydantic-settings: DEFAULT_RSN, ACCOUNT_TYPE, USER_AGENT, STORE_*, OAuth vars, INGEST_TOKEN, GIM_GROUP
    http.py          # shared AsyncClient: UA, 10s timeout, 3x backoff on 429/5xx, TTL cache keyed by URL
    errors.py        # ToolError helpers -> actionable text
    clients/  hiscores.py  wikisync.py  wom.py  prices.py  wiki.py
    reference/  loader.py  data/{quest_reqs,diary_tasks,combat_achievements}.json
    tools/  stats.py  quests.py  diaries.py  combat.py  gains.py  prices.py  bank.py
    auth.py          # GoogleProvider + email allowlist (Phase 2)
    store.py         # key-value store factory (FileTree | Firestore)
    ingest/gim.py    # GIM-tracker-compatible endpoints + payload models (Phase 3)
  scripts/refresh_reference_data.py   # pulls Questreq Lua, diary pages, CA bucket -> data/*.json
  tests/  fixtures/  test_*.py
  Dockerfile  deploy/deploy.sh  .claude/launch.json
```

## Tools (final set; every output carries `source` and `data_as_of`/`fetched_at`)

Every tool takes an optional `rsn`, defaulting to `DEFAULT_RSN`. Hiscore tools also take `account_type`. Errors come back as text, for example "not on the ironman hiscores, so try account_type=regular, or check for a name change", or "No WikiSync data: enable the WikiSync plugin in RuneLite and log in once."

| Tool | Source / TTL | Key inputs | Output (compact JSON) |
|---|---|---|---|
| `get_stats` | Hiscores, 5 min | rsn, account_type | total_level, total_xp, combat_level (computed), skills[{skill, level, xp, rank\|null, xp_to_next, xp_to_99}], sorted by xp_to_99 asc; `closest_99` |
| `get_activity_scores` | Hiscores, 5 min | category: bosses\|clues\|minigames\|all; include_unranked=false | [{name, score, rank}] for ranked entries only |
| `get_quest_progress` | WikiSync + Questreq | status: completed\|in_progress\|not_started\|incomplete(default)\|all; startable_only=false (sorted by the Wiki's Optimal quest guide); rewards_skill?; quest? (single quest + route) | counts; quests[{name, status, miniquest, optimal_order, can_start, missing:{skills[{skill,need,have}], quests[]}}] |
| `get_diary_progress` | WikiSync + diary data | region?, tier? | per region/tier {complete, done/total}; when a region/tier is given, incomplete tasks[{text}] + skill reqs vs current levels |
| `get_combat_achievements` | WikiSync + CA bucket | tier?, monster?, status=incomplete, limit=50 | per-tier done/total; tasks[{name, tier, monster, type, description}] |
| `get_collection_log` | WikiSync `collection_log` (obtained ids, after the in-game WikiSync button) + the RuneLite `collection_log` snapshot (quantities) + Module:Collection log | tab?, page?, limit=15 | obtained/total; by_tab; pages[{page, tab, obtained, total}]; most_common_missing[{name, pages, completion_pct}]; page_details when a page is given |
| `get_gains` | WOM, 5 min | period: day\|week\|month\|year | non-zero skill xp/level gains, boss/activity gains, ehp/ehb, window start/end |
| `refresh_gains_tracking` | WOM POST | rsn | Updated snapshot time. Description says it's rate-limited (20/min) and should be used rarely |
| `get_item_price` | Wiki prices: latest 60 s, mapping 24 h | query, limit=5 | matches[{name, id, high, high_time, low, low_time, buy_limit, high_alch, members}] |
| `get_bank` (P3) | GIM snapshot | search?, min_value?, sort=value\|qty\|name, limit=50 | updated_at, total_value, item_count, items[{name, qty, unit_price, value}] |
| `get_equipment_and_inventory` (P3) | GIM snapshot | none | per section updated_at + age_minutes; equipment by slot, inventory, rune_pouch, quiver |

Tool annotations: all tools are `readOnlyHint: true` except `refresh_gains_tracking`. Tool order is deterministic.

---

## Build order

**Step 0. Research record.** Write `RESEARCH.md` (the findings above, with sources) and `docs/PLAN.md` (this plan). Commit.

**Phase 1. Public data, local** (done when every tool works from MCP Inspector and Claude Code against `http://localhost:8000/mcp`)
1. Scaffold with uv: `pyproject.toml`, ruff, pytest, `.env.example`, `python -m osrs_mcp` with Streamable HTTP on `PORT` (default 8000), and `/healthz`.
2. `http.py`, with tests for UA, retry and cache.
3. Capture fixtures with a script that uses the real UA: hiscores JSON (regular and one unknown-player 404), WikiSync, WOM player/gained, prices mapping/latest. Then the clients and their parsers, unit-tested against those fixtures.
4. `scripts/refresh_reference_data.py`, then build `data/*.json`. Test that diary task counts match WikiSync array lengths for every region/tier, and that every Questreq quest name matches a WikiSync quest key (report mismatches).
5. Tools, one module at a time, each with a unit test on the tool's output shape.
6. Local E2E: `npx @modelcontextprotocol/inspector`; `claude mcp add --transport http osrs http://localhost:8000/mcp`; ask "what's my closest 99?".
7. Optional claude.ai check: `cloudflared tunnel --url http://localhost:8000`, then add `https://<random>.trycloudflare.com/mcp` as a custom connector. **Only while the server serves public data and the tunnel is up**; tear it down afterwards.

**Phase 2. OAuth (still local, then carried to GCP)**
1. The user creates a Google OAuth client (Web), with redirect `http://localhost:8000/auth/callback` (later the Cloud Run URL), and sets the consent screen to External and In production.
2. `auth.py`: `GoogleProvider(client_id, client_secret, base_url, required_scopes=["openid","https://www.googleapis.com/auth/userinfo.email"], jwt_signing_key, client_storage=Fernet(store))`. Confirm in FastMCP 4.1 docs how to read the verified email, then add middleware that rejects any token whose email ≠ `ALLOWED_EMAIL`. Fail closed if `ALLOWED_EMAIL` is unset.
3. Tests:
   - A request with no token gets 401 + `resource_metadata`.
   - `/.well-known/oauth-authorization-server` advertises `client_id_metadata_document_supported` and `none`.
   - A token for another email is rejected.
   - The whole flow works in Inspector and in Claude Code.

**Phase 3. Bank and gear via GIM Tracker (local first)**
1. `ingest/gim.py`:
   - `GET /api/group/{group}/am-i-in-group?member_name=` returns 200 only for `GIM_GROUP` + an allowlisted RSN + `Authorization == INGEST_TOKEN` (constant-time compare); everything else gets 401.
   - `POST …/update-group-member` does the same checks, enforces a body size limit, uses permissive pydantic models (unknown fields ignored and logged once), and **merges** partial updates into the `snapshots` collection, key `<normalised rsn>/<kind>`, with `updated_at`. `deposited` is added to the stored bank and `collection_log_v2` merged into a `collection_log` document. The per-second fields (`stats`, `coordinates`, `interacting`) are accepted but not stored.
   - These routes are outside the OAuth gate but protected by the ingest token.
2. Turn on debug logging in the plugin locally, capture real payloads into fixtures, and pin the models to them before trusting the shapes.
3. Configure the plugin: group name, token, base URL override `http://localhost:8000`. Open the bank and check the snapshot.
4. Build `get_bank` and `get_equipment_and_inventory`, joining with the prices mapping.

**Phase 4. GCP** ✅ Deployed 2026-10-17 to an existing billed personal project, set as `GCP_PROJECT` in `.env` (the billing account was at its 5-project limit, so the dedicated project couldn't be billed; a quota increase is requested). Service on its `run.app` URL, later behind a custom domain. Verified: one container on the commit-tagged image, dedicated service account, env names, `/health`, 401 on `/mcp`, and a real Google sign-in with tool calls reaching every data source from Cloud Run. Egress was checked from Cloud Run itself rather than Cloud Shell. A $5 budget alert covers the project.
1. **Egress probe first:** from Cloud Shell (a GCP IP), curl the hiscores JSON, WikiSync, WOM and prices with the real UA. If any are blocked, stop and discuss (options: a different region, or a relay).
2. `Dockerfile` (uv, non-root). Write `deploy/deploy.sh` to be idempotent:
   - Enable the APIs, create the Artifact Registry repo, and create the service account (`secretmanager.secretAccessor`, `datastore.user`).
   - Create the secrets: Google client secret, JWT signing key, Fernet key, ingest token.
   - `gcloud run deploy --region us-west1 --allow-unauthenticated --min-instances 0 --max-instances 1 --memory 512Mi`, with secrets as env and `STORE_BACKEND=firestore`.
   - Read back and keep any env that was set out of band.
   - Use max-instances=1 so FastMCP's per-process replay state stays consistent.
3. Add the Cloud Run redirect URI to the Google client. Point the plugin's base URL at Cloud Run.
4. After deploying, check the shape (`gcloud run services describe`: one container, expected image digest, env/secrets present), then run `/healthz`.
5. E2E in claude.ai web and mobile and inside the user's OSRS Project:
   - "What are my stats?", "Which diaries can I finish now?", "What did I gain this week?", "What's in my bank?"
   - Removing and re-adding the connector re-runs the OAuth flow.
   - A different Google account is rejected.

## Verification summary
- `uv run pytest` (parsers against real fixtures; reference-data consistency; auth rejections; ingest auth and merge) and `uv run ruff check`.
- Local: Inspector exercises every tool. Claude Code end to end. An optional claude.ai test over the quick tunnel.
- GCP: egress probe, then post-deploy shape check, then claude.ai web + mobile + Project E2E. When reporting results I'll keep "builds/tests pass" separate from "ran against the real thing".

## Open items for the user to decide or confirm
1. **Your RSN and account type** (regular, ironman, …), for `DEFAULT_RSN`. Also: do you want a WOM API key (ask on their Discord)? Optional.
2. ~~**GCP egress to the hiscores and Wiki**~~ Confirmed from Cloud Run on 2026-10-17: every source answered.
3. **Google consent screen "In production" without verification** for basic scopes: please confirm in the console when you create the client, rather than relying on docs.
4. **Connectors inside claude.ai Projects** isn't documented. We'll confirm it during E2E.
5. ~~**GIM Tracker payload details**~~ Confirmed by a live capture on 2026-10-17 (via `INGEST_CAPTURE_DIR`): the real plugin's updates match the source-derived fixtures. Empty rune pouch slots arrive as id `-1`. Still unseen live: the collection log field. Untradeable items are named from the Wiki item list (`item_names.json`).
6. ~~**Quest points**~~ Done 2026-10-17: not in WikiSync, so `quest_points.json` holds each quest's QP from the Wiki's `{{Quest rewards}}` (checked against the Wiki's 343 total) and the tools sum completed quests, plus 1 for Tutorial Island (worth 1 QP since the 22 October 2025 update; WikiSync doesn't track it).
