"""AWS Lambda entry point (API Gateway HTTP API, payload format 2.0).

One function serves both the web app (static files) and the JSON API, so the
whole product deploys as a single CloudFormation stack. local_server.py calls
the same handler() so local and cloud behave identically.
"""
import base64
import collections
import json
import logging
import math
import re
import time
from pathlib import Path

import ai
import engine
import roads
import store
from engine import CATALOG, CATALOG_LIST, PLACES, STORE_BY_ID, STORES
from geo import DEFAULT_MODE, MODES, haversine_km

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
    now_ts = time.time()
    return {"lat": lat, "lng": lng, "radius": radius, "now_min": engine.local_minutes(d.get("local_minutes")),
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
    results = engine.search(needs, c["lat"], c["lng"], c["radius"], c["now_min"], c["reports"], c["now_ts"])
    return {"results": results, "gaps": [r["item_id"] for r in results if r["status"] == "gap"],
            "meta": {"stores_total": len(STORES), "radius_km": c["radius"]}}


def api_alternatives(d):
    c, needs = _ctx(d), _needs(d)
    intent = ai.clean_intent(d.get("intent") or {"needs": needs})
    results = engine.search(needs, c["lat"], c["lng"], c["radius"], c["now_min"], c["reports"], c["now_ts"])
    gaps = [{"item_id": r["item_id"], "label": r["label"], "priority": r["priority"], "why": r["why"]}
            for r in results if r["status"] == "gap"]
    if not gaps:
        return {"alternatives": [], "trace": None}
    pool = engine.in_stock_nearby(c["lat"], c["lng"], c["radius"], c["now_min"], c["reports"], c["now_ts"],
                                  exclude={g["item_id"] for g in gaps})
    alts, trace = ai.alternatives(gaps, pool, intent)
    plan = engine.build_plan(needs, alts, c["lat"], c["lng"], c["radius"], c["now_min"], c["reports"], c["now_ts"])
    return {"alternatives": plan["alternatives"], "trace": trace}


def api_brief(d):
    c = _ctx(d)
    intent = ai.clean_intent(d.get("intent") or {})
    if not intent["needs"]:
        raise HttpError(400, "No valid supply items were provided.")
    plan = engine.build_plan(intent["needs"], _clean_alts(d.get("alternatives")), c["lat"], c["lng"],
                             c["radius"], c["now_min"], c["reports"], c["now_ts"])
    brief, trace = ai.brief(plan, intent)
    urgent = bool(intent["urgent_safety"].get("flag"))
    return {"plan": plan, "brief": brief, "trace": trace, "urgent": urgent,
            "plan_text": engine.render_plan_text(plan, brief, urgent)}


def api_plan(d):
    """Whole pipeline in one call (CLI, tests). Can take longer than API Gateway's
    30 s limit when several Bedrock calls are slow; the web app calls the stages
    separately instead so it can show live progress."""
    traces = []
    u = api_understand(d)
    traces.append(u["trace"])
    d2 = dict(d, intent=u["intent"])
    a = api_alternatives(d2)
    if a["trace"]:
        traces.append(a["trace"])
    b = api_brief(dict(d2, alternatives=a["alternatives"]))
    traces.append(b["trace"])
    return {"intent": u["intent"], "plan": b["plan"], "brief": b["brief"], "plan_text": b["plan_text"],
            "urgent": b["urgent"], "traces": traces}


def api_followup(d):
    q = str(d.get("question") or "").strip()[:400]
    if len(q) < 3:
        raise HttpError(400, "Type a question about your plan.")
    ans, trace = ai.followup(q, str(d.get("plan_text") or "")[:6000])
    return {"answer": ans, "trace": trace}


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
