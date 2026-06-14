#!/usr/bin/env node
/**
 * Smart build script for Broiler Base Mate frontend.
 *
 * Preview env (source available at /app/silo): rebuilds the Vite app then copies
 *   dist/public/* into /app/frontend/build/ so deployment will pick it up.
 *
 * Deployed env (no /app/silo): just verifies the prebuilt /app/frontend/build/
 *   exists. Errors out with a clear message if it doesn't.
 */
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const FRONTEND_DIR = __dirname;
const BUILD_DIR = path.join(FRONTEND_DIR, "build");
const SILO_DIR = "/app/silo";
const VITE_DIST = path.join(SILO_DIR, "artifacts/feed-program/dist/public");

function log(msg) { console.log(`[build] ${msg}`); }

function rebuildFromSource() {
  log("Source detected at /app/silo — running Vite build…");
  execSync("pnpm --filter @workspace/feed-program run build", {
    cwd: SILO_DIR, stdio: "inherit", env: { ...process.env, BASE_PATH: "/" },
  });
  log("Copying dist/public → /app/frontend/build/");
  fs.rmSync(BUILD_DIR, { recursive: true, force: true });
  fs.mkdirSync(BUILD_DIR, { recursive: true });
  fs.cpSync(VITE_DIST, BUILD_DIR, { recursive: true });
  log("✓ Build complete.");
}

function verifyPrebuilt() {
  log("No source at /app/silo — verifying prebuilt /app/frontend/build/");
  const indexPath = path.join(BUILD_DIR, "index.html");
  if (!fs.existsSync(indexPath)) {
    console.error("[build] ✗ /app/frontend/build/index.html missing!");
    console.error("[build]   To fix: run 'yarn build' in the preview env first, which will produce the build/ directory.");
    process.exit(1);
  }
  log("✓ Prebuilt static files found — nginx will serve them directly.");
}

try {
  if (fs.existsSync(VITE_DIST) || fs.existsSync(path.join(SILO_DIR, "artifacts/feed-program/package.json"))) {
    rebuildFromSource();
  } else {
    verifyPrebuilt();
  }
} catch (e) {
  console.error("[build] Build failed:", e.message);
  process.exit(1);
}
