"use client";

import { ApiError, type JobSummary, api, getToken } from "@/lib/api";
import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useState } from "react";

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function JobsPage() {
  const [jobs, setJobs] = useState<JobSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploadNote, setUploadNote] = useState<string | null>(null);

  const [title, setTitle] = useState("Senior Backend Engineer");
  const [description, setDescription] = useState(
    "Senior backend engineer to build and operate payment services. Python on PostgreSQL, deployed on Kubernetes, owning reliability.",
  );
  const [required, setRequired] = useState("Python, PostgreSQL, Kubernetes");
  const [minYears, setMinYears] = useState(5);

  const refresh = useCallback(async () => {
    try {
      setJobs(await api.listJobs());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load jobs.");
    }
  }, []);

  useEffect(() => {
    if (!getToken()) {
      setError("Not signed in.");
      setJobs([]);
      return;
    }
    void refresh();
  }, [refresh]);

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createJob({
        title,
        description,
        required_skills: splitList(required),
        nice_to_have: [],
        hard_requirements: [],
        min_years: minYears,
      });
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the job.");
    } finally {
      setBusy(false);
    }
  }

  async function upload(file: File) {
    setUploadNote(null);
    setError(null);
    try {
      const result = await api.uploadResume(file);
      setUploadNote(
        result.duplicate
          ? `Already in the system (${result.sha256.slice(0, 10)}…). Nothing reprocessed.`
          : `Uploaded ${result.sha256.slice(0, 10)}… — parsing and redaction are queued.`,
      );
    } catch (err) {
      // Rejection reasons are safe to show: they describe the caller's own file.
      setError(err instanceof ApiError ? err.message : "Upload failed.");
    }
  }

  return (
    <div className="shell">
      <div className="topbar">
        <h1>Jobs</h1>
        <nav>
          <Link href="/">Status</Link>
          <Link href="/login">Switch user</Link>
        </nav>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="card">
        <h2 style={{ fontSize: 14, margin: "0 0 12px" }}>Upload a resume</h2>
        <input
          type="file"
          accept="application/pdf,.pdf,.docx"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void upload(file);
            e.target.value = "";
          }}
        />
        {uploadNote && <p className="muted">{uploadNote}</p>}
        <p className="muted">
          PDF or DOCX, 10&nbsp;MB and 30 pages maximum. The file is sniffed by magic bytes,
          quarantined, then parsed and redacted by a worker with no network access.
        </p>
      </div>

      <form onSubmit={create} className="card">
        <h2 style={{ fontSize: 14, margin: "0 0 12px" }}>New role</h2>
        <label htmlFor="title">Title</label>
        <input id="title" value={title} onChange={(e) => setTitle(e.target.value)} required />
        <label htmlFor="desc">Description</label>
        <textarea
          id="desc"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          required
        />
        <label htmlFor="req">Required skills (comma separated)</label>
        <input id="req" value={required} onChange={(e) => setRequired(e.target.value)} />
        <label htmlFor="years">Minimum years</label>
        <input
          id="years"
          type="number"
          min={0}
          max={50}
          value={minYears}
          onChange={(e) => setMinYears(Number(e.target.value))}
        />
        <button type="submit" disabled={busy}>
          {busy ? "Creating…" : "Create role"}
        </button>
      </form>

      <h2 style={{ fontSize: 14, margin: "26px 0 12px" }}>Open roles</h2>
      {jobs === null && <p className="muted">Loading…</p>}
      {jobs?.length === 0 && <div className="empty">No roles yet. Create one above.</div>}
      {jobs?.map((job) => (
        <div key={job.id} className="card">
          <Link href={`/jobs/${job.id}`} style={{ fontSize: 15, fontWeight: 600 }}>
            {job.title}
          </Link>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            {job.required_skills.join(", ") || "no required skills"} · {job.min_years}+ years
          </p>
        </div>
      ))}
    </div>
  );
}
