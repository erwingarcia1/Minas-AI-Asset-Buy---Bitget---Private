/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // deep purple sampled from the AI Wealth Builder symbol (brand/aiwb-logo.jpg); used for the wordmark and the browser theme colour
        "brand-ink": "#543477",
        brand: {
          50: "#eef6ff", 100: "#d9eaff", 200: "#bcdbff", 300: "#8ec3ff", 400: "#59a1ff",
          500: "#337cff", 600: "#1b5cf5", 700: "#1447e1", 800: "#173ab6", 900: "#19358f",
        },
      },
    },
  },
  plugins: [],
};
