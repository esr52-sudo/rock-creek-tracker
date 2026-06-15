"""Uncovered-trail route suggester.

Greedy construction: from the starting point, repeatedly hop to the nearest
endpoint of an uncovered trail segment (straight "connector" legs stand in
for walking existing trails/park roads), walk the segment, and repeat until
the distance budget runs out, then connect back to the start.

All distance math in UTM 18N; output in WGS84.
"""
import json
import math
import uuid
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from shapely.geometry import LineString, Point, shape
from shapely.ops import substring

from .db import SessionLocal, SuggestedRoute, Trail
from .geo import to_utm, to_wgs

METERS_PER_MILE = 1609.344
DEFAULT_START = (38.9497, -77.0523)  # Rock Creek Park Nature Center
MIN_SEGMENT_METERS = 30.0  # ignore uncovered slivers shorter than this
MIN_CONNECTOR_METERS = 2.0  # skip degenerate connectors


def _load_uncovered(session):
    segments = []
    for trail in session.query(Trail).filter(Trail.pct_complete_total < 0.995):
        if not trail.uncovered_geojson:
            continue
        geom = to_utm(shape(json.loads(trail.uncovered_geojson)))
        lines = [geom] if isinstance(geom, LineString) else list(geom.geoms)
        for line in lines:
            if line.length < MIN_SEGMENT_METERS:
                continue
            flat = LineString([(c[0], c[1]) for c in line.coords])
            segments.append(
                {"trail_id": trail.id, "trail_name": trail.name, "line": flat}
            )
    return segments


def build_route(start_lat, start_lng, target_miles):
    """Build and persist a route. Returns the API response dict.
    Raises ValueError when no useful route can be built."""
    budget = target_miles * METERS_PER_MILE
    start_pt = to_utm(Point(start_lng, start_lat))
    start_xy = (start_pt.x, start_pt.y)

    with SessionLocal() as session:
        segments = _load_uncovered(session)
    if not segments:
        raise ValueError("every trail is already complete — nothing left to route")

    parts = []  # {kind, trail_id, trail_name, coords (UTM), meters}
    pos = start_xy
    remaining = budget

    while segments:
        best = None  # (distance, index, walk_reversed)
        for i, seg in enumerate(segments):
            coords = seg["line"].coords
            for endpoint, rev in ((coords[0], False), (coords[-1], True)):
                d = math.dist(pos, endpoint)
                if best is None or d < best[0]:
                    best = (d, i, rev)
        d, i, rev = best
        if d >= remaining:
            break
        seg = segments.pop(i)
        coords = list(seg["line"].coords)
        if rev:
            coords.reverse()
        line = LineString(coords)

        if d > MIN_CONNECTOR_METERS:
            parts.append(
                {"kind": "connector", "trail_id": None, "trail_name": None,
                 "coords": [pos, coords[0]], "meters": d}
            )
        remaining -= d

        if line.length <= remaining:
            walked, walked_m = coords, line.length
        else:
            walked = list(substring(line, 0, remaining).coords)
            walked_m = remaining
            if len(walked) < 2:
                break
        parts.append(
            {"kind": "trail", "trail_id": seg["trail_id"],
             "trail_name": seg["trail_name"], "coords": walked, "meters": walked_m}
        )
        remaining -= walked_m
        pos = (walked[-1][0], walked[-1][1])
        if remaining <= 0:
            break

    if not any(p["kind"] == "trail" for p in parts):
        raise ValueError(
            "target distance is too short to reach any uncovered trail from this start point"
        )

    d_home = math.dist(pos, start_xy)
    if d_home > MIN_CONNECTOR_METERS:
        parts.append(
            {"kind": "connector", "trail_id": None, "trail_name": None,
             "coords": [pos, start_xy], "meters": d_home}
        )

    # convert parts to WGS84 features + one concatenated LineString
    features, full_coords = [], []
    for part in parts:
        wgs = to_wgs(LineString(part["coords"]))
        coords = [[round(c[0], 6), round(c[1], 6)] for c in wgs.coords]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "kind": part["kind"],
                    "trail_id": part["trail_id"],
                    "trail_name": part["trail_name"],
                    "miles": round(part["meters"] / METERS_PER_MILE, 2),
                },
            }
        )
        for c in coords:
            if not full_coords or full_coords[-1] != c:
                full_coords.append(c)

    total_miles = sum(p["meters"] for p in parts) / METERS_PER_MILE
    new_miles = sum(p["meters"] for p in parts if p["kind"] == "trail") / METERS_PER_MILE

    touched = {}
    for p in parts:
        if p["kind"] != "trail":
            continue
        entry = touched.setdefault(
            p["trail_id"],
            {"trail_id": p["trail_id"], "trail_name": p["trail_name"], "segment_miles": 0.0},
        )
        entry["segment_miles"] += p["meters"] / METERS_PER_MILE
    trails_touched = sorted(
        ({**t, "segment_miles": round(t["segment_miles"], 2)} for t in touched.values()),
        key=lambda t: -t["segment_miles"],
    )

    route_id = uuid.uuid4().hex
    with SessionLocal() as session:
        session.add(
            SuggestedRoute(
                id=route_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                start_lat=start_lat,
                start_lng=start_lng,
                target_miles=target_miles,
                route_geojson=json.dumps({"type": "FeatureCollection", "features": features}),
                total_miles=round(total_miles, 2),
                new_coverage_miles=round(new_miles, 2),
            )
        )
        session.commit()

    return {
        "route_id": route_id,
        "route_geojson": {"type": "LineString", "coordinates": full_coords},
        "total_miles": round(total_miles, 2),
        "new_coverage_miles": round(new_miles, 2),
        "trails_touched": trails_touched,
        "parts": [
            {
                "kind": f["properties"]["kind"],
                "trail_name": f["properties"]["trail_name"],
                "miles": f["properties"]["miles"],
                "geojson": f["geometry"],
            }
            for f in features
        ],
        "gpx_download_url": f"/api/suggest-route/gpx/{route_id}",
    }


def build_gpx(route):
    """Valid GPX 1.1: single trk/trkseg; every trkpt carries a <type> so
    connector legs are distinguishable from real trail segments."""
    fc = json.loads(route.route_geojson)
    date_str = (route.created_at or "")[:10]
    points = []
    for feat in fc["features"]:
        props = feat["properties"]
        type_str = (
            "connector"
            if props["kind"] == "connector"
            else f"trail - {props['trail_name']}"
        )
        for lng, lat in feat["geometry"]["coordinates"]:
            points.append(
                f'      <trkpt lat="{lat:.6f}" lon="{lng:.6f}">'
                f"<type>{escape(type_str)}</type></trkpt>"
            )
    body = "\n".join(points)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="Rock Creek Tracker" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        "  <trk>\n"
        f"    <name>Rock Creek Park Route - {date_str}</name>\n"
        "    <desc>Generated by Rock Creek Tracker</desc>\n"
        "    <trkseg>\n"
        f"{body}\n"
        "    </trkseg>\n"
        "  </trk>\n"
        "</gpx>\n"
    )
