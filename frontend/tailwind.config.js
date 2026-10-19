/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Brand palette from the AI Wealth Builder symbol (brand/aiwb-logo.png).
        // brand-600 is the sampled deep purple #543477; tints mix towards white,
        // shades towards black, so hover/active/disabled states read correctly.
        // Contrast (WCAG AA): white on 600 9.8:1, 600 on white 9.8:1, 700 on 50 10.5:1,
        // 800 on 100 11.2:1, 500 ring vs white 6.3:1.
        "brand-ink": "#543477",   // alias of brand-600 (wordmark, theme colour)
        "brand-mid": "#61446f",   // sampled mid purple; secondary accent only
        brand: {
          50: "#f5f3f7", 100: "#eae7ef", 200: "#cfc6d9", 300: "#b0a2c0", 400: "#8e79a5",
          500: "#6f548d", 600: "#543477", 700: "#472c64", 800: "#392351", 900: "#2c1b3e",
        },
        // warm tan from the bottom of the symbol's gradient: sparing secondary accent, never a button
        tan: { 50: "#f5f4f2", 100: "#edeae6", 600: "#87735b", 700: "#655644", 800: "#514537" },
      },
    },
  },
  plugins: [],
};
