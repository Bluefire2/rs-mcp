# Research findings

This answers the research threads R1–R10 from the original design doc (OSRS_MCP_PLAN.md). It was
done on 2026-10-16. Each item lists what was **confirmed**, what was **corrected** versus the
original doc, and its **sources**. "Verified live" means I fetched the endpoint and looked at the
real response. It doesn't mean I read about it.

Anything still unconfirmed is listed at the end.

---

## R1. claude.ai custom connectors (the blocker)

**Confirmed**
- Custom connectors (remote MCP by URL) are available on Free (limited to one), Pro, Max, Team and
  Enterprise. On Pro/Max you add them under **Customize > Connectors > Add custom connector**.
- One connector backend serves claude.ai web, Desktop, mobile, Cowork and Claude Code, so a
  connector added once works everywhere. Calls come from Anthropic's cloud (`160.79.104.0/21`), so
  the server must be on the public internet. **claude.ai can't reach `localhost`.**
- Transport: Streamable HTTP. The legacy HTTP+SSE transport also works, but it's deprecated.
- Auth options in the dialog:
  - **No sign-in** (authless)
  - OAuth with **"Use Claude's published identity"** (Client ID Metadata Document, CIMD; recommended)
  - OAuth with **"Register automatically"** (Dynamic Client Registration, DCR)
  - OAuth with **"Use your own OAuth client"** (pre-registered client ID, optional secret)
  - **Request headers** (static API key or bearer token). This is a beta limited to some
    organizations, so we don't depend on it.
- Claude's OAuth client follows the 2025-03-26, 2025-06-18 and 2025-11-25 authorization specs.
  The parts that matter for this server:
  - The redirect URI for hosted apps is `https://claude.ai/api/mcp/auth_callback`. Claude Code
    uses a loopback redirect on any port (`http://localhost/callback`, `http://127.0.0.1/callback`).
  - Every authorization request uses PKCE `S256`.
  - Sign-in starts only from a **401** carrying
    `WWW-Authenticate: Bearer resource_metadata="…"`. That header is ignored on a 200.
  - The protected resource metadata `resource` must exactly equal the URL the user enters.
    Only the **first** `authorization_servers` entry is used.
  - CIMD is chosen only when the authorization server metadata advertises
    `"client_id_metadata_document_supported": true` **and** `"none"` in
    `token_endpoint_auth_methods_supported`. Otherwise Claude falls back to DCR.
  - Refresh happens on a 401 or up to 5 minutes before expiry. A dead refresh token must get
    `invalid_grant`. Refresh tokens for public clients should rotate.
  - `/token` must accept `application/x-www-form-urlencoded`.
  - OAuth discovery, registration and token endpoints get 10 s; refresh gets 30 s.
- Limits:
  - Tool result: about **150,000 characters** on claude.ai and Desktop (25k tokens in Claude Code).
  - Tool call timeout: **240 s**.
  - Resource subscriptions and sampling aren't supported.
  - Auth settings can't be changed after a connector is added; you remove it and add it again.
- Connectors are switched on or off per chat with **+ > Connectors**. Individual tools can be
  blocked.

**Corrected**: The doc wondered whether claude.ai requires DCR. It doesn't. CIMD is now preferred,
DCR still works, and a manual client ID is also accepted.

**Unconfirmed**: Using connectors inside a claude.ai **Project** isn't mentioned in the docs. We'll
check it during end-to-end testing.

Sources:
- https://claude.com/docs/connectors/building
- https://claude.com/docs/connectors/building/authentication
- https://claude.com/docs/connectors/custom/remote-mcp
- https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- https://claude.com/docs/connectors/mcp-tunnels/overview (Enterprise only, so not usable here)

## R2. MCP spec and SDKs

**Confirmed**
- The current spec revision is **2026-07-28**, and it's a big change. MCP is now **stateless**:
  - The `initialize` handshake and `Mcp-Session-Id` header are removed.
  - Protocol version and capabilities ride in each request's `_meta`.
  - Servers must implement `server/discover`.
  - Results carry `resultType`.
  - DCR is **deprecated** in favour of CIMD.
