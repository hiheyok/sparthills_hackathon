"""Deterministic supply search and route planning.

There is intentionally NO AI in this file. Stock, distance, opening hours and
routing are facts that must be reproducible and auditable; Amazon Bedrock is
used around this engine (ai.py) for the judgment calls: understanding what a
stressed person actually needs, deciding what is a safe substitute, and
explaining the plan. The AI can only pick from ids this engine knows about,
and everything it recommends is re-checked here for real stock.
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path

from geo import eta_min, fmt_clock, haversine_km

DATA = Path(__file__).parent / "data"


def _load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


CATALOG_LIST = _load("catalog.json")
CATALOG = {c["id"]: c for c in CATALOG_LIST}
STORES = _load("stores.json")
STORE_BY_ID = {s["id"]: s for s in STORES}
PLACES = _load("places.json")

def _snapshot_ts():
    try:
        return float(os.environ["INVENTORY_SNAPSHOT_TS"])
    except (KeyError, ValueError):
        return time.time()


# The demo inventory gives each record's age in minutes "as of the snapshot". The snapshot is
# pinned to one instant (INVENTORY_SNAPSHOT_TS, which deploy.sh sets so every Lambda container
# agrees, else this process's start), so a record has one stable observation time:
# SNAPSHOT_TS - age. Neighbor reports are compared with that time and the age shown to people
# is measured from it. The snapshot gets older as time passes, instead of looking freshly
# observed on every search. A real inventory feed would give each record its own "ts".
SNAPSHOT_TS = _snapshot_ts()

CLOSE_BUFFER_MIN = 15      # arrive at least this long before closing
FRESH_MIN = 60             # inventory checked within an hour
STALE_MIN = 240            # older than this: "call ahead"
LOW_Q = 3                  # at or below this we call it "low stock"
STATUS_TO_Q = {"in_stock": 5, "low": 1, "out": 0}
SHOPPING_MIN = 8           # time spent inside each stop
PRIORITY_WEIGHT = {1: 5, 2: 3, 3: 1}      # how much covering an item is worth when choosing stops
URGENCY_WEIGHT = {1: 3.0, 2: 1.0, 3: 0.0}  # how much waiting for an item hurts when ordering stops


# ---------------------------------------------------------------- time
def local_minutes(client_minutes=None):
    """Minutes after midnight in the user's time zone. The client sends its own
    clock; fall back to Pacific time (the demo region)."""
    if isinstance(client_minutes, (int, float)) and 0 <= client_minutes < 1440:
        return int(client_minutes)
    try:
        from zoneinfo import ZoneInfo
        t = datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:  # tz database missing in minimal runtimes
        t = datetime.now(timezone.utc) - timedelta(hours=7)
    return t.hour * 60 + t.minute


def open_state(store, arrive_min, reported_closed=False):
    """(is_open_on_arrival, 'until 10:00 PM' | None, note)"""
    if store["status"] == "closed":
        return False, None, store["note"] or "Closed"
    if reported_closed:
        return False, None, "A neighbor reported it closed"
    hours = store["hours"]
    if hours.get("always"):
        return True, "open 24 hours", store["note"]
    t = int(arrive_min) % 1440
    o, c = hours["open"], hours["close"]
    if t < o:
        return False, None, f"Opens at {fmt_clock(o)}"
    if t > c - CLOSE_BUFFER_MIN:
        return False, None, f"Closes at {fmt_clock(c)}"
    return True, f"until {fmt_clock(c)}", store["note"]


# --------------------------------------------------------------- stock
def _reported_closed(store_id, reports):
    rep = reports.get((store_id, "*"))
    return bool(rep and rep["status"] == "closed")


def _observed_ts(rec):
    """When an inventory record was observed (unix seconds)."""
    return rec["ts"] if "ts" in rec else SNAPSHOT_TS - rec["age"] * 60.0


def stock_of(store, item_id, reports, now_ts):
    """(quantity, age_minutes, source) or None if the store is not known to carry it.
    A neighbor report beats the inventory record while it is newer than the record's
    observation time, so it stays authoritative until it expires (store.REPORT_TTL_S)
    or the store's inventory is genuinely observed again."""
    rec = store["inv"].get(item_id)
    q = seen = None
    source = "snapshot"
    if rec:
        q, seen = rec["q"], _observed_ts(rec)
    rep = reports.get((store["id"], item_id))
    if rep and rep["status"] in STATUS_TO_Q and (rec is None or rep["ts"] >= seen):
        q, seen, source = STATUS_TO_Q[rep["status"]], rep["ts"], "community"
    if q is None:
        return None
    return q, int(max(0.0, (now_ts - seen) / 60.0)), source


