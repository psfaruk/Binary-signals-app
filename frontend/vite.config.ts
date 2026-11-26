import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Build output: backend/static/app-v2 — the FastAPI server mounts this
// directory as the new SPA root. Keeping the build INSIDE static/ means
// the existing Railway/Vercel includeFiles ("static/**") picks the bundle
// up with zero deployment-config changes.
export default defineConfig({
  // Assets are served by the EXISTING /static mount in server.py — the
  // build output lives inside static/app-v2, so a /static/app-v2/ base
  // makes the hashed asset URLs resolve with ZERO new server code.
  base: "/static/app-v2/",
  plugins: [react()],
  build: {
    outDir: "../static/app-v2",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2020",
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    // Dev proxy: run `uvicorn server:app` alongside; all API calls hit the
    // real backend. No mock data anywhere — directive: production real data.
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
