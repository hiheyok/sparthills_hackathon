# Architecture

![Architecture](architecture.png)

## The one idea

**Use a model where language and judgment are needed. Use plain code where the answer must be exact and repeatable.**

| Job | Done by | Why |
|---|---|---|
| Turn a messy, stressed request into a list of supplies, including needs the person did not say | Amazon Bedrock | This is language understanding. |
| Decide which substitutes do the same job | Amazon Bedrock, inside hard limits set by code | This is judgment, but a wrong answer can hurt someone, so code limits the choices. |
| Write the short briefing and answer follow-up questions | Amazon Bedrock | This is writing. |
| Distance, travel time, opening hours at arrival, stock age | Code (`engine.py`) | Must be correct and testable. A model would guess. |
| Choose stops and their order | Code (`engine.py`) | Must be repeatable and explainable. |
| Draw the actual road route on the map | OpenStreetMap routing (OSRM), called by code | Real road geometry needs a road network. The model is not a router. |
| What may be substituted | Code (catalog `policy`) | Safety rule. The model cannot override it. |

## Request flow

Driving and walking are two lanes over the same stores. The browser understands the request once, then starts both lanes concurrently. The foreground lane calls `search`, `alternatives` when gaps exist, and `brief`. The background lane calls `alternatives` and `brief`; both endpoints search again on the server, so it needs no separate `search` request. Each lane gets its own substitutes and briefing calls with its travel mode. Switching to a completed lane needs no new planning calls; a lane still building shows a loading state. The assistant reads the situation on the server and can answer before either lane is ready.

A background failure marks that lane "unavailable" with a retry and leaves the foreground lane alone. A foreground failure invalidates the search, clears both lanes, and shows the main retry notice. Failure isolation is therefore asymmetric.

```
understand ─┬─ foreground: search → alternatives if gaps → brief
            ├─ background: alternatives → brief (search runs inside both endpoints)
            └─ assistant: /api/followup on demand, from server-rebuilt plan facts

ready plans → /api/route for the selected lane, then the other lane
```

The foreground progress follows these four stages; the alternatives call is skipped when search finds no gaps. The four AI tasks are understand, alternatives, brief and followup; search is plain code.

1. `POST /api/understand` (Bedrock). Free text in, structured needs out. The model may only use catalog item IDs. Output is parsed, cleaned, and merged with a keyword safety net for 911 situations.
2. `POST /api/search` (code). For each need: stores within the radius, stock at the arrival time, opening hours with a 15 minute closing buffer, snapshot age and confidence, and neighbor reports that override older snapshots.
3. `POST /api/alternatives` (code and Bedrock). Repeats the search for that travel mode. If gaps remain, the model receives an in-stock candidate pool and each missing item's policy and, for `caution` items, its allowlist. Its answer is re-validated in `ai.py` and again in `engine.build_plan`. When there are no gaps the endpoint returns without calling Bedrock.
4. `POST /api/brief` (Bedrock). Writes the briefing from the finished plan. The route itself was already fixed by code.

After the plan is shown, the browser draws a straight dashed line through its pickup stops and calls `POST /api/route` (code, plus a public routing service) for road geometry in that pickup order. Once the map provider is ready, it displays those overlays. If routing fails the dashed line stays and the app says so. The road route is saved with the plan for offline reading. Map pin numbers instead show straight-line distance rank among displayed candidates, including nearest-stock suggestions outside the search radius; they do not identify pickup order. **Pickup route** lists the actual stop sequence.

