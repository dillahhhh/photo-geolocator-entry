#!/usr/bin/env python3
"""Photo geolocator agent for make.fun photo-geolocator-01.

Strategy (two stages, one vision model call per photo plus a second look
only where it can change the answer):

  Stage 1 -- one claude-opus-5-5 call per photo. Opus is the strongest
  allowlisted geolocator the rules cite (Anthropic's own benchmark:
  median error 181 km, vs 384 km for Sonnet 5), and the $1.00/trial
  budget leaves room for it: at list price ($4 / $20 per MTok) a call
  with ~550 input tokens and at most 768 output tokens costs at most
  ~$0.018, i.e. ~$0.44 for all 25 photos. The prompt makes the model
  work like an expert GeoGuessr player: read text first, then driving
  side and road furniture, architecture, vegetation and climate,
  vehicles and plates, and sun/shadows, and report one JSON object
  with lat, lon, a calibrated confidence and the evidence behind it.

  Stage 2 -- a second look, only for photos whose stage-1 confidence
  is low (< 0.55) or whose first answer could not be parsed, at most
  12 photos. A cheaper claude-sonnet-5-5 call is asked to verify or
  refute the first estimate against the same photo. If the two
  estimates agree (within 250 km) they are averaged weighted by
  confidence; if they disagree the higher-confidence one wins. This
  spends the leftover budget exactly where the first guess is shakiest,
  instead of burning it on photos that are already confident.

  Fallback -- any photo with no usable model answer (no key pushed,
  gateway refusal, download failure, unparsable answer) is answered
  with the geographic prior below, never left unanswered: an
  unanswered photo counts 20,015 km, the worst possible error.

  Prior -- the geometric median (Weiszfeld, 1,000 iterations) of the
  75 public practice photos in the kit (trials 0-2). It minimises the
  expected total haversine error over the OpenStreetView-5M
  distribution, unlike the plain centroid, which minimises squared
  error instead. Leave-one-trial-out check on the practice pool:
  geometric median beat the centroid on 2 of 3 held-out trials
  (151k vs 190k km and 126k vs 158k km total error; 237k vs 235k on
  the third), and 514k vs 568k km over all 75 photos.

Determinism / replay: every model request is a pure function of the
photo bytes and the answers before it (stage 2 is built from the
stage-1 answer); no wall clock and no unseeded randomness anywhere.
Each photo is resolved fully and its one final answer is posted
immediately, so a crash or a timeout still banks the answers already
made. Retries happen only for transient provider errors
(408/429/5xx/529); a gateway refusal (4xx, e.g. 401 with no key
pushed) is final, and a 401 on the first call switches the whole run
to the model-free fallback instead of pointlessly retrying 24 more.

Stdlib only. Runs as ``python3 -P -s /bot/main.py``.
"""

import base64
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------- config

# Geometric median of the kit's 75 public practice photos (see docstring).
PRIOR_LAT = 56.140909
PRIOR_LON = 32.397676

MODEL_PRIMARY = "claude-opus-5-5"      # best allowlisted geolocator
MODEL_SECOND = "claude-sonnet-5-5"     # cheaper verifier for stage 2
MAX_TOKENS_1 = 768
MAX_TOKENS_2 = 640
CONCURRENCY = 6                        # parallel calls; answers stay per-photo
LOW_CONFIDENCE = 0.55                  # below this, stage 2 takes a second look
MAX_SECOND_LOOKS = 12                  # stage-2 budget cap (worst ~$0.09)
AGREE_KM = 250.0                       # estimates this close are averaged
RETRY_STATUSES = (408, 429, 500, 502, 503, 504, 529)
RETRIES = 3

PROMPT_1 = (
    "You are an expert GeoGuessr player. Where was this street-level photo taken?\n"
    "Work through the clues in this order, then give ONE best estimate:\n"
    "1. Any readable text: language, script, place or shop names, road signs.\n"
    "2. Driving side, road markings, lane width, guardrails, licence-plate shape and colour.\n"
    "3. Architecture: roof shape, building materials, window and balcony styles.\n"
    "4. Vegetation and climate: tree and crop types, arid vs lush, snow, desert.\n"
    "5. Vehicles, people, clothing, utility poles, bollards, street furniture, flags.\n"
    "6. Sun position and shadows (hemisphere hint), terrain, coast or mountains.\n"
    "Reason briefly, then output ONLY this JSON object and no other text:\n"
    '{"lat": <decimal degrees, north positive>, "lon": <decimal degrees, east positive>, '
    '"confidence": <0 to 1, how sure you are>, "country": "<name or ISO code>", '
    '"evidence": ["<short cue>", "<short cue>", ...]}\n'
    "If you can only narrow it to a region, say so with low confidence and give the "
    "best-guess centre of that region. Never omit lat and lon."
)

