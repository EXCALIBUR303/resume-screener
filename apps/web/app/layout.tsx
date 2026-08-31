import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Secure AI Resume Screener",
  description: "Decision-support screening that redacts before it reasons.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* Required on every screen. Removing this is a scope violation, not a
            style choice — see the limitations section of the README. */}
        <div className="banner" role="note">
          Decision support only. This tool does not make hiring decisions, and must not be used with
          real candidate data.
        </div>
        {children}
      </body>
    </html>
  );
}