- claude.ai's client documents only the 2025-era revisions, so the server must answer **both** the
  new stateless protocol and the older handshake.
- TypeScript SDK v2 (`@modelcontextprotocol/server` 2.3.1, plus `/express` and `/node` adapters)
  implements 2026-07-28 and serves legacy clients statelessly by default. **But** its
  authorization-server helpers (`mcpAuthRouter`, `ProxyOAuthServerProvider`) are frozen in
  `@modelcontextprotocol/server-legacy`, and the docs say "use a dedicated identity provider for
  new servers". v2 is a resource server only.
- **FastMCP 4.1.0** (Python, released 2026-10-08, built on Python SDK v2):
  - It "answers both the sessionless 2026-07-28 protocol and the older session-based handshake".
  - Its `GoogleProvider` / `OAuthProxy` acts as the authorization server for MCP clients and
    delegates login to Google. It offers **CIMD (on by default) and DCR** and issues its own
    refresh tokens mapped to Google's.
  - It persists state through `client_storage` (py-key-value stores, wrappable with Fernet
    encryption) plus a fixed `jwt_signing_key`.
  - It has a consent screen, and the allowed redirect URI patterns are configurable.

**Corrected**: Stack changed from TypeScript to **Python + FastMCP**, because the TS SDK no longer
gives us the authorization-server piece that Phase 2 needs. Approved by the user.

Sources:
- https://modelcontextprotocol.io/specification/latest
- https://modelcontextprotocol.io/specification/2026-07-28/changelog
- https://github.com/modelcontextprotocol/typescript-sdk
- https://ts.sdk.modelcontextprotocol.io/v2/serving/express.html
- https://ts.sdk.modelcontextprotocol.io/v2/serving/legacy-clients.html
- https://ts.sdk.modelcontextprotocol.io/v2/serving/authorization.html
- https://gofastmcp.com/integrations/google
- https://gofastmcp.com/servers/auth/oauth-proxy
- https://gofastmcp.com/servers/storage-backends
- https://gofastmcp.com/updates
- https://github.com/strawgate/py-key-value
- npm and PyPI registries, checked 2026-10-16

## R3. Tool design for LLM consumers

**Confirmed** (Anthropic, "Writing effective tools for agents"):
- Have a few task-shaped tools rather than one per API endpoint.
- Return meaningful names, not opaque IDs.
- Filter and paginate by default, and keep responses compact.
- Write descriptions as if for a new colleague: what the tool does, its data source, how fresh
  the data is, and its caveats.
- Return errors as actionable text.

We applied this in the tool set in [docs/PLAN.md](docs/PLAN.md):
- Combat achievement IDs and diary task indexes are resolved to names.
- Quests come back with "can start now" and the missing requirements already worked out.
- Large lists are filtered by default.

Source: https://www.anthropic.com/engineering/writing-tools-for-agents

## R4. Cloud Run fit

**Confirmed**
- claude.ai can't present Google IAM identity tokens, so the service must allow unauthenticated
  invocation, with OAuth handled in the app.
- The 240 s tool timeout is far above a Python cold start (a few seconds), so `min-instances=0`
  is fine to start with.
- No custom domain is needed: the `*.run.app` URL works as the connector URL and as the OAuth issuer.
- `max-instances=1` keeps FastMCP's per-process OAuth replay-protection state consistent, and it's
  plenty for one user.

**Unconfirmed**
- Free-tier numbers. As I recall them, roughly 180k vCPU-s, 360k GiB-s and 2M requests a month,
  but the pricing page didn't render for me. Re-check at deploy time.
- **Outbound blocking from GCP IPs.** The hiscores sit behind Cloudflare: the 404 page carries a
  Cloudflare challenge script. Every endpoint worked from a residential IP. We'll probe from
  Cloud Shell before deploying (Phase 4 step 1).

## R5. Official hiscores (verified live)

