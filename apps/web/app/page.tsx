"use client";

import { API_BASE, getToken } from "@/lib/api";
import Link from "next/link";
import { useEffect, useState } from "react";

type Readiness = { status: string; database?: string; pgvector?: string };

export default function Home() {
  const [ready, setReady] = useState<Readiness | null>(null);
  const [failed, setFailed] = useState(false);
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => {
    setSignedIn(Boolean(getToken()));
    fetch(`${API_BASE}/readyz`)
      .then((r) => r.json())
      .then(setReady)
      .catch(() => setFailed(true));
  }, []);

  return (
    <div className="shell">
      <div className="topbar">
        <h1>Secure AI Resume Screener</h1>
        <nav>
          <Link href="/jobs">Jobs</Link>
          <Link href="/login">{signedIn ? "Switch user" : "Sign in"}</Link>
        </nav>
      </div>

      <p className="muted" style={{ maxWidth: "62ch" }}>
        Resumes are redacted inside a worker with no network access before anything is embedded,
        prompted, indexed or logged. Scores show every term, its weight, and whether a human or the
        model produced it.
      </p>

      <div className="card">
        <h2 style={{ fontSize: 14, margin: "0 0 12px", letterSpacing: "0.04em" }}>SYSTEM STATUS</h2>
        {failed && (
          <div className="error">
            API unreachable at {API_BASE}. Start it with <code>make up</code>.
          </div>
        )}
        {ready && (
          <table>
            <tbody>
              <tr>
                <td>API</td>
                <td className="num">{ready.status}</td>
              </tr>
              <tr>
                <td>Database</td>
                <td className="num">{ready.database ?? "unknown"}</td>
              </tr>
              <tr>
                <td>pgvector</td>
                <td className="num">{ready.pgvector ?? "unknown"}</td>
              </tr>
            </tbody>
          </table>
        )}
        {!ready && !failed && <p className="muted">Checking…</p>}
      </div>

      <p className="muted">
        {signedIn ? (
          <Link href="/jobs">Go to jobs →</Link>
        ) : (
          <Link href="/login">Sign in to continue →</Link>
        )}
      </p>
    </div>
  );
}