# NOTE: __LAT__ etc. are substituted with str.replace, not str.format --
# the JSON braces in this prompt must survive untouched.
PROMPT_2 = (
    "Your first estimate for this same photo was latitude __LAT__, longitude __LON__ "
    "(__COUNTRY__, confidence __CONF__). Look at the photo again very carefully.\n"
    "List the strongest visual evidence that SUPPORTS that estimate and the strongest "
    "that CONTRADICTS it. If the contradiction is strong, revise the estimate; if the "
    "evidence supports it, keep it or refine it slightly.\n"
    "Output ONLY this JSON object and no other text:\n"
    '{"lat": <decimal degrees, north positive>, "lon": <decimal degrees, east positive>, '
    '"confidence": <0 to 1>, "country": "<name or ISO code>", '
    '"evidence": ["<short cue>", ...]}'
)

# ---------------------------------------------------------------- helpers


def haversine_km(a, b):
    """Great-circle distance in km between (lat, lon) pairs."""
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    d = 2 * math.asin(math.sqrt(
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    return 6371.0 * d


def _is_lat(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v) and -90.0 <= v <= 90.0


def _is_lon(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v) and -180.0 <= v <= 180.0


def parse_guess(text):
    """Best (lat, lon, confidence, country) in a model answer, else None.

    Prefers the first JSON object holding lat/lon; falls back to the
    first two in-range numbers, as the kit starter does.
    """
    if not text:
        return None
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group(0))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        lat = obj.get("lat", obj.get("latitude"))
        lon = obj.get("lon", obj.get("lng", obj.get("longitude")))
        if _is_lat(lat) and _is_lon(lon):
            conf = obj.get("confidence", 0.5)
            if not isinstance(conf, (int, float)) or not math.isfinite(conf):
                conf = 0.5
            conf = min(1.0, max(0.0, float(conf)))
            country = obj.get("country", "")
            if not isinstance(country, str):
                country = ""
            return (round(float(lat), 6), round(float(lon), 6), conf, country)
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    for i in range(len(nums) - 1):
        if _is_lat(nums[i]) and _is_lon(nums[i + 1]):
            return (round(nums[i], 6), round(nums[i + 1], 6), 0.5, "")
    return None


def combine(g1, g2):
    """Merge stage 1 and stage 2 guesses (either may be None)."""
    if g1 is None:
        return g2
    if g2 is None:
        return g1
    if haversine_km((g1[0], g1[1]), (g2[0], g2[1])) <= AGREE_KM:
        w1 = max(g1[2], 0.05)
        w2 = max(g2[2], 0.05)
        lat = round((g1[0] * w1 + g2[0] * w2) / (w1 + w2), 6)
        lon = round((g1[1] * w1 + g2[1] * w2) / (w1 + w2), 6)
        conf = min(1.0, max(g1[2], g2[2]) + 0.05)
        return (lat, lon, conf, g1[3] or g2[3])
    return g1 if g1[2] >= g2[2] else g2


# ---------------------------------------------------------------- env client

class Env:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/")

    def _request(self, path, method="GET", body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def photos(self):
        return json.loads(self._request("/photos").decode())["photos"]

    def photo_bytes(self, photo):
        """Download one photo, verifying its sha256; one retry on mismatch."""
        for _ in range(2):
            raw = self._request(photo["url"], timeout=60)
            if not photo.get("sha256") or \
                    hashlib.sha256(raw).hexdigest() == photo["sha256"]:
                return raw
        return raw  # last copy: pixels are what they are; never skip a photo

    def post_answer(self, photo_id, lat, lon):
        """Post one final answer; retry once on a transient failure."""
        body = {"answers": [{"id": photo_id, "lat": lat, "lon": lon}]}
        for attempt in range(2):
            try:
                res = json.loads(self._request(
                    "/answers", method="POST", body=body).decode())
                return photo_id in res.get("accepted", [])
            except urllib.error.HTTPError as e:
                if e.code == 409:      # already answered: banked, not an error
                    return True
                if e.code in RETRY_STATUSES and attempt == 0:
                    time.sleep(2)
                    continue
                raise
        return False


# ---------------------------------------------------------------- model client

class Gateway:
    """Anthropic Messages through the grader's metered gateway."""

    def __init__(self, base_url, api_key):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.dead = False      # True after a 401: no key pushed, stop calling
        self.lock = threading.Lock()

    def ask(self, jpeg, model, max_tokens, prompt):
        """Model's reply text, or None when refused / failed (final)."""
        with self.lock:
            if self.dead:
                return None
        body = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(jpeg).decode()}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }).encode()
        for attempt in range(RETRIES + 1):
            req = urllib.request.Request(
                self.base + "/v1/messages", data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "x-api-key": self.key,
                         "anthropic-version": "2023-06-01"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    msg = json.loads(resp.read().decode())
                return "\n".join(
                    b.get("text", "") for b in msg.get("content", [])
                    if b.get("type") == "text")
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode()[:200]
                except Exception:
                    detail = ""
                if e.code == 401:
                    with self.lock:
                        self.dead = True
                    print("gateway: 401, no provider key pushed; "
                          "model-free fallback from here on", flush=True)
                    return None
                if e.code in RETRY_STATUSES and attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                print("gateway: call refused (HTTP %s): %s"
                      % (e.code, detail), flush=True)
                return None
            except Exception as e:  # network failure before any answer
                if attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                print("gateway: call failed: %s" % e, flush=True)
                return None
        return None


# ---------------------------------------------------------------- main


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        print("MAKEFUN_ENV_URL not set")
        return 1
    env = Env(env_url)
    try:
        photos = [p for p in env.photos() if not p.get("answered")]
    except Exception as e:
        print("cannot list photos: %s" % e)
        return 1
    if not photos:
        print("no unanswered photos")
        return 0

    gw = None
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if base and key:
        gw = Gateway(base, key)
    else:
        print("no Anthropic gateway/key in env; model-free fallback "
              "(push ANTHROPIC_API_KEY to unlock the vision path)", flush=True)

    second_looks = [0]           # shared counter, capped at MAX_SECOND_LOOKS
    second_lock = threading.Lock()
    stats = {"model": 0, "fallback": 0, "second": 0}
    stats_lock = threading.Lock()

    def resolve(photo):
        """Resolve one photo end to end; never raises, always posts."""
        lat, lon, conf, via = PRIOR_LAT, PRIOR_LON, 0.0, "prior"
        try:
            if gw is not None:
                jpeg = env.photo_bytes(photo)
                g1 = parse_guess(gw.ask(jpeg, MODEL_PRIMARY, MAX_TOKENS_1,
                                        PROMPT_1))
                g2 = None
                need_second = g1 is None or g1[2] < LOW_CONFIDENCE
                if need_second and not gw.dead:
                    with second_lock:
                        ok = second_looks[0] < MAX_SECOND_LOOKS
                        if ok:
                            second_looks[0] += 1
                    if ok:
                        prompt2 = (PROMPT_2
                                   .replace("__LAT__", str(g1[0]) if g1 else "?")
                                   .replace("__LON__", str(g1[1]) if g1 else "?")
                                   .replace("__COUNTRY__", g1[3] if g1 else "?")
                                   .replace("__CONF__",
                                            str(round(g1[2], 2)) if g1 else "?"))
                        g2 = parse_guess(gw.ask(jpeg, MODEL_SECOND,
                                                MAX_TOKENS_2, prompt2))
                        with stats_lock:
                            stats["second"] += 1
                final = combine(g1, g2)
                if final is not None:
                    lat, lon, conf, via = final[0], final[1], final[2], "model"
        except Exception as e:
            # Any per-photo failure falls back to the prior; the photo is
            # still answered below instead of crashing the whole run.
            print("%s: model path failed (%s); prior" % (photo["id"], e),
                  flush=True)
        try:
            posted = env.post_answer(photo["id"], lat, lon)
        except Exception as e:
            print("%s: post failed (%s)" % (photo["id"], e), flush=True)
            posted = False
        with stats_lock:
            stats["model" if via == "model" else "fallback"] += 1
        print("%s: %.6f, %.6f conf=%.2f via=%s posted=%s"
              % (photo["id"], lat, lon, conf, via, posted), flush=True)
        return posted

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        posted = list(pool.map(resolve, photos))
    n = sum(1 for p in posted if p)
    print("answered %d of %d photos (%d model, %d prior fallback, "
          "%d second looks)" % (n, len(photos), stats["model"],
                                stats["fallback"], stats["second"]))
    return 0 if n == len(photos) else 1


if __name__ == "__main__":
    raise SystemExit(main())
