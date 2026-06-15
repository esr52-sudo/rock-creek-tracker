#!/usr/bin/env python3
"""Phase 3: score each trail against activity GPS coverage, split by mode.

Foot and bike activities are buffered (20 m, in UTM 18N) into two separate
unions. Each trail centerline is split into real sub-geometries via Shapely
intersection/difference:

  covered_foot = trail ∩ foot_union
  covered_bike = (trail ∩ bike_union) − foot_union   (foot takes precedence)
  uncovered    = trail − foot_union − bike_union

All math happens in UTM 18N; results are stored back in WGS84.
"""
import json
import sys
from pathlib import Path

from shapely.geometry import GeometryCollection, LineString, MultiLineString, mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.boundary import get_park_boundary  # noqa: E402
from backend.db import (  # noqa: E402
    Activity,
    SessionLocal,
    Trail,
    TrailActivity,
    init_db,
    set_meta,
)
from backend.geo import to_utm, to_wgs  # noqa: E402

BUFFER_METERS = 20.0
# Complete at >= 99.5% — trail centerlines often extend a few meters past
# where a GPS track can plausibly reach (gates, road junctions).
COMPLETION_THRESHOLD = 0.995
# Minimum shared distance before an activity is credited to a trail.
MIN_OVERLAP_METERS = 25.0
METERS_PER_MILE = 1609.344
# Width of the corridor added around each trail so the tributary park units
# (Glover-Archbold, Battery Kemble, ...) count toward the in-park odometer,
# not just the main-stem OSM boundary polygon.
TRAIL_CORRIDOR_METERS = 30.0


def in_park_miles(session, activity_geoms_utm, trail_lines_utm):
    """Total activity distance (all history, repeats included) that falls
    inside the park. Clip region = main-stem boundary polygon ∪ a corridor
    around every tracked trail. Returns (total, foot, bike) miles."""
    boundary = get_park_boundary(session)
    regions = []
    if boundary is not None:
        regions.append(to_utm(boundary))
    if trail_lines_utm:
        regions.append(unary_union([ln.buffer(TRAIL_CORRIDOR_METERS) for ln in trail_lines_utm]))
    if not regions:
        return 0.0, 0.0, 0.0
    clip = unary_union(regions)

    total = foot = bike = 0.0
    for mode, line in activity_geoms_utm:
        inside = line.intersection(clip).length
        total += inside
        if mode == "bike":
            bike += inside
        else:
            foot += inside
    return (
        total / METERS_PER_MILE,
        foot / METERS_PER_MILE,
        bike / METERS_PER_MILE,
    )


def lines_only(geom):
    """Keep only the linear parts of a geometry (intersections can produce
    stray points). Returns a MultiLineString or None if nothing remains."""
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, LineString):
        return MultiLineString([geom])
    if isinstance(geom, MultiLineString):
        return geom
    if isinstance(geom, GeometryCollection):
        parts = []
        for g in geom.geoms:
            if isinstance(g, LineString):
                parts.append(g)
            elif isinstance(g, MultiLineString):
                parts.extend(g.geoms)
        return MultiLineString(parts) if parts else None
    return None


def store_geom(geom):
    """UTM geometry -> WGS84 GeoJSON string (or None)."""
    return json.dumps(mapping(to_wgs(geom))) if geom is not None else None


