/**
 * API client.
 *
 * The access token lives in memory plus sessionStorage rather than
 * localStorage: sessionStorage dies with the tab, which limits the window in
 * which an XSS can exfiltrate it. Neither is as good as an httpOnly cookie —
 * that is the documented V2 change, and this comment exists so the tradeoff is
 * visible rather than accidental.
 */

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const TOKEN_KEY = "screener.access_token";
const ORG_KEY = "screener.org_id";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(TOKEN_KEY);
}

export function setSession(token: string, orgId: string): void {
  window.sessionStorage.setItem(TOKEN_KEY, token);
  window.sessionStorage.setItem(ORG_KEY, orgId);
}

export function getOrgId(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(ORG_KEY);
}

export function clearSession(): void {
  window.sessionStorage.removeItem(TOKEN_KEY);
  window.sessionStorage.removeItem(ORG_KEY);
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      /* non-JSON error body; keep the status text */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  login: (orgId: string, email: string, password: string) =>
    request<{ access_token: string; expires_in: number }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ org_id: orgId, email, password }),
    }),
  me: () => request<{ email: string; roles: string[]; permissions: string[] }>("/auth/me"),
  listJobs: () => request<JobSummary[]>("/jobs"),
  createJob: (body: JobCreate) =>
    request<JobSummary>("/jobs", { method: "POST", body: JSON.stringify(body) }),
  scoreJob: (id: string) =>
    request<{ candidates: number; queued: number }>(`/jobs/${id}/score`, {
      method: "POST",
    }),
  matches: (id: string) => request<Match[]>(`/jobs/${id}/matches`),
  uploadResume: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<UploadResult>("/resumes", { method: "POST", body: form });
  },
  interviewGuide: (jobId: string, resumeId: string) =>
    request<InterviewGuide>(`/interviews/${jobId}/${resumeId}`, { method: "POST" }),
};

export type JobSummary = {
  id: string;
  title: string;
  required_skills: string[];
  min_years: number;
  status: string;
};

export type JobCreate = {
  title: string;
  description: string;
  required_skills: string[];
  nice_to_have: string[];
  hard_requirements: string[];
  min_years: number;
};

export type UploadResult = {
  resume_id: string;
  sha256: string;
  page_count: number | null;
  duplicate: boolean;
};

export type Contribution = {
  term: string;
  weight: number;
  value: number;
  points: number;
  computed_by: "python" | "model";
};

export type Competency = {
  name: string;
  claimed_level: number;
  effective_level: number;
  zeroed: boolean;
  quotes_cited: number;
  quotes_verified: number;
};

export type Match = {
  resume_id: string;
  candidate_id: string;
  pseudonym: string;
  score: number;
  score_out_of_ten: number;
  contributions: Contribution[];
  penalties: Record<string, number>;
  competencies: Competency[];
  evidence: Record<string, string[]>;
  unmet_requirements: string[];
  matched_skills: string[];
  missing_skills: string[];
  degraded: boolean;
  partially_supported: boolean;
  injection_suspected: boolean;
  model_id: string;
  prompt_version: string;
};

export type InterviewQuestion = {
  question: string;
  competency: string;
  difficulty: string;
  probe_reason: string;
  cites_requirement: string | null;
  cites_evidence: string | null;
  rubric: { level: number; descriptor: string }[];
};

export type InterviewGuide = {
  questions: InterviewQuestion[];
  focus_areas: string[];
  proposed: number;
  accepted: number;
  rejected_reasons: string[];
};
