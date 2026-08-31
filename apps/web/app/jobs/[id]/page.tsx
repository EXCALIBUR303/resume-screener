"use client";

import { ApiError, type InterviewGuide, type Match, api, getToken } from "@/lib/api";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

/** Every term, its weight, its value, and who computed it. */
function Explanation({ match }: { match: Match }) {
  return (
    <div style={{ marginTop: 14 }}>
      <table>
        <thead>
          <tr>
            <th>Term</th>
            <th className="num">Weight</th>
            <th className="num">Value</th>
            <th className="num">Points</th>
            <th>Computed by</th>
          </tr>
        </thead>
        <tbody>
          {match.contributions.map((c) => (
            <tr key={c.term}>
              <td>{c.term}</td>
              <td className="num">{c.weight.toFixed(2)}</td>
              <td className="num">{c.value.toFixed(2)}</td>
              <td className="num">
                {c.points >= 0 ? "+" : ""}
                {c.points.toFixed(3)}
              </td>
              <td>
                <span className={`chip ${c.computed_by === "model" ? "warn" : "good"}`}>
                  {c.computed_by}
                </span>
              </td>
            </tr>
          ))}
          {Object.entries(match.penalties).map(([name, value]) => (
            <tr key={name}>
              <td colSpan={3}>penalty · {name.replace(/_/g, " ")}</td>
              <td className="num" style={{ color: "var(--danger)" }}>
                −{value.toFixed(3)}
              </td>
              <td />
            </tr>
          ))}
        </tbody>
      </table>

      <h4 style={{ fontSize: 12, margin: "18px 0 6px", letterSpacing: "0.06em" }}>COMPETENCIES</h4>
      <table>
        <thead>
          <tr>
            <th>Competency</th>
            <th className="num">Claimed</th>
            <th className="num">Quotes verified</th>
            <th className="num">Effective</th>
          </tr>
        </thead>
        <tbody>
          {match.competencies.map((c) => (
            <tr key={c.name}>
              <td>
                {c.name}{" "}
                {/* A claim the model could not evidence contributes nothing,
                    however confident it was. */}
                {c.zeroed && <span className="chip bad">zeroed</span>}
              </td>
              <td className="num">{c.claimed_level}</td>
              <td className="num">
                {c.quotes_verified}/{c.quotes_cited}
              </td>
              <td className="num">{c.effective_level}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {Object.entries(match.evidence).length > 0 && (
        <>
          <h4 style={{ fontSize: 12, margin: "18px 0 6px", letterSpacing: "0.06em" }}>
            VERIFIED EVIDENCE
          </h4>
          {Object.entries(match.evidence).map(([name, quotes]) => (
            <p key={name} className="muted" style={{ margin: "0 0 6px" }}>
              <strong>{name}:</strong>{" "}
              {/* Rendered as plain text, never markdown or HTML: model output is
                  untrusted, and a rendered image tag is an exfiltration channel. */}
              {quotes.map((q) => `“${q}”`).join(" ")}
            </p>
          ))}
        </>
      )}

      <p className="muted mono" style={{ marginTop: 14 }}>
        model {match.model_id} · prompt {match.prompt_version}
      </p>
    </div>
  );
}

export default function JobDetail() {
  const params = useParams<{ id: string }>();
  const jobId = params.id;

  const [matches, setMatches] = useState<Match[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [guides, setGuides] = useState<Record<string, InterviewGuide>>({});

  const refresh = useCallback(async () => {
    try {
      setMatches(await api.matches(jobId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load candidates.");
    }
  }, [jobId]);

  useEffect(() => {
    if (!getToken()) {
      setError("Not signed in.");
      setMatches([]);
      return;
    }
    void refresh();
  }, [refresh]);

  async function score() {
    setNote(null);
    setError(null);
    try {
      const result = await api.scoreJob(jobId);
      setNote(
        `Queued ${result.queued} of ${result.candidates} candidates. Refresh in a few seconds.`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not queue scoring.");
    }
  }

  async function guide(resumeId: string) {
    setError(null);
    try {
      const generated = await api.interviewGuide(jobId, resumeId);
      setGuides((g) => ({ ...g, [resumeId]: generated }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not generate a guide.");
    }
  }

  return (
    <div className="shell">
      <div className="topbar">
        <h1>Ranked candidates</h1>
        <nav>
          <Link href="/jobs">All jobs</Link>
        </nav>
      </div>

      {error && <div className="error">{error}</div>}
      {note && <p className="muted">{note}</p>}

      <div style={{ display: "flex", gap: 10, marginBottom: 20 }}>
        <button type="button" onClick={score}>
          Score all candidates
        </button>
        <button type="button" className="secondary" onClick={refresh}>
          Refresh
        </button>
      </div>

      {matches === null && <p className="muted">Loading…</p>}
      {matches?.length === 0 && (
        <div className="empty">No scored candidates yet. Upload resumes, then run scoring.</div>
      )}

      {matches?.map((match, index) => (
        <div key={match.resume_id} className="card">
          <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
            <span className="muted mono">{index + 1}</span>
            <strong style={{ fontSize: 15 }}>{match.pseudonym}</strong>
            <span className="score" style={{ marginLeft: "auto" }}>
              {match.score_out_of_ten.toFixed(2)}
              <span className="muted" style={{ fontSize: 13 }}>
                /10
              </span>
            </span>
          </div>

          <div style={{ marginTop: 8 }}>
            {match.injection_suspected && <span className="chip bad">injection suspected</span>}
            {match.partially_supported && <span className="chip warn">partially supported</span>}
            {match.degraded && <span className="chip warn">degraded</span>}
            {!match.injection_suspected && !match.partially_supported && !match.degraded && (
              <span className="chip good">fully supported</span>
            )}
          </div>

          <p className="muted" style={{ margin: "10px 0 0" }}>
            matched: {match.matched_skills.join(", ") || "none"}
            {match.missing_skills.length > 0 && <> · missing: {match.missing_skills.join(", ")}</>}
          </p>

          <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
            <button
              type="button"
              className="secondary"
              onClick={() => setOpen(open === match.resume_id ? null : match.resume_id)}
            >
              {open === match.resume_id ? "Hide" : "Why this score?"}
            </button>
            <button type="button" className="secondary" onClick={() => guide(match.resume_id)}>
              Interview guide
            </button>
          </div>

          {open === match.resume_id && <Explanation match={match} />}

          {guides[match.resume_id] && (
            <div style={{ marginTop: 16, borderTop: "1px solid var(--rule)", paddingTop: 14 }}>
              <p className="muted">
                {guides[match.resume_id].accepted} of {guides[match.resume_id].proposed} questions
                accepted
                {guides[match.resume_id].rejected_reasons.length > 0 && (
                  <> · rejected: {guides[match.resume_id].rejected_reasons.join(", ")}</>
                )}
              </p>
              {guides[match.resume_id].questions.map((q) => (
                <div key={q.question} style={{ marginTop: 12 }}>
                  <span className="chip">{q.difficulty}</span>
                  <strong style={{ fontSize: 14 }}>{q.question}</strong>
                  <p className="muted" style={{ margin: "4px 0 0" }}>
                    {q.competency} · {q.probe_reason}
                  </p>
                  <p className="muted mono" style={{ margin: "2px 0 0" }}>
                    cites: {q.cites_requirement ?? q.cites_evidence ?? "—"}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