**Confirmed**
- **A JSON variant exists**: `GET https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player=<rsn>`.
  - It returns `{name, skills:[{id,name,rank,level,xp}], activities:[{id,name,rank,score}]}`.
  - Entries are **named**, so we don't depend on the CSV ordering.
  - It includes **Sailing**: 24 skills plus Overall, and 91 activities (clues, minigames, bosses,
    Collections Logged, and others).
- Unranked shows as `rank: -1` and `score: -1`. Unranked skills can show `level: 1, xp: 0`.
- An unknown player gets an HTML **404**.
- The CSV variant (`index_lite.ws`) still exists. We don't use it.

**Corrected**: Prefer JSON over CSV, as the doc hoped.

**Unconfirmed**
- The `_fresh_start` and `_tournament` endpoints were added from memory and need verifying.
- No documented rate limit. We cache for 5 minutes.

## R6. WikiSync and OSRS Wiki data (verified live)

**Confirmed**
- `GET https://sync.runescape.wiki/runelite/player/<rsn>/STANDARD` returns
  `{username, timestamp, quests, achievement_diaries, levels, music_tracks, combat_achievements,
  league_tasks, bingo_tasks, collection_log, collectionLogItemCount, sea_charting}`.
  - `quests`: `{ "<Quest name>": 0|1|2 }`, meaning not started, in progress, finished.
    This is confirmed by `weirdgloop/wikisync-api` `src/runelite/enum/QuestCompletionState.ts`.
  - `achievement_diaries`: `{ Region: { Easy|Medium|Hard|Elite: { complete: bool, tasks: [bool…] } } }`.
    Tasks are positional and unnamed.
  - `combat_achievements`: an array of **completed task IDs**.
  - `levels`: `{ Skill: level }`, as reported by the plugin, which isn't limited by hiscore rank.
  - `collection_log`: empty for the sampled players, so we don't rely on it.
- No data gets `400 {"code":"NO_USER_DATA","error":"No user data found."}`.
- Allowed profile types (from the API source): `STANDARD` and the current league.
- **Requirement data is available in usable form:**
  - Quest requirements: `https://oldschool.runescape.wiki/w/Module:Questreq/data?action=raw` is a
    structured Lua table, `{ [quest] = { quests = {…}, skills = { {skill, level, 'ironman'?, 'boostable'?} } } }`,
    about 47 KB.
  - Combat achievement names: the Bucket API.
    `api.php?action=bucket&format=json&query=bucket('combat_achievement').select('id','name','tier','monster','task','type').run()`
    gives the ID → name/tier/monster/description mapping.
  - Diary task text: each `<Region>_Diary?action=raw` page has one task table per tier, tagged
    `data-diary-name` / `data-diary-tier`, in task order. Tier skill requirements are in
    `{{DiarySkillStats}}`.
  - The Quest bucket's `requirements` field is rendered HTML/wikitext, which is harder to parse
    than `Questreq/data`.
- Wiki API policy: no explicit rate limit, but **a descriptive User-Agent with contact info is
  required**. Default UAs (python-requests, Python-urllib, curl, Java, …) are blocked. That
  applies to `prices.runescape.wiki` as well.
- Prices (verified live): `GET https://prices.runescape.wiki/api/v1/osrs/latest?id=4151` returns
  `{data:{"4151":{high,highTime,low,lowTime}}}`. `/mapping` and `/timeseries` are as the doc said.

**Unconfirmed**
- That diary table row order matches WikiSync's `tasks` index order. A test will check the counts
  for every region and tier, and we'll spot-check a few against a known account.

Sources:
- https://oldschool.runescape.wiki/w/RuneScape:WikiSync
- https://github.com/weirdgloop/wikisync-api
- https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices
- Live responses (above)

## R7. Wise Old Man (verified live)

**Confirmed**
- Base `https://api.wiseoldman.net/v2`.
  - `GET /players/<rsn>`: details plus `latestSnapshot`.
  - `GET /players/<rsn>/gained?period=day|week|month|year`: `{startsAt, endsAt,
    data:{skills:{<skill>:{experience:{gained,start,end}, level:{…}, rank:{…}, ehp:{…}}}, bosses, activities, computed}}`.
  - `POST /players/<rsn>`: triggers an update.