def confidence(age_min):
    if age_min <= FRESH_MIN:
        return "fresh"
    if age_min <= STALE_MIN:
        return "recent"
    return "stale"


def _store_view(s, dist_km, eta, until, q, age, source, qty_wanted):
    conf = confidence(age)
    return {
        "id": s["id"], "name": s["name"], "type": s["type"], "hood": s["hood"],
        "lat": s["lat"], "lng": s["lng"],
        "dist_km": round(dist_km, 1), "eta_min": eta,
        "open_note": until, "store_note": s["note"] if s["status"] == "limited" else "",
        "q": q, "low": q <= LOW_Q, "short": q < qty_wanted,
        "age_min": age, "confidence": conf, "source": source,
    }


def search_item(item_id, qty, lat, lng, radius_km, now_min, reports, now_ts, limit=8):
    cands = []
    stats = {"checked": 0, "closed": 0, "out": 0, "no_item": 0}
    for s in STORES:
        d = haversine_km(lat, lng, s["lat"], s["lng"])
        if d > radius_km:
            continue
        stats["checked"] += 1
        eta = eta_min(d)
        is_open, until, _ = open_state(s, now_min + eta, _reported_closed(s["id"], reports))
        st = stock_of(s, item_id, reports, now_ts)
        if st is None:
            stats["no_item"] += 1
            continue
        if not is_open:
            stats["closed"] += 1
            continue
        q, age, source = st
        if q <= 0:
            stats["out"] += 1
            continue
        view = _store_view(s, d, eta, until, q, age, source, qty)
        penalty = {"fresh": 0, "recent": 6, "stale": 20}[view["confidence"]]
        penalty += 8 if view["short"] else 0
        penalty += 4 if s["status"] == "limited" else 0
        view["score"] = eta + penalty
        cands.append(view)
    cands.sort(key=lambda v: (v["score"], v["dist_km"]))
    has_full = any(not c["short"] for c in cands)
    status = "found" if has_full else ("partial" if cands else "gap")
    top = cands[:limit]
    if has_full and top and all(c["short"] for c in top):
        # closer, short stores must not push out the best store that can fill the whole request
        top = top[:limit - 1] + [next(c for c in cands if not c["short"])]
    return {"item_id": item_id, "status": status, "stores": top,
            "total_matches": len(cands), "stats": stats}


def nearest_available(item_id, lat, lng, now_min, reports, now_ts):
    """Nearest open store with stock anywhere (used to explain a gap)."""
    best = None
    for s in STORES:
        d = haversine_km(lat, lng, s["lat"], s["lng"])
        if best is not None and d >= best[0]:
            continue
        eta = eta_min(d)
        is_open, until, _ = open_state(s, now_min + eta, _reported_closed(s["id"], reports))
        st = stock_of(s, item_id, reports, now_ts)
        if not is_open or st is None or st[0] <= 0:
            continue
        best = (d, _store_view(s, d, eta, until, st[0], st[1], st[2], 1))
    return best[1] if best else None


def search(needs, lat, lng, radius_km, now_min, reports, now_ts):
    out = []
    for n in needs:
        item = CATALOG[n["item_id"]]
        r = search_item(n["item_id"], n.get("qty", 1), lat, lng, radius_km, now_min, reports, now_ts)
        r.update({
            "name": item["name"], "label": n.get("label") or item["name"], "policy": item["policy"],
            "priority": n.get("priority", 2), "qty": n.get("qty", 1),
            "inferred": bool(n.get("inferred")), "why": n.get("why", ""),
        })
        if r["status"] == "gap":
            r["nearest_any"] = nearest_available(n["item_id"], lat, lng, now_min, reports, now_ts)
        out.append(r)
    return out


