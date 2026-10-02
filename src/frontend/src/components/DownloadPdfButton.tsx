"use client";

import { useRouter } from "next/navigation";
import { Download } from "lucide-react";
import { useState } from "react";

import { ApiError, downloadTripPdf } from "@/lib/api";
import { saveBlob } from "@/lib/download";
import Toast, { useToast } from "./Toast";
import { Spinner } from "./ui";

interface Props {
  tripId: string;
  /** The plan is being changed: what would be downloaded is about to be out of date. */
  disabled?: boolean;
}

/**
 * Phase 19 — the itinerary as a PDF.
 *
 * The backend builds the file (a second or two when the map's tiles are not cached yet), so the
 * button says so while it waits. The request carries the token in a header, which a plain link
 * cannot do — hence fetch → blob → download rather than an <a href>.
 */
export default function DownloadPdfButton({ tripId, disabled = false }: Props) {
  const router = useRouter();
  const [preparing, setPreparing] = useState(false);
  const { toast, show, dismiss } = useToast();

  async function download() {
    if (preparing) return;
    setPreparing(true);
    dismiss();
    try {
      const file = await downloadTripPdf(tripId);
      saveBlob(file.blob, file.filename);
      if (file.mapMissing) {
        show("info", "PDF downloaded without the map — it couldn't be loaded this time. Download again for a copy with it.");
      }
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login"); // the session has ended; the token is already forgotten
        return;
      }
      show("error", "PDF generation failed — try again");
    } finally {
      setPreparing(false);
    }
  }

  return (
    <>
      <button
        onClick={download}
        disabled={disabled || preparing}
        aria-busy={preparing}
        className="btn-secondary min-w-[9.75rem] shrink-0 px-3 py-2"
      >
        {preparing ? <Spinner /> : <Download className="h-4 w-4" aria-hidden />}
        {preparing ? "Preparing PDF…" : "Download PDF"}
      </button>
      <Toast toast={toast} onDismiss={dismiss} />
    </>
  );
}
