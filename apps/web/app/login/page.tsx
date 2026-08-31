"use client";

import { ApiError, api, setSession } from "@/lib/api";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

export default function LoginPage() {
  const router = useRouter();
  const [orgId, setOrgId] = useState("");
  const [email, setEmail] = useState("recruiter@example.com");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { access_token } = await api.login(orgId.trim(), email.trim(), password);
      setSession(access_token, orgId.trim());
      router.push("/jobs");
    } catch (err) {
      // The API returns one message for every credential failure on purpose:
      // distinguishing "no such user" from "wrong password" enumerates accounts.
      setError(err instanceof ApiError ? err.message : "Could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shell" style={{ maxWidth: 460 }}>
      <div className="topbar">
        <h1>Sign in</h1>
      </div>
      <form onSubmit={submit} className="card">
        {error && <div className="error">{error}</div>}
        <label htmlFor="org">Organisation ID</label>
        <input
          id="org"
          value={orgId}
          onChange={(e) => setOrgId(e.target.value)}
          placeholder="uuid from `make seed`"
          required
        />
        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="muted">
        Seeded users: <code>recruiter@</code>, <code>org_admin@</code>, <code>hiring_manager@</code>
        , <code>auditor@</code>, <code>org_owner@</code> <span className="mono">example.com</span>.
        The organisation ID is printed by <code>make seed</code>.
      </p>
    </div>
  );
}
