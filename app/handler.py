"""AWS Lambda entry point (API Gateway HTTP API, payload format 2.0).

One function serves both the web app (static files) and the JSON API, so the
whole product deploys as a single CloudFormation stack. local_server.py calls
the same handler() so local and cloud behave identically.
"""
import base64
import collections
import contextvars
import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ai
import engine
import roads
import store
from engine import CATALOG, CATALOG_LIST, PLACES, STORE_BY_ID, STORES
from geo import DEFAULT_MODE, MODES, WALK_MAX_KM, fmt_clock, haversine_km, reach_km

log = logging.getLogger("supplyline")
logging.getLogger().setLevel(logging.INFO)

STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8", ".svg": "image/svg+xml",
    ".woff2": "font/woff2", ".png": "image/png", ".txt": "text/plain; charset=utf-8",
}
BINARY = {".woff2", ".png"}
CSP = ("default-src 'self'; img-src 'self' data: blob: https://tile.openstreetmap.org; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; connect-src 'self'; "
       "manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin",  # OpenStreetMap blocks tile requests that carry no Referer
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=()",
    "Content-Security-Policy": CSP,
}


class HttpError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


# ---------------------------------------------------------------- plumbing
def _resp(status, body, ctype="application/json; charset=utf-8", cache="no-store", binary=False):
    headers = {"Content-Type": ctype, "Cache-Control": cache, **SECURITY_HEADERS}
    if binary:
        return {"statusCode": status, "headers": headers, "isBase64Encoded": True,
                "body": base64.b64encode(body).decode("ascii")}
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return {"statusCode": status, "headers": headers, "body": body}


_hits = collections.defaultdict(collections.deque)


def _rate_ok(ip, limit=90, window=60):
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    if len(_hits) > 5000:  # keep memory bounded in long-lived containers
        _hits.clear()
    return True


def _json_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw) > MAX_BODY:
        raise HttpError(413, "That request is too large.")
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        raise HttpError(400, "The request body must be JSON.")
    if not isinstance(d, dict):
        raise HttpError(400, "The request body must be a JSON object.")
    return d


def _mode(d):
    """How the person is getting there: "driving" (the default) or "walking"."""
    m = d.get("mode")
    if m is None or m == "":
        return DEFAULT_MODE
    if not isinstance(m, str) or m not in MODES:
        raise HttpError(400, "mode must be driving or walking.")
    return m


def _loc(d):
    try:
        lat, lng = float(d["lat"]), float(d["lng"])
    except (KeyError, TypeError, ValueError):
        raise HttpError(400, "lat and lng are required numbers.")
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        raise HttpError(400, "lat or lng is out of range.")
    try:
        radius = float(d.get("radius_km", 8))
        radius = radius if math.isfinite(radius) else 8.0
    except (TypeError, ValueError):
        radius = 8.0
    return lat, lng, max(1.0, min(50.0, radius))


def _ctx(d):
    lat, lng, radius = _loc(d)
    mode = _mode(d)
    now_ts = time.time()
    return {"lat": lat, "lng": lng, "mode": mode, "radius": reach_km(radius, mode),
            "now_min": engine.local_minutes(d.get("local_minutes")),
            "now_ts": now_ts, "reports": store.get_store().all_recent(now_ts)}


def _needs(d):
    needs, _ = ai.clean_needs(d.get("needs") or (d.get("intent") or {}).get("needs"))
    if not needs:
        raise HttpError(400, "No valid supply items were provided.")
    return needs


def _clean_alts(raw):
    """Alternatives come back from the browser: re-validate everything."""
    out = []
    for a in raw if isinstance(raw, list) else []:
        fid = a.get("for_item") if isinstance(a, dict) else None
        if fid not in CATALOG:
            continue
        policy = CATALOG[fid]["policy"]
        opts = []
        if policy != "none":
            for o in a.get("options") or []:
                iid = o.get("item_id") if isinstance(o, dict) else None
                if iid in CATALOG and iid != fid and not any(x["item_id"] == iid for x in opts):
                    opts.append({"item_id": iid,
                                 "fit": o.get("fit") if o.get("fit") in ("strong", "partial", "workaround") else "partial",
                                 "why": str(o.get("why") or "")[:220], "how_to_use": str(o.get("how_to_use") or "")[:220],
                                 "caution": str(o.get("caution") or "")[:220]})
        out.append({"for_item": fid, "options": opts[:3],
                    "verdict": "no_safe_substitute" if policy == "none" else ("substitutes_available" if opts else "none_found"),
                    "no_purchase_tips": [str(t)[:160] for t in (a.get("no_purchase_tips") or []) if isinstance(t, str)][:3],
                    "escalate": str(a.get("escalate") or "")[:300] or None})
    return out