def in_stock_nearby(lat, lng, radius_km, now_min, reports, now_ts, exclude=()):
    """{item_id: nearest store view} for every catalog item with stock inside radius.
    This is the candidate pool the AI may choose substitutes from."""
    pool = {}
    for item_id in CATALOG:
        if item_id in exclude:
            continue
        r = search_item(item_id, 1, lat, lng, radius_km, now_min, reports, now_ts, limit=1)
        if r["stores"]:
            pool[item_id] = r["stores"][0]
    return pool


# ---------------------------------------------------------------- plan
def build_plan(needs, alternatives, lat, lng, radius_km, now_min, reports, now_ts):
    results = search(needs, lat, lng, radius_km, now_min, reports, now_ts)
    alt_by_item = {a["for_item"]: a for a in (alternatives or [])}
    chosen, alts_out = [], []

    for r in results:
        key = r["item_id"]
        if r["status"] != "gap":
            chosen.append({"key": key, "item_id": key, "name": r["name"], "label": r["label"],
                           "priority": r["priority"], "qty": r["qty"], "is_alt": False,
                           "replaces": None, "stores": r["stores"]})
            continue
        a = alt_by_item.get(key) or {}
        cat = CATALOG[key]
        entry = {
            "for_item": key, "for_name": cat["name"], "label": r["label"], "policy": cat["policy"],
            "verdict": "none_found",  # derived below from policy and real stock, never trusted from input
            "options": [], "no_purchase_tips": a.get("no_purchase_tips", []),
            "escalate": a.get("escalate") or cat.get("escalate") or None,
            "nearest_any": r.get("nearest_any"), "stats": r["stats"],
        }
        picked = None
        allowed = cat.get("safe_subs") if cat["policy"] == "caution" else None
        for opt in a.get("options", []):
            if opt["item_id"] not in CATALOG or cat["policy"] == "none":
                continue
            if allowed is not None and opt["item_id"] not in allowed:
                continue  # caution items accept only curated substitutes, whoever proposed the option
            o_res = search_item(opt["item_id"], 1, lat, lng, radius_km, now_min, reports, now_ts)
            if o_res["status"] == "gap":
                continue  # a substitute nobody nearby has is not a substitute
            o = dict(opt)
            o.update({"name": CATALOG[opt["item_id"]]["name"], "status": o_res["status"],
                      "stores": o_res["stores"][:3], "all_stores": o_res["stores"]})
            entry["options"].append(o)
            if picked is None:
                picked = o
        if cat["policy"] == "none":
            entry["verdict"] = "no_safe_substitute"
        elif entry["options"]:
            entry["verdict"] = "substitutes_available"
        if picked:
            chosen.append({"key": key, "item_id": picked["item_id"], "name": picked["name"],
                           "label": r["label"], "priority": r["priority"], "qty": 1, "is_alt": True,
                           "replaces": key, "stores": picked["all_stores"]})
        for o in entry["options"]:
            o.pop("all_stores", None)
        alts_out.append(entry)

    route = plan_route(chosen, lat, lng, now_min, reports)
    shortfalls = route.pop("shortfalls")
    placed = {it["key"] for stop in route["stops"] for it in stop["items"]}
    unresolved = [{"item_id": r["item_id"], "name": r["name"], "label": r["label"],
                   "priority": r["priority"],
                   "reason": "no_stock" if r["item_id"] not in {c["key"] for c in chosen} else "not_in_route"}
                  for r in results if r["item_id"] not in placed]
    summary = {
        "needs": len(results), "in_route": len(placed),
        "covered": len(placed) - len(shortfalls),   # in the route AND the full amount is available
        "short": len(shortfalls),                   # in the route, but only part of the amount
        "swapped": sum(1 for stop in route["stops"] for it in stop["items"] if it["is_alt"]),
        "unresolved": len(unresolved),
    }
    return {"results": results, "alternatives": alts_out, "route": route, "unresolved": unresolved,
            "shortfalls": shortfalls, "summary": summary,
            "location": {"lat": lat, "lng": lng, "radius_km": radius_km}, "start_min": now_min}


