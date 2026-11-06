import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api so the session cookie stays first-party.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: false } },
  },
  // AG Grid is the bulk of the bundle; keep it in its own cacheable chunk.
  build: { outDir: "dist", sourcemap: true, chunkSizeWarningLimit: 1500, rollupOptions: { output: { manualChunks: { aggrid: ["ag-grid-community", "ag-grid-react"] } } } },
});
