import { fileURLToPath, URL } from "node:url";
import { readFileSync } from "node:fs";

import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const pkg = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf-8"),
) as { version: string };

const BACKEND = process.env.LINLING_WEBUI_BACKEND ?? "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: false,
      },
      "/ws": {
        target: BACKEND.replace(/^http/, "ws"),
        ws: true,
        changeOrigin: false,
      },
    },
  },
  build: {
    // Produce into the Python package's static/ for wheel bundling.
    outDir: "../src/linling_webui/static",
    emptyOutDir: true,
    target: "es2020",
    cssCodeSplit: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        // Reasonable split: vue-core / vendor / app.
        manualChunks(id) {
          if (id.includes("node_modules")) {
            if (id.includes("/vue/") || id.includes("/@vue/") || id.includes("/pinia"))
              return "vue-core";
            return "vendor";
          }
        },
      },
    },
  },
});