def _simulate(order, lat, lng, now_min, reports):
    """Drive the ordered stops. Returns per-leg info, or None if any store would
    be closed when we get there."""
    clock, cur_lat, cur_lng, legs = now_min, lat, lng, []
    for sid in order:
        s = STORE_BY_ID[sid]
        d = haversine_km(cur_lat, cur_lng, s["lat"], s["lng"])
        leg = eta_min(d)
        clock += leg
        ok, until, _ = open_state(s, clock, _reported_closed(sid, reports))
        if not ok:
            return None
        legs.append({"km": d, "min": leg, "arrive": clock, "until": until})
        clock += SHOPPING_MIN
        cur_lat, cur_lng = s["lat"], s["lng"]
    return legs


def plan_route(chosen, lat, lng, now_min, reports, max_stops=4):
    """Cheapest-insertion set cover. Repeatedly add the store (at the best
    position in the route) that covers the most urgent still-missing units per
    extra minute. Every candidate route is re-simulated so that each store is
    open when we actually arrive.

    Quantities count. If some store can fill a whole request, only such stores
    are candidates for it (one stop, full amount). If none can, any store with
    stock is a candidate and the amount is split across stops in route order;
    whatever the route still cannot supply is returned in "shortfalls"."""
    by_key = {c["key"]: c for c in chosen}
    stock = {}                        # (store id, item key) -> units on the shelf
    store_keys = defaultdict(set)

    def offer(c, stores):
        for s in stores:
            stock[(s["id"], c["key"])] = s["q"]
            store_keys[s["id"]].add(c["key"])

    for c in chosen:
        offer(c, [s for s in c["stores"] if s["q"] >= c["qty"]] or c["stores"])
    left = {k: c["qty"] for k, c in by_key.items()}

    order = []
    base_min = 0

    def grow():
        nonlocal order, base_min
        while any(n > 0 for n in left.values()) and len(order) < max_stops:
            best = None
            for sid, keys in store_keys.items():
                if sid in order:
                    continue
                gain = sum(PRIORITY_WEIGHT.get(by_key[k]["priority"], 3) * min(left[k], stock[(sid, k)]) / by_key[k]["qty"]
                           for k in keys if left[k] > 0)
                if gain <= 0:
                    continue
                for pos in range(len(order) + 1):
                    cand = order[:pos] + [sid] + order[pos:]
                    legs = _simulate(cand, lat, lng, now_min, reports)
                    if legs is None:
                        continue
                    added = sum(l["min"] for l in legs) - base_min + SHOPPING_MIN
                    value = gain / (added + 6.0)
                    if best is None or value > best[0]:
                        best = (value, cand, sid)
            if best is None:
                break
            _, order, sid = best
            base_min = sum(l["min"] for l in _simulate(order, lat, lng, now_min, reports))
            for k in store_keys[sid]:
                left[k] = max(0, left[k] - stock[(sid, k)])

    grow()
    # An item whose full-amount stores could not be reached in time may still be
    # partly available elsewhere: some of it is better than none.
    if any(left[c["key"]] == c["qty"] for c in chosen):
        for c in chosen:
            if left[c["key"]] == c["qty"]:
                offer(c, c["stores"])
        grow()

    # Re-order the chosen stops: mostly minimise driving, but pull stops that hold
    # critical (priority 1) items earlier. At most 4 stops, so 24 orders at most.
    if 1 < len(order) <= max_stops:
        best_perm = None
        for perm in permutations(order):
            legs = _simulate(list(perm), lat, lng, now_min, reports)
            if legs is None:
                continue
            taken, delay = set(), 0.0
            for sid, leg in zip(perm, legs):
                for k in store_keys[sid] - taken:
                    delay += URGENCY_WEIGHT.get(by_key[k]["priority"], 1.0) * (leg["arrive"] - now_min)
                    taken.add(k)
            cost = sum(l["min"] for l in legs) + 0.1 * delay
            if best_perm is None or cost < best_perm[0]:
                best_perm = (cost, list(perm))
        if best_perm:
            order = best_perm[1]

    # each item is bought at the earliest stops in the route that have it, up to the amount needed
    left = {k: c["qty"] for k, c in by_key.items()}
    assigned = defaultdict(list)      # store id -> [(item key, units to buy there)]
    for sid in order:
        for k in sorted(store_keys[sid], key=lambda k: (by_key[k]["priority"], k)):
            units = min(left[k], stock[(sid, k)])
            if units > 0:
                assigned[sid].append((k, units))
                left[k] -= units
    order = [sid for sid in order if assigned[sid]]
    legs = _simulate(order, lat, lng, now_min, reports) or []

    stops = []
    for idx, (sid, leg) in enumerate(zip(order, legs), start=1):
        s = STORE_BY_ID[sid]
        items = []
        for k, units in assigned[sid]:
            c = by_key[k]
            sv = next(x for x in c["stores"] if x["id"] == sid)
            items.append({
                "key": k, "item_id": c["item_id"], "name": c["name"], "label": c["label"],
                "qty": units, "qty_needed": c["qty"], "priority": c["priority"], "is_alt": c["is_alt"],
                "replaces": c["replaces"],
                "replaces_name": CATALOG[c["replaces"]]["name"] if c["replaces"] else None,
                "q": sv["q"], "low": sv["low"], "short": sv["q"] < c["qty"], "age_min": sv["age_min"],
                "confidence": sv["confidence"], "source": sv["source"],
            })
        stops.append({
            "order": idx,
            "store": {"id": sid, "name": s["name"], "type": s["type"], "hood": s["hood"],
                      "lat": s["lat"], "lng": s["lng"],
                      "store_note": s["note"] if s["status"] == "limited" else ""},
            "leg_km": round(leg["km"], 1), "leg_min": leg["min"], "arrive": fmt_clock(leg["arrive"]),
            "open_note": leg["until"], "items": items,
        })
    total_drive = sum(l["min"] for l in legs)
    shortfalls = [{"key": k, "item_id": c["item_id"], "name": c["name"], "label": c["label"],
                   "priority": c["priority"], "qty": c["qty"], "found": c["qty"] - left[k], "missing": left[k]}
                  for k, c in by_key.items() if 0 < left[k] < c["qty"]]
    return {"stops": stops, "total_km": round(sum(l["km"] for l in legs), 1),
            "total_drive_min": total_drive, "total_min": total_drive + SHOPPING_MIN * len(stops),
            "leaves": fmt_clock(now_min), "shortfalls": shortfalls}