# --------------------------------------------------------------- endpoints
def api_meta(_d):
    return {
        "places": PLACES,
        "catalog": [{"id": c["id"], "name": c["name"], "category": c["category"]} for c in CATALOG_LIST],
        "ai": ai.status(),
        "travel": {"modes": list(MODES), "walk_max_km": WALK_MAX_KM},
        "data": {"stores": len(STORES), "demo": True,
                 "note": "Stores, addresses and inventory are fictional demo data."},
    }


def api_understand(d):
    text = str(d.get("text") or "").strip()
    if len(text) < 3:
        raise HttpError(400, "Tell me a little about what you need.")
    intent, trace = ai.understand(text[:1500])
    if not intent["needs"]:
        raise HttpError(422, "I couldn't tell which supplies you need. Try naming a few items, like water, batteries or formula.",
                        {"intent": intent, "trace": trace})
    return {"intent": intent, "trace": trace}


def api_search(d):
    c, needs = _ctx(d), _needs(d)
    results = engine.search(needs, c["lat"], c["lng"], c["radius"], c["now_min"], c["reports"], c["now_ts"], c["mode"])
    return {"results": results, "gaps": [r["item_id"] for r in results if r["status"] == "gap"], "mode": c["mode"],
            "meta": {"stores_total": len(STORES), "radius_km": c["radius"], "mode": c["mode"]}}


def api_alternatives(d):
    c, needs = _ctx(d), _needs(d)
    intent = ai.clean_intent(d.get("intent") or {"needs": needs})
    results = engine.search(needs, c["lat"], c["lng"], c["radius"], c["now_min"], c["reports"], c["now_ts"], c["mode"])
    gaps = [{"item_id": r["item_id"], "label": r["label"], "priority": r["priority"], "why": r["why"]}
            for r in results if r["status"] == "gap"]
    if not gaps:
        return {"alternatives": [], "trace": None, "mode": c["mode"]}
    pool = engine.in_stock_nearby(c["lat"], c["lng"], c["radius"], c["now_min"], c["reports"], c["now_ts"],
                                  exclude={g["item_id"] for g in gaps}, mode=c["mode"])
    alts, trace = ai.alternatives(gaps, pool, intent, c["mode"])
    plan = engine.build_plan(needs, alts, c["lat"], c["lng"], c["radius"], c["now_min"], c["reports"], c["now_ts"], c["mode"])
    return {"alternatives": plan["alternatives"], "trace": trace, "mode": c["mode"]}


def api_brief(d):
    c = _ctx(d)
    intent = ai.clean_intent(d.get("intent") or {})
    if not intent["needs"]:
        raise HttpError(400, "No valid supply items were provided.")
    plan = engine.build_plan(intent["needs"], _clean_alts(d.get("alternatives")), c["lat"], c["lng"],
                             c["radius"], c["now_min"], c["reports"], c["now_ts"], c["mode"])
    brief, trace = ai.brief(plan, intent)
    urgent = bool(intent["urgent_safety"].get("flag"))
    return {"plan": plan, "brief": brief, "trace": trace, "urgent": urgent, "mode": c["mode"],
            "plan_text": engine.render_plan_text(plan, brief, urgent)}


def _modes(d):
    """The ways of travelling to plan for: "modes" lists them, "mode" alone means just that one."""
    raw = d.get("modes")
    if raw is None:
        return [_mode(d)]
    if not isinstance(raw, list) or not 1 <= len(raw) <= len(MODES):
        raise HttpError(400, "modes must list driving, walking or both.")
    out = []
    for m in raw:
        m = _mode({"mode": m})
        if m not in out:
            out.append(m)
    return out


