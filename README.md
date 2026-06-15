# Rock Creek Park Trail Tracker

A personal full-stack web app that maps my progress toward completing every named trail in Washington DC's Rock Creek Park before I leave the city.

Built because I'm a consultant finishing up two years in DC with a specific goal: walk and run every trail in the park before my August 2026 departure. I'd been logging every outdoor activity on Strava and wanted a way to visualize exactly which trail segments I'd covered, which I hadn't, and whether I was on pace to finish in time.

No existing tool does this. Most trail apps track individual hikes. This one tracks cumulative geographic coverage across hundreds of activities over two years — and tells me what to do next.

[Insert screenshot here]

---

## What It Does

- Pulls every GPS activity from Strava and spatially matches it against all 76 named NPS trails in Rock Creek Park (76.2 km of trail)
- Shows exactly which segments of each trail I've covered and which I haven't — at the sub-trail level, not just trail-by-trail
- Differentiates coverage by foot vs. bike with separate color layers
- Tracks a departure deadline with a live pace calculator: miles remaining, miles per week needed, current pace, on track or behind
- Suggests routes that maximize new trail coverage for a given target distance and exports them as GPX files for watch/Strava import
- Ranks partially-completed trails by proximity to done for quick wins

---

## Tech Stack

**Backend:** Python 3.12, FastAPI, SQLAlchemy, SQLite  
**Spatial analysis:** GeoPandas, Shapely, pyproj (UTM 18N projection for accurate meter-based coverage)  
**Frontend:** React 18, Vite, react-leaflet  
**Map tiles:** Stamen Terrain via Stadia Maps  
**Data sources:** NPS Public Trails feature service (ROCR unit), OpenStreetMap Overpass API fallback  
**Activity data:** Strava API v3 with refresh token flow and rate-limit backoff  
**Route export:** GPX 1.1  

---

## How It Works

Trail completion is not a simple "did you visit this trail" check. Each Strava GPS track is buffered by 20 meters (in UTM 18N projection for true meter accuracy), and the union of all activity buffers is intersected against each trail's centerline geometry. A trail is complete when ≥99.5% of its length falls within that union — accounting for GPS imprecision and trail endpoints at gates or junctions.

Coverage is computed separately for foot and bike activities and rendered as distinct map layers.

---

## Running Locally

**Prerequisites:** Python 3.12+, Node.js 18+, a Strava API application

1. Clone the repo and install dependencies:
```bash
cd rock-creek-tracker
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
```

2. Copy `.env.example` to `.env` and add your Strava credentials:
```
STRAVA_CLIENT_ID=your_client_id
STRAVA_CLIENT_SECRET=your_client_secret
STRAVA_REFRESH_TOKEN=your_refresh_token
```

3. Run the data pipeline:
```bash
python scripts/fetch_trails.py
python scripts/authorize_strava.py   # one-time OAuth
python scripts/sync_activities.py
python scripts/compute_progress.py
```

4. Start the app:
```bash
uvicorn backend.main:app --port 8000
```

Open http://localhost:8000.

---

## Syncing New Activities

When you add new Strava activities, run:
```bash
python scripts/sync_activities.py
python scripts/compute_progress.py
```

The sync is idempotent — it skips activities already in the database.

---

## Demo

The live demo uses seeded coverage data rather than my real Strava history. The trail geometries, names, and lengths are genuine NPS data. The completion percentages are fabricated for demonstration purposes.

https://rock-creek-tracker.onrender.com/ 

---

## Deploying the demo

No database files are committed to the repo. The demo database is generated at
build time from genuine NPS/OSM trail geometry, with fabricated coverage and a
handful of synthetic activities — never any real Strava data.

- **Build command:** `bash scripts/build.sh`
  (installs deps, fetches trail geometry, builds `data/demo.db`, builds the frontend)
- **Start command:** `RCT_DB=demo.db uvicorn backend.main:app --host 0.0.0.0 --port $PORT`

The `RCT_DB` environment variable selects which SQLite file under `data/` the app
serves; it defaults to `trails.db` (real data) and is set to `demo.db` for the
public demo. Re-run `python scripts/seed_demo_data.py --reset` any time to
regenerate the seeded data.
