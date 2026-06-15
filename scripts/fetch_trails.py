#!/usr/bin/env python3
"""Phase 1: acquire Rock Creek Park (ROCR) trail geometries.

Order of preference:
  1. NPS IRMA DataStore — searched programmatically for a downloadable
     ROCR trails GeoJSON (best-effort; IRMA rarely exposes direct links).
  2. NPS public trails ArcGIS feature service (the authoritative dataset
     behind the IRMA trail references), filtered to UNITCODE='ROCR'.
  3. OpenStreetMap Overpass API fallback.

Names are normalized (whitespace, Glover-Archbold variants, non-trail
features dropped, near-duplicate names merged) before segments sharing a
name are merged into single geometries and persisted to SQLite.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import linemerge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db import SessionLocal, Trail, init_db  # noqa: E402
from backend.geo import to_utm  # noqa: E402

USER_AGENT = "rock-creek-tracker/0.1 (personal trail completion app)"
UNIT_CODE = "ROCR"

IRMA_SEARCH_URL = "https://irma.nps.gov/DataStore/api/v1/rest/QuickSearch"
NPS_TRAILS_URL = (
    "https://mapservices.nps.gov/arcgis/rest/services/NationalDatasets/"
    "NPS_Public_Trails/FeatureServer/0/query"
)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = """
[out:json][timeout:60];
area["name"="Rock Creek Park"]["boundary"="national_park"]->.rcp;
(
  way["highway"~"path|footway|track"]["name"](area.rcp);
);
out geom;
"""
# OSM tagging for the park boundary varies; try these if the primary
# area filter matches nothing.
OVERPASS_AREA_VARIANTS = [
    '["name"="Rock Creek Park"]["boundary"="national_park"]',
    '["name"="Rock Creek Park"]["boundary"="protected_area"]',
    '["name"="Rock Creek Park"]["leisure"="park"]',
]

# ---------- name normalization ----------

GLOVER_CANONICAL = "Glover-Archbold Trail"
GLOVER_VARIANTS = {
    "glover-archbold trail",
    "glover-archbold trails",
    "glover archbold foot trail",
}
REMOVE_EXACT = {"Potomac River", "Trail Bridge to Cumberland"}
# names lacking all of these are likely non-trail features
TRAIL_KEYWORDS = ("trail", "path", "way", "run", "loop", "ridge", "creek", "branch")


def normalize_records(records):
    """Apply naming rules 1-3; returns (kept_records, report)."""
    report = {"original": len(records), "removed": [], "renamed": {}}
    kept = []
    for rec in records:
        name = rec["name"].strip()
        if name.lower() in GLOVER_VARIANTS and name != GLOVER_CANONICAL:
            report["renamed"][name] = GLOVER_CANONICAL
            name = GLOVER_CANONICAL
        lowered = name.lower()
        if name in REMOVE_EXACT or not any(k in lowered for k in TRAIL_KEYWORDS):
            if name not in report["removed"]:
                report["removed"].append(name)
            continue
        rec = dict(rec, name=name)
        kept.append(rec)
    return kept, report


def canonical_key(name):
    """Key under which near-duplicate names (trailing 's', hyphen vs space)
    collapse together."""
    key = re.sub(r"\s+", " ", name.lower().replace("-", " ")).strip()
    if key.endswith("s"):
        key = key[:-1]
    return key


# ---------- description extraction ----------


def nps_description(props, name):
    """First non-empty NPS metadata field that isn't just the name again."""
    for field in ("NOTES", "TRLALTNAME", "COMMENTS", "TRLNAME"):
        value = props.get(field)
        if isinstance(value, str) and value.strip() and value.strip() != name:
            return value.strip()
    return None


