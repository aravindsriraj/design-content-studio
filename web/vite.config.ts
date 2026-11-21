import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, the API runs on :8000 (`uv run uvicorn app.fast_api_app:app --port 8000`).
const api = "http://127.0.0.1:8000";
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": api, "/run_sse": api, "/apps": api } },
});
