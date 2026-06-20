"""
Production hardening: error logging, daily DB backups, global exception handler.

Wires three reliability nets into the FastAPI app:

  1. log_error()             — single helper used everywhere
                               (writes to MongoDB + emails admin with cooldown).
  2. Global exception handler — catches any uncaught backend exception,
                               returns a clean JSON 500 and logs it.
  3. POST /api/error-report   — receives JS errors from the browser.
  4. Nightly backup task      — gzipped JSON dump of every collection at 02:00 UTC,
                               keeps last 7 days at /app/backups, surfaces status via
                               GET /api/admin/last-backup.
  5. GET /api/admin/error-log — recent error visibility for the owner.
  6. Rate limiter             — protects abusable endpoints (auth/exchange-session,
                               error-report, demo-request, outreach send) from
                               accidental floods or scraper-bot abuse.

Designed to be:
  • Zero new dependencies — uses asyncio + stdlib only.
  • Self-throttling — admin email cooldown keyed by (category + signature).
  • Idempotent — safe to call init_hardening() multiple times.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from bson import ObjectId

log = logging.getLogger("hardening")

BACKUP_DIR = Path("/app/backups")
BACKUP_KEEP_DAYS = 7
BACKUP_HOUR_UTC = 2  # 02:00 UTC nightly
ADMIN_EMAIL_COOLDOWN_S = 3600  # 1h between identical alert emails

_email_cooldown: dict[str, datetime] = {}

# ─── Rate limiter (in-memory token-bucket, per-IP per-path) ────────────
# Designed for low traffic single-process FastAPI. If we ever scale to multi-
# worker, swap this for Redis. For now (one Uvicorn worker), this is fine.

# (per-path) max events per window-seconds
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/api/error-report":             (60, 60),     # 60 errors / min / IP — generous, individual page can burp
    "/api/auth/exchange-session":    (10, 60),     # 10 OAuth exchanges / min / IP
    "/api/demo-request":             (5, 600),     # 5 demo requests / 10min / IP
    "/api/partner-request":          (5, 600),     # 5 partner submits / 10min / IP
    "/api/outreach/send":            (30, 60),     # 30 outreach sends / min / IP (admin only anyway)
    "/api/scan-docket/auto":         (30, 60),     # 30 AI docket scans / min / IP (LLM cost)
}

_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    # Trust X-Forwarded-For when behind nginx; fall back to direct peer
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request) -> Optional[JSONResponse]:
    """Returns a 429 response if the caller exceeded the limit for this path,
    else None (allow through). Called from middleware."""
    path = request.url.path
    cfg = RATE_LIMITS.get(path)
    if not cfg:
        return None
    max_events, window_s = cfg
    key = f"{path}|{_client_ip(request)}"
    now = time.monotonic()
    cutoff = now - window_s
    bucket = _rate_buckets[key]
    # Trim expired
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_events:
        retry_after = int(window_s - (now - bucket[0])) + 1
        return JSONResponse(
            {"detail": f"Too many requests — try again in {retry_after}s."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    bucket.append(now)
    return None



def _sig(category: str, message: str) -> str:
    h = hashlib.sha1(f"{category}|{message[:200]}".encode("utf-8")).hexdigest()[:12]
    return f"{category}:{h}"


async def log_error(
    db,
    *,
    category: str,
    message: str,
    details: Optional[dict] = None,
    request: Optional[Request] = None,
    silent_email: bool = False,
) -> None:
    """Persist an error and (rate-limited) email the admin.

    `category` is a short string like 'stripe_provision', 'frontend_js',
    'backend_500', 'backup_job'. Used to group and to cool down email alerts.
    """
    now = datetime.now(timezone.utc)
    signature = _sig(category, message)
    doc: dict[str, Any] = {
        "category":  category,
        "message":   message[:2000],
        "details":   details or {},
        "signature": signature,
        "createdAt": now,
    }
    if request is not None:
        try:
            doc["path"] = str(request.url.path)
            doc["method"] = request.method
            doc["ip"] = request.client.host if request.client else None
            doc["ua"] = request.headers.get("user-agent", "")[:200]
        except Exception:
            pass
    try:
        await db["error_log"].insert_one(doc)
    except Exception:
        log.exception("Could not write to error_log collection")

    if silent_email:
        return

    last = _email_cooldown.get(signature)
    if last and (now - last).total_seconds() < ADMIN_EMAIL_COOLDOWN_S:
        return
    _email_cooldown[signature] = now

    admin = os.environ.get("ADMIN_EMAIL") or os.environ.get("OUTREACH_ADMIN_EMAILS", "").split(",")[0].strip()
    if not admin:
        return
    try:
        from email_service import send_email
        await send_email(
            to=admin,
            subject=f"🚨 BBM error [{category}]: {message[:80]}",
            html=(
                f"<h3>Broiler Base Mate — error alert</h3>"
                f"<p><b>Category:</b> {category}<br>"
                f"<b>When:</b> {now.isoformat()}<br>"
                f"<b>Signature:</b> {signature}</p>"
                f"<p><b>Message:</b><br><pre style='white-space:pre-wrap;font-family:monospace;background:#f5f5f5;padding:10px;border-radius:6px;'>"
                f"{message[:1500]}</pre></p>"
                + (f"<p><b>Path:</b> {doc.get('method','')} {doc.get('path','')}<br><b>IP:</b> {doc.get('ip','')}</p>" if request else "")
                + (f"<p><b>Details:</b><br><pre style='white-space:pre-wrap;font-family:monospace;background:#f5f5f5;padding:10px;border-radius:6px;'>{json.dumps(details, indent=2, default=str)[:1500]}</pre></p>" if details else "")
                + "<p style='color:#888;font-size:12px;'>Next identical alert suppressed for 1 hour.</p>"
            ),
        )
    except Exception:
        log.exception("Could not email admin error alert")


# ─── Backup ────────────────────────────────────────────────────────────────

def _json_default(o):
    if isinstance(o, ObjectId): return str(o)
    if isinstance(o, datetime): return o.isoformat()
    if isinstance(o, bytes):    return f"<bytes:{len(o)}>"
    return str(o)


async def run_backup(db) -> dict:
    """Dump every collection in the DB to a gzipped JSON file. Returns metadata."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp   = started.strftime("%Y%m%d_%H%M%S")
    out     = BACKUP_DIR / f"backup_{stamp}.json.gz"

    payload: dict[str, list] = {}
    counts: dict[str, int]   = {}
    collections = await db.list_collection_names()
    # Skip oversized/transient collections that don't belong in a recovery backup
    SKIP = {"sessions"}
    for coll in collections:
        if coll in SKIP:
            continue
        docs = await db[coll].find().to_list(length=100000)
        payload[coll]  = docs
        counts[coll]   = len(docs)

    body = json.dumps(payload, default=_json_default).encode("utf-8")
    with gzip.open(out, "wb", compresslevel=6) as fh:
        fh.write(body)

    # Retention: keep last N days only
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_KEEP_DAYS)
    for f in BACKUP_DIR.glob("backup_*.json.gz"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
        except Exception:
            pass

    meta = {
        "file":         str(out),
        "size_bytes":   out.stat().st_size,
        "collections":  counts,
        "total_docs":   sum(counts.values()),
        "started_at":   started.isoformat(),
        "finished_at":  datetime.now(timezone.utc).isoformat(),
    }
    await db["backup_log"].insert_one({**meta, "createdAt": started})
    return meta


async def _backup_loop(db):
    """Wakes up once a day at ~02:00 UTC and runs a backup."""
    while True:
        now = datetime.now(timezone.utc)
        # next 02:00 UTC
        target = now.replace(hour=BACKUP_HOUR_UTC, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        sleep_for = (target - now).total_seconds()
        try:
            await asyncio.sleep(sleep_for)
            meta = await run_backup(db)
            # Notify admin (cooled-down to once a day automatically by the sleep)
            admin = os.environ.get("ADMIN_EMAIL") or os.environ.get("OUTREACH_ADMIN_EMAILS", "").split(",")[0].strip()
            if admin:
                try:
                    from email_service import send_email
                    await send_email(
                        to=admin,
                        subject=f"✅ BBM nightly backup OK ({meta['total_docs']} docs, {round(meta['size_bytes']/1024,1)} KB)",
                        html=(
                            f"<p>Backup completed.</p>"
                            f"<p><b>File:</b> {meta['file']}<br>"
                            f"<b>Size:</b> {round(meta['size_bytes']/1024,1)} KB<br>"
                            f"<b>Total docs:</b> {meta['total_docs']}</p>"
                            f"<p><b>By collection:</b></p>"
                            f"<ul>" + "".join(f"<li>{k}: {v}</li>" for k,v in meta["collections"].items()) + "</ul>"
                        ),
                    )
                except Exception:
                    log.exception("Backup OK email failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                await log_error(db, category="backup_job", message=f"Nightly backup failed: {e}", details={"traceback": traceback.format_exc()})
            except Exception:
                log.exception("Backup failed AND log_error failed")
            # On failure still sleep a day so we don't busy-loop
            await asyncio.sleep(3600)


# ─── Wire-up ───────────────────────────────────────────────────────────────

class FrontendError(BaseModel):
    message: str
    stack: Optional[str] = None
    url:    Optional[str] = None
    page:   Optional[str] = None


def init_hardening(app: FastAPI, db, _require_admin) -> None:
    """Mount all hardening pieces into the FastAPI app."""

    # 0) Rate limit middleware — protect hot endpoints from runaway floods
    @app.middleware("http")
    async def _rate_limit_mw(request: Request, call_next):
        limited = check_rate_limit(request)
        if limited is not None:
            return limited
        return await call_next(request)

    # 1) Global exception handler — clean JSON 500 + log
    @app.exception_handler(Exception)
    async def _global_exc_handler(request: Request, exc: Exception):
        # Let intentional HTTPExceptions pass through normally
        if isinstance(exc, HTTPException):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        await log_error(
            db,
            category="backend_500",
            message=f"{type(exc).__name__}: {exc}",
            details={"traceback": traceback.format_exc()[-2000:]},
            request=request,
        )
        return JSONResponse(
            {"detail": "Something went wrong on our side. We've been notified."},
            status_code=500,
        )

    # 2) Frontend error report endpoint
    @app.post("/api/error-report")
    async def error_report(body: FrontendError, request: Request):
        await log_error(
            db,
            category="frontend_js",
            message=body.message[:500] or "unknown JS error",
            details={
                "stack": (body.stack or "")[:1500],
                "url":   body.url,
                "page":  body.page,
            },
            request=request,
        )
        return {"ok": True}

    # 3) Admin visibility endpoints
    @app.get("/api/admin/error-log")
    async def admin_error_log(request: Request, limit: int = 100):
        await _require_admin(request)
        rows = await db["error_log"].find().sort("createdAt", -1).limit(min(limit, 500)).to_list(length=limit)
        for r in rows:
            r.pop("_id", None)
            if isinstance(r.get("createdAt"), datetime):
                r["createdAt"] = r["createdAt"].isoformat()
        return rows

    @app.get("/api/admin/last-backup")
    async def admin_last_backup(request: Request):
        await _require_admin(request)
        row = await db["backup_log"].find_one(sort=[("createdAt", -1)])
        if not row:
            return {"backup": None}
        row.pop("_id", None)
        if isinstance(row.get("createdAt"), datetime):
            row["createdAt"] = row["createdAt"].isoformat()
        return {"backup": row}

    @app.post("/api/admin/backup-now")
    async def admin_backup_now(request: Request):
        await _require_admin(request)
        meta = await run_backup(db)
        return meta

    # Admin "health pulse" — one JSON the dashboard can render for daily check-ins.
    @app.get("/api/admin/health")
    async def admin_health(request: Request):
        await _require_admin(request)
        now = datetime.now(timezone.utc)
        day_ago  = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)

        errors_24h = await db["error_log"].count_documents({"createdAt": {"$gte": day_ago}})
        errors_7d  = await db["error_log"].count_documents({"createdAt": {"$gte": week_ago}})
        last_err   = await db["error_log"].find_one(sort=[("createdAt", -1)])

        last_backup = await db["backup_log"].find_one(sort=[("createdAt", -1)])
        farm_count  = await db["farms"].count_documents({})
        reading_24h = await db["readings"].count_documents({"createdAt": {"$gte": day_ago}})
        chat_24h    = await db["chat_messages"].count_documents({"created_at": {"$gte": day_ago}})

        paid_total  = 0.0
        paid_count  = 0
        cur = db["payment_transactions"].find({"payment_status": "paid"})
        async for p in cur:
            try:
                paid_total += float(p.get("amount") or 0)
                paid_count += 1
            except Exception:
                pass

        # Active outreach pulse
        out_pending = await db["outreach_contacts"].count_documents({"status": {"$in": ["pending", "in_progress"]}})
        out_replied = await db["outreach_contacts"].count_documents({"status": "replied"})

        def iso(v):
            return v.isoformat() if isinstance(v, datetime) else v

        return {
            "now": now.isoformat(),
            "errors": {
                "last_24h": errors_24h,
                "last_7d":  errors_7d,
                "latest":   ({
                    "category":  last_err.get("category"),
                    "message":   (last_err.get("message") or "")[:200],
                    "createdAt": iso(last_err.get("createdAt")),
                } if last_err else None),
            },
            "backup": {
                "last_run":   iso(last_backup.get("createdAt")) if last_backup else None,
                "size_bytes": (last_backup or {}).get("size_bytes"),
                "total_docs": (last_backup or {}).get("total_docs"),
            },
            "usage": {
                "farms":         farm_count,
                "readings_24h":  reading_24h,
                "chat_msgs_24h": chat_24h,
            },
            "revenue": {
                "paid_orders": paid_count,
                "gross":       round(paid_total, 2),
            },
            "outreach": {
                "pending": out_pending,
                "replied": out_replied,
            },
        }

    # 4) Start the nightly backup loop on app startup
    @app.on_event("startup")
    async def _start_backup_loop():
        if getattr(app.state, "_backup_task", None):
            return
        app.state._backup_task = asyncio.create_task(_backup_loop(db))
        log.info("[hardening] nightly backup loop started — runs at %02d:00 UTC daily", BACKUP_HOUR_UTC)

    @app.on_event("shutdown")
    async def _stop_backup_loop():
        t = getattr(app.state, "_backup_task", None)
        if t and not t.done():
            t.cancel()
