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
import math
import os
import socket
import ssl
import time
import urllib.error
from collections import OrderedDict

import road_transport

log = logging.getLogger("supplyline")

BASE_URL = os.environ.get("ROAD_ROUTING_URL", "https://router.project-osrm.org")
USER_AGENT = "SupplyLine-hackathon-demo/1.0"
TIMEOUT_S = 6
MAX_POINTS = 8
MAX_PATH_POINTS = 50000
CACHE_MAX = 200
CACHE_MAX_VERTICES = 100000
CACHE_TTL_S = 600

_cache = OrderedDict()


class RoadsUnavailable(Exception):
    """The routing service could not give us a road route. The message is safe to show."""

    def __init__(self, message, code="unavailable"):
        super().__init__(message)
        self.code = code


def _open(url, timeout):
    """The only network call. Tests replace this."""
    return road_transport.fetch(url, timeout, USER_AGENT)


def _failure(exc):
    """Safe diagnoses only: raw exceptions can include URLs and proxy secrets."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return RoadsUnavailable("The server could not verify the routing service's HTTPS certificate. Check the computer's date and trusted certificates or network proxy.", "tls_certificate")
    if isinstance(reason, ssl.SSLError):
        return RoadsUnavailable("The server could not establish a secure connection to the routing service. Check Python, Windows updates and your network proxy.", "tls_connection")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return RoadsUnavailable("The routing service timed out. Check the server's internet connection and retry.", "timeout")
    if isinstance(reason, socket.gaierror):
        return RoadsUnavailable("The server could not find the routing service's internet address. Check its DNS or proxy settings.", "dns")
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 407:
            return RoadsUnavailable("The server's network proxy requires authentication before it can reach the routing service.", "proxy_auth")
        if exc.code == 429:
            return RoadsUnavailable("The routing service is busy. Wait a minute before retrying.", "rate_limited")
        return RoadsUnavailable(f"The routing service returned HTTP {exc.code}. Check the server's network access or retry later.", "http_error")
    if isinstance(exc, ValueError):
        return RoadsUnavailable("The routing service returned an unreadable response. A network proxy may be intercepting the request.", "invalid_response")
    return RoadsUnavailable("The server could not connect to the routing service. Check its internet connection, firewall or proxy settings.", "connection")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Non-finite routing value")
    return value


def _measure(value, scale, precision):
    value = _number(value)
    if value < 0:
        raise ValueError("Negative routing measure")
    return round(value / scale, precision)


def fetch_route(points):
    """points: list of (lat, lng), start first. Returns the fastest driving route through them in order."""
    if os.environ.get("ROAD_ROUTING", "on").lower() == "off":
        raise RoadsUnavailable("Road directions are turned off on the server. Restart it with ROAD_ROUTING=on.", "disabled")
    key = tuple((round(lat, 5), round(lng, 5)) for lat, lng in points)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        _cache.move_to_end(key)
        return hit[1]

    coords = ";".join(f"{lng:.5f},{lat:.5f}" for lat, lng in key)
    url = f"{BASE_URL}/route/v1/driving/{coords}?overview=full&geometries=geojson&steps=false&alternatives=false"
    # One retry for a transient transport failure, within the API's 29-second budget.
    for attempt in range(2):
        try:
            data = json.loads(_open(url, TIMEOUT_S))
            break
        except (urllib.error.URLError, OSError, ValueError) as e:
            failure = _failure(e)
            log.warning("road routing unavailable (%s): %s", failure.code, failure)
            if attempt or failure.code in {"tls_certificate", "tls_connection", "proxy_auth", "rate_limited"}:
                raise failure from e
    try:
        if data.get("code") != "Ok":
            raise RoadsUnavailable("No road route was found between those places.", "no_route")
        r = data["routes"][0]
        geometry = r["geometry"]
        coords = geometry["coordinates"]
        if geometry["type"] != "LineString" or not isinstance(coords, list) or not 2 <= len(coords) <= MAX_PATH_POINTS:
            raise ValueError("Invalid road geometry")
        # Keep every bend. Sampling every Nth vertex cuts across corners and off roads.
        path = []
        for lng, lat in coords:
            if abs(_number(lat)) > 90 or abs(_number(lng)) > 180:
                raise ValueError("Road coordinate out of range")
            path.append([lat, lng])
        legs = [{"duration_min": _measure(l["duration"], 60, 1), "distance_km": _measure(l["distance"], 1000, 2)}
                for l in r.get("legs", [])]
        result = {"path": path, "duration_min": _measure(r["duration"], 60, 1),
                  "distance_km": _measure(r["distance"], 1000, 2), "legs": legs,
                  "source": "OpenStreetMap roads via OSRM, no live traffic"}
    except RoadsUnavailable:
        raise
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as e:
        log.warning("road routing unavailable (invalid_geometry)")
        raise RoadsUnavailable("The routing service returned an invalid road path. Retry road routing.", "invalid_geometry") from e
    _cache[key] = (now, result)
    while len(_cache) > CACHE_MAX or sum(len(v[1]["path"]) for v in _cache.values()) > CACHE_MAX_VERTICES:
        _cache.popitem(last=False)
    return result
