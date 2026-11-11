# Photo geolocator — make.fun bounty (photo-geolocator-01)

An agent that places 25 street-level photos (OpenStreetView-5M) on the map,
through the competition's metered model gateway, with a principled
code-only fallback when no provider key is available.

## How it works

**Stage 1 — one vision call per photo** (`claude-opus-5-5`). Opus is the
strongest allowlisted geolocator the rules cite (Anthropic benchmark:
median error 181 km, vs 384 km for Sonnet 5). The prompt works like an
expert GeoGuessr player: readable text first, then driving side and road
furniture, architecture, vegetation and climate, vehicles and plates,
and sun/shadows — and it must answer as one JSON object with `lat`,
`lon`, a calibrated `confidence`, and the `evidence` behind it.

**Stage 2 — a second look where it matters** (`claude-sonnet-5-5`, at
most 12 photos). Only photos with stage-1 confidence below 0.55, or an
unparsable first answer, get a verify-or-refute call against the same
photo. Estimates that agree within 250 km are averaged weighted by
confidence; when they disagree, the higher-confidence one wins.

**Fallback — never leave a photo unanswered.** Any photo with no usable
model answer (no key pushed, gateway refusal, download failure,
unparsable answer) gets the geographic prior: the geometric median of
the kit's 75 public practice photos (56.140909, 32.397676). It
minimises expected total haversine error; leave-one-trial-out it beat
the plain centroid on 2 of 3 held-out practice trials. An unanswered
photo counts 20,015 km — the worst possible error — so the prior is
always better than silence.

**Robustness.** Downloads are sha256-verified; transient provider
errors (408/429/5xx/529) are retried with backoff, gateway refusals
(4xx) are final; a 401 on the first call switches the whole run to
the model-free fallback. Each photo's one final answer is posted as
soon as it is resolved, so a crash or timeout still banks the answers
already made. Deterministic: every request is a pure function of the
photo bytes and earlier answers — no clock, no unseeded randomness —
so the replay matches the live run.

## Budget

At list price the worst case is ~$0.44 for stage 1 (25 × ~550 input
tokens at $4/MTok + 768 output tokens at $20/MTok) and ~$0.09 for
stage 2, ~$0.53 of the $1.00/trial budget.

## Unlocking the vision path

Model calls run on the entrant's own key, pushed before submitting
(`makefun secrets push photo-geolocator-01 --env-file .env` with
`ANTHROPIC_API_KEY=...`). With no key pushed the gateway answers 401
and the entry runs model-free on the prior above — still a valid entry,
just a much weaker one. No key is ever committed to this repository.
