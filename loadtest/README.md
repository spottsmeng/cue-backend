# Load testing (NFR-PRF, M10 item 7)

`k6` (not locust — no new Python dependency, and k6's `thresholds` block
turns NFR-PRF targets into pass/fail assertions the run itself checks,
rather than a number a human has to eyeball afterward).

## What this proves, and what it doesn't

**Proves**: the Ledger write path (commitment create → verify → transition)
holds up under concurrent load without falling over, and NFR-PRF-02 (Twin
recomputation after a commitment state change, ≤10s) against real request
latency — `/transitions` synchronously calls
`recompute_on_commitment_transition` in-request, so its own response time
*is* that measurement.

**Does not prove**: NFR-PRF-01 (capture-to-ledger latency, p50≤20s/
p95≤60s). That target is about the extraction pipeline (a captured message
→ LLM → a visible commitment), not this REST layer, and is only meaningful
against real message volume — M8 (real channel capture) is still
credential-blocked for the channels that would supply that volume
(`backend/PROGRESS.md`'s M8 notes: code-complete for WhatsApp/WeChat/Graph,
genuinely untested against real infrastructure). Running this script against
fixture-scale data and reporting it as NFR-PRF-01 validation would be
overclaiming — don't. It validates "the pipeline doesn't fall over," per
Prompt 13's own explicit framing for this item, nothing more.

## Setup

Requires a running Postgres (`docker compose up -d postgres`) and the app
itself running (`uv run uvicorn main:app`) — this exercises the real HTTP
layer, not an in-process test client.

```bash
brew install k6   # or see https://k6.io/docs/get-started/installation/

# from backend/, with the app's venv active:
uv run uvicorn main:app &
eval "$(uv run python3 loadtest/seed.py)"   # seeds an org/project/parties, mints a token
k6 run loadtest/commitments.js
```

Heavier run:

```bash
k6 run --vus 20 --duration 60s loadtest/commitments.js
```

## Extending

`loadtest/commitments.js` covers the Ledger core only, per Prompt 13's "at
minimum" — Twin/Documents/Ask endpoints would each need their own seed
data (a milestone archetype, an uploaded document, embedded content
respectively) and are real, scoped follow-ups, not attempted in this pass.

## First real run (this session, fixture-scale data, 3 VUs/10s)

p95: create 10ms, verify 9ms, transition 22ms — all well inside this
script's own thresholds. Meaningless as a production capacity number (3
concurrent users, a laptop, no real data volume) — recorded here only to
confirm the harness itself runs end-to-end, not as a performance claim.
