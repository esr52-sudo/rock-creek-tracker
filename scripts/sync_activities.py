#!/usr/bin/env python3
"""Phase 2: pull GPS tracks for Strava activities that enter Rock Creek Park.

Walks the athlete's full activity history, cheaply filters by the summary
polyline against the park's bounding box, then fetches the full-resolution
latlng stream only for matching activities (one API call each, throttled to
respect Strava's 100 requests / 15 min limit).
"""
import json
import sys
import time
from pathlib import Path

import polyline as polyline_codec
from dotenv import load_dotenv
from shapely.geometry import LineString, mapping, shape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from backend.db import Activity, SessionLocal, Trail, init_db  # noqa: E402
from backend.strava import StravaClient, classify_mode  # noqa: E402

BBOX_PAD_DEG = 0.005  # ~500 m of slack around the trail network


def park_bbox(session):
    bounds = None
    for trail in session.query(Trail):
        b = shape(json.loads(trail.geometry_geojson)).bounds
        bounds = b if bounds is None else (
            min(bounds[0], b[0]), min(bounds[1], b[1]),
            max(bounds[2], b[2]), max(bounds[3], b[3]),
        )
    if bounds is None:
        sys.exit("no trails in database — run scripts/fetch_trails.py first")
    return (
        bounds[0] - BBOX_PAD_DEG, bounds[1] - BBOX_PAD_DEG,
        bounds[2] + BBOX_PAD_DEG, bounds[3] + BBOX_PAD_DEG,
    )


def enters_bbox(latlng_points, bbox):
    minx, miny, maxx, maxy = bbox
    return any(
        miny <= lat <= maxy and minx <= lon <= maxx for lat, lon in latlng_points
    )


def main():
    init_db()
    client = StravaClient(env_path=ROOT / ".env")
    scanned = matched = added = 0

    with SessionLocal() as session:
        bbox = park_bbox(session)
        known = {row[0] for row in session.query(Activity.id)}

        for summary in client.iter_activities():
            scanned += 1
            poly = (summary.get("map") or {}).get("summary_polyline")
            if not poly:
                continue  # no GPS (trainer rides, manual entries, etc.)
            try:
                summary_pts = polyline_codec.decode(poly)
            except (ValueError, IndexError):
                continue
            if not enters_bbox(summary_pts, bbox):
                continue
            matched += 1
            if summary["id"] in known:
                continue

            latlng = client.activity_latlng_stream(summary["id"])
            pts = latlng if len(latlng) >= 2 else summary_pts
            coords = [(lon, lat) for lat, lon in pts]
            if len(coords) < 2:
                continue
            sport = summary.get("sport_type") or summary.get("type")
            session.add(
                Activity(
                    id=summary["id"],
                    name=summary.get("name"),
                    sport_type=sport,
                    mode=classify_mode(sport),
                    start_date=summary.get("start_date_local"),
                    geometry_geojson=json.dumps(mapping(LineString(coords))),
                )
            )
            session.commit()
            added += 1
            print(f"  + {summary.get('start_date_local', '')[:10]}  {summary.get('name')}")
            time.sleep(0.6)  # stay well inside Strava rate limits

    print(
        f"\nScanned {scanned} activities; {matched} pass through Rock Creek Park; "
        f"{added} new tracks stored ({matched - added} already cached)."
    )
    print("Next: python scripts/compute_progress.py")


if __name__ == "__main__":
    main()
