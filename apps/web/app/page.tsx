const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type Readiness = { status: string; database?: string; pgvector?: string };

async function getReadiness(): Promise<Readiness | null> {
  try {
    const res = await fetch(`${API}/readyz`, { cache: "no-store" });
    return (await res.json()) as Readiness;
  } catch {
    return null;
  }
}

export default async function Home() {
  const ready = await getReadiness();

  return (
    <>
      <h1 style={{ fontSize: 28, marginBottom: 8 }}>Secure AI Resume Screener</h1>
      <p style={{ color: "#5d655e", marginTop: 0 }}>
        M0 — foundations. Upload and ranking arrive in M2 and M6.
      </p>

      <h2 style={{ fontSize: 15, marginTop: 32, textTransform: "uppercase", letterSpacing: ".08em" }}>
        System status
      </h2>
      {ready ? (
        <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 20px", fontSize: 14 }}>
          <dt style={{ color: "#5d655e" }}>API</dt>
          <dd style={{ margin: 0 }}>{ready.status}</dd>
          <dt style={{ color: "#5d655e" }}>Database</dt>
          <dd style={{ margin: 0 }}>{ready.database ?? "unknown"}</dd>
          <dt style={{ color: "#5d655e" }}>pgvector</dt>
          <dd style={{ margin: 0 }}>{ready.pgvector ?? "unknown"}</dd>
        </dl>
      ) : (
        <p style={{ fontSize: 14, color: "#98302e" }}>
          API unreachable at {API}. Start it with <code>make up</code>.
        </p>
      )}
    </>
  );
}
