import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Secure AI Resume Screener",
  description: "Decision-support screening. Redacts before it reasons.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          fontFamily: "ui-sans-serif, system-ui, -apple-system, sans-serif",
          margin: 0,
          background: "#f3f4f0",
          color: "#1a1e1b",
        }}
      >
        {/* Required on every screen. Removing this is a scope violation, not a style choice. */}
        <div
          role="note"
          style={{
            background: "#f6e9d5",
            color: "#9e5c12",
            padding: "8px 16px",
            fontSize: 13,
            borderBottom: "1px solid #e3d2b4",
          }}
        >
          Decision support only. This tool does not make hiring decisions, and must not be
          used with real candidate data.
        </div>
        <main style={{ maxWidth: 720, margin: "0 auto", padding: "48px 24px" }}>{children}</main>
      </body>
    </html>
  );
}
