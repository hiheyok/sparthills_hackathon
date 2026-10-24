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

The browser runs four calls in order, so the progress shown on screen is real and each stage can fail on its own.

1. `POST /api/understand` (Bedrock). Free text in, structured needs out. The model may only use catalog item IDs. Output is parsed, cleaned, and merged with a keyword safety net for 911 situations.
2. `POST /api/search` (code). For each need: stores within the radius, stock at the arrival time, opening hours with a 15 minute closing buffer, snapshot age and confidence, and neighbor reports that override older snapshots.
3. `POST /api/alternatives` (Bedrock). Only called for items nobody has. The model is shown the pool of substitutes that are **in stock nearby** and, for `caution` items, only the allowlist. Its answer is re-validated in `ai.py` and again in `engine.build_plan`.
4. `POST /api/brief` (Bedrock). Writes the briefing from the finished plan. The route itself was already fixed by code.

After the plan is shown, the browser draws the stops on the map at once with a straight dashed line, then calls `POST /api/route` (code, plus a public routing service) to swap in the fastest road route through those stops in the plan's order. If that call fails the dashed line stays and the app says so. The road route is saved with the plan so a saved plan shows it offline.

`POST /api/plan` runs all four in one call (used by `scripts/smoke.py`). `POST /api/followup` answers questions about the plan. `POST /api/report` stores a neighbor report.

## Amazon Bedrock use

* **API:** Converse, with a system prompt per stage, low temperature for the structured stages, and a token cap.
* **Model chain:** Claude Haiku 4.5 (US inference profile), Nova 2 Lite (US profile), Nova 2 Lite (in region), Nova Lite (in region). The chain is configurable with `BEDROCK_MODEL_IDS`.
* **Reliability:** one JSON repair retry per model, then the next model. Access and validation errors are remembered for 15 minutes, throttling for 20 seconds. With no credentials the app skips Bedrock for 60 seconds. Time is bounded by the request, not just the stage: a stage gets at most 24 seconds, and never more than the Lambda invocation has left minus a 3 second reserve for the fallback and the reply (the handler passes in `get_remaining_time_in_millis()`). Each call's SDK timeout is cut to fit the time left, SDK retries are off (`total_max_attempts=1`), and no call or JSON repair starts with less than 5 seconds left, because Lambda ends at 29 seconds and API Gateway at 30.
* **Guardrails:** pass `GuardrailId` and the Converse call includes `guardrailConfig`.
* **Rules fallback:** every stage has a rules version. When it is used, the trace says `rules` and the reason. The UI never labels rules output as AI.
* **Trace:** each stage returns mode, model, milliseconds, token counts and the models it fell back from. The "Under the hood" panel shows this, and `scripts/smoke.py` prints it and fails if a stage fell back.

## Road routing

`app/roads.py` asks the public OSRM demo server (OpenStreetMap data) for the fastest driving route through the points, then returns the path, total time and distance. Design choices:

* **Called from the Lambda, not the browser.** The person's IP address is not sent to a third party, and the Content Security Policy keeps `connect-src 'self'`.
* **Fails soft.** Timeouts, errors and odd answers all become "unavailable". The endpoint still returns 200 with `available: false`, so the browser quietly keeps the straight line.
* **Validated and cached.** 2 to 8 numeric points, each hop under 80 km, an in-memory cache of 200 routes for 10 minutes, and the shared per-IP rate limit.
* **Swappable.** `fetch_route()` is the only place that knows about OSRM. Amazon Location Service `CalculateRoutes` could replace it with the same return shape. `ROAD_ROUTING=off` disables it.
* **Not the source of truth for the plan.** Stop order, arrival times and opening-hours checks still come from `engine.py`. The road time on the map is shown as information and can differ from the plan's estimate.

## Safety model

Catalog policy per item: `free`, `caution` (hand-written `safe_subs` allowlist, which may be empty) or `none` (never substituted, with an `escalate` message such as "ask a pharmacist").

Enforced in three places so one mistake does not become a harmful answer:

1. **Prompt.** The model is told the rules.
2. **`ai._alt_validator`.** Drops anything that breaks the policy or the allowlist and fills the caution text from the catalog.
3. **`engine.build_plan`.** Re-validates whatever arrives, including from the browser, drops options with no stock, and computes the verdict itself.

Found and fixed during testing: a client-supplied verdict was trusted, the fallback offered baby food pouches for formula, and a 20 lb propane tank was allowed as a 1 lb substitute. Each now has a test.

## Route planning

1. **Cover.** Cheapest insertion set cover, at most 4 stops. Every candidate is re-simulated so the store is open when you arrive.
2. **Reorder.** Try all orders of the chosen stops and pick the lowest cost, where cost is drive minutes plus 0.1 times delay weighted by urgency (priority 1 weighs 3.0, priority 2 weighs 1.0, priority 3 weighs 0). Critical items such as insulin are reached first even if it is a longer drive.
3. **Time.** ETA is straight-line kilometers times 1.35 detour at 28 km/h, plus 8 minutes per stop.

## Data

* `catalog.json`: 70 supplies with category, tags, aliases, policy and notes.
* `stores.json`: 53 fictional stores in 16 San José area neighborhoods with hours and stock snapshots. Generated by `scripts/gen_data.py` with seed 2026. Panic-buying shortages are placed around downtown (water, D batteries, 1 lb propane, powder formula and generators are out within 9 km) so substitutes matter in the demo. Six stores are closed and eight have limited stock.
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

Vanilla JavaScript, no build step. Leaflet is vendored so the app does not depend on a CDN. A service worker caches the app shell, and the last plan is saved on the device so it can be read with no signal. Fonts are served from the Lambda. Content Security Policy is set by the handler.

## Hardening

Security headers and CSP (with `Referrer-Policy: strict-origin-when-cross-origin`, because OpenStreetMap blocks tile requests that carry no Referer), a 64 KB body limit, per-IP rate limit (90 per minute), input length caps, path traversal protection for static files, and escaping of everything rendered in the browser. Unknown routes return 404 and wrong methods 405.

## Known gaps

No authentication, in-memory rate limiting (per Lambda instance, so it is a speed bump and not a wall), unverified neighbor reports, no live traffic, fictional data, and a road route that depends on a free public service with no uptime promise. See the README for the roadmap.
