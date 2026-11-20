import { webcrypto } from "node:crypto";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import svgr from "vite-plugin-svgr";
import path from "path";

import { monacoSameOrigin } from "./vite/monaco.ts";
import { suppressViteDisconnectReload } from "./vite/suppress-disconnect-reload.ts";

// serialize-javascript@7 (pulled in by vite-plugin-pwa → @rollup/plugin-terser)
// expects Web Crypto globals during `vite build`. Node <19 does not define them.
if (typeof globalThis.crypto === "undefined") {
  Object.defineProperty(globalThis, "crypto", {
    value: webcrypto,
    configurable: true,
  });
}

function normalizeViteBase(raw: string | undefined): string {
  const value = (raw || "/").trim() || "/";
  if (value === "/") return "/";
  const withLeading = value.startsWith("/") ? value : `/${value}`;
  return withLeading.endsWith("/") ? withLeading : `${withLeading}/`;
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  // Empty = same-origin; frontend and backend served together, no hardcoded host.
  const apiBaseUrl = env.BASE_URL ?? "";
  // Subpath deploy (e.g. reverse-proxy ``/octop/``). Distinct from ``BASE_URL``
  // (optional absolute API origin for local/dev splits).
  const viteBase = normalizeViteBase(
    process.env.VITE_BASE_PATH ?? env.VITE_BASE_PATH,
  );
  // Allow overriding the backend port via VITE_API_PORT env variable (default: 8088)
  const apiPort = process.env.VITE_API_PORT ?? env.VITE_API_PORT ?? "8088";

  const isProd = mode === "production";
  const analyze = env.ANALYZE === "true";
  // Dev server defaults to Vite's :5173. Override with VITE_DEV_PORT (e.g. 80
  // for LAN / same-origin setups). Must stay in sync with server.hmr.clientPort.
  const devServerPort = Number(process.env.VITE_DEV_PORT || 5173);

  // Conditionally load the visualizer plugin (sync require to avoid async issues)
  const extraPlugins: Plugin[] = [];
  if (analyze) {
    try {
      const { visualizer } =
        // eslint-disable-next-line @typescript-eslint/no-require-imports
        require("rollup-plugin-visualizer") as typeof import("rollup-plugin-visualizer");
      extraPlugins.push(
        visualizer({
          open: true,
          filename: "dist/bundle-stats.html",
          gzipSize: true,
          brotliSize: true,
        }) as unknown as Plugin,
      );
    } catch {
      console.warn(
        "rollup-plugin-visualizer not installed, skipping bundle analysis",
      );
    }
  }

  return {
    base: viteBase,
    build: {
      outDir: path.resolve(__dirname, "../src/octop/dashboard"),
      emptyOutDir: true,
      chunkSizeWarningLimit: 1600,
      // Limit Rollup worker concurrency to prevent OOM on memory-constrained hosts.
      // Without this, Rollup spawns one worker per CPU core and antd's 7000+ modules
      // exhaust available RAM when there is no swap space.
      rollupOptions: {
        maxParallelFileOps: 3,
        output: {
          entryFileNames: "assets/index.[hash].js",
          chunkFileNames: "assets/[name].[hash].js",
          assetFileNames: "assets/[name].[hash].[ext]",
          manualChunks(id) {
            if (id.includes("node_modules")) {
              if (id.includes("react-dom")) return "vendor-react";
              if (id.includes("react-router")) return "vendor-react";
              if (/\/node_modules\/react\//.test(id)) return "vendor-react";
              // Keep rc-* with antd in one chunk. Splitting rc-* into vendor-rc
              // creates a circular chunk (vendor-rc ↔ vendor-antd) that breaks
              // production with "Cannot access 'FastColor' before initialization".
              if (id.includes("rc-")) return "vendor-antd";
              if (id.includes("/antd/")) return "vendor-antd";
              if (id.includes("antd-style")) return "vendor-antd";
              if (id.includes("@ant-design")) return "vendor-antd";
              if (id.includes("@xterm")) return "vendor-xterm";
              // mermaid + deps are dynamically imported — skip manualChunks so
              // Vite generates async chunks automatically.
              if (
                id.includes("mermaid") ||
                id.includes("cytoscape") ||
                id.includes("dagre") ||
                id.includes("elkjs") ||
                id.includes("cose-bilkent")
              )
                return undefined;
              if (
                id.includes("react-markdown") ||
                id.includes("remark-gfm") ||
                id.includes("remark-") ||
                id.includes("rehype-") ||
                id.includes("katex")
              )
                return "vendor-markdown";
              if (
                id.includes("react-syntax-highlighter") ||
                id.includes("refractor") ||
                id.includes("highlight.js")
              )
                return undefined;
              if (id.includes("lucide-react")) return "vendor-icons";
              if (
                id.includes("i18next") ||
                id.includes("ahooks") ||
                id.includes("jszip")
              )
                return "vendor-utils";
            }
          },
        },
      },
    },
    esbuild: {
      drop: isProd ? ["debugger"] : [],
      pure: isProd ? ["console.log", "console.debug", "console.info"] : [],
      // Disable identifier mangling to work around @xterm/xterm@6.0.0 crash in
      // production builds: the lib ships as pre-minified ESM with a closure
      // inside `InputHandler.requestMode` that breaks when esbuild renames
      // the outer parameter `i` during re-minification. See
      // https://github.com/xtermjs/xterm.js/issues/5800 — first DCS/CSI mode
      // query from tools like vim / htop / less / opencode triggers
      // "Uncaught ReferenceError: i is not defined" and all further input
      // is dropped. Identifier mangling saves very little vs whitespace /
      // dead-code removal, which remain on.
      minifyIdentifiers: false,
    },
    define: {
      BASE_URL: JSON.stringify(apiBaseUrl),
      MOBILE: false,
    },
    plugins: [
      monacoSameOrigin(__dirname),
      suppressViteDisconnectReload(),
      react(),
      svgr(),
      ...extraPlugins,
    ],
    css: {
      modules: {
        localsConvention: "camelCase",
        generateScopedName: "[name]__[local]__[hash:base64:5]",
      },
      preprocessorOptions: {
        less: {
          javascriptEnabled: true,
        },
      },
    },
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
      dedupe: ["react", "react-dom"],
    },
    server: {
      host: "0.0.0.0",
      port: devServerPort,
      allowedHosts: true,
      // NEVER default to `hmr: false` — Vite still injects @vite/client and a
      // failed WS then triggers location.reload() loops. Pin clientPort so
      // Safari on privileged ports (e.g. :80 via VITE_DEV_PORT) does not build
      // `ws://host:/` when location.port is empty.
      hmr: {
        clientPort: Number(process.env.VITE_HMR_CLIENT_PORT || devServerPort),
      },
      watch: {
        // Exclude large directories that do not need watching to reduce inotify fd usage.
        ignored: [
          "**/node_modules/**",
          "**/.git/**",
          "**/dist/**",
          "**/build/**",
        ],
        usePolling: false,
      },
      proxy: {
        "/api": {
          target: `http://127.0.0.1:${apiPort}`,
          changeOrigin: true,
          // Preserve the browser-facing origin so OAuth callbacks do not point
          // at the proxy target (127.0.0.1:8088).
          xfwd: true,
          // Only proxy API websockets — do not steal Vite's HMR upgrade on `/`.
          ws: true,
        },
      },
    },
    optimizeDeps: {
      // TokenUsage lazy-loads recharts; without pre-bundling the first request
      // often 504s while Vite optimizes on demand.
      include: ["recharts"],
    },
  };
});
