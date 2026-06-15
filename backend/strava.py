"""Strava API client.

Uses the refresh-token flow exclusively: a fresh access token is obtained
at the start of every session (access tokens expire after 6 hours and are
never persisted). If Strava rotates the refresh token, the new value is
written back to .env so the next run keeps working.
"""
import os
import time

import requests
from dotenv import set_key

TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

FOOT_TYPES = {"Run", "Walk", "Hike", "TrailRun", "VirtualRun"}
BIKE_TYPES = {"Ride", "MountainBikeRide", "GravelRide", "EBikeRide", "VirtualRide"}


def classify_mode(sport_type):
    """'foot' | 'bike' for a Strava sport type. Unknown types default to foot."""
    return "bike" if (sport_type or "") in BIKE_TYPES else "foot"


class StravaClient:
    def __init__(self, env_path=None):
        self.env_path = str(env_path) if env_path else None
        try:
            self.client_id = os.environ["STRAVA_CLIENT_ID"]
            self.client_secret = os.environ["STRAVA_CLIENT_SECRET"]
            self.refresh_token = os.environ["STRAVA_REFRESH_TOKEN"]
        except KeyError as missing:
            raise SystemExit(
                f"Missing {missing} — copy .env.example to .env and fill in your Strava credentials"
            ) from None
        self._access_token = None

    def _refresh_access_token(self) -> None:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Strava token refresh failed ({resp.status_code}): {resp.text[:300]}"
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        rotated = payload.get("refresh_token")
        if rotated and rotated != self.refresh_token:
            self.refresh_token = rotated
            if self.env_path:
                set_key(self.env_path, "STRAVA_REFRESH_TOKEN", rotated)
                print("note: Strava rotated the refresh token; .env updated")

    def _get(self, path, params=None, _retried=False):
        if self._access_token is None:
            self._refresh_access_token()
        resp = requests.get(
            f"{API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=60,
        )
        if resp.status_code == 401 and not _retried:
            self._refresh_access_token()
            return self._get(path, params, _retried=True)
        if resp.status_code == 429:
            if _retried:
                raise RuntimeError("Strava rate limit hit twice in a row; aborting")
            # the 15-minute quota resets on the quarter hour
            wait = 900 - (time.time() % 900) + 5
            print(f"Strava rate limit reached; sleeping {int(wait)}s ...")
            time.sleep(wait)
            return self._get(path, params, _retried=True)
        resp.raise_for_status()
        return resp.json()

    def iter_activities(self, per_page=200):
        """Yield every activity summary in the athlete's history."""
        page = 1
        while True:
            batch = self._get(
                "/athlete/activities", {"per_page": per_page, "page": page}
            )
            if not batch:
                return
            yield from batch
            if len(batch) < per_page:
                return
            page += 1

    def activity_latlng_stream(self, activity_id):
        """Full-resolution GPS track for one activity: list of [lat, lng]."""
        data = self._get(
            f"/activities/{activity_id}/streams",
            {"keys": "latlng", "key_by_type": "true"},
        )
        return (data.get("latlng") or {}).get("data") or []
