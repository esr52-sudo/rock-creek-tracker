#!/usr/bin/env bash
#
# Build step for the portfolio demo deployment.
#
# No database files are committed to git. This regenerates the real NPS/OSM
# trail geometry, caches the park boundary, produces data/demo.db (seeded fake
# coverage), and builds the frontend — all at build time. The demo never uses
# real Strava activity data.
#
# Start the app afterwards with:
#     RCT_DB=demo.db uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
#
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Installing Python dependencies"
pip install -r requirements.txt

echo "==> Fetching NPS/OSM trail geometry (trails.db, no activities)"
python scripts/fetch_trails.py

echo "==> Caching park boundary so the demo is self-contained at runtime"
python - <<'PY'
import sys
sys.path.insert(0, ".")
from backend.boundary import get_park_boundary
from backend.db import SessionLocal

with SessionLocal() as session:
    get_park_boundary(session)
    session.commit()
PY

echo "==> Building seeded demo database (data/demo.db)"
python scripts/seed_demo_data.py --reset

echo "==> Building frontend"
cd frontend
npm ci
npm run build

echo "==> Build complete. Start with:"
echo "    RCT_DB=demo.db uvicorn backend.main:app --host 0.0.0.0 --port \${PORT:-8000}"
