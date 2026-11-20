import { createRoot } from "react-dom/client";
import App from "./App.tsx";
import { initI18n } from "./i18n";
import {
  clearChunkReloadFlag,
  installChunkLoadRecovery,
  isChunkLoadError,
  tryReloadOnStaleChunk,
} from "./utils/reloadOnStaleChunk";
import { bootNativeWindowChrome } from "./utils/nativeWindowChrome";

if (typeof window !== "undefined") {
  // Recover from post-deploy stale hashed chunks (white screen → soft reload).
  installChunkLoadRecovery();

  const originalError = console.error;
  const originalWarn = console.warn;

  console.error = function (...args: unknown[]) {
    const msg = args[0]?.toString() || "";
    if (msg.includes(":first-child") || msg.includes("pseudo class")) {
      return;
    }
    originalError.apply(console, args);
  };

  console.warn = function (...args: unknown[]) {
    const msg = args[0]?.toString() || "";
    if (
      msg.includes(":first-child") ||
      msg.includes("pseudo class") ||
      msg.includes("potentially unsafe")
    ) {
      return;
    }
    originalWarn.apply(console, args);
  };

  // Catch unhandled Promise rejections (e.g. uncaught async errors)
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    // Chunk recovery (installChunkLoadRecovery) already handles these.
    if (isChunkLoadError(reason)) return;
    // Suppress common benign errors
    const msg = reason?.message || String(reason) || "";
    if (
      msg.includes("ResizeObserver") ||
      msg.includes("AbortError") ||
      msg.includes("The operation was aborted")
    ) {
      return;
    }
    console.error("[UnhandledRejection]", reason);
  });

  // Native window buttons are drawn by the OS, so paint them in the stored
  // theme before React mounts (an effect would be one frame too late).
  bootNativeWindowChrome();
}

void initI18n()
  .then(() => {
    createRoot(document.getElementById("root")!).render(<App />);
    // Delay clearing the one-shot reload guard until after first paint / lazy
    // chunks settle. Clearing immediately let Firefox modulepreload noise and
    // aborted dynamic imports re-arm an infinite reload loop.
    window.setTimeout(() => clearChunkReloadFlag(), 4000);
  })
  .catch((err: unknown) => {
    if (tryReloadOnStaleChunk(err)) return;
    console.error("[initI18n]", err);
  });

// The product uses fresh role-specific HTML and durable local data. Retire any
// worker installed by the former web-chat application, including its API cache.
if ("serviceWorker" in navigator) {
  void navigator.serviceWorker
    .getRegistrations()
    .then((registrations) =>
      Promise.all(
        registrations.map((registration) => registration.unregister()),
      ),
    );
}
