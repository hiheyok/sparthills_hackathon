"""Small geo helpers. Distances are straight-line (haversine); drive times are
an ESTIMATE, not live traffic: straight-line km x a detour factor at a
congested disaster-time speed."""
import math

EARTH_KM = 6371.0088
DETOUR = 1.35        # road distance is longer than straight-line distance
SPEED_KMH = 28.0     # slow average: damaged roads, dark signals, evacuation traffic


def haversine_km(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def eta_min(km):
    return max(1, int(round(km * DETOUR / SPEED_KMH * 60)))


def fmt_clock(minutes):
    """Minutes after midnight -> '6:05 PM' (wraps past 24h)."""
    m = int(minutes) % 1440
    h, mm = divmod(m, 60)
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{mm:02d} {suffix}"
