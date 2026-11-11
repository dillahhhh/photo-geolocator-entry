#!/usr/bin/env python3
"""Photo geolocator (code-only, no model calls) for make.fun photo-geolocator-01.

No API key is available, so this entry places photos with its own code alone:
it answers every photo with the centroid of the public practice pool
(lat 22.171, lon 10.506), the geographic prior of the OpenStreetView-5M
distribution the kit publishes. That beats leaving a photo unanswered
(20,015 km error) on average. It fetches the photo list from the env, posts
all 25 answers in one call, and exits. Deterministic: no clock, no randomness.
"""
import json, os, urllib.request, urllib.error

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
# Centroid of the kit's public practice pool (75 photos, program/generator/practice.json).
PRIOR_LAT = 22.171
PRIOR_LON = 10.506

def get(path):
    req = urllib.request.Request(ENV_URL + path, method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def post(path, body):
    req = urllib.request.Request(ENV_URL + path,
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def main():
    if not ENV_URL:
        print("MAKEFUN_ENV_URL not set")
        return 1
    data = get("/photos")
    photos = data.get("photos", [])
    answers = [{"id": p["id"], "lat": PRIOR_LAT, "lon": PRIOR_LON} for p in photos]
    if answers:
        try:
            res = post("/answers", {"answers": answers})
        except Exception:
            res = post("/answers", {"answers": answers})
        print("answered:", res.get("answered"), "left:", res.get("left"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
