// NFR-PRF load-testing harness (M10, Prompt 13 item 7) — the core Ledger
// request path: commitment create -> verify -> transition. Run against a
// live `uvicorn` process (not `docker compose up`'s Postgres alone — start
// the app itself: `uv run uvicorn main:app`), seeded via loadtest/seed.py.
//
// IMPORTANT — what this does and does not prove: NFR-PRF-01 (capture-to-
// ledger latency, p50<=20s/p95<=60s) is about the *extraction* pipeline
// (message -> LLM -> commitment), not this REST layer, and needs real
// captured-message volume to mean anything — M8 (real channel capture) is
// still credential-blocked for the channels that would supply that volume
// (backend/PROGRESS.md's M8 notes). This script instead validates NFR-PRF-
// 02 (Twin recomputation after a commitment state change: <=10s) via the
// /transitions endpoint, which synchronously calls
// recompute_on_commitment_transition in the same request — a real,
// meaningful measurement, just not a stand-in for NFR-PRF-01. Don't cite
// a result from this script as proof of NFR-PRF-01; it proves "the
// pipeline doesn't fall over under load," per Prompt 13's own explicit
// framing for this item.
//
// Usage:
//   eval "$(uv run python3 loadtest/seed.py)"
//   k6 run loadtest/commitments.js
//   k6 run --vus 20 --duration 60s loadtest/commitments.js   # heavier run

import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.CUE_LOADTEST_BASE_URL || "http://localhost:8000";
const TOKEN = __ENV.CUE_LOADTEST_TOKEN;
const PROJECT_ID = __ENV.CUE_LOADTEST_PROJECT_ID;
const PARTY_ID = __ENV.CUE_LOADTEST_PARTY_ID;
const COUNTERPARTY_ID = __ENV.CUE_LOADTEST_COUNTERPARTY_ID;

if (!TOKEN || !PROJECT_ID || !PARTY_ID || !COUNTERPARTY_ID) {
  throw new Error(
    "CUE_LOADTEST_TOKEN/PROJECT_ID/PARTY_ID/COUNTERPARTY_ID not set — run `eval \"$(uv run python3 loadtest/seed.py)\"` first"
  );
}

const headers = { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" };

export const options = {
  scenarios: {
    ledger_write_path: {
      executor: "constant-vus",
      vus: 5,
      duration: "30s",
    },
  },
  thresholds: {
    // NFR-PRF-02: Twin recomputation after a commitment state change <=10s
    // — /transitions does that recompute synchronously in-request, so its
    // own response time is a direct measurement of this target.
    "http_req_duration{endpoint:transition}": ["p(95)<10000"],
    // Create/verify have no PRD-named target of their own; a generous,
    // load-bearing-API-should-obviously-not-hang bound, not a cited NFR.
    "http_req_duration{endpoint:create}": ["p(95)<5000"],
    "http_req_duration{endpoint:verify}": ["p(95)<5000"],
    http_req_failed: ["rate<0.01"],
  },
};

export default function () {
  const createBody = JSON.stringify({
    act_type: "commit",
    party_id: PARTY_ID,
    counterparty_id: COUNTERPARTY_ID,
    deliverable_en: `Load-test deliverable ${__VU}-${__ITER}`,
  });
  const createRes = http.post(`${BASE_URL}/projects/${PROJECT_ID}/commitments`, createBody, {
    headers, tags: { endpoint: "create" },
  });
  check(createRes, { "create: 201": (r) => r.status === 201 });
  if (createRes.status !== 201) return;
  const commitmentId = createRes.json("id");

  const verifyRes = http.post(
    `${BASE_URL}/projects/${PROJECT_ID}/commitments/${commitmentId}/verify`,
    JSON.stringify({}),
    { headers, tags: { endpoint: "verify" } }
  );
  check(verifyRes, { "verify: 200": (r) => r.status === 200 });

  const transitionRes = http.post(
    `${BASE_URL}/projects/${PROJECT_ID}/commitments/${commitmentId}/transitions`,
    JSON.stringify({ to_state: "committed" }),
    { headers, tags: { endpoint: "transition" } }
  );
  check(transitionRes, { "transition: 200": (r) => r.status === 200 });
}