def wikipedia_summary(tag):
    """Resolve an OSM wikipedia=lang:Title tag to the article's first sentence."""
    try:
        lang, _, title = tag.partition(":")
        if not title:
            lang, title = "en", tag
        resp = requests.get(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if resp.status_code == 200:
            extract = (resp.json().get("extract") or "").strip()
            if extract:
                first = extract.split(". ")[0].rstrip(".")
                return first + "."
    except (requests.RequestException, ValueError):
        pass
    return None


def osm_description(tags):
    for key in ("description", "note"):
        if tags.get(key, "").strip():
            return tags[key].strip()
    if tags.get("wikipedia", "").strip():
        summary = wikipedia_summary(tags["wikipedia"].strip())
        if summary:
            return summary
    if tags.get("operator", "").strip():
        return tags["operator"].strip()
    return None


# ---------- sources ----------


def fetch_nps_irma():
    """Best-effort search of the IRMA DataStore for a direct trails download."""
    try:
        resp = requests.get(
            IRMA_SEARCH_URL,
            params={"q": f"{UNIT_CODE} trails geojson", "top": 25},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        results = resp.json()
        items = results if isinstance(results, list) else results.get("items", [])
        for item in items:
            for f in item.get("files", []) or []:
                url = f.get("downloadLink") or f.get("url") or ""
                if url.lower().endswith((".geojson", ".json")):
                    geo = requests.get(
                        url, headers={"User-Agent": USER_AGENT}, timeout=60
                    ).json()
                    records = []
                    for feat in geo.get("features", []):
                        props = feat.get("properties") or {}
                        name = (props.get("TRLNAME") or "").strip()
                        if not name or not feat.get("geometry"):
                            continue
                        records.append(
                            {
                                "name": name,
                                "geometry": shape(feat["geometry"]),
                                "description": nps_description(props, name),
                            }
                        )
                    if records:
                        return records
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def fetch_nps_arcgis():
    """ROCR trails from the official NPS public-trails feature service."""
    try:
        resp = requests.get(
            NPS_TRAILS_URL,
            params={
                "where": f"UNITCODE='{UNIT_CODE}'",
                "outFields": "*",
                "outSR": "4326",
                "f": "geojson",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=60,
        )
        resp.raise_for_status()
        features = resp.json().get("features") or []
    except (requests.RequestException, ValueError):
        return None

    records = []
    for feat in features:
        props = feat.get("properties") or {}
        status = (props.get("TRLSTATUS") or "").strip().lower()
        if status and status not in ("existing", "maintained"):
            continue
        name = (
            props.get("TRLNAME") or props.get("MAPLABEL") or props.get("TRLALTNAME") or ""
        ).strip()
        if not name or name.lower() in ("unknown", "no name", "unnamed"):
            continue
        if not feat.get("geometry"):
            continue
        geom = shape(feat["geometry"])
        if geom.is_empty:
            continue
        records.append(
            {"name": name, "geometry": geom, "description": nps_description(props, name)}
        )
    return records or None


def fetch_osm():
    """Named path/footway/track ways inside the park, via Overpass."""
    for area_filter in OVERPASS_AREA_VARIANTS:
        query = OVERPASS_QUERY.replace(
            '["name"="Rock Creek Park"]["boundary"="national_park"]', area_filter
        )
        resp = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=90,
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
        records = []
        for el in elements:
            if el.get("type") != "way" or "geometry" not in el:
                continue
            tags = el.get("tags") or {}
            name = tags.get("name", "").strip()
            coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
            if not name or len(coords) < 2:
                continue
            records.append(
                {
                    "name": name,
                    "geometry": LineString(coords),
                    "description": osm_description(tags),
                }
            )
        if records:
            return records
    return None


# ---------- merge + persist ----------


def merge_records(records, source):
    """Group by canonical name key (rule 4), merging geometries.
    Returns (trails, merges) where merges lists (kept_name, [variant_names])."""
    groups = {}
    for rec in records:
        groups.setdefault(canonical_key(rec["name"]), []).append(rec)

    trails, merges = [], []
    for recs in groups.values():
        names = sorted({r["name"] for r in recs}, key=lambda n: (len(n), n))
        display = names[0]  # shortest/simplest name wins
        if len(names) > 1:
            merges.append((display, names[1:]))
        parts = []
        for rec in recs:
            geom = rec["geometry"]
            if isinstance(geom, LineString):
                parts.append(geom)
            elif isinstance(geom, MultiLineString):
                parts.extend(geom.geoms)
        if not parts:
            continue
        merged = parts[0] if len(parts) == 1 else linemerge(MultiLineString(parts))
        trails.append(
            {
                "name": display,
                "geometry": merged,
                "length_meters": to_utm(merged).length,
                "source": source,
                "description": next(
                    (r["description"] for r in recs if r.get("description")), None
                ),
            }
        )
    return trails, merges


def acquire_and_build():
    """Full acquisition pipeline. Returns (trails, source, report)."""
    print("Trying NPS IRMA DataStore ...")
    records = fetch_nps_irma()
    source = "nps" if records else None
    if not records:
        print("  no direct IRMA download found; trying NPS trails feature service ...")
        records = fetch_nps_arcgis()
        if records:
            source = "nps"
    if not records:
        print("  NPS unavailable; falling back to OpenStreetMap Overpass ...")
        records = fetch_osm()
        source = "osm"
    if not records:
        sys.exit("error: no trail geometries available from NPS or OSM")

    records, report = normalize_records(records)
    trails, merges = merge_records(records, source)
    report["merges"] = merges
    report["final"] = len(trails)
    return trails, source, report


def print_report(report):
    print("\nNormalization report")
    print(f"  raw segments:      {report['original']}")
    for old, new in report["renamed"].items():
        print(f"  renamed:           {old!r} -> {new!r}")
    for name in report["removed"]:
        print(f"  removed:           {name!r}")
    for kept, variants in report["merges"]:
        print(f"  merged into {kept!r}: {', '.join(repr(v) for v in variants)}")
    print(f"  final trails:      {report['final']}")


def persist(trails):
    init_db()
    with SessionLocal() as session:
        existing = {t.name: t for t in session.query(Trail)}
        for rec in trails:
            row = existing.pop(rec["name"], None)
            if row is None:
                row = Trail(name=rec["name"])
                session.add(row)
            row.geometry_geojson = json.dumps(mapping(rec["geometry"]))
            row.length_meters = rec["length_meters"]
            row.source = rec["source"]
            row.description = rec["description"]
        for stale in existing.values():
            session.delete(stale)
        session.commit()


def main():
    trails, source, report = acquire_and_build()
    persist(trails)
    print_report(report)

    total_km = sum(t["length_meters"] for t in trails) / 1000
    print(f"\n{len(trails)} trails loaded from {source.upper()}, "
          f"{total_km:.1f} km of trail total.")
    for t in sorted(trails, key=lambda t: -t["length_meters"]):
        print(f"  {t['name']:<42} {t['length_meters'] / 1000:6.2f} km")


if __name__ == "__main__":
    main()
