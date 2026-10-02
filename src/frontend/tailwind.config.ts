import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/app/**/*.{ts,tsx}",
    "./src/components/**/*.{ts,tsx}",
    "./src/lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Warm neutrals: `paper` is the page, `ink` is everything written on it.
        paper: "#f9f9f7",
        ink: {
          50:  "#f6f6f3",
          100: "#f0efec",
          200: "#e1e0d9",
          300: "#c3c2b7",
          400: "#898781",   // icons and placeholders only — too light for body text
          500: "#6f6d68",
          600: "#52514e",
          700: "#3a3936",
          800: "#22211f",
          900: "#0b0b0b",
        },
        // Status colours are reserved for state and always ship with an icon and a label.
        good: { DEFAULT: "#0ca30c", ink: "#0b5d0b", soft: "#ecf8ec" },
        warn: { DEFAULT: "#fab219", ink: "#7a5200", soft: "#fff5d9" },
        bad:  { DEFAULT: "#d03b3b", ink: "#9f2424", soft: "#fdeeee" },
        saffron: "#f4c430",
      },
      fontFamily: {
        sans:    ["var(--font-sans)", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        display: ["var(--font-display)", "Georgia", "serif"],
      },
      backgroundImage: {
        // The one decorative gradient: logo, assistant avatar, login artwork. Never data.
        sunset: "linear-gradient(135deg, #f6b73c 0%, #f2703f 55%, #e0457b 100%)",
      },
      boxShadow: {
        card:  "0 1px 2px rgba(11,11,11,0.04), 0 8px 24px -12px rgba(11,11,11,0.10)",
        lift:  "0 2px 4px rgba(11,11,11,0.05), 0 16px 36px -14px rgba(11,11,11,0.18)",
        float: "0 2px 6px rgba(11,11,11,0.08), 0 20px 44px -12px rgba(11,11,11,0.28)",
      },
      keyframes: {
        "rise": {
          from: { opacity: "0", transform: "translateY(10px)" },
          to:   { opacity: "1", transform: "translateY(0)" },
        },
        "fade": {
          from: { opacity: "0" },
          to:   { opacity: "1" },
        },
        "shimmer": {
          from: { backgroundPosition: "200% 0" },
          to:   { backgroundPosition: "-200% 0" },
        },
        "indeterminate": {
          "0%":   { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(350%)" },
        },
      },
      animation: {
        "rise":          "rise 0.35s cubic-bezier(0.2, 0.7, 0.2, 1) both",
        "fade":          "fade 0.3s ease-out both",
        "shimmer":       "shimmer 1.8s linear infinite",
        "indeterminate": "indeterminate 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
