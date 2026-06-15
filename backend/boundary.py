"""Rock Creek Park boundary, used to clip activities for the in-park odometer.

The authoritative main-stem boundary comes from the OpenStreetMap
``boundary=national_park`` relation (the same area filter fetch_trails.py
already relies on). It is fetched once and cached in the meta table, since
it never meaningfully changes. The OSM relation covers only the main park,
so callers union it with a corridor around the tracked trails to also count
the tributary units (Glover-Archbold, Battery Kemble, etc.).
"""
import json

import requests
from shapely.geometry import LineString, mapping, shape
from shapely.ops import polygonize, unary_union

from .db import get_meta, set_meta

USER_AGENT = "rock-creek-tracker/0.1 (personal trail completion app)"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BOUNDARY_QUERY = (
    '[out:json][timeout:60];'
    'relation["name"="Rock Creek Park"]["boundary"="national_park"];'
    "out geom;"
)
META_KEY = "park_boundary_geojson"


def _fetch_osm_boundary():
    """Assemble the park polygon (WGS84) from OSM relation member ways."""
    resp = requests.post(
        OVERPASS_URL,
        data={"data": BOUNDARY_QUERY},
        headers={"User-Agent": USER_AGENT},
        timeout=90,
    )
    resp.raise_for_status()
    lines = []
    for el in resp.json().get("elements", []):
        for member in el.get("members", []):
            geom = member.get("geometry")
            if member.get("type") == "way" and geom and len(geom) >= 2:
                lines.append(LineString([(p["lon"], p["lat"]) for p in geom]))
    polys = list(polygonize(unary_union(lines)))
    return unary_union(polys) if polys else None


def get_park_boundary(session):
    """Return the cached park boundary (WGS84 shapely geometry), fetching and
    caching it on first use. Returns None if OSM is unreachable and nothing
    is cached — callers should fall back to a trail-only corridor."""
    cached = get_meta(session, META_KEY)
    if cached:
        return shape(json.loads(cached))
    try:
        boundary = _fetch_osm_boundary()
    except (requests.RequestException, ValueError):
        boundary = None
    if boundary is not None:
        set_meta(session, META_KEY, json.dumps(mapping(boundary)))
        session.commit()
    return boundary
