import { ExternalLink } from "lucide-react";

import { bookingUrl } from "@/lib/booking";

/**
 * Phase 39 — "Book flight", "Book hotel": the booking site's search, filled in, in a new tab. Greyed out, and
 * no link at all, when there is none to open (lib/booking.ts says when).
 */
export default function BookButton({ url, label, site }: { url?: string | null; label: string; site: string }) {
  const href = bookingUrl(url);
  const look =
    "mt-2 inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors";
  if (!href) {
    return (
      <span
        role="link"
        aria-disabled="true"
        data-book={label}
        title="No booking link for this one"
        className={`${look} cursor-not-allowed border-ink-100 bg-ink-50 text-ink-400`}
      >
        {label}
      </span>
    );
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      data-book={label}
      title={`Opens ${site} in a new tab`}
      className={`${look} focus-ring border-ink-200 bg-white text-ink-800 hover:border-ink-300 hover:bg-ink-50`}
    >
      {label}
      <ExternalLink className="h-3.5 w-3.5 text-ink-400" aria-hidden />
      <span className="sr-only">(opens {site} in a new tab)</span>
    </a>
  );
}
