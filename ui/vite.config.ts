import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy /api (and /health) to the running mnesis HTTP server so the UI
// talks to the real backend. In production the static bundle is served alongside
// the API (same origin) or pointed elsewhere via runtime config (public/config.js).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: true },
      "/health": { target: "http://localhost:8080", changeOrigin: true },
    },
  },
});
