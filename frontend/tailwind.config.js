/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Accent — Claude Code warm brown/caramel
        accent: {
          DEFAULT: "#a89278",
          primary: "#a89278",
          strong: "#c4a882",
          hover: "#d4b896",
          active: "#8c7a64",
          soft: "rgba(168, 146, 120, 0.10)",
          border: "rgba(168, 146, 120, 0.25)",
          glow: "none",
          dim: "rgba(168, 146, 120, 0.06)",
        },
        // Surface layers — layered warm dark grays
        surface: {
          base: "#1a1a1a",
          page: "#1e1e1e",
          panel: "#242424",
          sidebar: "#1f1f1f",
          raised: "#2a2a2a",
          soft: "#272727",
          input: "#1f1f1f",
          hover: "#2f2f2f",
          active: "#333333",
          overlay: "#2a2a2a",
        },
        // Borders
        border: {
          DEFAULT: "#2f2f2f",
          subtle: "rgba(255, 255, 255, 0.07)",
          soft: "rgba(255, 255, 255, 0.05)",
          strong: "rgba(255, 255, 255, 0.12)",
          accent: "rgba(168, 146, 120, 0.25)",
        },
        // Text — warm off-white tones
        text: {
          primary: "#e0ddd6",
          secondary: "#a8a29e",
          muted: "#78716c",
          disabled: "#44403c",
        },
        // Semantic states
        state: {
          success: "#4ade80",
          warning: "#fbbf24",
          danger: "#f87171",
          info: "#60a5fa",
        },
      },
      fontFamily: {
        sans: ['"Inter"', "-apple-system", "BlinkMacSystemFont", '"Segoe UI"', '"Noto Sans SC"', "sans-serif"],
        mono: ['"JetBrains Mono"', '"SF Mono"', "Monaco", "Consolas", '"Liberation Mono"', "monospace"],
      },
      fontSize: {
        "2xs": ["10px", { lineHeight: "1.4" }],
        xs: ["11px", { lineHeight: "1.5" }],
        sm: ["12px", { lineHeight: "1.5" }],
        base: ["13px", { lineHeight: "1.5" }],
        md: ["14px", { lineHeight: "1.5" }],
        lg: ["16px", { lineHeight: "1.5" }],
        xl: ["18px", { lineHeight: "1.4" }],
        "2xl": ["22px", { lineHeight: "1.3" }],
        "3xl": ["28px", { lineHeight: "1.2" }],
      },
      spacing: {
        "0": "0",
        "1": "4px",
        "2": "8px",
        "3": "12px",
        "4": "16px",
        "5": "20px",
        "6": "24px",
        "8": "32px",
        "10": "40px",
        "12": "48px",
        "16": "64px",
      },
      borderRadius: {
        sm: "3px",
        md: "5px",
        lg: "7px",
        xl: "10px",
        "2xl": "12px",
        full: "9999px",
      },
      boxShadow: {
        sm: "0 1px 2px rgba(0, 0, 0, 0.25)",
        md: "0 2px 6px rgba(0, 0, 0, 0.30)",
        lg: "0 2px 8px rgba(0, 0, 0, 0.25)",
        xl: "0 4px 16px rgba(0, 0, 0, 0.40)",
        glow: "none",
      },
      transitionDuration: {
        micro: "100ms",
        fast: "150ms",
        base: "200ms",
        normal: "200ms",
        slow: "250ms",
      },
    },
  },
  plugins: [],
};
