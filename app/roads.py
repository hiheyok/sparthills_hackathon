"""Road routing for the map: the fastest drive through the plan's stops, in order.

The stops and their order come from engine.py. This module only asks a routing
service for the actual road path between those points, so the map can draw it.

It uses the public OSRM demo server (OpenStreetMap road data). That is a free
community service with no uptime promise, so everything here fails soft: if it
cannot answer, the app keeps working and the map shows a straight line.
The browser never calls it directly. Our function does, so the person's IP address
is not sent to a third party and the page's Content Security Policy stays strict.

For production, swap fetch_route() for Amazon Location Service CalculateRoutes.
The return shape would stay the same.
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections import OrderedDict

log = logging.getLogger("supplyline")

BASE_URL = os.environ.get("ROAD_ROUTING_URL", "https://router.project-osrm.org")
USER_AGENT = "SupplyLine-hackathon-demo/1.0"
TIMEOUT_S = 6
MAX_POINTS = 8
MAX_PATH_POINTS = 2500
CACHE_MAX = 200
CACHE_TTL_S = 600

_cache = OrderedDict()


class RoadsUnavailable(Exception):
    """The routing service could not give us a road route. The message is safe to show."""


def _open(url, timeout):
    """The only network call. Tests replace this."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (fixed https host, numbers only)
        return r.read(3_000_000)


def _thin(path, limit=MAX_PATH_POINTS):
    if len(path) <= limit:
        return path
    step = len(path) / float(limit - 1)
    out = [path[int(i * step)] for i in range(limit - 1)]
    out.append(path[-1])
    return out


def fetch_route(points):
    """points: list of (lat, lng), start first. Returns the fastest driving route through them in order."""
    if os.environ.get("ROAD_ROUTING", "on").lower() == "off":
        raise RoadsUnavailable("Road directions are turned off.")
    key = tuple((round(lat, 5), round(lng, 5)) for lat, lng in points)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        _cache.move_to_end(key)
        return hit[1]

    coords = ";".join(f"{lng:.5f},{lat:.5f}" for lat, lng in key)
    url = f"{BASE_URL}/route/v1/driving/{coords}?overview=full&geometries=geojson&steps=false&alternatives=false"
    try:
        data = json.loads(_open(url, TIMEOUT_S))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("road routing unavailable: %s", e)
        raise RoadsUnavailable("Road directions are not available right now.")
    try:
        if data.get("code") != "Ok":
            raise RoadsUnavailable("No road route was found between those places.")
        r = data["routes"][0]
        path = _thin([[round(lat, 5), round(lng, 5)] for lng, lat in r["geometry"]["coordinates"]])
        legs = [{"duration_min": round(float(l["duration"]) / 60, 1), "distance_km": round(float(l["distance"]) / 1000, 2)}
                for l in r.get("legs", [])]
        result = {"path": path, "duration_min": round(float(r["duration"]) / 60, 1),
                  "distance_km": round(float(r["distance"]) / 1000, 2), "legs": legs,
                  "source": "OpenStreetMap roads via OSRM, no live traffic"}
    except RoadsUnavailable:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as e:
        log.warning("unexpected road routing answer: %s", e)
        raise RoadsUnavailable("Road directions are not available right now.")
    if len(path) < 2:
        raise RoadsUnavailable("No road route was found between those places.")

    _cache[key] = (now, result)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return result
