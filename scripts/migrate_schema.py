#!/usr/bin/env python3
"""Schema-breaking migration to per-mode, per-segment coverage.

1. Backs up data/trails.db to trails.db.bak
2. Drops + recreates the trails / trail_activities tables (new columns),
   adds the `mode` column to activities and classifies cached activities
3. Re-fetches trail geometries from the NPS source (with normalization
   and descriptions)
4. Re-runs the spatial computation against the preserved activities
5. Prints confirmation

Activities are never re-downloaded; the cached GPS tracks are kept.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from backend.db import (  # noqa: E402
    DB_PATH,
    Activity,
    SessionLocal,
    Trail,
    TrailActivity,
    engine,
    init_db,
)
from backend.strava import classify_mode  # noqa: E402
from scripts.fetch_trails import acquire_and_build, persist, print_report  # noqa: E402
from scripts.compute_progress import recompute  # noqa: E402


def main():
    if DB_PATH.exists():
        backup = Path(str(DB_PATH) + ".bak")
        shutil.copy2(DB_PATH, backup)
        print(f"[1/5] backed up {DB_PATH.name} -> {backup.name}")
    else:
        print("[1/5] no existing database; starting fresh")

    TrailActivity.__table__.drop(engine, checkfirst=True)
    Trail.__table__.drop(engine, checkfirst=True)
    init_db()
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(activities)"))]
        if "mode" not in cols:
            conn.execute(text("ALTER TABLE activities ADD COLUMN mode VARCHAR"))
    with SessionLocal() as session:
        counts = {"foot": 0, "bike": 0}
        for act in session.query(Activity):
            act.mode = classify_mode(act.sport_type)
            counts[act.mode] += 1
        session.commit()
    print(
        f"[2/5] schema recreated; classified {sum(counts.values())} cached activities "
        f"({counts['foot']} foot, {counts['bike']} bike)"
    )

    trails, source, report = acquire_and_build()
    persist(trails)
    print_report(report)
    print(f"[3/5] {len(trails)} trails re-fetched from {source.upper()}")

    summary, _ = recompute()
    overall = summary["covered_m"] / summary["total_m"] * 100 if summary["total_m"] else 0
    print(
        f"[4/5] coverage recomputed: {overall:.1f}% of {summary['total_m'] / 1000:.1f} km "
        f"({summary['foot_m'] / 1000:.1f} km foot, {summary['bike_m'] / 1000:.1f} km bike-only), "
        f"{summary['complete']}/{summary['trails']} trails complete"
    )
    print("[5/5] migration complete.")


if __name__ == "__main__":
    main()
