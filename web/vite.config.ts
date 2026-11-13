import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served from the same origin as the API in production, because Apchi's session is
// an httponly, samesite=lax cookie (app/api/auth.py) and a lax cookie is not sent on a
// cross-origin request -- a separately hosted SPA could not authenticate at all. The proxy
// below keeps development on one origin too, so the cookie behaves the same in both.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: false },
    },
  },
});
