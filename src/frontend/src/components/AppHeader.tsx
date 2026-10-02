"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { LogOut } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { clearToken, getEmail } from "@/lib/api";
import Brand from "./Brand";

/** Sticky top bar for the signed-in pages. `children` is the breadcrumb slot. */
export default function AppHeader({ children }: { children?: ReactNode }) {
  const router = useRouter();
  const [email, setEmail] = useState<string | null>(null);

  // localStorage is not there during server rendering
  useEffect(() => setEmail(getEmail()), []);

  function signOut() {
    clearToken();
    router.replace("/login");
  }

  return (
    <header className="sticky top-0 z-40 border-b border-ink-200/70 bg-paper/95 backdrop-blur-lg">
      <div className="mx-auto flex h-16 max-w-7xl items-center gap-3 px-4 sm:gap-5 sm:px-6 lg:px-8">
        <Link href="/trips" className="focus-ring shrink-0 rounded-lg" aria-label="TripPlanner — your trips">
          <Brand />
        </Link>

        <div className="min-w-0 flex-1">{children}</div>

        {email && (
          <span className="hidden items-center gap-2 text-sm text-ink-600 md:flex" title={email}>
            <span className="grid h-7 w-7 place-items-center rounded-full bg-ink-900 text-xs font-semibold uppercase text-white">
              {email.charAt(0)}
            </span>
            <span className="max-w-[14rem] truncate">{email}</span>
          </span>
        )}
        <button onClick={signOut} className="btn-ghost shrink-0 px-2.5 py-2" title="Sign out">
          <LogOut className="h-4 w-4" aria-hidden />
          <span className="hidden sm:inline">Sign out</span>
          <span className="sr-only sm:hidden">Sign out</span>
        </button>
      </div>
    </header>
  );
}
