#!/usr/bin/env node
/**
 * Smart start script for Broiler Base Mate frontend.
 *
 * Preview env (source available at /app/silo): launches the Vite hot-reload dev server.
 * Deployed env (no /app/silo): serves the prebuilt /app/frontend/build/ via a small
 *   static HTTP server on PORT (default 3000). Production deployments typically use
 *   nginx + the build/ directory directly, so this is a safety net.
 */
const fs = require("fs");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");

const SILO_DIR = "/app/silo";
const BUILD_DIR = path.join(__dirname, "build");
const PORT = parseInt(process.env.PORT || "3000", 10);
const HOST = process.env.HOST || "0.0.0.0";

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "application/javascript",
  ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".ico": "image/x-icon", ".webmanifest": "application/manifest+json",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".map": "application/json", ".txt": "text/plain",
};

function serveStatic() {
  if (!fs.existsSync(path.join(BUILD_DIR, "index.html"))) {
    console.error(`[start] ✗ ${BUILD_DIR}/index.html missing — run 'yarn build' first.`);
    process.exit(1);
  }
  console.log(`[start] Serving prebuilt static files from ${BUILD_DIR} on http://${HOST}:${PORT}`);
  const srv = http.createServer((req, res) => {
    let urlPath = decodeURIComponent((req.url || "/").split("?")[0]);
    if (urlPath.endsWith("/")) urlPath += "index.html";
    const filePath = path.join(BUILD_DIR, urlPath);
    // Prevent path traversal
    if (!filePath.startsWith(BUILD_DIR)) {
      res.writeHead(403); return res.end("forbidden");
    }
    fs.stat(filePath, (err, stat) => {
      if (err || !stat.isFile()) {
        // SPA fallback — everything unknown returns index.html
        const index = path.join(BUILD_DIR, "index.html");
        fs.readFile(index, (e, d) => {
          if (e) { res.writeHead(404); return res.end("not found"); }
          res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
          res.end(d);
        });
        return;
      }
      const ext = path.extname(filePath).toLowerCase();
      res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream" });
      fs.createReadStream(filePath).pipe(res);
    });
  });
  srv.listen(PORT, HOST, () => console.log(`[start] ✓ Listening on http://${HOST}:${PORT}`));
}

function devServer() {
  console.log("[start] Source at /app/silo detected — launching Vite dev server with hot reload");
  const child = spawn(
    "bash",
    ["-lc", "command -v pnpm >/dev/null 2>&1 || npm install -g pnpm@9 >/dev/null 2>&1; cd /app/silo && BASE_PATH=/ PORT=" + PORT + " pnpm --filter @workspace/feed-program run dev"],
    { stdio: "inherit", env: process.env },
  );
  child.on("exit", (code) => process.exit(code || 0));
  process.on("SIGTERM", () => child.kill("SIGTERM"));
  process.on("SIGINT", () => child.kill("SIGINT"));
}

if (fs.existsSync(path.join(SILO_DIR, "artifacts/feed-program/package.json"))) {
  devServer();
} else {
  serveStatic();
}
