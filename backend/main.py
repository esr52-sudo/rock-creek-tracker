"""FastAPI app serving trail geometries, completion stats, and the built
frontend (if frontend/dist exists)."""
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import shape
from shapely.ops import unary_union

from .db import (
    Activity,
    SessionLocal,
    SuggestedRoute,
    Trail,
    TrailActivity,
    get_meta,
    init_db,
)
from .geo import to_utm
from .router import METERS_PER_MILE, build_gpx, build_route

ROOT = Path(__file__).resolve().parents[1]
MAX_LINKED_ACTIVITIES = 8
DEPARTURE_DATE = date(2026, 8, 1)
BUFFER_METERS = 20.0  # must match scripts/compute_progress.py

app = FastAPI(title="Rock Creek Park Trail Tracker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
init_db()


def _maybe_geojson(text):
    return json.loads(text) if text else None


@app.get("/api/trails")
def list_trails():
    with SessionLocal() as session:
        links = defaultdict(list)
        rows = (
            session.query(TrailActivity, Activity)
            .join(Activity, TrailActivity.activity_id == Activity.id)
            .order_by(TrailActivity.overlap_meters.desc())
            .all()
        )
        for link, act in rows:
            if len(links[link.trail_id]) < MAX_LINKED_ACTIVITIES:
                links[link.trail_id].append(
                    {
                        "id": act.id,
                        "name": act.name,
                        "mode": act.mode,
                        "date": (act.start_date or "")[:10],
                        "url": f"https://www.strava.com/activities/{act.id}",
                    }
                )

        features = [
            {
                "type": "Feature",
                "geometry": json.loads(trail.geometry_geojson),
                "properties": {
                    "id": trail.id,
                    "name": trail.name,
                    "description": trail.description,
                    "length_meters": trail.length_meters,
                    "pct_complete_foot": trail.pct_complete_foot,
                    "pct_complete_bike": trail.pct_complete_bike,
                    "pct_complete_total": trail.pct_complete_total,
                    "is_complete": trail.is_complete,
                    "source": trail.source,
                    "segments": {
                        "foot": _maybe_geojson(trail.covered_foot_geojson),
                        "bike": _maybe_geojson(trail.covered_bike_geojson),
                        "uncovered": _maybe_geojson(trail.uncovered_geojson),
                    },
                    "activities": links.get(trail.id, []),
                },
            }
            for trail in session.query(Trail).order_by(Trail.name)
        ]
    return {"type": "FeatureCollection", "features": features}


def _meta_float(session, key):
    raw = get_meta(session, key)
    try:
        return round(float(raw), 1) if raw is not None else None
    except ValueError:
        return None


@app.get("/api/stats")
def stats():
    with SessionLocal() as session:
        trails = session.query(Trail).all()
        in_park_miles = _meta_float(session, "in_park_miles")
        in_park_foot_miles = _meta_float(session, "in_park_foot_miles")
        in_park_bike_miles = _meta_float(session, "in_park_bike_miles")
    total_m = sum(t.length_meters for t in trails)
    covered_m = sum(t.length_meters * t.pct_complete_total for t in trails)
    foot_m = sum(t.length_meters * t.pct_complete_foot for t in trails)
    bike_m = sum(t.length_meters * t.pct_complete_bike for t in trails)
    complete = sum(1 for t in trails if t.is_complete)
    partial = sum(1 for t in trails if not t.is_complete and t.pct_complete_total > 0)

    def pct(value):
        return round((value / total_m) * 100, 1) if total_m else 0.0

    return {
        "total_trails": len(trails),
        "complete": complete,
        "partial": partial,
        "incomplete": len(trails) - complete - partial,
        "total_km": round(total_m / 1000, 1),
        "covered_km": round(covered_m / 1000, 1),
        "covered_foot_km": round(foot_m / 1000, 1),
        "covered_bike_km": round(bike_m / 1000, 1),
        "overall_pct": pct(covered_m),
        "overall_pct_foot": pct(foot_m),
        "overall_pct_bike": pct(bike_m),
        "in_park_miles": in_park_miles,
        "in_park_foot_miles": in_park_foot_miles,
        "in_park_bike_miles": in_park_bike_miles,
    }


# ---------- departure deadline ----------

_pace_cache = {}


def _activity_date(act):
    try:
        return date.fromisoformat((act.start_date or "")[:10])
    except ValueError:
        return None


def _current_pace_mpw(session, today):
    """New trail miles covered in the last 28 days, divided by 4.

    "New" means trail centerline inside the buffers of recent contributing
    activities but NOT inside any older contributing activity's buffer —
    re-walking an already-covered section doesn't count. Returns None when
    there is less than 7 days of activity history."""
    contributing = {r[0] for r in session.query(TrailActivity.activity_id).distinct()}
    if not contributing:
        return None
    acts = session.query(Activity).filter(Activity.id.in_(contributing)).all()
    dated = [(a, _activity_date(a)) for a in acts]
    dated = [(a, d) for a, d in dated if d is not None]
    if not dated:
        return None
    if (today - min(d for _, d in dated)).days < 7:
        return None  # not enough history to estimate a pace

    window_start = today - timedelta(days=28)
    recent_ids = {a.id for a, d in dated if d >= window_start}
    if not recent_ids:
        return 0.0

    cache_key = (today.isoformat(), len(contributing), max(contributing))
    if cache_key in _pace_cache:
        return _pace_cache[cache_key]

    trail_ids = {
        link.trail_id
        for link in session.query(TrailActivity).filter(
            TrailActivity.activity_id.in_(recent_ids)
        )
    }
    older_ids = {
        link.activity_id
        for link in session.query(TrailActivity).filter(
            TrailActivity.trail_id.in_(trail_ids)
        )
    } - recent_ids

    def buffer_union(ids):
        if not ids:
            return None
        return unary_union(
            [
                to_utm(shape(json.loads(a.geometry_geojson))).buffer(BUFFER_METERS)
                for a in session.query(Activity).filter(Activity.id.in_(ids))
            ]
        )

    recent_union = buffer_union(recent_ids)
    older_union = buffer_union(older_ids)
    new_m = 0.0
    for trail in session.query(Trail).filter(Trail.id.in_(trail_ids)):
        geom = to_utm(shape(json.loads(trail.geometry_geojson)))
        covered = geom.intersection(recent_union)
        if older_union is not None:
            covered = covered.difference(older_union)
        new_m += covered.length

    pace = (new_m / METERS_PER_MILE) / 4.0
    _pace_cache.clear()  # only ever keep today's answer
    _pace_cache[cache_key] = pace
    return pace


@app.get("/api/deadline")
def deadline():
    today = date.today()
    days_remaining = (DEPARTURE_DATE - today).days
    weeks_remaining = days_remaining / 7
    with SessionLocal() as session:
        trails = session.query(Trail).all()
        total_miles = sum(t.length_meters for t in trails) / METERS_PER_MILE
        covered_miles = (
            sum(t.length_meters * t.pct_complete_total for t in trails) / METERS_PER_MILE
        )
        current = _current_pace_mpw(session, today) if days_remaining > 0 else None
    remaining_miles = max(0.0, total_miles - covered_miles)
    required = remaining_miles / weeks_remaining if weeks_remaining > 0 else None
    on_track = current is not None and required is not None and current >= required
    days_per_mile = (
        days_remaining / remaining_miles
        if remaining_miles > 0 and days_remaining > 0
        else None
    )
    return {
        "departure_date": DEPARTURE_DATE.isoformat(),
        "days_remaining": days_remaining,
        "weeks_remaining": round(weeks_remaining, 2),
        "total_trail_miles": round(total_miles, 2),
        "covered_miles": round(covered_miles, 2),
        "remaining_miles": round(remaining_miles, 2),
        "required_pace_mpw": round(required, 2) if required is not None else None,
        "current_pace_mpw": round(current, 2) if current is not None else None,
        "on_track": on_track,
        "days_per_trail_mile": round(days_per_mile, 2) if days_per_mile is not None else None,
    }


# ---------- route suggester ----------


class RouteRequest(BaseModel):
    start_lat: float = 38.9497
    start_lng: float = -77.0523
    target_miles: float = Field(default=5.0, ge=1.0, le=20.0)


@app.post("/api/suggest-route")
def suggest_route(req: RouteRequest):
    try:
        return build_route(req.start_lat, req.start_lng, req.target_miles)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))


@app.get("/api/suggest-route/gpx/{route_id}")
def download_route_gpx(route_id: str):
    with SessionLocal() as session:
        route = session.get(SuggestedRoute, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="route not found")
    filename = f"rock-creek-route-{(route.created_at or '')[:10]}.gpx"
    return Response(
        content=build_gpx(route),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_dist = ROOT / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