def _run_lane(d, mode):
    """One way of travelling, start to finish: substitutes, then the plan and its briefing. Lanes
    read the same data and share nothing they change, so they can run side by side."""
    dm = dict(d, mode=mode)
    a = api_alternatives(dm)
    b = api_brief(dict(dm, alternatives=a["alternatives"]))
    return {"mode": mode, "plan": b["plan"], "brief": b["brief"], "plan_text": b["plan_text"], "urgent": b["urgent"],
            "alternatives": a["alternatives"], "traces": ([a["trace"]] if a["trace"] else []) + [b["trace"]]}


def api_plan(d):
    """Whole pipeline in one call (CLI, tests). Can take longer than API Gateway's
    30 s limit when several Bedrock calls are slow; the web app calls the stages
    separately instead so it can show live progress.

    With "modes": ["driving", "walking"] the lanes run at the same time, each with its own
    substitutes and briefing agents, so two plans take about as long as one."""
    modes = _modes(d)
    u = api_understand(d)
    d2 = dict(d, intent=u["intent"])
    if len(modes) == 1:
        lanes = {modes[0]: _run_lane(d2, modes[0])}
    else:
        with ThreadPoolExecutor(max_workers=len(modes)) as workers:
            # each lane gets its own copy of the request context, which carries the Bedrock deadline
            futures = {m: workers.submit(contextvars.copy_context().run, _run_lane, d2, m) for m in modes}
            lanes = {m: f.result() for m, f in futures.items()}
    first = lanes[modes[0]]
    out = {"intent": u["intent"], "plan": first["plan"], "brief": first["brief"], "plan_text": first["plan_text"],
           "urgent": first["urgent"], "traces": [u["trace"]] + [t for m in modes for t in lanes[m]["traces"]]}
    if len(modes) > 1:
        out["lanes"] = {m: {k: v for k, v in lane.items() if k != "traces"} for m, lane in lanes.items()}
    return out


def _situation(d, mode):
    """Everything the assistant may know about the person's situation right now, built here from the
    engine's own plans for both ways of travelling instead of taken on trust from the browser.
    None when the request does not say where the person is or what they need."""
    try:
        c = _ctx(dict(d, mode=DEFAULT_MODE))
    except HttpError:
        return None
    intent = ai.clean_intent(d.get("intent") or {})
    if not intent["needs"]:
        return None
    alts = d.get("alternatives") if isinstance(d.get("alternatives"), dict) else {}
    plans = {m: ai.plan_facts(engine.build_plan(intent["needs"], _clean_alts(alts.get(m)), c["lat"], c["lng"], c["radius"],
                                                c["now_min"], c["reports"], c["now_ts"], m)) for m in MODES}
    return {"local_time": fmt_clock(c["now_min"]), "selected_mode": mode, "walking_range_km": WALK_MAX_KM,
            "disaster": intent["disaster"], "household": intent["household"], "urgent_safety": intent["urgent_safety"],
            "needs": [{"item": CATALOG[n["item_id"]]["name"], "quantity": n["qty"], "priority": n["priority"]}
                      for n in intent["needs"]],
            "plans": plans}


def api_followup(d):
    q = str(d.get("question") or "").strip()[:400]
    if len(q) < 3:
        raise HttpError(400, "Type a question about your plan.")
    situation = _situation(d, _mode(d))
    ans, trace = ai.assistant(q, situation, str(d.get("plan_text") or "")[:6000])
    return {"answer": ans, "trace": trace, "grounded": situation is not None}


