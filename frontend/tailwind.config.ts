import type { Config } from "tailwindcss";

/**
 * DaemonCut console design system.
 *
 * The palette is locked to the backend contract (backend.config.UI_PALETTE):
 * matte obsidian, elevated slate, structural border, warm parchment,
 * muted brass, deep steel, deep navy, and crimson. Status colors use brass,
 * steel, or crimson only — the console never uses success-green tokens.
 */
const config: Config = {
  content: [
    "./src/**/*.{ts,tsx,css}",
  ],
  theme: {
    extend: {
      colors: {
        obsidian: "#121316",
        slate: "#1A1D24",
        border: "#2B303B",
        parchment: "#F4EFE6",
        brass: "#D6AD55",
        steel: "#2B5B84",
        navy: "#102B4E",
        crimson: "#C94A4A",
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', "Segoe UI", "sans-serif"],
        mono: ['"IBM Plex Mono"', "Consolas", "monospace"],
      },
      screens: {
        xs: "380px",
      },
    },
  },
  plugins: [],
};

export default config;