Search, alternatives, briefing, routing and follow-up take `mode` (`driving`, the default, or `walking`); understand is shared across modes. `POST /api/plan` is the combined planning endpoint used by `scripts/smoke.py`: it understands once, then calls alternatives and brief, whose server-side searches supply the stock facts. With `"modes": ["driving", "walking"]` it runs those lanes on separate threads, each with a copy of the request context so the Bedrock deadline still applies, and adds `lanes`. It does not fetch road geometry. `POST /api/followup` rebuilds both plans on the server from the supplied location, validated needs and alternatives. The current dialog sends those structured fields, requires `grounded: true`, and renders the answer as text. Older API clients can also supply `plan_text` as context. `POST /api/report` stores a neighbor report. `GET /api/meta` supplies neighborhood choices, catalog categories, demo opening schedules, map configuration and AI status.

## Amazon Bedrock use

* **API:** Converse, with a system prompt per stage, low temperature for the structured stages, and a token cap.
* **One agent per task:** `ai.AGENTS` gives `understand`, `alternatives`, `brief` and `followup` each their own output size, temperature and time budget, and `BEDROCK_MODEL_IDS_<TASK>` gives one its own model chain. These are task-specific Converse calls orchestrated by the application, rather than resources in the managed Amazon Bedrock Agents service. Model health (not enabled, throttled) is shared by model across tasks. Traces carry the agent and, for the briefing and substitutes agents, the lane (`driving` or `walking`).
* **Model chain:** Claude Haiku 4.5 (US inference profile), Nova 2 Lite (US profile), Nova 2 Lite (in region), Nova Lite (in region). The chain is configurable with `BEDROCK_MODEL_IDS`.
* **Reliability:** one JSON repair retry per model, then the next model. Access and validation errors are remembered for 15 minutes, throttling for 20 seconds. With no credentials the app skips Bedrock for 60 seconds. Time is bounded by the request, not just the stage: a stage gets at most 24 seconds, and never more than the Lambda invocation has left minus a 3 second reserve for the fallback and the reply (the handler passes in `get_remaining_time_in_millis()`). Each call's SDK timeout is cut to fit the time left, SDK retries are off (`total_max_attempts=1`), and no call or JSON repair starts with less than 5 seconds left, because Lambda ends at 29 seconds and API Gateway at 30.
* **Guardrails:** pass `GuardrailId` and the Converse call includes `guardrailConfig`. The template then adds `bedrock:ApplyGuardrail` on that guardrail to the function role (`tests/test_template.py` checks this, because the SDK request-shape tests cannot see IAM).
* **Rules fallback:** every AI task has a rules version implemented in `ai.py`, inside the same Lambda function. When it is used, the trace says `rules` and the reason. The UI never labels rules output as AI.
* **Trace:** each stage returns mode, model, milliseconds, token counts and the models it fell back from. The "Under the hood" panel shows this, and `scripts/smoke.py` prints it and fails if a stage fell back.

## Road routing

`app/roads.py` asks a public OSRM service (OpenStreetMap data) for the fastest route through the points, then returns the path, total time and distance. Driving uses the OSRM demo server, which only routes cars and ignores the profile in the URL. Walking uses the OpenStreetMap community's foot-profile server (`routing.openstreetmap.de/routed-foot`), which can use footpaths and crossings. The browser fetches the selected lane's path first, then the other lane's. Each mode has its own cache entries, and a lock guards the cache against concurrent server requests. Design choices:

`app.js` also calls `/api/route` with two points for the nearest-store card and browsed or selected store popups, for both driving and walking. All these requests share one queue per page. Whole-plan routes have higher priority than store previews; an already-running request finishes before the next starts. Stale queued work from an older search is skipped. Available first-leg results are reused, and duplicate per-store requests are shared. Browser travel-cache entries are reused for 10 minutes for routed results and 1 minute for estimates; those values and the selected store are included in saved plans. This queue does not enforce a global requests-per-second limit across users.

