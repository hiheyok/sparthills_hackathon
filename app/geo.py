"""Small geo helpers. Distances are straight-line (haversine); travel times are
an ESTIMATE, not live traffic or live conditions: straight-line km x a detour
factor at a slow disaster-time speed, for each way of getting there."""
import math

EARTH_KM = 6371.0088

# How people get around when things are disrupted. Driving: damaged roads, dark signals,
# evacuation traffic. Walking: carrying supplies, debris, detours.
MODES = {
    "driving": {"detour": 1.35, "speed_kmh": 28.0, "word": "drive"},   # road distance > straight line
    "walking": {"detour": 1.25, "speed_kmh": 4.0, "word": "walk"},
}
DEFAULT_MODE = "driving"
WALK_MAX_KM = 3.0    # farthest (straight line) a plan sends anyone on foot: about an hour each way

DETOUR = MODES["driving"]["detour"]          # the original driving constants, still imported elsewhere
SPEED_KMH = MODES["driving"]["speed_kmh"]


def haversine_km(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def eta_min(km, mode=DEFAULT_MODE):
    m = MODES[mode]
    return max(1, int(round(km * m["detour"] / m["speed_kmh"] * 60)))


def reach_km(radius_km, mode=DEFAULT_MODE):
    """The search radius that makes sense for this way of travelling."""
    return min(radius_km, WALK_MAX_KM) if mode == "walking" else radius_km


def fmt_clock(minutes):
    """Minutes after midnight -> '6:05 PM' (wraps past 24h)."""
    m = int(minutes) % 1440
    h, mm = divmod(m, 60)
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{mm:02d} {suffix}"
