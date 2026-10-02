"use client";

import { ArrowRight, BedDouble, Eye, EyeOff, Lock, Mail, Plane } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, type CSSProperties } from "react";

import Brand from "@/components/Brand";
import { Spinner } from "@/components/ui";
import { getToken, login, register } from "@/lib/api";
import { DAY_COLORS } from "@/lib/map";

/** A decorative miniature of what the app produces — a route, pins, and the three things it finds. */
function Preview() {
  const glass = "absolute rounded-2xl border border-white/10 bg-white/[0.08] p-3.5 shadow-float backdrop-blur-md";
  const stops = [
    { left: "13%", top: "78%", color: DAY_COLORS[0], label: "1" },
    { left: "45%", top: "53%", color: DAY_COLORS[0], label: "2" },
    { left: "75%", top: "59%", color: DAY_COLORS[1], label: "1" },
    { left: "90%", top: "19%", color: DAY_COLORS[1], label: "2" },
  ];

  return (
    <div className="relative mx-auto aspect-[5/4] w-full max-w-md">
      <svg viewBox="0 0 400 320" className="absolute inset-0 h-full w-full" fill="none">
        <defs>
          <linearGradient id="login-route" x1="40" y1="260" x2="360" y2="60" gradientUnits="userSpaceOnUse">
            <stop stopColor="#f6b73c" />
            <stop offset="0.55" stopColor="#f2703f" />
            <stop offset="1" stopColor="#e0457b" />
          </linearGradient>
        </defs>
        <g stroke="#ffffff" strokeOpacity="0.07">
          <path d="M-20 90C60 40 140 120 220 80s140-60 220-10" />
          <path d="M-20 150c80-50 160 30 240-10s140-60 220-10" />
          <path d="M-20 210c80-50 160 30 240-10s140-60 220-10" />
          <path d="M-20 270c80-50 160 30 240-10s140-60 220-10" />
        </g>
        <path
          d="M52 250C110 250 120 170 180 170s70 60 120 20 40-110 60-130"
          stroke="url(#login-route)"
          strokeWidth="3"
          strokeLinecap="round"
          strokeDasharray="2 9"
        />
      </svg>

      {stops.map((stop) => (
        <span
          key={`${stop.left}-${stop.top}`}
          className="pin absolute -translate-x-1/2 -translate-y-1/2"
          style={{ left: stop.left, top: stop.top, "--pin": stop.color, "--pin-ink": stop.color === DAY_COLORS[1] ? "#0b0b0b" : "#fff" } as CSSProperties}
        >
          <span>{stop.label}</span>
        </span>
      ))}

      <div className={`${glass} left-0 top-[2%] w-52`}>
        <p className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-white/60">
          <Plane className="h-3.5 w-3.5" /> Flight
        </p>
        <p className="mt-1.5 text-sm font-semibold text-white">DEL → GOI</p>
        <p className="text-xs text-white/60">Non-stop · 2h 35m · ₹8,200</p>
      </div>

      <div className={`${glass} right-0 top-[27%] w-48`}>
        <p className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-white/60">
          <BedDouble className="h-3.5 w-3.5" /> Stay
        </p>
        <p className="mt-1.5 text-sm font-semibold text-white">Calangute, Goa</p>
        <p className="text-xs text-white/60">4 nights · ₹4,500 a night</p>
      </div>

      <div className={`${glass} bottom-0 left-[30%] w-56`}>
        <p className="text-[11px] font-semibold uppercase tracking-wider text-white/60">Day 1</p>
        <p className="mt-1.5 flex items-center justify-between text-sm text-white">
          <span className="font-semibold">Fort Aguada</span>
          <span className="text-xs text-white/50">Morning</span>
        </p>
        <p className="mt-1 flex items-center justify-between text-sm text-white">
          <span className="font-semibold">Baga Beach</span>
          <span className="text-xs text-white/50">Afternoon</span>
        </p>
      </div>
    </div>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Already signed in? Skip the form.
  useEffect(() => {
    if (getToken()) router.replace("/trips");
  }, [router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (mode === "register") await register(email, password); // then sign straight in
      await login(email, password);
      router.replace("/trips");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      setLoading(false);
    }
  }

  const registering = mode === "register";

  return (
    <main className="grid min-h-dvh lg:grid-cols-[1.05fr_1fr]">
      {/* Artwork — large screens only, purely decorative */}
      <section className="relative hidden overflow-hidden bg-ink-900 p-12 text-white lg:flex lg:flex-col lg:justify-between" aria-hidden>
        <div className="pointer-events-none absolute -left-32 bottom-[-12rem] h-[34rem] w-[34rem] rounded-full bg-sunset opacity-25 blur-3xl" />
        <div className="pointer-events-none absolute -right-24 top-[-10rem] h-80 w-80 rounded-full bg-[#2a78d6] opacity-20 blur-3xl" />

        <div className="relative">
          <Brand tone="paper" />
        </div>
        <div className="relative">
          <Preview />
        </div>
        <div className="relative max-w-md">
          <h2 className="text-balance font-display text-4xl font-medium leading-[1.1] tracking-tight">
            Plan the whole trip in one conversation.
          </h2>
          <p className="mt-4 text-[15px] leading-relaxed text-white/70">
            Say where and when. It finds the flights, a place to stay and things to do — and keeps the plan inside your budget.
          </p>
        </div>
      </section>

      {/* Form */}
      <section className="flex items-center justify-center px-5 py-12 sm:px-8">
        <div className="w-full max-w-sm animate-rise">
          <div className="mb-10 lg:hidden">
            <Brand />
          </div>

          <h1 className="font-display text-3xl font-medium tracking-tight text-ink-900">
            {registering ? "Create your account" : "Welcome back"}
          </h1>
          <p className="mt-2 text-sm text-ink-600">
            {registering ? "It takes a few seconds, and your trips are saved to it." : "Sign in to pick your trips up where you left them."}
          </p>

          <form onSubmit={handleSubmit} className="mt-8 space-y-4">
            <div>
              <label htmlFor="email" className="label">
                Email
              </label>
              <div className="relative">
                <Mail className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-400" aria-hidden />
                <input
                  id="email"
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  className="input pl-10"
                />
              </div>
            </div>

            <div>
              <label htmlFor="password" className="label">
                Password
              </label>
              <div className="relative">
                <Lock className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-400" aria-hidden />
                <input
                  id="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete={registering ? "new-password" : "current-password"}
                  required
                  minLength={registering ? 8 : undefined}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={registering ? "At least 8 characters" : "Your password"}
                  className="input px-10"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((shown) => !shown)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  aria-pressed={showPassword}
                  className="focus-ring absolute right-2 top-1/2 grid h-7 w-7 -translate-y-1/2 place-items-center rounded-lg text-ink-400 hover:bg-ink-100 hover:text-ink-700"
                >
                  {showPassword ? <EyeOff className="h-4 w-4" aria-hidden /> : <Eye className="h-4 w-4" aria-hidden />}
                </button>
              </div>
            </div>

            {error && (
              <p role="alert" className="rounded-xl bg-bad-soft px-3.5 py-2.5 text-sm text-bad-ink">
                {error}
              </p>
            )}

            <button type="submit" disabled={loading} className="btn-primary w-full py-3">
              {loading ? (
                <>
                  <Spinner /> Please wait…
                </>
              ) : (
                <>
                  {registering ? "Create account" : "Sign in"}
                  <ArrowRight className="h-4 w-4" aria-hidden />
                </>
              )}
            </button>
          </form>

          <p className="mt-6 text-sm text-ink-600">
            {registering ? "Already have one?" : "No account?"}{" "}
            <button
              type="button"
              onClick={() => {
                setMode(registering ? "login" : "register");
                setError(null);
              }}
              className="focus-ring rounded font-medium text-ink-900 underline decoration-ink-300 underline-offset-4 hover:decoration-ink-900"
            >
              {registering ? "Sign in" : "Create one"}
            </button>
          </p>
        </div>
      </section>
    </main>
  );
}