* **Called from the Lambda.** The browser posts coordinates to `/api/route`, and the server calls OSRM; OSRM receives the server's connection and route coordinates. Map display has separate browser connections to Google Maps when configured, or OpenStreetMap tiles otherwise. The CSP permits those map-provider domains alongside `'self'`.
* **Fails soft.** Timeouts, errors and odd answers all become "unavailable". The endpoint still returns 200 with `available: false`, so the browser quietly keeps the straight line.
* **Validated and cached.** 2 to 8 numeric points, each hop at most 80 km driving or 15 km walking, an in-memory cache bounded by 200 routes and 100,000 total path vertices for 10 minutes, and the shared per-IP rate limit.
* **Swappable.** `fetch_route()` is the only place that knows about OSRM. Amazon Location Service `CalculateRoutes` could replace it with the same return shape. `ROAD_ROUTING=off` disables it.
* **Not the source of truth for the plan.** Stop order, arrival times and opening-hours checks still come from `engine.py`. The road time on the map is shown as information and can differ from the plan's estimate.
* **Transport.** `road_transport.py` verifies HTTPS certificates. On Windows, a Python connection failure can fall back to native PowerShell HTTPS. On macOS, when Python has no default trusted roots and no custom CA environment settings, it loads `/etc/ssl/cert.pem` if available.

## Safety model

Catalog policy per item: `free`, `caution` (hand-written `safe_subs` allowlist, which may be empty) or `none` (never substituted, with an `escalate` message such as "ask a pharmacist").

Enforced in three places so one mistake does not become a harmful answer:

1. **Prompt.** The model is told the rules.
2. **`ai._alt_validator`.** Drops anything that breaks the policy or the allowlist and fills the caution text from the catalog.
3. **`engine.build_plan`.** Re-validates whatever arrives, including from the browser, drops options with no stock, and computes the verdict itself.

Found and fixed during testing: a client-supplied verdict was trusted, the fallback offered baby food pouches for formula, and a 20 lb propane tank was allowed as a 1 lb substitute. Each now has a test.

## Route planning

1. **Rank candidates.** Search scores estimated travel minutes plus penalties for stock freshness, insufficient quantity, limited operations and place type. `ACQUISITION_PENALTY` adds 6 minutes for distribution points, 10 for food banks, 16 for shelters and 24 for cooling/charging centers; ordinary stores add none. These are heuristics, not observed reliability measurements. A sufficiently closer relief site can still rank first. The UI's distance-ranked map browser is separate from this recommendation score.
2. **Cover.** Cheapest insertion set cover, at most 4 stops. Selection weighs urgent units per added travel/shopping cost, rewards bundling multiple needs at one stop (`BUNDLE_BONUS = 0.6`), and includes the place-type penalty. Every candidate is re-simulated so the store is open when you arrive. Quantities can be split across stops when no store can fill the whole request; unresolved items and partial quantities remain visible.
3. **Reorder.** Try all orders of the chosen stops and pick the lowest cost, where cost is travel minutes plus 0.1 times delay weighted by urgency (priority 1 weighs 3.0, priority 2 weighs 1.0, priority 3 weighs 0). This favors earlier arrivals for critical supplies while also limiting total travel.
4. **Time.** ETA is straight-line kilometers times a detour factor at a slow speed, plus 8 minutes per stop: 1.35 at 28 km/h by car, 1.25 at 4 km/h on foot. Walking plans only look within 3 km (`geo.WALK_MAX_KM`), and because every leg is simulated, a store that closes soon can be reachable by car and not on foot.

## Data

* `catalog.json`: 70 supplies with category, tags, aliases, policy and notes.
* `stores.json`: 63 fictional locations: 53 stores plus 10 crisis-relief sites (food banks, shelters, distribution points and cooling/charging centers) across 16 San José area neighborhoods, with hours and stock snapshots. Generated by `scripts/gen_data.py` with seed 2026. Panic-buying shortages affect stores around downtown (water, D batteries, 1 lb propane, powder formula and generators are out within 9 km), while relief sites provide additional demo options. Six stores are closed and eight have limited stock.
* Neighbor reports live in DynamoDB on AWS. Locally, `LocalReportStore` keeps them in memory and mirrors them to `local_data/reports.json` by default (`REPORTS_FILE` overrides the path). Recent reports are filtered to their 6-hour lifetime when read; DynamoDB also has TTL cleanup configured.
* To use real data, replace the loader in `engine.py` with a call to a retailer or inventory API. Nothing else depends on the file.