def api_report(d):
    sid, iid, status = d.get("store_id"), d.get("item_id"), d.get("status")
    if sid not in STORE_BY_ID:
        raise HttpError(400, "Unknown store.")
    if status not in store.VALID_STATUS:
        raise HttpError(400, "Status must be in_stock, low, out or closed.")
    if (status == "closed") != (iid == "*"):
        raise HttpError(400, "Use item_id '*' with status 'closed', and an item id for stock reports.")
    if iid != "*" and iid not in CATALOG:
        raise HttpError(400, "Unknown item.")
    store.get_store().put(sid, iid, status)
    return {"ok": True, "expires_in_hours": store.REPORT_TTL_S // 3600}


MAX_LEG_KM = 80        # the search radius is at most 50 km, so a longer hop is not a real plan
MAX_WALK_LEG_KM = 15   # walking plans stay within a few km; a longer walk is not a real plan either


def api_route(d):
    """The fastest route through the plan's points, in the order given, by car or on foot.
    Never an error for a routing outage: the browser falls back to a straight line."""
    mode = _mode(d)
    raw = d.get("points")
    if not isinstance(raw, list) or not (2 <= len(raw) <= roads.MAX_POINTS):
        raise HttpError(400, f"Send between 2 and {roads.MAX_POINTS} points.")
    pts = []
    for p in raw:
        try:
            lat, lng = float(p["lat"]), float(p["lng"])
        except (KeyError, TypeError, ValueError):
            raise HttpError(400, "Each point needs numeric lat and lng.")
        if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
            raise HttpError(400, "A point is out of range.")
        pts.append((lat, lng))
    limit = MAX_WALK_LEG_KM if mode == "walking" else MAX_LEG_KM
    if any(haversine_km(a[0], a[1], b[0], b[1]) > limit for a, b in zip(pts, pts[1:])):
        raise HttpError(400, "Those points are too far apart.")
    try:
        return {"available": True, **roads.fetch_route(pts, mode)}
    except roads.RoadsUnavailable as e:
        return {"available": False, "mode": mode, "reason": str(e), "code": e.code}


def api_health(_d):
    return {"ok": True, "ai": ai.status(), "stores": len(STORES), "catalog": len(CATALOG)}


POST_ROUTES = {
    "/api/understand": api_understand, "/api/search": api_search, "/api/alternatives": api_alternatives,
    "/api/brief": api_brief, "/api/plan": api_plan, "/api/followup": api_followup, "/api/report": api_report,
    "/api/route": api_route,
}
GET_ROUTES = {"/api/meta": api_meta, "/api/health": api_health}
STATIC_ALIASES = {"/": "index.html", "/index.html": "index.html", "/sw.js": "sw.js",
                  "/manifest.webmanifest": "manifest.webmanifest"}
NO_CACHE = {"index.html", "sw.js", "manifest.webmanifest", "app.js", "styles.css"}


def _static(path):
    rel = STATIC_ALIASES.get(path)
    if rel is None:
        m = re.fullmatch(r"/static/((?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+)", path)
        if not m:
            return None
        rel = m.group(1)
    f = (STATIC / rel).resolve()
    try:
        f.relative_to(STATIC.resolve())
    except ValueError:
        return None
    if not f.is_file() or f.suffix not in CONTENT_TYPES:
        return None
    ctype = CONTENT_TYPES[f.suffix]
    cache = "no-cache" if rel in NO_CACHE else "public, max-age=86400"
    if f.suffix in BINARY:
        return _resp(200, f.read_bytes(), ctype, cache, binary=True)
    return _resp(200, f.read_text(encoding="utf-8"), ctype, cache)


def handler(event, context=None):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or event.get("httpMethod") or "GET").upper()
    path = event.get("rawPath") or event.get("path") or "/"
    ip = http.get("sourceIp") or "local"
    # Bedrock calls may only use the time this invocation has left (minus a reserve for the fallback).
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    ai.set_deadline(remaining() / 1000.0 if callable(remaining) else None)
    try:
        if method == "GET":
            if path in GET_ROUTES:
                return _resp(200, GET_ROUTES[path]({}))
            s = _static(path)
            if s:
                return s
            raise HttpError(404, "Not found.")
        if method == "POST" and path in POST_ROUTES:
            if not _rate_ok(ip):
                raise HttpError(429, "Too many requests. Wait a minute and try again.")
            return _resp(200, POST_ROUTES[path](_json_body(event)))
        raise HttpError(404 if method in ("GET", "POST") else 405, "Not found.")
    except HttpError as e:
        return _resp(e.status, {"error": e.message, **e.extra})
    except Exception:  # noqa: BLE001
        log.exception("unhandled error on %s %s", method, path)
        return _resp(500, {"error": "Something went wrong on our side. Your last saved plan is still on this device."})
    finally:
        ai.set_deadline(None)
