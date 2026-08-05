import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  const archiveEnv = loadEnv(mode, "..", "");
  const apiTarget = archiveEnv.VITE_API_PROXY_TARGET
    ?? `http://127.0.0.1:${archiveEnv.WEB_PORT || "8000"}`;

  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      proxy: {
        "/api": apiTarget,
        "/health": apiTarget
      }
    }
  };
});