- Rate limit: **20 requests per 60 s** without a key (we saw `ratelimit-limit: 20` in the headers),
  or **100** with an API key sent as `x-api-key`. Keys come from their Discord.
- They ask for a User-Agent and for delays between requests. Periodic player updates should be
  every 1–6 hours.

Source: https://docs.wiseoldman.net/ and live responses

## R8. Existing tools that might replace Phase 3 work

**Chosen: Group Ironmen Tracker** (`christoabrown/group-ironmen-tracker`). It's on the Plugin Hub
and maintained: the plugin was pushed 2026-07-20 and the server 2026-10-14. From reading its
source:
- The config has `groupName`, `groupToken`, and a **`baseUrlOverride`**
  ("Server base URL override … Only change this if you are hosting your own server").
- `GET {base}/api/group/{group}/am-i-in-group?member_name=<rsn>`: a 2xx means "member".
- `POST {base}/api/group/{group}/update-group-member`: only the fields that changed. They're
  `name, inventory, bank, equipment, shared_bank, resources(?), skills, quests, coordinates,
  rune_pouch, quiver, interacting, deposited, seed_vault, potion_storage, diary_vars`, plus
  collection log items.
  - Item containers are a flat `[id, qty, id, qty, …]` list.
  - Auth header is `Authorization: <token>`, raw with no `Bearer` prefix.
  - On a 401 the plugin re-checks membership.
- There's no account-type check. The only thing skipped is "bad world types" (for example
  special game modes).

Alternatives considered:
- **Dink**: event webhooks only, with no bank snapshot.
- **Data Exporter, Bank Memory, Claude Export, Local Data Exporter**: write local files, with no
  network push.
- **Bank Ledger Uploader**: sends to a third-party site.

**Corrected**: There's no custom RuneLite plugin in Phase 3, so `runelite-plugin/` is dropped.
Approved by the user.

**Unconfirmed**: The exact field shapes come from reading the source, not from a live capture. A
live capture is the first step of Phase 3.

## R9. RuneLite plugin rules

- Because we use a Plugin Hub plugin, we don't build RuneLite from source or run a developer-mode
  client. That also avoids the extra developer-mode setup that Jagex accounts need.
- The plugin sends the client's own game state to a server URL the user configures. No Jagex
  credentials are involved anywhere, which satisfies the hard constraint.

## R10. OAuth design

- Google supports **neither DCR nor CIMD**.
- Design: FastMCP `GoogleProvider` (an OAuth proxy) is the authorization server Claude sees. It
  offers CIMD and DCR, and sends the user to Google to log in with the scopes `openid` and
  `userinfo.email`.
- We enforce a **one-email allowlist** on every authenticated request ourselves, because the
  provider has no built-in allowlist. If `ALLOWED_EMAIL` is unset, the server fails closed.
- State (client registrations and upstream tokens) goes in an encrypted key-value store: a local
  file tree, or Firestore on GCP. It uses a fixed `jwt_signing_key`, so tokens survive restarts
  and scale-to-zero.
- **Google consent screen:** must be External and **In production**. In "Testing" status Google
  expires refresh tokens after 7 days. Basic scopes shouldn't need verification. **Confirm in the
  Google Cloud console**, not from docs.
- Hosted alternatives (WorkOS, Auth0, Stytch) aren't needed.

---

## Still unconfirmed (to decide or check)

1. GCP egress to the hiscores, Wiki and WOM (Cloudflare). Probe from Cloud Shell.
2. Whether the Google consent screen can be "In production" without verification for basic scopes.
3. Connectors inside claude.ai Projects.
4. Group Ironmen Tracker payload shapes (pin them with a live capture).
5. Diary task order versus WikiSync indexes (checked by a test plus spot checks).
6. Quest points: not in WikiSync or `Questreq/data`. Low priority.
7. The `_fresh_start` and `_tournament` hiscore paths.
