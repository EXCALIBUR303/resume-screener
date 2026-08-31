// AC-12: p95 < 300 ms on non-LLM endpoints at 50 concurrent virtual users.
//
// Deliberately excludes anything that calls a model: the criterion is about the
// service's own latency, and mixing in a 9-second inference would measure the
// model instead.
//
//   k6 run -e BASE=http://localhost:8000 -e TOKEN=$TOKEN tests/load/api_smoke.js
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE || "http://localhost:8000";
const TOKEN = __ENV.TOKEN || "";

export const options = {
  stages: [
    { duration: "20s", target: 50 },
    { duration: "40s", target: 50 },
    { duration: "10s", target: 0 },
  ],
  thresholds: {
    // The acceptance criterion, expressed where it can fail the run.
    "http_req_duration{kind:read}": ["p(95)<300"],
    http_req_failed: ["rate<0.01"],
  },
};

const authed = { headers: { Authorization: `Bearer ${TOKEN}` }, tags: { kind: "read" } };

export default function () {
  check(http.get(`${BASE}/healthz`, { tags: { kind: "read" } }), {
    "healthz 200": (r) => r.status === 200,
  });
  if (TOKEN) {
    check(http.get(`${BASE}/auth/me`, authed), { "me 200": (r) => r.status === 200 });
    check(http.get(`${BASE}/jobs`, authed), { "jobs 200": (r) => r.status === 200 });
  }
  sleep(1);
}