def recompute():
    """Recompute coverage for every trail. Returns (summary, results)."""
    init_db()
    with SessionLocal() as session:
        trails = session.query(Trail).all()
        if not trails:
            sys.exit("no trails in database — run scripts/fetch_trails.py first")

        foot_buffers, bike_buffers, all_buffers = [], [], []
        activity_geoms = []  # (mode, utm line) for the in-park odometer
        for act in session.query(Activity):
            line = to_utm(shape(json.loads(act.geometry_geojson)))
            if line.length <= 0:
                continue
            mode = "bike" if (act.mode or "foot") == "bike" else "foot"
            activity_geoms.append((mode, line))
            buf = line.buffer(BUFFER_METERS)
            all_buffers.append((act.id, buf))
            if mode == "bike":
                bike_buffers.append(buf)
            else:
                foot_buffers.append(buf)

        foot_union = unary_union(foot_buffers) if foot_buffers else None
        bike_union = unary_union(bike_buffers) if bike_buffers else None

        session.query(TrailActivity).delete()

        results = []
        trail_lines_utm = []
        for trail in trails:
            geom = to_utm(shape(json.loads(trail.geometry_geojson)))
            trail_lines_utm.append(geom)
            total = geom.length

            covered_foot = (
                lines_only(geom.intersection(foot_union)) if foot_union is not None else None
            )
            bike_cov = geom.intersection(bike_union) if bike_union is not None else None
            if bike_cov is not None and foot_union is not None:
                bike_cov = bike_cov.difference(foot_union)
            covered_bike = lines_only(bike_cov)

            unc = geom
            if foot_union is not None:
                unc = unc.difference(foot_union)
            if bike_union is not None:
                unc = unc.difference(bike_union)
            uncovered = lines_only(unc)

            foot_len = covered_foot.length if covered_foot is not None else 0.0
            bike_len = covered_bike.length if covered_bike is not None else 0.0
            pct_foot = foot_len / total if total > 0 else 0.0
            pct_bike = bike_len / total if total > 0 else 0.0
            pct_total = min(1.0, pct_foot + pct_bike)

            trail.covered_foot_geojson = store_geom(covered_foot)
            trail.covered_bike_geojson = store_geom(covered_bike)
            trail.uncovered_geojson = store_geom(uncovered)
            trail.pct_complete_foot = round(pct_foot, 4)
            trail.pct_complete_bike = round(pct_bike, 4)
            trail.pct_complete_total = round(pct_total, 4)
            trail.is_complete = pct_total >= COMPLETION_THRESHOLD

            tminx, tminy, tmaxx, tmaxy = geom.bounds
            for act_id, buf in all_buffers:
                bminx, bminy, bmaxx, bmaxy = buf.bounds
                if bminx > tmaxx or bmaxx < tminx or bminy > tmaxy or bmaxy < tminy:
                    continue
                overlap = geom.intersection(buf).length
                if overlap >= MIN_OVERLAP_METERS:
                    session.add(
                        TrailActivity(
                            trail_id=trail.id,
                            activity_id=act_id,
                            overlap_meters=round(overlap, 1),
                        )
                    )
            results.append(
                (trail.name, trail.length_meters, pct_foot, pct_bike, pct_total, trail.is_complete)
            )

        park_total, park_foot, park_bike = in_park_miles(
            session, activity_geoms, trail_lines_utm
        )
        set_meta(session, "in_park_miles", f"{park_total:.4f}")
        set_meta(session, "in_park_foot_miles", f"{park_foot:.4f}")
        set_meta(session, "in_park_bike_miles", f"{park_bike:.4f}")

        session.commit()

        total_m = sum(t.length_meters for t in trails)
        summary = {
            "trails": len(trails),
            "complete": sum(1 for t in trails if t.is_complete),
            "total_m": total_m,
            "covered_m": sum(t.length_meters * t.pct_complete_total for t in trails),
            "foot_m": sum(t.length_meters * t.pct_complete_foot for t in trails),
            "bike_m": sum(t.length_meters * t.pct_complete_bike for t in trails),
            "in_park_miles": park_total,
            "in_park_foot_miles": park_foot,
            "in_park_bike_miles": park_bike,
        }
    return summary, results


def main():
    summary, results = recompute()
    print(f"{'TRAIL':<42} {'LENGTH':>8} {'FOOT':>6} {'BIKE':>6} {'TOTAL':>7}")
    for name, length, foot, bike, total, done in sorted(results, key=lambda r: -r[4]):
        mark = "  COMPLETE" if done else ""
        print(
            f"{name:<42} {length / 1000:6.2f}km {foot * 100:5.1f}% {bike * 100:5.1f}% "
            f"{total * 100:6.1f}%{mark}"
        )
    overall = summary["covered_m"] / summary["total_m"] * 100 if summary["total_m"] else 0
    print(
        f"\nOverall: {overall:.1f}% of {summary['total_m'] / 1000:.1f} km covered "
        f"({summary['foot_m'] / 1000:.1f} km foot, {summary['bike_m'] / 1000:.1f} km bike-only) — "
        f"{summary['complete']}/{summary['trails']} trails complete."
    )
    print(
        f"Lifetime miles inside the park: {summary['in_park_miles']:.1f} mi "
        f"({summary['in_park_foot_miles']:.1f} on foot, "
        f"{summary['in_park_bike_miles']:.1f} by bike)."
    )


if __name__ == "__main__":
    main()