## AWS resources (`infra/template.yaml`)

| Resource | Notes |
|---|---|
| Lambda | Python 3.12, arm64, 512 MB, 29 s. Serves both the API and the static files. |
| API Gateway HTTP API | Payload v2, stage throttling (25 per second, burst 50). |
| DynamoDB | On demand, TTL on `ttl` (6 hours), server-side encryption. Keys: `store_id`, `item_id`. |
| CloudWatch Logs | 14 day retention. |
| IAM role | Basic Lambda logging, `bedrock:InvokeModel` and `InvokeModelWithResponseStream` on foundation models and inference profiles, `dynamodb:PutItem` and `Scan` on the one table. |
| S3 | A private bucket created by `deploy.sh` that only holds the code zip. |

Why one Lambda: for a prototype it is the least to deploy and explain. The handler already separates routing from logic, so splitting later is mechanical.

## Front end

Vanilla JavaScript, no build step. `maps.js` provides a shared interface for Google Maps and Leaflet. With `GOOGLE_MAPS_API_KEY` configured, the browser loads the Google Maps JavaScript API and map resources directly. Without a key, or if Google fails to load or authorize, it uses bundled Leaflet with OpenStreetMap tiles. Map rendering does not determine stock, stop order, arrival estimates or OSRM route geometry. `GOOGLE_MAPS_MAP_ID` configures the Google map; the local server reads both map settings from environment variables or `.env.local`.

The Haven-style interface adds checklist categories, a resizable desktop results panel, map recenter/fullscreen controls, distance-ranked store browsing and inventory popups. The homepage illustration is local markup; the live map is initialized when results open. `haven.css`, `haven-port.css` and `haven-home.css` layer the interface styles over `styles.css`. The installable manifest currently uses the display name **Haven**, while the webpage uses **SupplyLine**.

**Ask about plan** opens a dialog once the request is understood. It keeps the current question and answer when closed, resets them when the search or travel mode changes, and shows preliminary context while plans are building. Bedrock answers include model, region, time and token evidence; rules responses identify their source. Answers are inserted as text, not HTML.

Opening labels use the demo schedules supplied by `/api/meta` and plan store records. The browser recalculates Pacific-time labels every 15 seconds and when the page becomes visible, including for saved plans. This does not refresh inventory, fetch live business hours or recompute the pickup route.

The application scripts, style layers, Leaflet and fonts are served from the Lambda. A service worker caches the app shell. Local storage holds the last automatic plan (`supplyline.last.v1`), bookmarked snapshots (`haven.supply-lists.v1`), the preferred travel mode and desktop panel width. Plan snapshots include completed travel lanes, road geometry, per-store travel values, the selected store, categories and collected-pickup checkboxes. Saved cards derive labels from the location and request. There are no accounts, custom list names or cross-device sync.

API responses and third-party map resources are not cached by the service worker, so offline use provides saved plans and paths without fresh stock or a downloaded basemap. Content Security Policy is set by the handler, with Google map-provider domains allowed to support the optional integration.

## Hardening

Security headers and CSP (with `Referrer-Policy: strict-origin-when-cross-origin`, because OpenStreetMap blocks tile requests that carry no Referer), a 65,536-character JSON body limit, per-IP rate limit (90 POST API requests per minute per function instance), input length caps, path traversal protection for static files, and escaping of everything rendered in the browser. Unknown routes return 404 and wrong methods 405.

## Known gaps

No authentication, in-memory rate limiting (per Lambda instance, so it is a speed bump and not a wall), unverified neighbor reports, no live traffic, fictional data, and a road route that depends on a free public service with no uptime promise. See the README for the roadmap.
