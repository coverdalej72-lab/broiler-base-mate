#!/usr/bin/env node
/**
 * Smart build script for Broiler Base Mate frontend.
 *
 * Preview env (source available at /app/silo): rebuilds the Vite app then copies
 *   dist/public/* into /app/frontend/build/.
 *
 * IMPORTANT: Also copies the static HTML pages (landing, reader, ops-dashboard,
 *   success) and their assets from /app/backend/static/ into the build/ directory
 *   so production nginx can serve them at /landing/, /reader/, /ops-dashboard/
 *   instead of falling back to the React SPA index.html.
 *
 * Deployed env (no /app/silo): just verifies the prebuilt /app/frontend/build/ exists.
 */
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const FRONTEND_DIR = __dirname;
const BUILD_DIR = path.join(FRONTEND_DIR, "build");
const SILO_DIR = "/app/silo";
const VITE_DIST = path.join(SILO_DIR, "artifacts/feed-program/dist/public");
const BACKEND_STATIC = "/app/backend/static";

function log(msg) { console.log(`[build] ${msg}`); }

function cpFile(src, dest) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
}

function copyStaticPages() {
  // Map each static HTML to /<route>/index.html so nginx serves it on /<route>/ requests
  // and a fallback /<route>.html so /<route> (no slash) also resolves via $uri.html.
  const pages = [
    { src: "landing.html",       dirRoute: "landing" },
    { src: "reader.html",        dirRoute: "reader" },
    { src: "ops-dashboard.html", dirRoute: "ops-dashboard" },
    // success.html lives under /landing/success in production
    { src: "success.html",       dirRoute: "landing/success" },
  ];
  for (const p of pages) {
    const src = path.join(BACKEND_STATIC, p.src);
    if (!fs.existsSync(src)) { log(`  ⚠ ${p.src} missing in backend/static — skipping`); continue; }
    cpFile(src, path.join(BUILD_DIR, p.dirRoute, "index.html"));
    cpFile(src, path.join(BUILD_DIR, p.dirRoute + ".html"));
    log(`  ✓ ${p.src} → ${p.dirRoute}/index.html (+ ${p.dirRoute}.html)`);
  }

  // Copy /reader-assets/* — both as build root /reader-assets/* and as raw static.
  // These are referenced by URLs starting with "/reader-assets/" from the HTML pages.
  const assets = fs.readdirSync(BACKEND_STATIC);
  let count = 0;
  for (const a of assets) {
    if (a.endsWith(".html")) continue; // already handled
    const stat = fs.statSync(path.join(BACKEND_STATIC, a));
    if (stat.isFile()) {
      cpFile(path.join(BACKEND_STATIC, a), path.join(BUILD_DIR, "reader-assets", a));
      count++;
    }
  }
  log(`  ✓ Copied ${count} files into /reader-assets/`);
}

function rebuildFromSource() {
  log("Source detected at /app/silo — running Vite build…");
  execSync("pnpm --filter @workspace/feed-program run build", {
    cwd: SILO_DIR, stdio: "inherit", env: { ...process.env, BASE_PATH: "/" },
  });
  log("Copying dist/public → /app/frontend/build/");
  fs.rmSync(BUILD_DIR, { recursive: true, force: true });
  fs.mkdirSync(BUILD_DIR, { recursive: true });
  fs.cpSync(VITE_DIST, BUILD_DIR, { recursive: true });
  log("Injecting static HTML pages + assets…");
  copyStaticPages();
  log("✓ Build complete.");
}

function verifyPrebuilt() {
  log("No source at /app/silo — verifying prebuilt /app/frontend/build/");
  const indexPath = path.join(BUILD_DIR, "index.html");
  if (!fs.existsSync(indexPath)) {
    console.error("[build] ✗ /app/frontend/build/index.html missing!");
    console.error("[build]   To fix: run 'yarn build' in the preview env first.");
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
