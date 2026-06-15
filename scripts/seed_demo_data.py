#!/usr/bin/env python3
"""Seed a SEPARATE demo database with realistic *fake* coverage for portfolio use.

This never reads or mutates the real database's activities. It builds (or
rebuilds) data/demo.db from the real trails — geometry, names, lengths and the
cached park boundary only, never the real Strava activities — then writes fake
coverage and a few synthetic activities into that demo copy. The real
data/trails.db is left completely untouched.

Run the app against the demo DB with the RCT_DB env var:

    RCT_DB=demo.db uvicorn backend.main:app --port 8000

What it produces in demo.db
---------------------------
* ~40% of trails complete (is_complete = True, pct_complete_total = 1.0)
* ~30% partially complete (pct_complete_total uniform in [0.15, 0.85])
* ~30% untouched (pct_complete_total = 0.0)
* Coverage split ~70% foot / 30% bike across the covered trails, rendered as
  real sub-geometries (covered_foot/bike/uncovered GeoJSON) by slicing each
  trail centerline — so the map shows colored coverage, not just numbers.
* A handful of synthetic recent activities (+ trail_activities links) sized so
  backend.main._current_pace_mpw() reports a pace slightly BELOW the required
  pace toward the Aug 1, 2026 departure — i.e. the tracker reads "BEHIND PACE".

Departure date is the hardcoded DEPARTURE_DATE = date(2026, 8, 1) in
backend/main.py; nothing here needs to set it.

Each run wipes the demo DB's activities and coverage and re-seeds from scratch,
so it's safe to re-run. --reset additionally deletes demo.db entirely and
rebuilds it from the real trails.

Usage:
    python scripts/seed_demo_data.py            # build/refresh demo.db and seed
    python scripts/seed_demo_data.py --reset    # delete demo.db, rebuild, seed
    python scripts/seed_demo_data.py --seed 7   # different random draw
"""
import argparse
import json
import os
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import linemerge, substring, unary_union
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Operate on the demo DB by default (overridable, but never the real one unless
# the caller explicitly asks). Must be set before importing backend.db, which
# reads RCT_DB at import time to bind its engine.
os.environ.setdefault("RCT_DB", "demo.db")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db import (  # noqa: E402
    DATA_DIR,
    Activity,
    Meta,
    SessionLocal,
    Trail,
    TrailActivity,
    init_db,
    set_meta,
)
from backend.geo import to_utm, to_wgs  # noqa: E402

REAL_DB_PATH = DATA_DIR / "trails.db"
DEMO_DB_PATH = DATA_DIR / os.environ["RCT_DB"]
# Read-only session bound to the real database, used to source trail geometry.
_real_engine = create_engine(f"sqlite:///{REAL_DB_PATH}", future=True)
RealSession = sessionmaker(bind=_real_engine, future=True)

# Keep these in sync with the real pipeline.
METERS_PER_MILE = 1609.344
DEPARTURE_DATE = date(2026, 8, 1)  # mirrors backend/main.py

COMPLETE_SHARE = 0.40
PARTIAL_SHARE = 0.30
FOOT_SHARE = 0.70  # fraction of covered trails whose coverage is on foot
PARTIAL_MIN, PARTIAL_MAX = 0.15, 0.85

# Stop trimming once the *measured* pace drops to this fraction of required —
# clearly "behind pace" but only slightly. < 1.0 guarantees a BEHIND reading.
BEHIND_FACTOR = 0.92
# Coarsen synthetic activity tracks (meters) so each pace recompute — which
# buffers and unions them — stays fast. Far below the 20 m coverage buffer, so
# it doesn't change the result.
ACTIVITY_SIMPLIFY_M = 5.0
# Marker so seeded activities can be found and cleared without touching any
# real Strava rows that might exist.
SEED_PREFIX = "[seed]"
SEED_ID_BASE = 9_000_000_001


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def _components(geom):
    """Return the trail's centerline as a list of contiguous LineStrings
    (UTM). MultiLineStrings are stitched where endpoints touch."""
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        merged = linemerge(geom)
        if merged.geom_type == "LineString":
            return [merged]
        return [g for g in merged.geoms if isinstance(g, LineString)]
    return []


def _as_multi(parts):
    parts = [p for p in parts if p is not None and not p.is_empty]
    return MultiLineString(parts) if parts else None


def split_by_fraction(geom, frac):
    """Split a UTM trail centerline into (covered, uncovered) MultiLineStrings,
    where `covered` is the first `frac` of its length. Either side may be None."""
    comps = _components(geom)
    total = sum(c.length for c in comps)
    target = total * frac
    covered, uncovered, acc = [], [], 0.0
    for c in comps:
        if acc >= target:
            uncovered.append(c)
        elif acc + c.length <= target:
            covered.append(c)
            acc += c.length
        else:
            cut = target - acc
            covered.append(substring(c, 0.0, cut))
            uncovered.append(substring(c, cut, c.length))
            acc = target
    return _as_multi(covered), _as_multi(uncovered)


def store_geom(geom):
    """UTM geometry -> WGS84 GeoJSON string (or None)."""
    return json.dumps(mapping(to_wgs(geom))) if geom is not None else None


def longest_component(utm_geom):
    comps = _components(utm_geom)
    return max(comps, key=lambda c: c.length) if comps else None


# --------------------------------------------------------------------------- #
# seeding steps
# --------------------------------------------------------------------------- #
def _reset_coverage(session):
    """Drop all activities and zero every trail's coverage columns in this DB."""
    session.query(TrailActivity).delete()
    session.query(Activity).delete()
    for t in session.query(Trail):
        t.covered_foot_geojson = None
        t.covered_bike_geojson = None
        t.uncovered_geojson = None
        t.pct_complete_foot = 0.0
        t.pct_complete_bike = 0.0
        t.pct_complete_total = 0.0
        t.is_complete = False


def build_demo_db(session):
    """Prepare this database's trails for seeding — zeroed coverage, no
    activities. Works in two modes so it's robust both locally and on deploy:

    * In-place: if this DB already holds trail geometry (e.g. fetch_trails.py
      wrote it straight into demo.db during a Render build), seed onto those
      trails directly. No other database is read.
    * Copy: otherwise source trail geometry + the cached park boundary from the
      real trails.db, leaving its real activity data untouched (local workflow).
    """
    if session.query(Trail).count() > 0:
        _reset_coverage(session)
        session.flush()
        return

    with RealSession() as real:
        real_trails = real.query(Trail).all()
        if not real_trails:
            sys.exit(
                f"no trails found in this DB or {REAL_DB_PATH} — "
                "run scripts/fetch_trails.py first"
            )
        for t in real_trails:
            session.add(
                Trail(
                    id=t.id,
                    name=t.name,
                    geometry_geojson=t.geometry_geojson,
                    length_meters=t.length_meters,
                    source=t.source,
                    description=t.description,
                    pct_complete_foot=0.0,
                    pct_complete_bike=0.0,
                    pct_complete_total=0.0,
                    is_complete=False,
                )
            )
        boundary = real.query(Meta).filter_by(key="park_boundary_geojson").first()
        boundary_value = boundary.value if boundary is not None else None

    if boundary_value is not None:
        set_meta(session, "park_boundary_geojson", boundary_value)
    session.flush()


def seed_coverage(session, rng):
    """Assign the 40/30/30 split and write real sub-geometries for each trail.
    Returns the list of (trail, utm_geom, frac, mode) for covered trails."""
    trails = session.query(Trail).order_by(Trail.name).all()
    if not trails:
        sys.exit("no trails in database — run scripts/fetch_trails.py first")

    order = trails[:]
    rng.shuffle(order)
    n = len(order)
    n_complete = round(n * COMPLETE_SHARE)
    n_partial = round(n * PARTIAL_SHARE)

    covered = []
    for i, trail in enumerate(order):
        utm = to_utm(shape(json.loads(trail.geometry_geojson)))
        if i < n_complete:
            frac = 1.0
        elif i < n_complete + n_partial:
            frac = rng.uniform(PARTIAL_MIN, PARTIAL_MAX)
        else:
            frac = 0.0

        mode = "foot" if rng.random() < FOOT_SHARE else "bike"
        cov, unc = split_by_fraction(utm, frac)

        # reset both mode columns, then fill the chosen one
        trail.covered_foot_geojson = None
        trail.covered_bike_geojson = None
        trail.pct_complete_foot = 0.0
        trail.pct_complete_bike = 0.0
        if frac > 0:
            if mode == "foot":
                trail.covered_foot_geojson = store_geom(cov)
                trail.pct_complete_foot = round(frac, 4)
            else:
                trail.covered_bike_geojson = store_geom(cov)
                trail.pct_complete_bike = round(frac, 4)
            covered.append((trail, utm, frac, mode))

        trail.uncovered_geojson = store_geom(unc)
        trail.pct_complete_total = round(frac, 4)
        trail.is_complete = frac >= 1.0

    return trails, covered


def required_pace(trails, today):
    """Mirror backend/main.py's required-pace math from the seeded coverage."""
    total_miles = sum(t.length_meters for t in trails) / METERS_PER_MILE
    covered_miles = (
        sum(t.length_meters * t.pct_complete_total for t in trails) / METERS_PER_MILE
    )
    remaining_miles = max(0.0, total_miles - covered_miles)
    weeks_remaining = (DEPARTURE_DATE - today).days / 7
    required = remaining_miles / weeks_remaining if weeks_remaining > 0 else 0.0
    return remaining_miles, required


def _add_activity(session, act_id, trail, utm_slice, seg_len, days_ago, mode, today):
    when = today - timedelta(days=days_ago)
    session.add(
        Activity(
            id=act_id,
            name=f"{SEED_PREFIX} {trail.name} ({mode})",
            sport_type="Ride" if mode == "bike" else "Run",
            mode=mode,
            start_date=f"{when.isoformat()}T08:00:00Z",
            geometry_geojson=json.dumps(mapping(to_wgs(utm_slice))),
        )
    )
    session.add(
        TrailActivity(
            trail_id=trail.id, activity_id=act_id, overlap_meters=round(seg_len, 1)
        )
    )


def _local_pace(selected, today):
    """In-memory copy of backend.main._current_pace_mpw's math, reusing the
    already-projected trail/slice geometry so calibration doesn't re-parse and
    re-project every trail on each iteration.

    Each candidate is a recent activity on its own trail, and the anchor sits on
    a separate trail, so (matching production) there is no older overlap to
    subtract: new coverage = each recent trail ∩ the union of recent buffers."""
    if not selected:
        return 0.0
    recent_union = unary_union([c["buf"] for c in selected])
    new_m = sum(c["utm"].intersection(recent_union).length for c in selected)
    return (new_m / METERS_PER_MILE) / 4.0


def seed_activities(session, covered, required, today, rng):
    """Create synthetic recent activities so the real _current_pace_mpw() reports
    a pace slightly below `required` (clearly "behind pace"). Returns
    (n_recent, confirmed_pace).

    Slice lengths don't map cleanly to the geometric pace — a recent activity's
    20 m buffer also clips neighboring trails — so we over-seed a generous pool,
    calibrate it down with the fast in-memory _local_pace(), then write the final
    selection and confirm once with the production function. An older "anchor"
    activity on a separate trail supplies the >=7 days of history the estimator
    needs without eroding the recent coverage."""
    import backend.main as bm

    target_hi = required * BEHIND_FACTOR
    target_lo = required * (BEHIND_FACTOR - 0.12)
    cap_m = required * 1.6 * 4.0 * METERS_PER_MILE  # over-seed ~1.6x for trimming

    # Build the candidate pool entirely in memory (no DB writes yet).
    pool = sorted(covered, key=lambda c: (c[2] >= 1.0, rng.random()))
    cands = []
    next_id = SEED_ID_BASE
    acc = 0.0
    for trail, utm, _frac, _mode in pool:
        if acc >= cap_m:
            break
        comp = longest_component(utm)
        if comp is None or comp.length < 200:
            continue
        seg_len = min(comp.length, rng.uniform(800, 3500))
        slice_ = substring(comp, 0.0, seg_len).simplify(ACTIVITY_SIMPLIFY_M)
        cands.append(
            {
                "id": next_id,
                "trail": trail,
                "utm": utm,  # full-res projected trail (cached from seed_coverage)
                "slice": slice_,
                "buf": slice_.buffer(20.0),  # 20 m == backend BUFFER_METERS
                "seg": seg_len,
                "days": rng.randint(2, 26),
            }
        )
        acc += seg_len
        next_id += 1

    # --- calibrate selection with the fast in-memory model ---
    selected = cands[:]
    current = _local_pace(selected, today)
    if current > target_hi:
        total_len = sum(c["seg"] for c in selected)
        # Pace drops slower than slice length (neighbor overlap), so aim the
        # bulk trim a notch below the ceiling; keep the smallest (many short
        # outings) up to that share and drop the rest.
        keep_frac = (required * (BEHIND_FACTOR - 0.10)) / current
        keep, kept = [], 0.0
        for c in sorted(selected, key=lambda c: c["seg"]):
            if kept + c["seg"] <= keep_frac * total_len or len(keep) < 2:
                keep.append(c)
                kept += c["seg"]
        selected = keep
        current = _local_pace(selected, today)
    while current > target_hi and len(selected) > 2:  # cheap single-trim cleanup
        selected.remove(max(selected, key=lambda c: c["seg"]))
        current = _local_pace(selected, today)

    # --- write the chosen recent activities ---
    for c in selected:
        _add_activity(
            session, c["id"], c["trail"], c["slice"], c["seg"], c["days"], "foot", today
        )

    # Anchor: older activity on a trail NOT in the recent set (>=7 days history).
    recent_trail_ids = {c["trail"].id for c in selected}
    anchor = next(
        (c for c in covered if c[0].id not in recent_trail_ids and c[2] >= 1.0),
        next((c for c in covered if c[0].id not in recent_trail_ids),
             covered[0] if covered else None),
    )
    if anchor is not None:
        comp = longest_component(anchor[1])
        if comp is not None:
            a_slice = substring(comp, 0.0, min(comp.length, 2500.0)).simplify(
                ACTIVITY_SIMPLIFY_M
            )
            _add_activity(
                session, next_id, anchor[0], a_slice, a_slice.length, 50, "foot", today
            )

    # --- confirm once against the production pace function ---
    session.flush()
    bm._pace_cache.clear()
    confirmed = bm._current_pace_mpw(session, today)
    if confirmed is not None and confirmed < target_lo:
        print(
            f"  note: pace settled at {confirmed:.2f} mi/wk, a bit below the "
            f"{target_lo:.2f}-{target_hi:.2f} target band (limited trail length).",
            file=sys.stderr,
        )
    return len(selected), confirmed


def seed_in_park_meta(session, trails):
    """Plausible lifetime in-park odometer (you re-walk trails), so the stats
    panel's mileage line isn't blank in the demo."""
    foot = sum(t.length_meters * t.pct_complete_foot for t in trails) / METERS_PER_MILE
    bike = sum(t.length_meters * t.pct_complete_bike for t in trails) / METERS_PER_MILE
    foot_total = foot * 1.8  # repeats on foot
    bike_total = bike * 1.4
    set_meta(session, "in_park_foot_miles", f"{foot_total:.4f}")
    set_meta(session, "in_park_bike_miles", f"{bike_total:.4f}")
    set_meta(session, "in_park_miles", f"{foot_total + bike_total:.4f}")


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="wipe all coverage columns (and seeded activities) before seeding",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="RNG seed for a reproducible draw"
    )
    args = parser.parse_args()
    rng = random.Random(args.seed)
    today = date.today()

    # Safety: never operate on the real database.
    if DEMO_DB_PATH.resolve() == REAL_DB_PATH.resolve():
        sys.exit(
            "refusing to seed the real database — set RCT_DB to a demo file "
            "(default demo.db), not trails.db"
        )
    if args.reset and DEMO_DB_PATH.exists():
        DEMO_DB_PATH.unlink()

    init_db()
    with SessionLocal() as session:
        build_demo_db(session)
        trails, covered = seed_coverage(session, rng)
        remaining_miles, required = required_pace(trails, today)
        n_recent, current_pace = seed_activities(
            session, covered, required, today, rng
        )
        seed_in_park_meta(session, trails)
        session.commit()

        complete = sum(1 for t in trails if t.is_complete)
        partial = sum(
            1 for t in trails if not t.is_complete and t.pct_complete_total > 0
        )
        untouched = len(trails) - complete - partial

    print(f"Seeded {len(trails)} trails into {DEMO_DB_PATH} (rng seed {args.seed}):")
    print(f"  complete : {complete:3d}  ({complete / len(trails) * 100:.0f}%)")
    print(f"  partial  : {partial:3d}  ({partial / len(trails) * 100:.0f}%)")
    print(f"  untouched: {untouched:3d}  ({untouched / len(trails) * 100:.0f}%)")
    print(
        f"  foot/bike covered trails: "
        f"{sum(1 for c in covered if c[3] == 'foot')}/"
        f"{sum(1 for c in covered if c[3] == 'bike')}"
    )
    current_str = f"{current_pace:.2f}" if current_pace is not None else "n/a"
    behind = current_pace is not None and current_pace < required
    print(
        f"Deadline ({DEPARTURE_DATE.isoformat()}): "
        f"{remaining_miles:.1f} mi remaining, "
        f"required {required:.2f} mi/wk, "
        f"current {current_str} mi/wk "
        f"({n_recent} recent activities) -> "
        f"{'BEHIND PACE' if behind else 'on track'}"
    )
    print(
        f"Done. Run the demo with:  RCT_DB={os.environ['RCT_DB']} "
        f"uvicorn backend.main:app --port 8000"
    )


if __name__ == "__main__":
    main()
