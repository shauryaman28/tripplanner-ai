import type { Metadata, Viewport } from "next";
import { Fraunces, Inter } from "next/font/google";
import "./globals.css";

// Self-hosted by Next at build time: no request to Google from the browser, no layout shift.
// `latin-ext` carries ₹, which every page shows.
const sans = Inter({ subsets: ["latin", "latin-ext"], variable: "--font-sans", display: "swap" });
// Headings only. `opsz` lets the browser pick the display cut at large sizes.
const display = Fraunces({ subsets: ["latin"], variable: "--font-display", display: "swap", axes: ["opsz", "SOFT"] });

export const metadata: Metadata = {
  title: { default: "TripPlanner AI", template: "%s · TripPlanner AI" },
  description: "Plan a whole trip in one conversation — flights, a place to stay and things to do, checked against your budget.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#f9f9f7",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${display.variable}`} suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
