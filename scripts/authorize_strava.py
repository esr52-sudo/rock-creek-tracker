#!/usr/bin/env python3
"""One-time helper: mint a refresh token with activity:read_all scope.

The refresh token currently in .env was authorized without activity:read,
so /athlete/activities returns 401. Run this script, open the printed URL,
approve access, then paste the `code=` value from the redirect URL back in.
The new refresh token is written to .env.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH)

client_id = os.environ["STRAVA_CLIENT_ID"]
client_secret = os.environ["STRAVA_CLIENT_SECRET"]

authorize_url = "https://www.strava.com/oauth/authorize?" + urlencode(
    {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": "http://localhost",
        "approval_prompt": "force",
        "scope": "read,activity:read_all",
    }
)

print("1. Open this URL in your browser and click Authorize:\n")
print(f"   {authorize_url}\n")
print("2. You'll be redirected to http://localhost/?state=&code=...&scope=...")
print("   (the page won't load — that's fine; copy the `code` param from the URL)\n")
code = input("3. Paste the code here: ").strip()
if not code:
    sys.exit("no code provided")

resp = requests.post(
    "https://www.strava.com/oauth/token",
    data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
    },
    timeout=30,
)
if resp.status_code != 200:
    sys.exit(f"token exchange failed ({resp.status_code}): {resp.text[:300]}")

payload = resp.json()
set_key(str(ENV_PATH), "STRAVA_REFRESH_TOKEN", payload["refresh_token"])
print("\nSuccess — new refresh token written to .env.")
print("Now run: python scripts/sync_activities.py")