def render_plan_text(plan, brief=None, urgent=False):
    """Plain-text version of the plan for SMS / clipboard / printing. Built
    from the structured plan (not from AI prose) so it is always consistent."""
    lines = ["SUPPLYLINE PLAN (" + fmt_clock(plan["start_min"]) + ")"]
    if urgent:
        lines.append("FIRST: if anyone is in danger, call 911 and get to safety before shopping.")
    if brief and brief.get("headline"):
        lines.append(brief["headline"])
    def units(i):
        if i["qty_needed"] > i["qty"]:
            return " x%d of %d" % (i["qty"], i["qty_needed"])
        return " x%d" % i["qty"] if i["qty"] > 1 else ""

    for stop in plan["route"]["stops"]:
        st = stop["store"]
        names = ", ".join(("%s%s (instead of %s)" % (i["name"], units(i), i["replaces_name"]) if i["is_alt"]
                           else i["name"] + units(i)) for i in stop["items"])
        lines.append("%d. %s, %d min, %s km, arrive %s" % (stop["order"], st["name"], stop["leg_min"], stop["leg_km"], stop["arrive"]))
        lines.append("   Get: " + names)
    for u in plan["unresolved"]:
        lines.append("Not found nearby: " + u["name"])
    for s in plan.get("shortfalls", []):
        lines.append("Not enough in stock: %s, found %d of %d" % (s["name"], s["found"], s["qty"]))
    for a in plan["alternatives"]:
        if a.get("escalate"):
            lines.append("%s: %s" % (a["for_name"], a["escalate"]))
    if brief:
        for n in brief.get("safety_notes", [])[:4]:
            lines.append("Safety: " + n)
    lines.append("Stock levels are estimates. Call ahead if you can.")
    return "\n".join(lines)
