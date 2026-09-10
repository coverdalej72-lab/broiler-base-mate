"""
Broiler Base Mate — Backend (MongoDB-backed).

Provides the API endpoints the Feed Program polls (`/api/readings/today` every
2 minutes) so silo readings entered on a phone in `/reader` flow automatically
into the Feed Program's "FEED USAGE" column.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import RedirectResponse
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

load_dotenv()
# ─── DB ────────────────────────────────────────────────────────────────────
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
_client = AsyncIOMotorClient(MONGO_URL)
db = _client[DB_NAME]

shed_groups_col = db["shed_groups"]
silos_col = db["silos"]
readings_col = db["readings"]
deliveries_col = db["deliveries"]
photos_col = db["photos"]
farm_config_col = db["farm_config"]
feed_program_state_col = db["feed_program_state"]
feed_program_state_history_col = db["feed_program_state_history"]
feed_program_catches_col = db["feed_program_catches"]
payments_col = db["payment_transactions"]
farms_col = db["farms"]
eob_snapshots_col = db["eob_snapshots"]
external_morts_col = db["external_morts"]
external_weighins_col = db["external_weighins"]

DEFAULT_FARM_ID = "default"


def _farm_filter(farm_id: str) -> dict:
    """Returns a Mongo filter that matches docs with farmId == farm_id.

    For backward compatibility, when farm_id is 'default' it also matches docs
    written before the multi-farm migration (no farmId field at all).
    """
    if farm_id == DEFAULT_FARM_ID:
        return {"$or": [{"farmId": DEFAULT_FARM_ID}, {"farmId": {"$exists": False}}]}
    return {"farmId": farm_id}


def _and(*filters: dict) -> dict:
    """Combine filters with logical AND, skipping empties."""
    parts = [f for f in filters if f]
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}

# ─── App ───────────────────────────────────────────────────────────────────
app = FastAPI(title="Broiler Base Mate API")

@app.get("/health")
async def _root_health():
    return {"status": "ok"}

# CORS: explicit origins (not "*") because we use credential cookies. Add both
# preview and production hosts plus localhost for dev.
def _cors_origins() -> list[str]:
    pub = (os.environ.get("APP_PUBLIC_URL") or "").rstrip("/")
    base = ["http://localhost:3000", "http://localhost:8001"]
    if pub: base.append(pub)
    # Common companion URL in same Emergent environment
    if ".preview.emergentagent.com" in pub:
        base.append(pub.replace(".preview.emergentagent.com", ".emergent.host"))
    if ".emergent.host" in pub:
        base.append(pub.replace(".emergent.host", ".preview.emergentagent.com"))
    return sorted(set(base))

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── www → apex canonical 301 redirect ─────────────────────────────────────
# SEO backup for DNS-level redirect. Semrush flagged https://www.broilerbasemate.com.au/
# as uncrawlable — this middleware guarantees any request hitting the www host
# gets 301'd to the apex, preserving link equity.
@app.middleware("http")
async def www_to_apex_redirect(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    if host.startswith("www."):
        apex = host[4:]
        target = f"https://{apex}{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(url=target, status_code=301)
    return await call_next(request)

# ─── Auth (Emergent Google Auth) ──────────────────────────────────────────
# REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
from auth import build_router as _build_auth_router, get_current_user as _get_current_user, list_user_farms as _list_user_farms, COOKIE_NAME as _AUTH_COOKIE  # noqa: E402

app.include_router(_build_auth_router(db))


async def _user_from_request(request: Request) -> Optional[dict]:
    """Helper that the API endpoints can call to check who the caller is."""
    token = request.cookies.get(_AUTH_COOKIE)
    auth_hdr = request.headers.get("authorization")
    return await _get_current_user(db, session_token=token, authorization=auth_hdr)


async def _require_admin(request: Request) -> dict:
    """Ensure the current user has admin role (SUPERUSER_EMAILS). Returns user dict."""
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    farms_info = await _list_user_farms(db, user["email"])
    if farms_info["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user


# ─── Production hardening (error logging, daily backups, etc.) ──────────
from hardening import init_hardening, log_error  # noqa: E402
init_hardening(app, db, _require_admin)

# ─── Farm Buddy AI advisor ───────────────────────────────────────────────
from farm_buddy import init_farm_buddy, init_farm_buddy_reconcile  # noqa: E402
init_farm_buddy(app, db)
init_farm_buddy_reconcile(app, db)


# ─── End-of-Batch email report sender ───────────────────────────────────
# Many users don't have a default mail client configured on phones/PCs,
# so mailto: links open a blank tab and frustrate them. This endpoint
# sends the report directly via Resend instead.

class EobDeliveryRow(BaseModel):
    date:   str = ""
    docket: str = ""
    kg:     float = 0

class EobFeedType(BaseModel):
    name:  str
    color: Optional[str] = None
    total: float = 0
    rows:  List[EobDeliveryRow] = []

class EobShedRow(BaseModel):
    shed:    str
    placed:  int = 0
    morts:   int = 0
    caught:  int = 0
    balance: int = 0
    mtec:    Optional[float] = None  # NEW — MTEC (mortality trend) column from processor sheet


class EobReport(BaseModel):
    """Structured End-of-Batch payload. When sent, the backend renders a
    beautiful branded HTML email instead of the plain `<pre>` text dump.
    Every field is optional so old clients that only send `body` still work."""
    batchName:        Optional[str] = None
    generatedDate:    Optional[str] = None
    feedTypes:        List[EobFeedType] = []
    totalPurchased:   Optional[float] = None
    sheds:            List[EobShedRow] = []
    totalPlaced:      Optional[int] = None
    totalMorts:       Optional[int] = None
    totalCaught:      Optional[int] = None
    totalBalance:     Optional[int] = None
    mortalityPct:     Optional[float] = None
    lastBatchLeft:    Optional[float] = None
    totalDelivered:   Optional[float] = None
    totalUsed:        Optional[float] = None
    feedLeft:         Optional[float] = None
    netConsumed:      Optional[float] = None
    aveWeight:        Optional[float] = None
    fcr:              Optional[float] = None
    cfcr:             Optional[float] = None
    # NEW — processor-standard metrics that the settlement sheet always shows.
    # Previously missing from the email payload, so these KPI tiles rendered "—".
    actualAge:        Optional[float] = None   # Average catch age (days)
    correctedAge:     Optional[float] = None   # Age corrected to 2.45 kg standard
    totalLiveWeightKg: Optional[float] = None  # Total kg of birds picked up
    # NEW — head-office additions matching the physical Excel report
    farmName:         Optional[str] = None   # e.g. "Double B"
    batchNumber:      Optional[int] = None   # e.g. 121
    lastBatchNumber:  Optional[int] = None   # e.g. 114
    farmLogoData:     Optional[str] = None   # base64 PNG, if user uploaded one
    # NEW Feb 2026 — processor settlement metrics (cage, efficiency, payment)
    cageRating:       Optional[float] = None  # 39.5 / correctedAge
    efficiencyRating: Optional[float] = None  # (1.78/cFCR)×0.7 + (39.5/correctedAge)×0.3
    payment:          Optional[float] = None  # $/bird = ER × 0.005
    paymentTotal:     Optional[float] = None  # payment × totalCaught


class EobEmailRequest(BaseModel):
    to:       List[str]
    subject:  str
    body:     str
    farmName: Optional[str] = None
    report:   Optional[EobReport] = None  # NEW — when present, renders the polished HTML


def _render_eob_html(r: EobReport, farm_name: str, sender: str) -> str:
    """Render the structured EOB payload as a premium branded HTML email.

    Designed to be the kind of report a grower can forward to head office /
    integrator / accountant and feel proud of — branded hero, KPI tiles,
    per-feed-type delivery breakdown, per-shed bird table, full feed summary.
    All inline CSS (no external stylesheets) so Gmail/Outlook/Apple Mail all
    render it identically. ~700 px max width — looks great on phone + desktop.
    """
    # SEC-005: HTML-escape any user-controlled string that lands inside the
    # email markup. Prevents an attacker who controls farm name / batch name /
    # feed type from injecting <a href="phish"> or images into the report.
    import html as _html
    def esc(s: Optional[str]) -> str:
        return _html.escape(str(s), quote=True) if s is not None else ""

    def fmt_n(n: Optional[float], suffix: str = "") -> str:
        if n is None or n == 0:
            return "—"
        if isinstance(n, float) and not n.is_integer():
            return f"{n:,.2f}{suffix}"
        return f"{int(n):,}{suffix}"

    def fmt_pct(n: Optional[float]) -> str:
        return "—" if n is None or n == 0 else f"{n:.2f}%"

    # Logo — use the farm's uploaded base64 if provided, else default Appcovi mark
    logo_src = (
        f"data:image/png;base64,{r.farmLogoData}"
        if r.farmLogoData else
        "https://broilerbasemate.com.au/reader-assets/company-logo.png"
    )
    batch = esc(r.batchName or (f"Batch #{r.batchNumber}" if r.batchNumber else "Batch"))
    prev_batch_note = f" · Prev Batch #{esc(r.lastBatchNumber)}" if r.lastBatchNumber else ""
    gen   = esc(r.generatedDate or datetime.now(timezone.utc).strftime("%d %b %Y"))

    # ── HERO (Appcovi navy + gold) ─────────────────────────────────────
    # Note: farm-uploaded logos still show in the hero. The Appcovi brand mark
    # moves to the footer per Jason (Feb 2026 — cleaner corporate look).
    show_hero_logo = bool(r.farmLogoData)
    hero_logo_cell = (
        f'<td valign="middle" style="padding-right:14px;width:72px;">'
        f'<img src="{logo_src}" alt="" width="64" height="64" style="display:block;border-radius:12px;background:#fff;padding:4px;" />'
        f'</td>'
        if show_hero_logo else ""
    )
    hero = f"""
      <div style="background:linear-gradient(135deg,#1e2f4d 0%,#0f1a2f 100%);padding:32px 28px;color:#fff;border-radius:16px 16px 0 0;border-bottom:3px solid #C9A227;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          {hero_logo_cell}
          <td valign="middle">
            <div style="font-size:11px;letter-spacing:3px;color:#C9A227;font-weight:800;margin-bottom:2px;">END OF BATCH REPORT</div>
            <div style="font-size:24px;font-weight:900;letter-spacing:-0.4px;line-height:1.15;">{esc(r.farmName or farm_name)}</div>
            <div style="font-size:13px;color:#bdd3ee;margin-top:4px;">{batch}{prev_batch_note} · Generated {gen}</div>
          </td>
        </tr></table>
      </div>
    """

    # ── KPI tiles (Appcovi palette) ────────────────────────────────────
    def kpi(label: str, value: str, accent: str = "#1e2f4d") -> str:
        return f"""
          <td valign="top" style="padding:6px;">
            <div style="background:#fff;border:1px solid #dce3ee;border-radius:10px;padding:14px 12px;text-align:center;">
              <div style="font-size:22px;font-weight:900;color:{accent};letter-spacing:-0.5px;line-height:1;">{value}</div>
              <div style="font-size:10px;letter-spacing:1.2px;color:#5a6a86;text-transform:uppercase;margin-top:6px;font-weight:700;">{label}</div>
            </div>
          </td>
        """
    kpis = f"""
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;">
        <tr>
          {kpi("Birds Placed", fmt_n(r.totalPlaced))}
          {kpi("Birds Caught", fmt_n(r.totalCaught), "#1a7a40")}
          {kpi("Morts", fmt_n(r.totalMorts), "#c0392b")}
          {kpi("Mortality", fmt_pct(r.mortalityPct), "#c0392b")}
        </tr>
        <tr>
          {kpi("Total Feed", fmt_n(r.totalPurchased, " kg"), "#C9A227")}
          {kpi("Feed On Hand", (fmt_n(r.feedLeft, ' kg') if r.feedLeft else "—"), "#e67e22")}
          {kpi("Net Consumed", (fmt_n(r.netConsumed, ' kg') if r.netConsumed else "—"), "#1e2f4d")}
          {kpi("Batch", (r.batchName or "—"), "#5a6a86")}
        </tr>
      </table>
    """

    # ── Payment-Critical Metrics band removed (Feb 2026 — Jason: report should
    # only show what's on the EOB tab, nothing extra). FCR / cFCR are already in
    # the KPI grid above. ────────────────────────────────────────────────────
    payment_band = ""

    # ── Feed deliveries by feed type ───────────────────────────────────
    def feed_section(ft: EobFeedType) -> str:
        if not ft.rows and not ft.total:
            return ""
        color = ft.color or "#2b4266"
        rows_html = "".join(
            f"""<tr>
              <td style="padding:7px 12px;border-bottom:1px solid #e5e9ee;font-size:13px;color:#1a2320;">{esc(row.date) or "—"}</td>
              <td style="padding:7px 12px;border-bottom:1px solid #e5e9ee;font-size:13px;color:#5a6a86;">{esc(row.docket) or "—"}</td>
              <td style="padding:7px 12px;border-bottom:1px solid #e5e9ee;font-size:13px;color:#1a2320;text-align:right;font-weight:600;font-variant-numeric:tabular-nums;">{int(row.kg):,} kg</td>
            </tr>"""
            for row in ft.rows if row.kg > 0
        ) or """<tr><td colspan="3" style="padding:14px;text-align:center;color:#9ca3af;font-size:12px;font-style:italic;">No deliveries</td></tr>"""
        return f"""
          <div style="margin-top:16px;border:1px solid #dce3ee;border-radius:10px;overflow:hidden;background:#fff;">
            <div style="background:{color};color:#fff;padding:9px 14px;font-weight:800;letter-spacing:0.5px;font-size:13px;display:flex;justify-content:space-between;align-items:center;">
              <span style="text-transform:uppercase;">{esc(ft.name)}</span>
              <span style="font-size:15px;background:rgba(255,255,255,0.18);padding:2px 10px;border-radius:99px;">{int(ft.total):,} kg</span>
            </div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <thead>
                <tr style="background:#eef2f9;">
                  <th style="padding:8px 12px;text-align:left;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Date</th>
                  <th style="padding:8px 12px;text-align:left;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Docket #</th>
                  <th style="padding:8px 12px;text-align:right;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Amount</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
          </div>
        """
    feed_sections = "".join(feed_section(ft) for ft in r.feedTypes)
    if not feed_sections:
        feed_sections = """<div style="margin-top:16px;padding:18px;text-align:center;color:#9ca3af;font-size:13px;background:#eef2f9;border:1px dashed #e3dccb;border-radius:10px;">No feed deliveries recorded for this batch.</div>"""

    # ── Per-shed bird table ────────────────────────────────────────────
    if r.sheds:
        # Only show the MTEC column if at least one shed has an MTEC value —
        # keeps the table clean for growers who don't track it.
        show_mtec = any((s.mtec or 0) > 0 for s in r.sheds)
        total_mtec = sum((s.mtec or 0) for s in r.sheds) if show_mtec else 0
        shed_rows = "".join(
            f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e9ee;font-weight:700;color:#1e2f4d;">Shed {s.shed}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e9ee;text-align:right;font-variant-numeric:tabular-nums;">{s.placed:,}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e9ee;text-align:right;color:#c0392b;font-variant-numeric:tabular-nums;">{('−' + format(s.morts, ',')) if s.morts > 0 else '—'}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e9ee;text-align:right;font-variant-numeric:tabular-nums;">{(format(s.caught, ',') if s.caught > 0 else '—')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e9ee;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;color:{'#1e2f4d' if s.balance >= 0 else '#c0392b'};">{s.balance:,}</td>
              {f'<td style="padding:8px 12px;border-bottom:1px solid #e5e9ee;text-align:right;font-variant-numeric:tabular-nums;color:#5a6a86;">{int(s.mtec or 0):,}</td>' if show_mtec else ''}
            </tr>"""
            for s in r.sheds
        )
        totals_row = f"""<tr style="background:#1e2f4d;color:#fff;">
            <td style="padding:10px 12px;font-weight:800;letter-spacing:0.5px;text-transform:uppercase;font-size:12px;">Totals</td>
            <td style="padding:10px 12px;text-align:right;font-weight:800;font-variant-numeric:tabular-nums;">{fmt_n(r.totalPlaced)}</td>
            <td style="padding:10px 12px;text-align:right;font-weight:800;color:#ffb3a7;font-variant-numeric:tabular-nums;">{('−' + (fmt_n(r.totalMorts))) if (r.totalMorts or 0) > 0 else '—'}</td>
            <td style="padding:10px 12px;text-align:right;font-weight:800;font-variant-numeric:tabular-nums;">{fmt_n(r.totalCaught)}</td>
            <td style="padding:10px 12px;text-align:right;font-weight:800;font-variant-numeric:tabular-nums;">{fmt_n(r.totalBalance)}</td>
            {f'<td style="padding:10px 12px;text-align:right;font-weight:800;font-variant-numeric:tabular-nums;">{int(total_mtec):,}</td>' if show_mtec else ''}
          </tr>"""
        mtec_header = '<th style="padding:9px 12px;text-align:right;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">MTEC</th>' if show_mtec else ''
        bird_section = f"""
          <h3 style="margin:28px 0 10px;color:#1e2f4d;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🐔 Bird Summary</h3>
          <div style="background:#fff;border:1px solid #dce3ee;border-radius:10px;overflow:hidden;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:13px;">
              <thead>
                <tr style="background:#eef2f9;">
                  <th style="padding:9px 12px;text-align:left;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Shed</th>
                  <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Placed</th>
                  <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Morts</th>
                  <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Caught</th>
                  <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Balance</th>
                  {mtec_header}
                </tr>
              </thead>
              <tbody>{shed_rows}{totals_row}</tbody>
            </table>
          </div>
        """
    else:
        bird_section = ""

    # ── Feed summary block ─────────────────────────────────────────────
    feed_summary = f"""
      <h3 style="margin:28px 0 10px;color:#1e2f4d;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🌾 Feed Summary</h3>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fff;border:1px solid #dce3ee;border-radius:10px;overflow:hidden;font-size:13px;">
        <tr><td style="padding:9px 14px;border-bottom:1px solid #e5e9ee;color:#5a6a86;">Last Batch Left</td><td style="padding:9px 14px;border-bottom:1px solid #e5e9ee;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{fmt_n(r.lastBatchLeft, ' kg')}</td></tr>
        <tr><td style="padding:9px 14px;border-bottom:1px solid #e5e9ee;color:#5a6a86;">Total Delivered</td><td style="padding:9px 14px;border-bottom:1px solid #e5e9ee;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{fmt_n(r.totalDelivered, ' kg')}</td></tr>
        <tr><td style="padding:9px 14px;border-bottom:1px solid #e5e9ee;color:#5a6a86;">Total Used</td><td style="padding:9px 14px;border-bottom:1px solid #e5e9ee;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{fmt_n(r.totalUsed, ' kg')}</td></tr>
        <tr><td style="padding:9px 14px;border-bottom:1px solid #e5e9ee;color:#5a6a86;">Feed Left</td><td style="padding:9px 14px;border-bottom:1px solid #e5e9ee;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{fmt_n(r.feedLeft, ' kg')}</td></tr>
        <tr style="background:#fffbe6;"><td style="padding:11px 14px;color:#1e2f4d;font-weight:800;letter-spacing:0.4px;text-transform:uppercase;font-size:12px;">Net Consumed</td><td style="padding:11px 14px;text-align:right;font-weight:900;font-variant-numeric:tabular-nums;color:#1e2f4d;font-size:14px;">{fmt_n(r.netConsumed, ' kg')}</td></tr>
      </table>
    """

    # ── Final assembly (Appcovi navy palette) ─────────────────────────
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8" />
<title>End of Batch — {esc(farm_name)}</title></head>
<body style="margin:0;padding:24px 12px;background:#eef2f9;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Roboto,sans-serif;color:#1e2f4d;-webkit-font-smoothing:antialiased;">
  <div style="max-width:680px;margin:0 auto;background:#f7f9fc;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px -12px rgba(15,26,47,0.28);">
    {hero}
    <div style="padding:20px 24px 28px;">
      <h3 style="margin:0 0 4px;color:#1e2f4d;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">📊 Batch Performance</h3>
      {kpis}
      {payment_band}
      <h3 style="margin:28px 0 10px;color:#1e2f4d;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🚚 Feed Deliveries</h3>
      {feed_sections}
      {bird_section}
      {feed_summary}
      <div style="margin-top:32px;padding:22px 20px;background:linear-gradient(135deg,#1e2f4d 0%,#0f1a2f 100%);border-radius:12px;border:1px solid #2b4266;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td valign="middle" style="width:64px;padding-right:16px;">
            <img src="https://broilerbasemate.com.au/reader-assets/company-logo.png" alt="Appcovi" width="56" height="56" style="display:block;border-radius:10px;background:#fff;padding:4px;" />
          </td>
          <td valign="middle" style="color:#dbe4ea;">
            <div style="font-size:10px;letter-spacing:2.5px;color:#8a99b8;font-weight:700;margin-bottom:2px;">BUILT BY</div>
            <div style="font-size:20px;font-weight:900;color:#fff;letter-spacing:0.5px;line-height:1;">APPCOVI</div>
            <div style="font-size:11px;color:#8a99b8;font-weight:600;letter-spacing:1.4px;margin-top:3px;">POULTRY MANAGEMENT SOFTWARE</div>
            <div style="font-size:12px;color:#bdd3ee;margin-top:8px;">
              <a href="https://broilerbasemate.com.au" style="color:#C9A227;text-decoration:none;font-weight:700;">broilerbasemate.com.au</a>
              &nbsp;·&nbsp; Sent by {esc(sender)}
            </div>
          </td>
        </tr></table>
      </div>
    </div>
  </div>
</body></html>"""


# ── PDF generator for the EOB email attachment ────────────────────────────
# Uses weasyprint (pure-Python, no browser needed) to render the same HTML → PDF,
# so head-office receivers get a print-perfect archival copy that matches the
# email view. Returns None on any failure so the email still sends without
# attachment. Runs the CPU-bound render in a thread to keep the event loop free.
async def _render_html_to_pdf(html: str) -> Optional[bytes]:
    import asyncio as _aio
    def _render():
        try:
            from weasyprint import HTML  # noqa: WPS433
            return HTML(string=html).write_pdf()
        except Exception:
            return None
    try:
        return await _aio.to_thread(_render)
    except Exception:
        return None



from fastapi.responses import HTMLResponse

@app.get("/api/eob/preview-sample", response_class=HTMLResponse)
async def eob_preview_sample():
    """Preview-only endpoint that renders the End-of-Batch HTML with sample
    Batch 121 data so the user can eyeball the layout before sending real
    reports. Not linked from the UI — access via /api/eob/preview-sample."""
    r = EobReport(
        farmName="Double B", batchNumber=121, lastBatchNumber=114,
        batchName="Batch #121", generatedDate="13 Jul 2026",
        totalPlaced=532589, totalCaught=515872, totalMorts=16717, mortalityPct=3.14,
        aveWeight=3.069, fcr=1.522, cfcr=1.355, actualAge=41.4, correctedAge=33.0,
        totalLiveWeightKg=1567081, totalPurchased=2306260,
        cageRating=1.197, efficiencyRating=1.276, payment=0.00638, paymentTotal=3268.42,
        lastBatchLeft=118000, totalDelivered=2306260, totalUsed=2306260,
        feedLeft=140000, netConsumed=2166260,
        sheds=[
            EobShedRow(shed="1",  placed=41000, morts=872,  caught=40128, balance=0, mtec=1898),
            EobShedRow(shed="2",  placed=44000, morts=1072, caught=42928, balance=0, mtec=1735),
            EobShedRow(shed="3",  placed=40500, morts=2820, caught=37680, balance=0, mtec=1657),
            EobShedRow(shed="4",  placed=41200, morts=592,  caught=40608, balance=0, mtec=1740),
            EobShedRow(shed="5",  placed=41200, morts=1440, caught=39760, balance=0, mtec=1906),
            EobShedRow(shed="7",  placed=49500, morts=396,  caught=49104, balance=0, mtec=2282),
            EobShedRow(shed="8",  placed=47083, morts=2027, caught=45056, balance=0, mtec=2323),
            EobShedRow(shed="9",  placed=46000, morts=2832, caught=43168, balance=0, mtec=2416),
            EobShedRow(shed="10", placed=46000, morts=1704, caught=44296, balance=0, mtec=2728),
            EobShedRow(shed="11", placed=46406, morts=1502, caught=44904, balance=0, mtec=2390),
            EobShedRow(shed="12", placed=48500, morts=1708, caught=46792, balance=0, mtec=1940),
        ],
        feedTypes=[
            EobFeedType(name="STARTER",   color="#8B5A2B", total=175380, rows=[
                EobDeliveryRow(date="13/05/26", docket="57171", kg=43800),
                EobDeliveryRow(date="13/05/26", docket="57169", kg=43880),
                EobDeliveryRow(date="13/05/26", docket="57168", kg=43720),
                EobDeliveryRow(date="13/05/26", docket="57170", kg=43980),
            ]),
            EobFeedType(name="GROWER",    color="#B8860B", total=524340, rows=[
                EobDeliveryRow(date="23/05/26", docket="57447", kg=44020),
                EobDeliveryRow(date="24/05/26", docket="57448", kg=43540),
                EobDeliveryRow(date="29/05/26", docket="57571", kg=43720),
                EobDeliveryRow(date="29/05/26", docket="57572", kg=43460),
                EobDeliveryRow(date="02/06/26", docket="58626", kg=43740),
                EobDeliveryRow(date="02/06/26", docket="58627", kg=43580),
                EobDeliveryRow(date="04/06/26", docket="58675", kg=43900),
                EobDeliveryRow(date="04/06/26", docket="58673", kg=43580),
                EobDeliveryRow(date="04/06/26", docket="58674", kg=43040),
                EobDeliveryRow(date="07/06/26", docket="58676", kg=43840),
            ]),
            EobFeedType(name="FINISHER",  color="#556B2F", total=868080, rows=[
                EobDeliveryRow(date="10/06/26", docket="58712", kg=43500),
                EobDeliveryRow(date="11/06/26", docket="58789", kg=43880),
                EobDeliveryRow(date="13/06/26", docket="58842", kg=43620),
                EobDeliveryRow(date="16/06/26", docket="58941", kg=43900),
                EobDeliveryRow(date="18/06/26", docket="59012", kg=43760),
                EobDeliveryRow(date="20/06/26", docket="59088", kg=43420),
            ]),
            EobFeedType(name="WITHDRAWAL", color="#4682B4", total=738460, rows=[
                EobDeliveryRow(date="22/06/26", docket="59245", kg=43800),
                EobDeliveryRow(date="23/06/26", docket="59289", kg=43520),
                EobDeliveryRow(date="25/06/26", docket="59356", kg=43880),
            ]),
        ],
    )
    return _render_eob_html(r, "Double B", "grower@doubleb.au")


@app.post("/api/eob/send-report")
async def send_eob_report(req: EobEmailRequest, request: Request):
    import logging as _logging
    _log = _logging.getLogger("eob_email")
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    if not req.to:
        raise HTTPException(400, "No recipients provided")
    if len(req.to) > 20:
        raise HTTPException(400, "Too many recipients (max 20)")

    from email_service import send_email

    farm_label = req.farmName or "your farm"
    pdf_attachment: Optional[dict] = None
    if req.report:
        html = _render_eob_html(req.report, req.farmName or "Broiler Base Mate", user.get("email") or "")
        # to render the SAME HTML → PDF, so the email and PDF are visually
        # identical. Silent no-op if Chrome isn't available (email still sends).
        try:
            pdf_bytes = await _render_html_to_pdf(html)
            if pdf_bytes:
                import base64 as _b64
                batch_slug = "".join(c if c.isalnum() else "-" for c in (req.report.batchName or f"batch-{req.report.batchNumber or 'report'}"))
                pdf_attachment = {
                    "filename": f"EOB-{batch_slug}.pdf",
                    "content": _b64.b64encode(pdf_bytes).decode("ascii"),
                }
        except Exception as e:
            _log.warning("EOB PDF generation failed (email will still send without attachment): %s", e)
    else:
        # Fallback — when the client couldn't build a structured report payload
        # (rare, but happens if a cell read throws mid-build). Render the plain
        # text body INSIDE the branded Appcovi navy/gold shell so the grower
        # still receives a professional-looking email, not a plaintext dump.
        _log.warning("EOB fallback path: no structured report sent by client (recipients=%s, farm=%s)", req.to, req.farmName)
        safe_body = (
            req.body
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        _now = datetime.now(timezone.utc).strftime("%d %b %Y")
        html = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\" />"
            "<title>End of Batch Report</title></head>"
            "<body style=\"margin:0;padding:24px 12px;background:#eef2f9;"
            "font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Roboto,sans-serif;"
            "color:#1e2f4d;-webkit-font-smoothing:antialiased;\">"
            "<div style=\"max-width:680px;margin:0 auto;background:#f7f9fc;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px -12px rgba(15,26,47,0.28);\">"
            # Branded hero (matches _render_eob_html)
            "<div style=\"background:linear-gradient(135deg,#1e2f4d 0%,#0f1a2f 100%);padding:32px 28px;color:#fff;border-bottom:3px solid #C9A227;\">"
            "<div style=\"font-size:11px;letter-spacing:3px;color:#C9A227;font-weight:800;margin-bottom:2px;\">END OF BATCH REPORT</div>"
            f"<div style=\"font-size:24px;font-weight:900;letter-spacing:-0.4px;line-height:1.15;\">{farm_label}</div>"
            f"<div style=\"font-size:13px;color:#bdd3ee;margin-top:4px;\">Generated {_now}</div>"
            "</div>"
            # Body
            "<div style=\"padding:24px;\">"
            "<div style=\"font-size:11px;letter-spacing:1.5px;color:#1e2f4d;font-weight:800;text-transform:uppercase;border-bottom:2px solid #C9A227;padding-bottom:6px;margin-bottom:14px;\">Batch Summary</div>"
            "<pre style=\"background:#fff;border:1px solid #dce3ee;border-radius:10px;padding:16px;"
            "font-family:'SF Mono','Menlo',Consolas,monospace;font-size:12px;line-height:1.55;white-space:pre-wrap;color:#1e2f4d;margin:0;\">"
            f"{safe_body}"
            "</pre>"
            f"<p style=\"color:#5a6a86;font-size:12px;margin:18px 0 0;\">Sent by <strong style=\"color:#1e2f4d;\">{user.get('email')}</strong> via Broiler Base Mate.</p>"
            "</div>"
            # Footer
            "<div style=\"background:#f0f3f9;padding:16px 24px;border-top:1px solid #dce3ee;text-align:center;\">"
            "<div style=\"font-size:10px;letter-spacing:2px;color:#5a6a86;font-weight:800;text-transform:uppercase;\">Powered by Appcovi</div>"
            "<div style=\"font-size:11px;color:#5a6a86;margin-top:4px;\">broilerbasemate.com.au</div>"
            "</div>"
            "</div></body></html>"
        )

    sent_to: list[str] = []
    failed_to: list[str] = []
    for addr in req.to:
        addr = addr.strip()
        if not addr or "@" not in addr:
            failed_to.append(addr)
            continue
        try:
            result = await send_email(
                to=addr,
                subject=req.subject,
                html=html,
                reply_to=user.get("email"),
                attachments=[pdf_attachment] if pdf_attachment else None,
            )
            # `send_email` swallows Resend errors internally and returns
            # {"ok": False, ...} instead of raising — checking only for a
            # raised exception here meant every send was counted as
            # delivered even when Resend rejected it (e.g. sandbox sender
            # `onboarding@resend.dev` refusing non-owner recipients).
            if result.get("ok") and not result.get("skipped"):
                sent_to.append(addr)
            else:
                _log.warning("EOB email send failed for %s: %s", addr, result.get("error") or result.get("reason"))
                failed_to.append(addr)
        except Exception as e:
            _log.warning("EOB email send failed for %s: %s", addr, e)
            failed_to.append(addr)

    if not sent_to:
        raise HTTPException(500, "Could not send to any of the recipients. Check the email service is configured.")

    return {"ok": True, "sent": sent_to, "failed": failed_to}


# ─── End-of-Batch LOCK / CLOSE — snapshots + auto-email to owner ─────────
# Grower taps "🏁 End Batch" on the EOB tab → we freeze all the numbers
# (placement + morts + catches + weights + feed) into an immutable snapshot,
# email the branded PDF to the batch owner, and mark the batch closed so
# processor amendments arriving days later can't retroactively change the
# on-record numbers. Users see a "Batch Closed · Locked on <date>" banner
# and the button disappears until a new batch is placed.

class EobLockRequest(BaseModel):
    batchIdentifier: str  # sheet name or batch number — unique per farm
    farmSlug:        Optional[str] = None
    farmName:        Optional[str] = None
    report:          EobReport
    # Free-form fingerprint blob so we can round-trip everything the SPA
    # showed at lock-time (edits map, catch map, morts log, etc.) if the
    # grower ever needs to audit. Kept optional so old clients still work.
    fingerprint:     Optional[Dict[str, Any]] = None


@app.post("/api/eob/lock-batch")
async def lock_eob_batch(req: EobLockRequest, request: Request):
    import logging as _logging
    _log = _logging.getLogger("eob_lock")
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    farm_id = (req.farmSlug or DEFAULT_FARM_ID).strip() or DEFAULT_FARM_ID
    batch_id = (req.batchIdentifier or "").strip()
    if not batch_id:
        raise HTTPException(400, "batchIdentifier is required")

    # ── Already locked? Return the existing snapshot instead of re-emailing.
    existing = await eob_snapshots_col.find_one(
        {"farmId": farm_id, "batchIdentifier": batch_id},
        {"_id": 0},
    )
    if existing:
        return {"ok": True, "alreadyLocked": True, "snapshot": existing}

    now_iso = datetime.now(timezone.utc).isoformat()
    owner_email = (user.get("email") or "").strip()

    # ── Render branded HTML + PDF (same renderer as send-report) ────────
    from email_service import send_email
    farm_label = req.farmName or "your farm"
    html = _render_eob_html(req.report, farm_label, owner_email)
    pdf_b64: Optional[str] = None
    pdf_attachment: Optional[dict] = None
    try:
        pdf_bytes = await _render_html_to_pdf(html)
        if pdf_bytes:
            import base64 as _b64
            pdf_b64 = _b64.b64encode(pdf_bytes).decode("ascii")
            batch_slug = "".join(c if c.isalnum() else "-" for c in batch_id)
            pdf_attachment = {"filename": f"EOB-{batch_slug}.pdf", "content": pdf_b64}
    except Exception as e:
        _log.warning("EOB lock PDF generation failed (email will still send without attachment): %s", e)

    # ── Email the branded PDF to the batch owner ────────────────────────
    email_result: Dict[str, Any] = {"sent": False, "error": None}
    if owner_email and "@" in owner_email:
        try:
            await send_email(
                to=owner_email,
                subject=f"🏁 Batch Closed — {batch_id}",
                html=html,
                reply_to=owner_email,
                attachments=[pdf_attachment] if pdf_attachment else None,
            )
            email_result["sent"] = True
        except Exception as e:
            _log.warning("EOB lock email send failed for %s: %s", owner_email, e)
            email_result["error"] = str(e)
    else:
        email_result["error"] = "No owner email on session"

    # ── Persist the snapshot (immutable) ────────────────────────────────
    snapshot = {
        "id":              str(uuid.uuid4()),
        "farmId":          farm_id,
        "batchIdentifier": batch_id,
        "farmName":        req.farmName,
        "lockedAt":        now_iso,
        "lockedBy":        owner_email,
        "report":          req.report.dict(),
        "fingerprint":     req.fingerprint,
        "html":            html,     # keep the rendered HTML so admins can re-view later
        "pdfBase64":       pdf_b64,  # rendered PDF (may be None if Chrome unavailable)
        "email":           email_result,
    }
    try:
        await eob_snapshots_col.insert_one(dict(snapshot))
    except Exception as e:
        _log.error("EOB snapshot persist failed for farm=%s batch=%s: %s", farm_id, batch_id, e)
        raise HTTPException(500, f"Could not persist snapshot: {e}")

    # Never return the html/pdf/base64 blobs in the initial response
    # — the client only needs the confirmation payload.
    return {
        "ok":              True,
        "alreadyLocked":   False,
        "snapshot": {
            "id":              snapshot["id"],
            "farmId":          farm_id,
            "batchIdentifier": batch_id,
            "lockedAt":        now_iso,
            "lockedBy":        owner_email,
            "email":           email_result,
        },
    }


@app.get("/api/eob/locked-batches")
async def list_locked_batches(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    """Return metadata for closed batches on this farm. Excludes html/pdf/report
    blobs so the response stays small — client only needs to know which batches
    are locked (to hide the End Batch button + show the badge), plus a small
    safe `kpis` subset for the "Locked Batches Archive" list view (Mar 2026).
    SEC-001: also excludes `lockedBy` (owner email) to prevent PII disclosure
    from the loginless reader endpoint."""
    cursor = eob_snapshots_col.find(
        {"farmId": farm},
        {"_id": 0, "html": 0, "pdfBase64": 0, "lockedBy": 0, "email": 0},
    ).sort("lockedAt", -1).limit(200)
    out = []
    async for doc in cursor:
        report = doc.pop("report", None) or {}
        doc.pop("fingerprint", None)
        doc["kpis"] = {
            "aveWeight": report.get("aveWeight"),
            "fcr": report.get("fcr"),
            "cfcr": report.get("cfcr"),
            "totalCaught": report.get("totalCaught"),
        }
        out.append(doc)
    return out


@app.get("/api/eob/locked-batches/{snap_id}")
async def get_locked_batch(snap_id: str, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Return the full snapshot (report payload + optional PDF base64) for viewing.
    SEC-006: requires session OR farmToken — the snapshot contains the owner's
    email + rendered PDF and must not be readable via an anonymous slug lookup."""
    await _require_farm_access(request, farm)
    doc = await eob_snapshots_col.find_one(
        {"id": snap_id, "farmId": farm},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, "Snapshot not found")
    return doc


@app.get("/api/eob/locked-batches/{snap_id}/pdf")
async def get_locked_batch_pdf(snap_id: str, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Serve the archived PDF (or HTML fallback if PDF render failed at lock
    time) for the "Locked Batches Archive" view/download button. Mar 2026."""
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    doc = await eob_snapshots_col.find_one(
        {"id": snap_id, "farmId": farm},
        {"_id": 0, "pdfBase64": 1, "html": 1, "batchIdentifier": 1},
    )
    if not doc:
        raise HTTPException(404, "Snapshot not found")
    slug = "".join(c if c.isalnum() else "-" for c in (doc.get("batchIdentifier") or "batch"))
    if doc.get("pdfBase64"):
        import base64 as _b64
        pdf_bytes = _b64.b64decode(doc["pdfBase64"])
        return Response(content=pdf_bytes, media_type="application/pdf",
                         headers={"Content-Disposition": f'inline; filename="EOB-{slug}.pdf"'})
    if doc.get("html"):
        return Response(content=doc["html"], media_type="text/html")
    raise HTTPException(404, "No PDF or HTML on record for this batch")


class ResendEobRequest(BaseModel):
    to: Optional[str] = None  # defaults to the batch owner's email on record


@app.post("/api/eob/locked-batches/{snap_id}/resend")
async def resend_locked_batch_email(snap_id: str, req: ResendEobRequest, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Re-send the ALREADY-rendered EOB report (no regeneration) for a locked
    batch — "Locked Batches Archive" resend-email button. Mar 2026."""
    await _require_farm_access(request, farm)   # SEC-006
    doc = await eob_snapshots_col.find_one({"id": snap_id, "farmId": farm}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Snapshot not found")
    to_addr = (req.to or doc.get("lockedBy") or "").strip()
    if not to_addr or "@" not in to_addr:
        raise HTTPException(400, "No valid recipient email on record — pass one explicitly")
    from email_service import send_email
    pdf_attachment = None
    if doc.get("pdfBase64"):
        slug = "".join(c if c.isalnum() else "-" for c in (doc.get("batchIdentifier") or "batch"))
        pdf_attachment = {"filename": f"EOB-{slug}.pdf", "content": doc["pdfBase64"]}
    result = await send_email(
        to=to_addr,
        subject=f"🏁 Batch Closed — {doc.get('batchIdentifier')} (resend)",
        html=doc.get("html") or "<p>No report content on record for this batch.</p>",
        attachments=[pdf_attachment] if pdf_attachment else None,
    )
    if not result.get("ok"):
        raise HTTPException(500, f"Could not resend email: {result.get('error') or result.get('reason')}")
    return {"ok": True, "sentTo": to_addr}


# ─── External Morts Integration — link a companion app (e.g. "Mort Buddy")
# for daily mortality/culls entry ───────────────────────────────────────────
# Jason is building a separate app for logging daily morts/culls per shed and
# wants it to feed straight into BBM's Morts tab / EOB / Batch Results instead
# of double-entering the same numbers in both apps. Auth reuses the exact same
# SEC-006 farm-token mechanism as every other endpoint (`?t=<farmToken>` or
# `x-farm-token` header) — no separate API key system needed, and the other
# app never gets a login session, just the farm's own secret token.
class ExternalMortEntry(BaseModel):
    shed: int
    date: str          # YYYY-MM-DD (grower's local date the morts/culls occurred)
    morts: int = 0
    culls: int = 0
    staffName: Optional[str] = None   # who recorded it, from the Staff QR page — "so I know who did what" (Jason)


class ExternalMortsPushRequest(BaseModel):
    entries: List[ExternalMortEntry]
    source: Optional[str] = "external"   # which companion app sent this (e.g. "mort-buddy")


@app.post("/api/integrations/morts")
async def push_external_morts(req: ExternalMortsPushRequest, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Upsert daily per-shed morts/culls from a companion app into BBM.
    Idempotent — resending the same (farm, date, shed) overwrites that entry
    rather than duplicating it, so the sender can safely retry/resync."""
    await _require_farm_access(request, farm)
    if not req.entries or len(req.entries) > 500:
        raise HTTPException(400, "Provide 1-500 entries")
    now = datetime.now(timezone.utc)
    upserted = 0
    for e in req.entries:
        await external_morts_col.update_one(
            {"farmId": farm, "date": e.date, "shed": e.shed},
            {"$set": {
                "farmId": farm, "date": e.date, "shed": e.shed,
                "morts": max(0, e.morts), "culls": max(0, e.culls),
                "source": req.source, "staffName": (e.staffName or "").strip()[:60] or None,
                "updatedAt": now,
            }},
            upsert=True,
        )
        upserted += 1
    return {"ok": True, "upserted": upserted}


@app.get("/api/integrations/morts")
async def get_external_morts(request: Request, farm: str = Query(default=DEFAULT_FARM_ID), since: Optional[str] = None):
    """Return every external morts/culls entry on record for this farm (used
    by BBM's own frontend to merge Mort Buddy's numbers into the Morts tab,
    and by the companion app itself to confirm what's already synced)."""
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    q: dict = {"farmId": farm}
    if since:
        q["date"] = {"$gte": since}
    cursor = external_morts_col.find(q, {"_id": 0}).sort("date", 1)
    return {"entries": await cursor.to_list(2000)}


@app.delete("/api/integrations/morts")
async def delete_external_morts_by_date(request: Request, farm: str = Query(default=DEFAULT_FARM_ID), date: str = Query(...)):
    """Clear wrong-day Morts & Culls entries — Jason: "the data stuck in Friday
    ... can you clear Fridays entrys." Added as a permanent admin control (not
    a one-off) so any wrong-day entry (timezone bug or otherwise) can be
    cleared by the farm owner themselves, no agent DB access needed. Deletes
    every external_morts row for this farm on the given date."""
    await _require_farm_access(request, farm)   # SEC-001 — DESTRUCTIVE, must be gated
    r = await external_morts_col.delete_many({"farmId": farm, "date": date})
    return {"ok": True, "deleted": r.deleted_count}


# ─── External Weigh-Ins Integration — "Weigh Birds" tab on the same Staff
# QR page (Mort Buddy) ────────────────────────────────────────────────────
# Jason: "we have mort buddy... could we have a tab weigh birds pick a shed
# and add age and manual weight". Staff pick a shed, type the age/day and a
# manual weight (grams or kg), no login. Keyed on (farm, shed, age) so
# re-weighing the same shed/age overwrites rather than duplicating — the
# desktop app then merges this straight into the existing Flock Forecast
# weigh-in store (`feedmate-flock-weighins`) so charts/Buddy tips auto-update.
class ExternalWeighInEntry(BaseModel):
    shed: int
    age: int             # day of batch, staff-entered manually, no default
    date: str            # YYYY-MM-DD the weighing happened (for the recording-status display)
    weightGrams: float
    staffName: Optional[str] = None


class ExternalWeighInsPushRequest(BaseModel):
    entries: List[ExternalWeighInEntry]
    source: Optional[str] = "external"


@app.post("/api/integrations/weighins")
async def push_external_weighins(req: ExternalWeighInsPushRequest, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Upsert manual bird weighings from the Staff QR page. Idempotent on
    (farm, shed, age) — re-weighing the same shed/age overwrites the value."""
    await _require_farm_access(request, farm)
    if not req.entries or len(req.entries) > 200:
        raise HTTPException(400, "Provide 1-200 entries")
    now = datetime.now(timezone.utc)
    upserted = 0
    for e in req.entries:
        if e.weightGrams <= 0:
            continue
        await external_weighins_col.update_one(
            {"farmId": farm, "shed": e.shed, "age": e.age},
            {"$set": {
                "farmId": farm, "shed": e.shed, "age": e.age, "date": e.date,
                "weightGrams": e.weightGrams,
                "source": req.source, "staffName": (e.staffName or "").strip()[:60] or None,
                "updatedAt": now,
            }},
            upsert=True,
        )
        upserted += 1
    return {"ok": True, "upserted": upserted}


@app.get("/api/integrations/weighins")
async def get_external_weighins(request: Request, farm: str = Query(default=DEFAULT_FARM_ID), since: Optional[str] = None):
    """Return every external weigh-in on record for this farm — used by the
    desktop app to merge into the Flock Forecast weigh-in store, and by the
    Staff QR page itself to prefill today's entries."""
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    q: dict = {"farmId": farm}
    if since:
        q["date"] = {"$gte": since}
    cursor = external_weighins_col.find(q, {"_id": 0}).sort("age", 1)
    return {"entries": await cursor.to_list(2000)}


@app.get("/api/eob/batch-accuracy")
async def get_batch_accuracy(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Predicted-vs-actual EOB accuracy for every locked batch on this farm.
    Feb 2026 — Jason: "Batch Accuracy Tracker ... so growers can see how
    accurate the AI forecast really was". `predicted` comes from the Flock
    Forecast "Predicted End-of-Batch" snapshot saved client-side at lock time
    (fingerprint.predictedEob); `actual` is the locked EOB report's KPIs.
    Only exposes KPI numbers — no html/pdf/PII, safe for the loginless reader."""
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    cursor = eob_snapshots_col.find(
        {"farmId": farm},
        {"_id": 0, "batchIdentifier": 1, "lockedAt": 1, "report": 1, "fingerprint.predictedEob": 1},
    ).sort("lockedAt", -1).limit(200)
    out = []
    async for doc in cursor:
        report = doc.get("report") or {}
        predicted = (doc.get("fingerprint") or {}).get("predictedEob")
        out.append({
            "batchIdentifier": doc.get("batchIdentifier"),
            "lockedAt": doc.get("lockedAt"),
            "predicted": predicted,
            "actual": {
                "aveWeight":         report.get("aveWeight"),
                "fcr":               report.get("fcr"),
                "cfcr":              report.get("cfcr"),
                "cageRating":        report.get("cageRating"),
                "correctedAge":      report.get("correctedAge"),
                "actualAge":         report.get("actualAge"),
                "totalCaught":       report.get("totalCaught"),
                "totalLiveWeightKg": report.get("totalLiveWeightKg"),
            },
        })
    return out


# ─── Share-history email — Reader → head office ──────────────────────────
# Lets a grower email the recent silo + delivery history straight from the
# Reader app. Renders the same premium look as the EOB report so anything
# sent from this farm carries consistent branding.

class HistoryShareRequest(BaseModel):
    to:           List[str]
    farm:         Optional[str] = "default"
    days:         Optional[int] = 7        # how many days of history to include
    farmName:     Optional[str] = None
    farmLogoData: Optional[str] = None     # optional base64 PNG override


def _render_history_html(farm_name: str, days: int, sender: str,
                          silos_grouped: list, deliveries: list,
                          totals: dict, farm_logo_data: Optional[str],
                          silo_period_label: str = "latest per silo") -> str:
    """Stock Take email — just that day's silo stock (kg/t per silo), same
    navy/gold branded style as the End of Batch report (Jason: "end of batch
    style is great i want same style when i send monthly stock take from app
    just thats days stock"). Deliveries/period KPIs intentionally dropped —
    stock take is silo levels only."""
    logo_src = (
        f"data:image/png;base64,{farm_logo_data}"
        if farm_logo_data else
        "https://broilerbasemate.com.au/reader-assets/company-logo.png"
    )
    gen = (datetime.now(timezone.utc) + AEST_OFFSET).strftime("%d %b %Y")
    total_t = totals.get("totalT", 0)

    hero = f"""
      <div style="background:linear-gradient(135deg,#1e2f4d 0%,#0f1a2f 100%);padding:32px 28px;color:#fff;border-radius:16px 16px 0 0;border-bottom:3px solid #C9A227;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td valign="middle" style="padding-right:14px;width:72px;">
            <img src="{logo_src}" alt="" width="64" height="64" style="display:block;border-radius:12px;background:#fff;padding:4px;" />
          </td>
          <td valign="middle">
            <div style="font-size:11px;letter-spacing:3px;color:#C9A227;font-weight:800;margin-bottom:2px;">STOCK TAKE</div>
            <div style="font-size:24px;font-weight:900;letter-spacing:-0.4px;line-height:1.15;">{farm_name}</div>
            <div style="font-size:13px;color:#bdd3ee;margin-top:4px;">Today's silo stock · {gen}</div>
          </td>
        </tr></table>
      </div>
    """

    silo_html = ""
    for grp in silos_grouped:
        silo_rows = "".join(
            f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e9ee;font-weight:700;color:#1e2f4d;">Silo {s['letter']}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e9ee;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{s['amount']:,.2f} t</td>
            </tr>"""
            for s in grp['silos']
        ) or """<tr><td colspan="2" style="padding:14px;text-align:center;color:#9ca3af;font-size:12px;font-style:italic;">No readings yet</td></tr>"""
        silo_html += f"""
          <div style="margin-top:14px;background:#fff;border:1px solid #dce3ee;border-radius:10px;overflow:hidden;">
            <div style="background:#1e2f4d;color:#fff;padding:9px 14px;font-weight:800;letter-spacing:0.5px;font-size:13px;display:flex;justify-content:space-between;align-items:center;">
              <span style="text-transform:uppercase;">{grp['name']}</span>
              <span style="font-size:15px;background:rgba(201,162,39,0.28);padding:2px 10px;border-radius:99px;">{grp.get('groupTotalT', 0):,.1f} t</span>
            </div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <thead><tr style="background:#eef2f9;">
                <th style="padding:8px 12px;text-align:left;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Silo</th>
                <th style="padding:8px 12px;text-align:right;font-size:10px;color:#5a6a86;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Stock</th>
              </tr></thead>
              <tbody>{silo_rows}</tbody>
            </table>
          </div>
        """
    if not silo_html:
        silo_html = """<div style="margin-top:14px;padding:18px;text-align:center;color:#9ca3af;font-size:13px;background:#eef2f9;border:1px dashed #e3dccb;border-radius:10px;">No silo readings taken today yet.</div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8" />
<title>Stock Take — {farm_name}</title></head>
<body style="margin:0;padding:24px 12px;background:#eef2f9;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Roboto,sans-serif;color:#1e2f4d;-webkit-font-smoothing:antialiased;">
  <div style="max-width:680px;margin:0 auto;background:#f7f9fc;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px -12px rgba(15,26,47,0.28);">
    {hero}
    <div style="padding:20px 24px 28px;">
      <h3 style="margin:0 0 4px;color:#1e2f4d;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🌾 Silo Stock ({silo_period_label})</h3>
      {silo_html}
      <div style="margin-top:22px;padding:14px 16px;background:#fffbe6;border:1px solid #f1e2a8;border-radius:10px;text-align:center;">
        <span style="font-size:11px;letter-spacing:1px;color:#8a6d00;text-transform:uppercase;font-weight:700;">Total Feed on Farm</span>
        <div style="font-size:22px;font-weight:900;color:#1e2f4d;margin-top:4px;">{total_t:,.1f} t</div>
      </div>
      <div style="margin-top:32px;padding:22px 20px;background:linear-gradient(135deg,#1e2f4d 0%,#0f1a2f 100%);border-radius:12px;border:1px solid #2b4266;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td valign="middle" style="width:64px;padding-right:16px;">
            <img src="https://broilerbasemate.com.au/reader-assets/company-logo.png" alt="Appcovi" width="56" height="56" style="display:block;border-radius:10px;background:#fff;padding:4px;" />
          </td>
          <td valign="middle" style="color:#dbe4ea;">
            <div style="font-size:10px;letter-spacing:2.5px;color:#8a99b8;font-weight:700;margin-bottom:2px;">BUILT BY</div>
            <div style="font-size:20px;font-weight:900;color:#fff;letter-spacing:0.5px;line-height:1;">APPCOVI</div>
            <div style="font-size:11px;color:#8a99b8;font-weight:600;letter-spacing:1.4px;margin-top:3px;">POULTRY MANAGEMENT SOFTWARE</div>
            <div style="font-size:12px;color:#bdd3ee;margin-top:8px;">
              <a href="https://broilerbasemate.com.au" style="color:#C9A227;text-decoration:none;font-weight:700;">broilerbasemate.com.au</a>
              &nbsp;·&nbsp; Sent by {sender}
            </div>
          </td>
        </tr></table>
      </div>
    </div>
  </div>
</body></html>"""


@app.post("/api/history/share-email")
async def share_history_email(req: HistoryShareRequest, request: Request):
    """Email a beautifully-formatted history snapshot to head office /
    integrator. Includes latest silo readings per shed + recent deliveries."""
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    if not req.to or len(req.to) > 20:
        raise HTTPException(400, "Provide 1-20 recipients")

    from email_service import send_email

    farm = req.farm or "default"
    days = max(1, min(req.days or 7, 90))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    # Silo readings are filtered to TODAY (AEST calendar day) only — per Jason
    # (Feb 2026): "only need the silo reading just the day I click share to head
    # office" — not the whole week of daily readings. Deliveries still use the
    # `days` window since head office wants delivery history context.
    aest_now = now + AEST_OFFSET
    aest_today_start = aest_now.replace(hour=0, minute=0, second=0, microsecond=0)
    silos_since_utc = (aest_today_start - AEST_OFFSET).replace(tzinfo=timezone.utc)

    # ── Latest silo reading per silo (TODAY only), grouped by shed group
    groups = await shed_groups_col.find(_farm_filter(farm)).sort("displayOrder", 1).to_list(200)
    silos  = await silos_col.find(_farm_filter(farm)).sort("letter", 1).to_list(1000)
    latest: dict[str, dict] = {}
    pipeline = [
        {"$match": _and(_farm_filter(farm), {"readingDate": {"$gte": silos_since_utc}})},
        {"$sort": {"readingDate": -1}},
        {"$group": {"_id": "$siloId", "amountRemaining": {"$first": "$amountRemaining"},
                    "unit": {"$first": "$unit"}, "readingDate": {"$first": "$readingDate"}}},
    ]
    async for doc in readings_col.aggregate(pipeline):
        amt = float(doc.get("amountRemaining") or 0)
        if (doc.get("unit") or "").lower() == "kg":
            amt = amt / 1000.0
        latest[doc["_id"]] = {"amount": amt, "readingDate": doc["readingDate"]}

    silos_grouped = []
    total_t = 0.0
    silos_read = 0
    sheds_reporting = 0
    for g in groups:
        group_silos = [s for s in silos if s.get("shedGroupId") == g["id"]]
        if not group_silos:
            continue
        grp_silos_out = []
        grp_total = 0.0
        for s in group_silos:
            rec = latest.get(s["id"])
            if not rec:
                continue
            ts = rec["readingDate"]
            ts_str = (ts + AEST_OFFSET).strftime("%d %b · %H:%M") if ts else "—"
            grp_silos_out.append({"letter": s.get("letter", "?"), "amount": rec["amount"], "readAt": ts_str + " AEST"})
            grp_total += rec["amount"]
            silos_read += 1
        if grp_silos_out:
            sheds_reporting += 1
            total_t += grp_total
            silos_grouped.append({"name": g.get("name", ""), "silos": grp_silos_out, "groupTotalT": grp_total})

    # ── Deliveries within window
    deliveries_docs = await deliveries_col.find(_and(
        _farm_filter(farm), {"deliveryDate": {"$gte": since}}
    )).sort("deliveryDate", -1).to_list(200)
    deliveries = []
    for d in deliveries_docs:
        amt = float(d.get("amount") or 0)
        if (d.get("unit") or "").lower() == "kg":
            amt = amt / 1000.0
        dt = d.get("deliveryDate")
        date_str = (dt + AEST_OFFSET).strftime("%d %b %Y") if dt else "—"
        deliveries.append({
            "date":     date_str,
            "supplier": d.get("supplier") or d.get("companyName") or "—",
            "feedType": d.get("feedType") or "—",
            "amountT":  amt,
        })

    farm_name = req.farmName or "Farm History"
    totals = {"totalT": total_t, "shedsReporting": sheds_reporting, "silosRead": silos_read}
    html = _render_history_html(farm_name, days, user.get("email") or "", silos_grouped, deliveries, totals, req.farmLogoData, silo_period_label="taken today")

    sent, failed = [], []
    subject = f"🌾 Stock Take — {farm_name} ({datetime.now(timezone.utc).strftime('%d %b %Y')})"
    for addr in req.to:
        addr = addr.strip()
        if not addr or "@" not in addr:
            failed.append(addr); continue
        try:
            await send_email(to=addr, subject=subject, html=html, reply_to=user.get("email"))
            sent.append(addr)
        except Exception as e:
            failed.append(addr)
            import logging; logging.getLogger("history_email").warning(f"send failed for {addr}: {e}")

    if not sent:
        raise HTTPException(500, "Could not send to any recipient. Check email configuration.")
    return {"ok": True, "sent": sent, "failed": failed}


# ─── Monthly auto-send config (last Friday before 2pm AEST) ────────────────
class MonthlyReportConfig(BaseModel):
    enabled: bool = False
    emails:  List[str] = []
    days:    int = 30   # how many days of history to include in the snapshot

@app.get("/api/history/auto-send")
async def get_auto_send(farm: str = "default"):
    fc = await farm_config_col.find_one({"id": farm}) or {}
    cfg = fc.get("monthlyReport") or {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "emails":  cfg.get("emails") or [],
        "days":    int(cfg.get("days") or 30),
        "lastSentMonth": fc.get("monthlyReportLastSentMonth"),
    }

@app.put("/api/history/auto-send")
async def put_auto_send(cfg: MonthlyReportConfig, farm: str = "default"):
    # Clean / validate the email list
    clean_emails = [e.strip() for e in cfg.emails if e and "@" in e][:20]
    await farm_config_col.update_one(
        {"id": farm},
        {"$set": {"monthlyReport": {
            "enabled": cfg.enabled,
            "emails":  clean_emails,
            "days":    max(1, min(cfg.days, 365)),
        }}},
        upsert=True,
    )
    return {"ok": True, "emails": clean_emails}


async def _maybe_send_monthly_reports():
    """Background task — runs once an hour. If today is the LAST Friday of
    the month (AEST) and the local time is between 09:00 and 14:00 AEST, and
    the farm has auto-send enabled and hasn't already been sent this month,
    fire the same branded history email used by the manual button.
    """
    import logging as _log
    log = _log.getLogger("monthly_report")
    while True:
        try:
            now = datetime.now(timezone.utc)
            aest = now + AEST_OFFSET
            # Last Friday = Friday whose date + 7 days lands in next month
            is_friday = aest.weekday() == 4  # Mon=0 … Fri=4
            is_last   = (aest + timedelta(days=7)).month != aest.month
            in_window = 9 <= aest.hour < 14
            if is_friday and is_last and in_window:
                month_key = aest.strftime("%Y-%m")
                async for fc in farm_config_col.find({"monthlyReport.enabled": True}):
                    farm_id = fc.get("id") or "default"
                    if fc.get("monthlyReportLastSentMonth") == month_key:
                        continue  # already sent this month — skip
                    emails = (fc.get("monthlyReport") or {}).get("emails") or []
                    days   = int((fc.get("monthlyReport") or {}).get("days") or 30)
                    if not emails:
                        continue
                    try:
                        # Reuse the data-pull logic from share_history_email
                        await _send_monthly_snapshot(farm_id, emails, days, fc)
                        await farm_config_col.update_one(
                            {"id": farm_id},
                            {"$set": {"monthlyReportLastSentMonth": month_key}},
                        )
                        log.info(f"Monthly snapshot sent for farm={farm_id} to {len(emails)} recipients")
                    except Exception as e:
                        log.warning(f"Monthly snapshot failed for farm={farm_id}: {e}")
        except Exception as e:
            log.warning(f"Monthly scheduler loop error: {e}")
        # Re-check once an hour. The 2pm cutoff window is 5 hours wide so we
        # have plenty of margin even if the host restarts.
        await asyncio.sleep(3600)


async def _send_monthly_snapshot(farm: str, emails: list, days: int, fc: dict):
    """Internal helper — replicates the history-email data assembly + send."""
    from email_service import send_email
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    groups = await shed_groups_col.find(_farm_filter(farm)).sort("displayOrder", 1).to_list(200)
    silos  = await silos_col.find(_farm_filter(farm)).sort("letter", 1).to_list(1000)
    latest: dict[str, dict] = {}
    pipeline = [
        {"$match": _and(_farm_filter(farm), {"readingDate": {"$gte": since}})},
        {"$sort": {"readingDate": -1}},
        {"$group": {"_id": "$siloId", "amountRemaining": {"$first": "$amountRemaining"},
                    "unit": {"$first": "$unit"}, "readingDate": {"$first": "$readingDate"}}},
    ]
    async for doc in readings_col.aggregate(pipeline):
        amt = float(doc.get("amountRemaining") or 0)
        if (doc.get("unit") or "").lower() == "kg": amt /= 1000.0
        latest[doc["_id"]] = {"amount": amt, "readingDate": doc["readingDate"]}
    silos_grouped, total_t, silos_read, sheds_reporting = [], 0.0, 0, 0
    for g in groups:
        group_silos = [s for s in silos if s.get("shedGroupId") == g["id"]]
        if not group_silos: continue
        out, grp_total = [], 0.0
        for s in group_silos:
            rec = latest.get(s["id"])
            if not rec: continue
            ts_str = (rec["readingDate"] + AEST_OFFSET).strftime("%d %b · %H:%M") + " AEST" if rec["readingDate"] else "—"
            out.append({"letter": s.get("letter", "?"), "amount": rec["amount"], "readAt": ts_str})
            grp_total += rec["amount"]; silos_read += 1
        if out:
            sheds_reporting += 1; total_t += grp_total
            silos_grouped.append({"name": g.get("name", ""), "silos": out, "groupTotalT": grp_total})
    deliv = await deliveries_col.find(_and(_farm_filter(farm), {"deliveryDate": {"$gte": since}})).sort("deliveryDate", -1).to_list(200)
    deliveries = []
    for d in deliv:
        amt = float(d.get("amount") or 0)
        if (d.get("unit") or "").lower() == "kg": amt /= 1000.0
        date_str = ((d.get("deliveryDate") or now) + AEST_OFFSET).strftime("%d %b %Y")
        deliveries.append({"date": date_str, "supplier": d.get("supplier") or d.get("companyName") or "—",
                          "feedType": d.get("feedType") or "—", "amountT": amt})
    farm_name = fc.get("farmName") or "Farm History"
    logo = fc.get("logoData")
    if logo and logo.startswith("data:image"):
        m = re.match(r"^data:image/[a-z]+;base64,(.+)$", logo, re.I)
        logo = m.group(1) if m else None
    html = _render_history_html(farm_name, days, "Broiler Base Mate Auto-Send",
                                 silos_grouped, deliveries, {"totalT": total_t, "shedsReporting": sheds_reporting, "silosRead": silos_read}, logo)
    subj = f"🌾 Monthly Stock Take — {farm_name} ({now.strftime('%b %Y')})"
    for addr in emails:
        try: await send_email(to=addr, subject=subj, html=html)
        except Exception as e:
            import logging; logging.getLogger("monthly_report").warning(f"send fail {addr}: {e}")


async def _require_outreach_admin(request: Request) -> dict:
    """Outreach tracker access — gated by OUTREACH_ADMIN_EMAILS so the platform
    owner can manage their cold-email pipeline without inheriting SUPERUSER
    farm-visibility powers (which would break the customer-data privacy promise)."""
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    raw = os.environ.get("OUTREACH_ADMIN_EMAILS", "") or ""
    allowed = {e.strip().lower() for e in raw.split(",") if e.strip()}
    # SUPERUSER_EMAILS also gets through (admin is a superset)
    raw2 = os.environ.get("SUPERUSER_EMAILS", "") or ""
    allowed |= {e.strip().lower() for e in raw2.split(",") if e.strip()}
    if (user.get("email") or "").lower() not in allowed:
        raise HTTPException(403, "Outreach admin access required")
    return user


# ─── Outreach tracker (admin-only) ────────────────────────────────────────
from outreach import build_router as _build_outreach_router, track_slug_click as _track_outreach_click, run_due_followups as _run_outreach_drip  # noqa: E402
app.include_router(_build_outreach_router(db, _require_outreach_admin))


async def _require_farm_access(request: Request, farm_slug: str) -> dict:
    """Ensure caller can access this farm slug. Returns user dict; raises 401/403.

    Accepts EITHER:
      1. A logged-in session cookie whose email owns / is invited to the farm
      2. A `?t=<farmToken>` query param OR `x-farm-token` header matching the
         farm's stored `farmToken` (per-farm secret set at signup). This is the
         "scan-and-go" path — the QR/URL IS the auth, no login round-trip.

    SEC-006 (Feb 28, 2026, Jason): "bulletproof no fail for users — each user
    only sees their farm data".
    """
    # ─── Path 2: farmToken match ────────────────────────────────────────
    token = request.query_params.get("t") or request.headers.get("x-farm-token") or ""
    if token:
        farm_doc = await farms_col.find_one({"slug": farm_slug}, {"farmToken": 1, "ownerEmail": 1})
        if farm_doc and farm_doc.get("farmToken") and secrets.compare_digest(farm_doc["farmToken"], token):
            # Token match — grant read/write access as the farm owner (no user session created).
            return {"email": farm_doc.get("ownerEmail", "unknown"), "auth": "farm-token"}

    # ─── Path 1: session cookie ─────────────────────────────────────────
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    farms_info = await _list_user_farms(db, user["email"])
    if farms_info["role"] == "admin":
        return user
    allowed = {f["slug"] for f in farms_info["owned"]} | {f["slug"] for f in farms_info["invited"]}
    if farm_slug not in allowed:
        raise HTTPException(403, f"No access to farm '{farm_slug}'")
    return user


async def _require_owner_session(request: Request, farm_slug: str) -> dict:
    """Session-cookie-only variant of _require_farm_access — deliberately does
    NOT accept the farm's own ?t=<farmToken> as auth. Used by /api/farm-token
    so a staff member who already has the QR link/token can't use it to
    re-derive the same token (harmless today, but keeps the token's blast
    radius from growing). Flagged by testing_agent iteration_19."""
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    farms_info = await _list_user_farms(db, user["email"])
    if farms_info["role"] == "admin":
        return user
    allowed = {f["slug"] for f in farms_info["owned"]} | {f["slug"] for f in farms_info["invited"]}
    if farm_slug not in allowed:
        raise HTTPException(403, f"No access to farm '{farm_slug}'")
    return user


# ─── Helpers ───────────────────────────────────────────────────────────────
AEST_OFFSET = timedelta(hours=10)


def aest_today() -> str:
    return (datetime.now(timezone.utc) + AEST_OFFSET).date().isoformat()


def aest_today_range() -> tuple[datetime, datetime]:
    """Return UTC datetime range covering "today" in AEST."""
    today_aest = (datetime.now(timezone.utc) + AEST_OFFSET).date()
    start_aest = datetime.combine(today_aest, datetime.min.time(), tzinfo=timezone.utc)
    end_aest = datetime.combine(today_aest, datetime.max.time(), tzinfo=timezone.utc)
    # AEST = UTC + 10, so UTC start = AEST start - 10h
    return start_aest - AEST_OFFSET, end_aest - AEST_OFFSET


def clean(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    doc = {**doc}
    doc.pop("_id", None)
    return doc


# ─── Schemas ───────────────────────────────────────────────────────────────
class SiloInfo(BaseModel):
    id: str
    letter: str
    name: str
    defaultFeedType: Optional[str] = None


class ShedGroup(BaseModel):
    id: str
    name: str
    displayOrder: int
    silos: List[SiloInfo] = []


class CreateSiloBody(BaseModel):
    name: str
    defaultFeedType: Optional[str] = None
    shedGroupId: Optional[str] = None
    letter: Optional[str] = None


class UpdateSiloBody(BaseModel):
    name: Optional[str] = None
    defaultFeedType: Optional[str] = None


class SiloReadingInput(BaseModel):
    siloId: str
    feedType: str
    amountRemaining: float
    unit: str
    notes: Optional[str] = None


class BatchCreateReadingsBody(BaseModel):
    readings: List[SiloReadingInput]
    readingDate: Optional[str] = None


class CreateDeliveryBody(BaseModel):
    shedGroupId: Optional[str] = None
    siloId: Optional[str] = None
    feedType: str
    amount: float
    unit: str
    notes: Optional[str] = None
    deliveryDate: Optional[str] = None
    # Rich docket fields (optional — populated when saved from the AI scanner)
    docketNumber: Optional[str] = None
    productCode: Optional[str] = None
    customerName: Optional[str] = None
    siteCode: Optional[str] = None
    truckRego: Optional[str] = None
    outloadingBin: Optional[str] = None
    deliveryInstructions: Optional[str] = None
    supplier: Optional[str] = None  # "Ingham's" | "Baiada" | "BPL Adelaide" etc
    imageThumb: Optional[str] = None  # base64 jpeg (small)


# ─── Seed default farm (10 shed groups × 3 silos) ──────────────────────────
async def _seed_farm(farm_id: str, name: Optional[str] = None) -> None:
    """Seed 10 shed-groups × 3 silos for a given farm_id (idempotent)."""
    if await shed_groups_col.count_documents(_farm_filter(farm_id)) > 0:
        return
    groups = []
    silos = []
    for i in range(1, 11):
        gid = str(uuid.uuid4())
        groups.append({
            "id": gid,
            "farmId": farm_id,
            "name": f"Sheds {2 * i - 1} & {2 * i}",
            "displayOrder": i,
        })
        for letter in ("A", "B", "C"):
            silos.append({
                "id": str(uuid.uuid4()),
                "farmId": farm_id,
                "shedGroupId": gid,
                "letter": letter,
                "name": f"Silo {letter}",
                "defaultFeedType": None,
            })
    if groups:
        await shed_groups_col.insert_many(groups)
    if silos:
        await silos_col.insert_many(silos)


async def seed_if_empty() -> None:
    # Seed the default farm if nothing exists yet
    await _seed_farm(DEFAULT_FARM_ID)
    # Ensure a "default" farm record exists in the farms collection
    if not await farms_col.find_one({"slug": DEFAULT_FARM_ID}):
        await farms_col.insert_one({
            "id": str(uuid.uuid4()),
            "slug": DEFAULT_FARM_ID,
            "name": "My Farm",
            "ownerEmail": os.environ.get("ADMIN_EMAIL"),
            "createdAt": datetime.now(timezone.utc),
            "isDefault": True,
        })


@app.on_event("startup")
async def _startup():
    await seed_if_empty()
    # SEC-006 migration: backfill farmToken on every existing farm so the
    # per-farm read-access mechanism works retroactively.
    async for f in farms_col.find({"$or": [{"farmToken": {"$exists": False}}, {"farmToken": ""}, {"farmToken": None}]}, {"slug": 1}):
        await farms_col.update_one(
            {"slug": f["slug"]},
            {"$set": {"farmToken": secrets.token_urlsafe(24)}},
        )
    # Background scheduler — last-Friday-of-month auto-send
    asyncio.create_task(_maybe_send_monthly_reports())
    await _ensure_indexes()


async def _ensure_indexes():
    """Create indexes on hot collections so queries stay fast as data grows.

    Mongo's createIndex is idempotent: safe to call on every startup. Most
    queries here filter by `farmId` + a date/sort field, so we index those
    pairs. Single-field indexes are added where the collection is also queried
    standalone (e.g. id lookups, hash dedupe).
    """
    try:
        # readings: every /api/readings/today scan & /api/farm-buddy aggregations
        await readings_col.create_index([("farmId", 1), ("readingDate", -1)])
        await readings_col.create_index([("farmId", 1), ("siloId", 1), ("readingDate", -1)])
        await readings_col.create_index([("hash", 1)])
        await readings_col.create_index([("id", 1)], unique=True, sparse=True)

        # deliveries: dashboard + reader history + dedupe
        await deliveries_col.create_index([("farmId", 1), ("deliveryDate", -1)])
        await deliveries_col.create_index([("hash", 1)])
        await deliveries_col.create_index([("id", 1)], unique=True, sparse=True)

        # silos & shed_groups: small but read on every endpoint
        await silos_col.create_index([("farmId", 1), ("letter", 1)])
        await silos_col.create_index([("id", 1)], unique=True, sparse=True)
        await shed_groups_col.create_index([("farmId", 1), ("displayOrder", 1)])
        await shed_groups_col.create_index([("id", 1)], unique=True, sparse=True)

        # feed_program_state — keyed by farmId in PUT/GET
        await feed_program_state_col.create_index([("farmId", 1)], unique=True)
        # feed_program_state_history — Cloud Rewind snapshots
        await feed_program_state_history_col.create_index([("farmId", 1), ("capturedAt", -1)])
        await feed_program_state_history_col.create_index([("id", 1)], unique=True, sparse=True)

        # farms — looked up by slug & ownerEmail (auth flow)
        await farms_col.create_index([("slug", 1)], unique=True)
        await farms_col.create_index([("ownerEmail", 1)])

        # farm_config — keyed by id (which is farmId)
        await farm_config_col.create_index([("id", 1)], unique=True)

        # photos — bulk listings by farm
        await photos_col.create_index([("farmId", 1), ("createdAt", -1)])

        # chat_messages (Farm Buddy / owner-admin chat) — per-farm timeline
        await db["chat_messages"].create_index([("farm_id", 1), ("created_at", -1)])

        # payments — Stripe webhook lookups
        await payments_col.create_index([("session_id", 1)], unique=True, sparse=True)

        # eob_snapshots — one per (farm, batch); listing sorted by lockedAt
        await eob_snapshots_col.create_index(
            [("farmId", 1), ("batchIdentifier", 1)], unique=True,
        )
        await eob_snapshots_col.create_index([("farmId", 1), ("lockedAt", -1)])
        await eob_snapshots_col.create_index([("id", 1)], unique=True, sparse=True)
    except Exception as e:  # pragma: no cover — never block startup on index errors
        import logging
        logging.getLogger("server").warning(f"Index creation skipped: {e}")


# ─── Router ────────────────────────────────────────────────────────────────
api = APIRouter(prefix="/api")


@api.get("/")
async def root():
    return {"ok": True, "service": "broiler-base-mate", "ts": datetime.now(timezone.utc).isoformat()}


@api.get("/healthz")
@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/bootstrap")
async def bootstrap():
    return {
        "user": {"id": "local-user", "email": "local@offline", "name": "Local User"},
        "farms": [{"id": "local-farm", "name": "My Farm"}],
        "currentFarmId": "local-farm",
        "mode": "standalone",
    }


@api.get("/batch/version")
async def batch_version():
    return {"version": None}


@api.delete("/batch/reset")
async def batch_reset(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """New Batch: wipe this farm's readings, deliveries, and photos so app starts empty.
    Also clears external_weighins — Staff QR bird weights are keyed by
    (farm, shed, AGE), which collides across batches (Day 21 exists in every
    batch), unlike external_morts which is keyed by absolute calendar date
    and therefore doesn't cross-contaminate a new batch on its own. Found via
    code review Sep 2026: without this, the 60s desktop poll would silently
    re-merge the previous batch's weigh-ins into the new batch's Flock
    Forecast within a minute of starting fresh."""
    await _require_farm_access(request, farm)   # SEC-001 — DESTRUCTIVE, absolutely must be gated
    r = await readings_col.delete_many(_farm_filter(farm))
    d = await deliveries_col.delete_many(_farm_filter(farm))
    p = await photos_col.delete_many(_farm_filter(farm))
    w = await external_weighins_col.delete_many({"farmId": farm})
    return {"ok": True, "readingsDeleted": r.deleted_count, "deliveriesDeleted": d.deleted_count, "photosDeleted": p.deleted_count, "externalWeighInsDeleted": w.deleted_count}


@api.delete("/admin/delete-user")
async def admin_delete_user(request: Request,
                            email: str = Query(...),
                            confirm: str = Query(...)):
    """
    ⚠️  DESTRUCTIVE — hard-delete a user account by email:
      · every farm they own (record + shed groups + silos + readings + deliveries
        + photos + feed program state + history + EOB snapshots)
      · their user document
      · every active session token they hold (so any open browser is logged out)
      · any farm invites addressed to them

    Admin-only. Requires ?confirm=DELETE-USER-<email> to prevent a fat-finger
    click nuking the wrong account.

    Feb 28, 2026 (Jason): "just my broilerbasemate account as signing 2 user
    in appcovi2026@gmail.com and doublebb@baqerifarming.com.au".
    """
    await _require_admin(request)
    email_l = (email or "").strip().lower()
    if not email_l or "@" not in email_l:
        raise HTTPException(400, "email required")
    expected = f"DELETE-USER-{email_l}"
    if confirm != expected:
        raise HTTPException(400, f"Missing/invalid confirm token — pass ?confirm={expected}")

    # Owned farms
    owned = await farms_col.find({"ownerEmail": {"$regex": f"^{email_l}$", "$options": "i"}}, {"slug": 1, "name": 1}).to_list(length=None)
    total_readings = total_deliveries = total_photos = 0
    total_state = total_hist = total_eob = total_sheds = total_silos = 0
    farm_slugs = []
    for f in owned:
        slug = f["slug"]
        farm_slugs.append(slug)
        ff = _farm_filter(slug)
        r  = await readings_col.delete_many(ff);          total_readings += r.deleted_count
        d  = await deliveries_col.delete_many(ff);        total_deliveries += d.deleted_count
        p  = await photos_col.delete_many(ff);            total_photos += p.deleted_count
        fp = await feed_program_state_col.delete_many({"farmId": slug});          total_state += fp.deleted_count
        fh = await feed_program_state_history_col.delete_many({"farmId": slug});  total_hist += fh.deleted_count
        e  = await eob_snapshots_col.delete_many({"farmId": slug});               total_eob += e.deleted_count
        sg = await shed_groups_col.delete_many(ff);       total_sheds += sg.deleted_count
        sl = await silos_col.delete_many(ff);             total_silos += sl.deleted_count
    farms_deleted = await farms_col.delete_many({"ownerEmail": {"$regex": f"^{email_l}$", "$options": "i"}})
    invites_deleted = await db["farm_invites"].delete_many({"operatorEmail": {"$regex": f"^{email_l}$", "$options": "i"}})
    sessions_deleted = 0
    users_deleted = 0
    user = await db["users"].find_one({"email": {"$regex": f"^{email_l}$", "$options": "i"}}, {"user_id": 1})
    if user:
        sess = await db["user_sessions"].delete_many({"user_id": user.get("user_id")})
        sessions_deleted = sess.deleted_count
        ud = await db["users"].delete_many({"email": {"$regex": f"^{email_l}$", "$options": "i"}})
        users_deleted = ud.deleted_count

    return {
        "ok": True,
        "email": email_l,
        "farmsDeleted": farms_deleted.deleted_count,
        "farmSlugs": farm_slugs,
        "shedGroupsDeleted": total_sheds,
        "silosDeleted": total_silos,
        "readingsDeleted": total_readings,
        "deliveriesDeleted": total_deliveries,
        "photosDeleted": total_photos,
        "feedProgramStateDeleted": total_state,
        "feedProgramHistoryDeleted": total_hist,
        "eobSnapshotsDeleted": total_eob,
        "invitesDeleted": invites_deleted.deleted_count,
        "userRecordsDeleted": users_deleted,
        "sessionsDeleted": sessions_deleted,
        "note": "Account fully purged. Env-configured OWNER_EMAIL may auto-recreate on next magic-link visit.",
    }


@api.delete("/farm/factory-reset")
async def farm_factory_reset(request: Request,
                             farm: str = Query(...),
                             confirm: str = Query(...)):
    """
    ⚠️  DESTRUCTIVE — wipes ALL operational data for a farm so it starts as clean
    slate: silo readings, deliveries, docket photos, feed-program state + history,
    locked EOB snapshots. Preserves the farm record itself + shed/silo setup.

    Admin-only. Requires ?confirm=WIPE-EVERYTHING to prevent accidental clicks
    (query-string works even from a simple curl or admin-panel button).

    Feb 28, 2026 (Jason): "my feed load delivers and showing my silo readings on
    a shed page — needs to be a clean slate".
    """
    await _require_admin(request)  # admin-only, stricter than farm access
    if confirm != "WIPE-EVERYTHING":
        raise HTTPException(400, "Missing/invalid confirm token — pass ?confirm=WIPE-EVERYTHING to proceed")

    farm_doc = await farms_col.find_one({"slug": farm})
    if not farm_doc:
        raise HTTPException(404, f"Farm '{farm}' not found")

    ff = _farm_filter(farm)
    r  = await readings_col.delete_many(ff)
    d  = await deliveries_col.delete_many(ff)
    p  = await photos_col.delete_many(ff)
    fp = await feed_program_state_col.delete_many({"farmId": farm})
    fh = await feed_program_state_history_col.delete_many({"farmId": farm})
    e  = await eob_snapshots_col.delete_many({"farmId": farm})
    return {
        "ok": True,
        "farmSlug": farm,
        "farmName": farm_doc.get("name"),
        "readingsDeleted": r.deleted_count,
        "deliveriesDeleted": d.deleted_count,
        "photosDeleted": p.deleted_count,
        "feedProgramStateDeleted": fp.deleted_count,
        "feedProgramHistoryDeleted": fh.deleted_count,
        "eobSnapshotsDeleted": e.deleted_count,
        "note": "Farm record, shed groups, silos and users kept. Operational data cleared.",
    }


@api.get("/onedrive/status")
async def onedrive_status():
    return {
        "onedriveConnected": False,
        "onedriveFileId": None,
        "onedriveFileName": None,
        "gdriveConnected": False,
        "gdriveFileId": None,
        "gdriveFileName": None,
    }


# ── Shed groups ──────────────────────────────────────────────────────────
@api.get("/shed-groups", response_model=List[ShedGroup])
async def list_shed_groups(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    groups = await shed_groups_col.find(_farm_filter(farm)).sort("displayOrder", 1).to_list(length=200)
    silos = await silos_col.find(_farm_filter(farm)).sort("letter", 1).to_list(length=1000)
    out: List[ShedGroup] = []
    for g in groups:
        out.append(ShedGroup(
            id=g["id"],
            name=g["name"],
            displayOrder=g["displayOrder"],
            silos=[
                SiloInfo(
                    id=s["id"],
                    letter=s.get("letter", ""),
                    name=s.get("name") or s.get("label", ""),
                    defaultFeedType=s.get("defaultFeedType"),
                )
                for s in silos if s.get("shedGroupId") == g["id"]
            ],
        ))
    return out


# ── Silos ────────────────────────────────────────────────────────────────
@api.get("/silos")
async def list_silos(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    silos = await silos_col.find(_farm_filter(farm)).sort("letter", 1).to_list(length=1000)
    return [clean(s) for s in silos]


@api.post("/silos", status_code=201)
async def create_silo(body: CreateSiloBody, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-001
    doc = {
        "id": str(uuid.uuid4()),
        "farmId": farm,
        "name": body.name,
        "defaultFeedType": body.defaultFeedType,
        "shedGroupId": body.shedGroupId,
        "letter": body.letter or "",
    }
    await silos_col.insert_one(doc)
    return clean(doc)


@api.patch("/silos/{silo_id}")
async def update_silo(silo_id: str, body: UpdateSiloBody, request: Request):
    # SEC-001: session required BEFORE the existence check so anon callers
    # can't probe silo IDs. Farm access is then re-checked on the record.
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    existing = await silos_col.find_one({"id": silo_id}, {"farmId": 1, "_id": 0})
    if existing is None:
        raise HTTPException(404, "Silo not found")
    await _require_farm_access(request, existing.get("farmId") or DEFAULT_FARM_ID)
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not patch:
        raise HTTPException(400, "Nothing to update")
    res = await silos_col.find_one_and_update({"id": silo_id}, {"$set": patch}, return_document=True)
    if res is None:
        raise HTTPException(404, "Silo not found")
    return clean(res)


@api.delete("/silos/{silo_id}", status_code=204)
async def delete_silo(silo_id: str, request: Request):
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    existing = await silos_col.find_one({"id": silo_id}, {"farmId": 1, "_id": 0})
    if existing is None:
        raise HTTPException(404, "Silo not found")
    await _require_farm_access(request, existing.get("farmId") or DEFAULT_FARM_ID)   # SEC-001
    res = await silos_col.delete_one({"id": silo_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Silo not found")
    return JSONResponse(content=None, status_code=204)


# ── Readings ─────────────────────────────────────────────────────────────
@api.get("/readings/today")
async def readings_today(request: Request, localDate: Optional[str] = Query(default=None), farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    """Today's readings, grouped by shed for the Feed Program auto-sync."""
    if localDate and len(localDate) == 10:
        d = datetime.fromisoformat(localDate)
        start = datetime.combine(d.date(), datetime.min.time(), tzinfo=timezone.utc) - AEST_OFFSET
        end = datetime.combine(d.date(), datetime.max.time(), tzinfo=timezone.utc) - AEST_OFFSET
        date_str = localDate
    else:
        start, end = aest_today_range()
        date_str = aest_today()

    groups = await shed_groups_col.find(_farm_filter(farm)).sort("displayOrder", 1).to_list(length=200)
    silos = await silos_col.find(_farm_filter(farm)).sort("letter", 1).to_list(length=1000)
    todays = await readings_col.find(_and(
        _farm_filter(farm),
        {"readingDate": {"$gte": start, "$lte": end}},
    )).to_list(length=5000)

    sheds = []
    for g in groups:
        group_silos = [s for s in silos if s.get("shedGroupId") == g["id"]]
        silo_statuses = []
        for s in group_silos:
            reading = next((r for r in todays if r["siloId"] == s["id"]), None)
            silo_statuses.append({
                "siloId": s["id"],
                "letter": s.get("letter", ""),
                "name": s.get("name") or s.get("label", s.get("letter", "Silo")),
                "saved": reading is not None,
                "readingId": reading["id"] if reading else None,
                "amountRemaining": float(reading["amountRemaining"]) if reading else None,
                "feedType": reading["feedType"] if reading else None,
                "unit": reading["unit"] if reading else None,
            })
        sheds.append({
            "shedGroupId": g["id"],
            "shedGroupName": g["name"],
            "allSaved": len(silo_statuses) > 0 and all(s["saved"] for s in silo_statuses),
            "silos": silo_statuses,
        })

    return {
        "date": date_str,
        "savedCount": sum(1 for s in sheds if s["allSaved"]),
        "totalCount": len(sheds),
        "sheds": sheds,
    }


# ── Farm Buddy "quick alerts" for the mobile Reader ────────────────────────
# Lightweight threshold-based alerts (no LLM) — fast enough to call from the
# Reader every 30 s. Flags any shed-group whose total stored feed is critical
# (<5 t) or low (<10 t) based on the most recent silo readings.
# Used by the Reader to push proactive "🚨 Order feed for Sheds 3 & 4" banners
# straight to the field manager's phone so they don't have to remember to
# check the Feed Program on the desktop.
@api.get("/farm-buddy/alerts")
async def farm_buddy_alerts(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    groups = await shed_groups_col.find(_farm_filter(farm)).sort("displayOrder", 1).to_list(length=200)
    silos = await silos_col.find(_farm_filter(farm)).sort("letter", 1).to_list(length=1000)

    # Pull farm_config to find sheds the grower has marked as "no birds left"
    # (toggled OFF in the Reader's Settings). Those sheds get a 🐔 EMPTY pin
    # from Farm Buddy AND get excluded from the low-silo / stale / missing-today
    # checks below (you don't want red alerts on a shed you just depopulated).
    fc_doc = await farm_config_col.find_one({"id": farm}) or {}
    all_group_ids = {g["id"] for g in groups}
    enabled_ids_raw = fc_doc.get("enabledGroupIds")
    # If never set → all sheds are considered active (default behaviour).
    if enabled_ids_raw is None:
        enabled_ids: set[str] = set(all_group_ids)
    else:
        enabled_ids = set(enabled_ids_raw)
    empty_group_ids = all_group_ids - enabled_ids

    # Sheds that were never placed with birds AND aren't currently enabled
    # shouldn't produce alerts of any kind — they're phantom sheds from the
    # default 20-shed seed template (e.g. Sheds 13/14/15/16 on a farm that
    # only runs 12). We identify these as groups with zero historical
    # readings AND not currently enabled.
    group_ids_with_readings: set[str] = set()

    # Latest reading per silo (most recent saved value, regardless of date)
    latest_by_silo: dict[str, float] = {}
    latest_date_by_silo: dict[str, datetime] = {}
    pipeline = [
        {"$match": _farm_filter(farm)},
        {"$sort": {"readingDate": -1}},
        {"$group": {
            "_id": "$siloId",
            "amountRemaining": {"$first": "$amountRemaining"},
            "unit": {"$first": "$unit"},
            "readingDate": {"$first": "$readingDate"},
        }},
    ]
    async for doc in readings_col.aggregate(pipeline):
        amt = float(doc.get("amountRemaining") or 0)
        # Normalize kg → t for any legacy rows still stored in kg.
        if (doc.get("unit") or "").lower() == "kg":
            amt = amt / 1000.0
        latest_by_silo[doc["_id"]] = amt
        rd = doc.get("readingDate")
        if rd is not None:
            if rd.tzinfo is None:
                rd = rd.replace(tzinfo=timezone.utc)
            latest_date_by_silo[doc["_id"]] = rd

    # Map silo → shedGroup so we can identify which groups have ever had a reading.
    silo_to_group: dict[str, str] = {s["id"]: s.get("shedGroupId") for s in silos if s.get("shedGroupId")}
    for silo_id in latest_by_silo:
        gid = silo_to_group.get(silo_id)
        if gid:
            group_ids_with_readings.add(gid)

    # "Phantom" sheds: default-seeded shed_groups the user never placed birds in.
    # If a shed is currently disabled AND has no historical readings, it doesn't
    # exist for this farm — suppress all alerts (including the 🐔 EMPTY pin).
    phantom_group_ids = {gid for gid in empty_group_ids if gid not in group_ids_with_readings}

    alerts: list[dict] = []
    now = datetime.now(timezone.utc)
    today_start, today_end = aest_today_range()

    # ─── 🐔 Empty sheds — highlight first, suppress noise on these ────────
    # Only pin sheds that were ACTUALLY placed at some point (have readings on
    # file). Phantom sheds from the default 20-shed seed are silently skipped.
    for g in groups:
        if g["id"] in phantom_group_ids:
            continue
        if g["id"] in empty_group_ids:
            alerts.append({
                "shedGroupId": g["id"],
                "shedGroupName": g["name"],
                "totalT": None,
                "level": "info",
                "message": f"🐔 {g['name']} is EMPTY (no birds) — feed drops will skip this shed",
                "category": "empty_shed",
            })

    for g in groups:
        if g["id"] in empty_group_ids:
            continue  # skip — already pinned above as empty (or phantom)
        group_silos = [s for s in silos if s.get("shedGroupId") == g["id"]]
        if not group_silos:
            continue
        total_t = sum(latest_by_silo.get(s["id"], 0.0) for s in group_silos)
        # Only flag groups that have at least one real reading on file.
        if not any(s["id"] in latest_by_silo for s in group_silos):
            continue
        if total_t < 5:
            level = "critical"
            msg = f"🚨 ORDER FEED NOW — {g['name']} only has {total_t:.1f} t left"
        elif total_t < 10:
            level = "watch"
            msg = f"⚠️ Getting low — {g['name']} down to {total_t:.1f} t"
        else:
            continue
        alerts.append({
            "shedGroupId": g["id"],
            "shedGroupName": g["name"],
            "totalT": round(total_t, 2),
            "level": level,
            "message": msg,
        })

    # ─── Watchdog checks — catch silent sync failures Farm Buddy-style ──────
    # The user's last batch had a sync-loss bug where readings were saved on
    # the phone but never propagated to the Feed Program. Surface that
    # actively so it can't happen again without a visible alert.

    # 1) Stale silo readings — last save older than 26h (AEST overnight cycle
    #    is ~22h, so >26h = something is wrong).
    stale_cutoff = now - timedelta(hours=26)
    stale_groups: list[tuple[str, datetime]] = []
    for g in groups:
        if g["id"] in empty_group_ids:
            continue  # depopulated shed — no readings expected
        group_silos = [s for s in silos if s.get("shedGroupId") == g["id"]]
        if not group_silos:
            continue
        # Pick the freshest reading across all silos in this group
        dates = [latest_date_by_silo[s["id"]] for s in group_silos if s["id"] in latest_date_by_silo]
        if not dates:
            continue  # never had a reading — not "stale", just unseeded
        freshest = max(dates)
        if freshest < stale_cutoff:
            stale_groups.append((g["name"], freshest))
            hours_ago = int((now - freshest).total_seconds() // 3600)
            alerts.append({
                "shedGroupId": g["id"],
                "shedGroupName": g["name"],
                "totalT": None,
                "level": "watch",
                "message": f"⏱️ {g['name']} hasn't been read in {hours_ago} h — last reading {freshest.strftime('%a %d %b %H:%M')} AEST. Send the catcher round?",
                "category": "stale_reading",
            })

    # 2) Missing today after 4pm AEST — sheds with no reading saved today
    #    when the workday is essentially over.
    aest_now_hour = (now + AEST_OFFSET).hour
    if aest_now_hour >= 16:  # 4pm AEST or later
        for g in groups:
            if g["id"] in empty_group_ids:
                continue  # depopulated shed — no reading expected
            group_silos = [s for s in silos if s.get("shedGroupId") == g["id"]]
            if not group_silos:
                continue
            # Did any silo in this group get a reading today?
            saved_today = any(
                latest_date_by_silo.get(s["id"]) and today_start <= latest_date_by_silo[s["id"]] <= today_end
                for s in group_silos
            )
            if saved_today:
                continue
            # Has this group EVER been read? (skip never-seeded sheds)
            if not any(s["id"] in latest_date_by_silo for s in group_silos):
                continue
            # Don't double-flag if already stale-flagged above
            if any(name == g["name"] for name, _ in stale_groups):
                continue
            alerts.append({
                "shedGroupId": g["id"],
                "shedGroupName": g["name"],
                "totalT": None,
                "level": "watch",
                "message": f"📭 No reading saved for {g['name']} today — workday nearly done.",
                "category": "missing_today",
            })

    # 3) Sync health pulse — count readings saved in the last 6 hours so the
    #    Reader's banner can show "✓ N readings in last 6h" as positive
    #    confirmation that the pipe is healthy.
    six_h_ago = now - timedelta(hours=6)
    recent_count = await readings_col.count_documents(_and(
        _farm_filter(farm),
        {"readingDate": {"$gte": six_h_ago}},
    ))

    # Sort critical first, then watch, then the rest
    LEVEL_ORDER = {"critical": 0, "watch": 1, "info": 2}
    alerts.sort(key=lambda a: (LEVEL_ORDER.get(a["level"], 9), a.get("totalT") or 999))
    # Risk level: info-only alerts (e.g. empty sheds) shouldn't push the farm
    # into "watch" mode — they're status pins, not warnings.
    risk = "critical" if any(a["level"] == "critical" for a in alerts) else (
        "watch" if any(a["level"] == "watch" for a in alerts) else "ok"
    )
    return {
        "riskLevel": risk,
        "alerts": alerts,
        "syncHealth": {
            "readingsLast6h": recent_count,
            "staleGroups": len(stale_groups),
            "missingTodayGroups": sum(1 for a in alerts if a.get("category") == "missing_today"),
            "emptyGroups": sum(1 for a in alerts if a.get("category") == "empty_shed"),
        },
        "checkedAt": now.isoformat(),
    }


@api.post("/readings/batch", status_code=201)
async def batch_create_readings(body: BatchCreateReadingsBody, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-001
    if not body.readings:
        raise HTTPException(400, "No readings provided")
    date = datetime.fromisoformat(body.readingDate.replace("Z", "+00:00")) if body.readingDate else datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)

    # Upsert: one reading per silo per AEST-day. Re-saving the same silo
    # on the same day replaces the previous reading (acts as a "correction").
    today_start, today_end = aest_today_range()
    inserted = []
    for r in body.readings:
        if today_start <= date <= today_end:
            await readings_col.delete_many(_and(
                _farm_filter(farm),
                {"siloId": r.siloId, "readingDate": {"$gte": today_start, "$lte": today_end}},
            ))
        doc = {
            "id": str(uuid.uuid4()),
            "farmId": farm,
            "siloId": r.siloId,
            "feedType": r.feedType,
            "amountRemaining": float(r.amountRemaining),
            "unit": r.unit,
            "notes": r.notes,
            "readingDate": date,
            "createdAt": datetime.now(timezone.utc),
        }
        await readings_col.insert_one(doc)
        silo = await silos_col.find_one({"id": r.siloId})
        group = await shed_groups_col.find_one({"id": silo.get("shedGroupId")}) if silo else None
        inserted.append({
            "id": doc["id"],
            "siloId": doc["siloId"],
            "siloName": (silo.get("name") or silo.get("label", "")) if silo else "",
            "siloLetter": silo.get("letter", "") if silo else "",
            "shedGroupName": group.get("name", "") if group else "",
            "feedType": doc["feedType"],
            "amountRemaining": doc["amountRemaining"],
            "unit": doc["unit"],
            "notes": doc["notes"],
            "readingDate": doc["readingDate"].isoformat(),
            "createdAt": doc["createdAt"].isoformat(),
        })
    return inserted


@api.get("/readings")
async def list_readings(request: Request, limit: int = Query(default=100, le=1000), siloId: Optional[str] = None, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    q: dict = dict(_farm_filter(farm))
    if siloId:
        q = _and(q, {"siloId": siloId})
    rows = await readings_col.find(q).sort("readingDate", -1).limit(limit).to_list(length=limit)
    silos = await silos_col.find(_farm_filter(farm)).to_list(length=1000)
    groups = await shed_groups_col.find(_farm_filter(farm)).to_list(length=200)
    out = []
    for r in rows:
        silo = next((s for s in silos if s["id"] == r["siloId"]), None)
        group = next((g for g in groups if g["id"] == (silo.get("shedGroupId") if silo else None)), None)
        out.append({
            "id": r["id"],
            "siloId": r["siloId"],
            "siloName": (silo.get("name") or silo.get("label", "")) if silo else "",
            "siloLetter": silo.get("letter", "") if silo else "",
            "shedGroupName": group.get("name", "") if group else "",
            "feedType": r["feedType"],
            "amountRemaining": float(r["amountRemaining"]),
            "unit": r["unit"],
            "notes": r.get("notes"),
            "readingDate": r["readingDate"].isoformat(),
            "createdAt": r["createdAt"].isoformat(),
        })
    return out


@api.delete("/readings/{reading_id}", status_code=204)
async def delete_reading(reading_id: str, request: Request):
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    existing = await readings_col.find_one({"id": reading_id}, {"farmId": 1, "_id": 0})
    if existing is None:
        raise HTTPException(404, "Reading not found")
    await _require_farm_access(request, existing.get("farmId") or DEFAULT_FARM_ID)
    res = await readings_col.delete_one({"id": reading_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Reading not found")
    return JSONResponse(content=None, status_code=204)


# ── Deliveries ───────────────────────────────────────────────────────────
@api.get("/deliveries")
async def list_deliveries(request: Request, limit: int = Query(default=100, le=1000), farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    rows = await deliveries_col.find(_farm_filter(farm)).sort("deliveryDate", -1).limit(limit).to_list(length=limit)
    silos = await silos_col.find(_farm_filter(farm)).to_list(length=1000)
    groups = await shed_groups_col.find(_farm_filter(farm)).to_list(length=200)
    out = []
    for r in rows:
        silo = next((s for s in silos if s["id"] == r.get("siloId")), None)
        group = next((g for g in groups if g["id"] == r.get("shedGroupId")), None) or (
            next((g for g in groups if silo and g["id"] == silo.get("shedGroupId")), None) if silo else None
        )
        out.append({
            "id": r["id"],
            "shedGroupId": r.get("shedGroupId"),
            "shedGroupName": group.get("name") if group else None,
            "siloId": r.get("siloId"),
            "siloLetter": silo.get("letter") if silo else None,
            "feedType": r["feedType"],
            "feedTypeOriginal": r.get("feedTypeOriginal"),
            "amount": float(r["amount"]),
            "unit": r["unit"],
            "notes": r.get("notes"),
            "deliveryDate": r["deliveryDate"].isoformat(),
            "createdAt": r["createdAt"].isoformat(),
            "docketNumber": r.get("docketNumber"),
            "productCode": r.get("productCode"),
            "customerName": r.get("customerName"),
            "siteCode": r.get("siteCode"),
            "truckRego": r.get("truckRego"),
            "outloadingBin": r.get("outloadingBin"),
            "deliveryInstructions": r.get("deliveryInstructions"),
            "supplier": r.get("supplier"),
            "imageThumb": r.get("imageThumb"),
        })
    return out


@api.post("/deliveries", status_code=201)
async def create_delivery(body: CreateDeliveryBody, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-001
    date = datetime.fromisoformat(body.deliveryDate.replace("Z", "+00:00")) if body.deliveryDate else datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    # Normalise feed type so the Feed Program's End-of-Batch matcher always finds a column
    # ("Broiler Grower" → "Grower", "Gourmet Broiler Starter" → "Starter", etc.)
    normalised_feed = _normalise_feed_type(body.feedType)
    doc = {
        "id": str(uuid.uuid4()),
        "farmId": farm,
        "shedGroupId": body.shedGroupId,
        "siloId": body.siloId,
        "feedType": normalised_feed,
        "feedTypeOriginal": body.feedType if normalised_feed != body.feedType else None,
        "amount": float(body.amount),
        "unit": body.unit,
        "notes": body.notes,
        "deliveryDate": date,
        "createdAt": datetime.now(timezone.utc),
        "docketNumber": body.docketNumber,
        "productCode": body.productCode,
        "customerName": body.customerName,
        "siteCode": body.siteCode,
        "truckRego": body.truckRego,
        "outloadingBin": body.outloadingBin,
        "deliveryInstructions": body.deliveryInstructions,
        "supplier": body.supplier,
        "imageThumb": body.imageThumb,
    }
    await deliveries_col.insert_one(doc)
    return clean(doc)


def _normalise_feed_type(raw: str) -> str:
    """Map any feed-type label to canonical 'Starter' / 'Grower' / 'Finisher' / 'Withdrawal'.

    The Feed Program's End-of-Batch matcher does prefix-matching only, so labels
    like 'Broiler Grower' or 'Gourmet Broiler Finisher' need to be normalised
    so they land in the right column.
    """
    if not raw:
        return raw
    lo = raw.lower()
    if "withdraw" in lo:
        return "Withdrawal"
    if "finisher" in lo or "finish" in lo:
        return "Finisher"
    if "grower" in lo or "grow" in lo:
        return "Grower"
    if "starter" in lo or "start" in lo:
        return "Starter"
    return raw


@api.delete("/deliveries/{delivery_id}", status_code=204)
async def delete_delivery(delivery_id: str, request: Request):
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    existing = await deliveries_col.find_one({"id": delivery_id}, {"farmId": 1, "_id": 0})
    if existing is None:
        raise HTTPException(404, "Delivery not found")
    await _require_farm_access(request, existing.get("farmId") or DEFAULT_FARM_ID)   # SEC-001
    res = await deliveries_col.delete_one({"id": delivery_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Delivery not found")
    return JSONResponse(content=None, status_code=204)


# ── Stubs (unused in standalone but called by silo-tracker) ───────────────

# ─── Language directive helper for AI vision endpoints ────────────────
_LANG_NAMES_AI = {
    "en":"English","vi":"Vietnamese","zh":"Simplified Chinese","zh-CN":"Simplified Chinese",
    "pt":"Brazilian Portuguese","es":"Spanish","id":"Indonesian","th":"Thai",
    "ko":"Korean","tl":"Filipino/Tagalog","hi":"Hindi","ar":"Arabic",
    "fr":"French","de":"German","ja":"Japanese","ms":"Malay",
}

def _lang_directive(payload: dict) -> str:
    """Return a system-message suffix that tells Gemini to write user-visible text in the requested language."""
    code = ((payload or {}).get("language") or "en").lower()
    name = _LANG_NAMES_AI.get(code, "English")
    if code == "en" or name == "English":
        return ""
    return f"\n\nIMPORTANT: Write ALL user-visible text (notes, error messages, confidence descriptions) in {name}. Keep JSON keys and numeric values in English."


@api.post("/weigh-bird")
async def weigh_bird(payload: dict):
    """Estimate a live broiler's weight from a photo, GROUNDED in the bird's
    known age (mandatory) and the Ross 308 growth curve. Instead of asking
    Gemini to guess a raw kg from a photo (impossible without a scale
    reference), we anchor the estimate on the industry-standard target for
    that day and ask Gemini only to judge SIZE-vs-AGE — small / on-target /
    large — then compute the weight from the target ± that offset.

    Body: { imageBase64: str, mimeType?: str, ageDays: int, shedNum?: int, breed?: 'ross308'|'cobb500' }
    Returns: { ok, estimatedWeightKg, confidenceLevel, notes, sizeVsAge, targetKg, breed }
    """
    image_b64 = (payload or {}).get("imageBase64")
    if not image_b64:
        raise HTTPException(400, "imageBase64 required")
    if "," in image_b64 and image_b64.startswith("data:"):
        image_b64 = image_b64.split(",", 1)[1]
    age_days = (payload or {}).get("ageDays")
    if not age_days or not isinstance(age_days, (int, float)) or age_days < 1 or age_days > 60:
        raise HTTPException(400, "ageDays (1-60) is required to anchor the estimate")
    age_days = int(round(age_days))
    breed = ((payload or {}).get("breed") or "ross308").lower()

    # Ross 308 target body weight (kg) by day — Aviagen 2022 as-hatched.
    ROSS_308 = {
        1:0.049, 2:0.065, 3:0.085, 4:0.108, 5:0.135, 6:0.166, 7:0.205,
        8:0.245, 9:0.291, 10:0.343, 11:0.398, 12:0.457, 13:0.520, 14:0.586,
        15:0.655, 16:0.727, 17:0.803, 18:0.881, 19:0.862, 20:0.945, 21:1.012,
        22:1.099, 23:1.188, 24:1.279, 25:1.371, 26:1.464, 27:1.558, 28:1.616,
        29:1.748, 30:1.843, 31:1.938, 32:2.033, 33:2.128, 34:2.222, 35:2.296,
        36:2.408, 37:2.500, 38:2.591, 39:2.681, 40:2.769, 41:2.855, 42:2.998,
        43:3.070, 44:3.140, 45:3.210, 46:3.278, 47:3.346, 48:3.414, 49:3.480,
        50:3.545, 51:3.610, 52:3.674, 53:3.737, 54:3.799, 55:3.860, 56:3.920,
        57:3.980, 58:4.038, 59:4.096, 60:4.152,
    }
    # Cobb 500 approximate as-hatched targets by day
    COBB_500 = {
        1:0.052, 7:0.210, 14:0.597, 21:1.158, 28:1.840, 35:2.529, 42:3.020,
        49:3.500, 56:3.940,
    }
    if breed == "cobb500":
        # linear-interpolate cobb targets between known milestones
        known = sorted(COBB_500.keys())
        if age_days <= known[0]:
            target = COBB_500[known[0]]
        elif age_days >= known[-1]:
            target = COBB_500[known[-1]]
        else:
            for i in range(len(known)-1):
                if known[i] <= age_days <= known[i+1]:
                    lo, hi = known[i], known[i+1]
                    frac = (age_days - lo) / (hi - lo)
                    target = COBB_500[lo] + frac * (COBB_500[hi] - COBB_500[lo])
                    break
    else:
        target = ROSS_308.get(age_days) or ROSS_308[min(ROSS_308.keys(), key=lambda k: abs(k - age_days))]
    target = round(target, 3)

    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(503, "LLM key not configured")
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    except Exception as e:
        raise HTTPException(503, f"LLM lib missing: {e}")

    breed_label = "Cobb 500" if breed == "cobb500" else "Ross 308"
    system_msg = f"""You are an experienced Australian broiler grower assessing bird SIZE-VS-AGE from a single photograph. You do NOT guess raw weight — you compare the bird against the {breed_label} target for its age.

CONTEXT:
- Bird is {age_days} days old (grower-confirmed).
- Breed: {breed_label}.
- {breed_label} target weight at day {age_days}: {target:.3f} kg (as-hatched, on-farm).

YOUR JOB — return ONE JSON object, no markdown fences:
{{
  "sizeVsAge": "much_smaller" | "smaller" | "on_target" | "larger" | "much_larger",
  "confidenceLevel": "high" | "medium" | "low",
  "notes": "One short sentence: describe what you see (feathering stage, comb size, standing height, body fullness) and why you picked that size bucket."
}}

HOW TO PICK sizeVsAge — use these cues:
- Feather cover (down vs primary feathers vs full plumage) → maps to expected age look
- Comb + wattle visibility → larger + redder = mature bird
- Body fullness relative to leg length
- Overall postural presence in the frame vs. a bird you'd expect at day {age_days}

RULES:
- If the photo is blurry, dark, or clearly NOT a live broiler → confidenceLevel=low, sizeVsAge="on_target" (safe default), notes="photo unclear".
- confidenceLevel=high ONLY if the bird is clear, in-focus, on a plain background, filling most of the frame.
- Never invent a weight number — that job is done server-side from your sizeVsAge bucket.
""" + _lang_directive(payload)

    chat = LlmChat(
        api_key=api_key,
        session_id=f"weigh-bird-{uuid.uuid4().hex[:8]}",
        system_message=system_msg,
    ).with_model("gemini", "gemini-2.5-flash")

    # Offset applied to the target weight for each size bucket. Broilers vary
    # by roughly ±10% of target within a healthy flock, ±25% in outliers.
    OFFSETS = {
        "much_smaller": -0.25,
        "smaller":      -0.10,
        "on_target":     0.00,
        "larger":       +0.10,
        "much_larger":  +0.25,
    }

    try:
        msg = UserMessage(
            text=f"Assess this bird against the {breed_label} day-{age_days} target ({target:.3f} kg) and return JSON only.",
            file_contents=[ImageContent(image_base64=image_b64)],
        )
        raw = await chat.send_message(msg)
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1] if "\n" in text else text
            if text.endswith("```"): text = text[:-3]
        import json as _json
        start = text.find("{"); end = text.rfind("}")
        if start == -1 or end == -1:
            return {"ok": False, "estimatedWeightKg": None, "confidenceLevel": "low",
                    "notes": "AI did not return JSON", "targetKg": target, "breed": breed_label}
        data = _json.loads(text[start:end+1])
        bucket = (data.get("sizeVsAge") or "on_target").lower()
        offset = OFFSETS.get(bucket, 0.0)
        estimated = round(target * (1.0 + offset), 3)
        return {
            "ok": True,
            "estimatedWeightKg": estimated,
            "confidenceLevel": data.get("confidenceLevel", "medium"),
            "notes": data.get("notes", ""),
            "sizeVsAge": bucket,
            "targetKg": target,
            "breed": breed_label,
            "ageDays": age_days,
        }
    except Exception as e:
        return {"ok": False, "estimatedWeightKg": None, "confidenceLevel": "low",
                "notes": f"AI error: {str(e)[:120]}", "targetKg": target, "breed": breed_label}


@api.post("/weigh-bird-video")
async def weigh_bird_video(payload: dict):
    """Video-mode weigh — grower records ~60 s of birds walking around the
    shed on their phone. The client extracts 8–10 evenly-spaced still frames
    and posts them here as an array of base64 images. We send all frames to
    Gemini 2.5 Flash in a single multi-image call, so it can average across
    many birds and angles (way more robust than a single-photo assessment).

    Body: {
      framesBase64: [str, str, ...],           # 4–12 frames, JPEG/PNG, base64 (no data-URL prefix)
      ageDays: int,                            # bird age in days (mandatory)
      breed?: 'ross308' | 'cobb500',           # optional, default ross308
      shedNum?: int,
    }
    Returns: { ok, estimatedWeightKg, confidenceLevel, notes, sizeVsAge,
               targetKg, breed, ageDays, framesAnalysed }
    """
    frames = (payload or {}).get("framesBase64") or []
    if not isinstance(frames, list) or len(frames) < 3:
        raise HTTPException(400, "framesBase64 must be a list of at least 3 base64 images")
    if len(frames) > 12:
        frames = frames[:12]  # cap for Gemini token budget
    # strip data-URL prefixes if the client sent them
    frames = [
        (f.split(",", 1)[1] if isinstance(f, str) and f.startswith("data:") and "," in f else f)
        for f in frames if f
    ]
    age_days = (payload or {}).get("ageDays")
    if not age_days or not isinstance(age_days, (int, float)) or age_days < 1 or age_days > 60:
        raise HTTPException(400, "ageDays (1-60) required to anchor the estimate")
    age_days = int(round(age_days))
    breed = ((payload or {}).get("breed") or "ross308").lower()

    # Reuse the same Ross 308 / Cobb 500 target tables as /weigh-bird
    ROSS_308 = {
        1:0.049, 2:0.065, 3:0.085, 4:0.108, 5:0.135, 6:0.166, 7:0.205,
        8:0.245, 9:0.291, 10:0.343, 11:0.398, 12:0.457, 13:0.520, 14:0.586,
        15:0.655, 16:0.727, 17:0.803, 18:0.881, 19:0.862, 20:0.945, 21:1.012,
        22:1.099, 23:1.188, 24:1.279, 25:1.371, 26:1.464, 27:1.558, 28:1.616,
        29:1.748, 30:1.843, 31:1.938, 32:2.033, 33:2.128, 34:2.222, 35:2.296,
        36:2.408, 37:2.500, 38:2.591, 39:2.681, 40:2.769, 41:2.855, 42:2.998,
        43:3.070, 44:3.140, 45:3.210, 46:3.278, 47:3.346, 48:3.414, 49:3.480,
        50:3.545, 51:3.610, 52:3.674, 53:3.737, 54:3.799, 55:3.860, 56:3.920,
        57:3.980, 58:4.038, 59:4.096, 60:4.152,
    }
    COBB_500 = {1:0.052, 7:0.210, 14:0.597, 21:1.158, 28:1.840, 35:2.529, 42:3.020, 49:3.500, 56:3.940}
    if breed == "cobb500":
        known = sorted(COBB_500.keys())
        if age_days <= known[0]:
            target = COBB_500[known[0]]
        elif age_days >= known[-1]:
            target = COBB_500[known[-1]]
        else:
            for i in range(len(known)-1):
                if known[i] <= age_days <= known[i+1]:
                    lo, hi = known[i], known[i+1]
                    frac = (age_days - lo) / (hi - lo)
                    target = COBB_500[lo] + frac * (COBB_500[hi] - COBB_500[lo])
                    break
    else:
        target = ROSS_308.get(age_days) or ROSS_308[min(ROSS_308.keys(), key=lambda k: abs(k - age_days))]
    target = round(target, 3)

    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(503, "LLM key not configured")
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    except Exception as e:
        raise HTTPException(503, f"LLM lib missing: {e}")

    breed_label = "Cobb 500" if breed == "cobb500" else "Ross 308"
    system_msg = f"""You are an experienced Australian broiler grower assessing bird SIZE-VS-AGE from a short video clip (delivered as {len(frames)} evenly-spaced still frames from a ~60 second recording of birds walking around a broiler shed).

CONTEXT:
- All birds in the frames are {age_days} days old (grower-confirmed).
- Breed: {breed_label}.
- {breed_label} target weight at day {age_days}: {target:.3f} kg (as-hatched, on-farm).
- Because you're seeing MANY birds across MANY frames, your answer should reflect the AVERAGE of the flock, not any single bird.

YOUR JOB — return ONE JSON object, no markdown fences, no code blocks:
{{
  "sizeVsAge": "much_smaller" | "smaller" | "on_target" | "larger" | "much_larger",
  "confidenceLevel": "high" | "medium" | "low",
  "notes": "One short sentence: what you see across the frames + why you picked that size bucket. Reference visual cues like feathering stage, comb size, body fullness, standing height."
}}

HOW TO PICK sizeVsAge:
- Compare typical bird body-fullness and feather-cover across the frames against what a healthy day-{age_days} {breed_label} bird should look like
- "on_target" ≈ within ±5% of target ({target:.3f} kg)
- "smaller" ≈ 10% under · "much_smaller" ≈ 25% under
- "larger" ≈ 10% over · "much_larger" ≈ 25% over

CONFIDENCE RULES:
- high: birds are clear, in-focus, multiple frames show good detail, at least 3 birds visible
- medium: some blur or dim frames but the flock's general size is readable
- low: video is too dark, too blurry, or shows something other than broilers → return sizeVsAge="on_target"

Never invent a raw kg number — the server computes it from your sizeVsAge bucket + target.
""" + _lang_directive(payload)

    chat = LlmChat(
        api_key=api_key,
        session_id=f"weigh-video-{uuid.uuid4().hex[:8]}",
        system_message=system_msg,
    ).with_model("gemini", "gemini-2.5-flash")

    OFFSETS = {
        "much_smaller": -0.25, "smaller": -0.10, "on_target": 0.00,
        "larger": +0.10, "much_larger": +0.25,
    }

    try:
        msg = UserMessage(
            text=f"Assess the flock size vs the {breed_label} day-{age_days} target ({target:.3f} kg). Return JSON only.",
            file_contents=[ImageContent(image_base64=b) for b in frames],
        )
        raw = await chat.send_message(msg)
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1] if "\n" in text else text
            if text.endswith("```"):
                text = text[:-3]
        import json as _json
        start = text.find("{"); end = text.rfind("}")
        if start == -1 or end == -1:
            return {"ok": False, "estimatedWeightKg": None, "confidenceLevel": "low",
                    "notes": "AI did not return JSON", "targetKg": target, "breed": breed_label,
                    "framesAnalysed": len(frames)}
        data = _json.loads(text[start:end+1])
        bucket = (data.get("sizeVsAge") or "on_target").lower()
        offset = OFFSETS.get(bucket, 0.0)
        estimated = round(target * (1.0 + offset), 3)
        return {
            "ok": True,
            "estimatedWeightKg": estimated,
            "confidenceLevel": data.get("confidenceLevel", "medium"),
            "notes": data.get("notes", ""),
            "sizeVsAge": bucket,
            "targetKg": target,
            "breed": breed_label,
            "ageDays": age_days,
            "framesAnalysed": len(frames),
        }
    except Exception as e:
        return {"ok": False, "estimatedWeightKg": None, "confidenceLevel": "low",
                "notes": f"AI error: {str(e)[:140]}", "targetKg": target,
                "breed": breed_label, "framesAnalysed": len(frames)}




@api.post("/read-scale")
async def read_scale(payload: dict):
    """Read the weight shown on a farm scale (digital LCD or analogue needle).

    Body: { imageBase64: str, mimeType?: str, shedNum?: int, language?: str }
    Returns: { ok, weightKg, rawReading, unit, isAnalogue, confidenceLevel, notes }
    """
    image_b64 = (payload or {}).get("imageBase64")
    if not image_b64:
        raise HTTPException(400, "imageBase64 required")
    if "," in image_b64 and image_b64.startswith("data:"):
        image_b64 = image_b64.split(",", 1)[1]
    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(503, "LLM key not configured")
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    except Exception as e:
        raise HTTPException(503, f"LLM lib missing: {e}")

    system_msg = """You are reading a farm-scale weight display in a broiler shed. Return ONLY a JSON object, no markdown, no code fences.

The scale may be:
  • Digital LCD — a numeric display like "1.850", "1850", "2.415".
  • Analogue needle dial — a physical pointer on a printed scale of numbers.

The reading may be in kilograms (e.g. 1.850) OR grams (e.g. 1850). Detect which by looking for "kg" or "g" text near the number, OR by the magnitude (a broiler weighs 0.05–5 kg; if the number is 50–5000 with no decimal, it's almost certainly grams).

Always normalise the final answer to kilograms.

JSON schema:
{
  "rawReading": <string — exactly what you see, e.g. "1.850" or "1850">,
  "unit": "kg" | "g",
  "isAnalogue": <bool>,
  "weightKg": <float, 3 decimals — the reading converted to kg>,
  "confidenceLevel": "high" | "medium" | "low",
  "notes": "One short sentence: what you saw + any concerns (glare, blur, needle-between-marks, etc.)"
}

If the photo is not clearly a scale reading OR the number is unreadable, return:
{ "rawReading": null, "unit": null, "isAnalogue": false, "weightKg": null, "confidenceLevel": "low", "notes": "Scale reading not detected" }
""" + _lang_directive(payload)

    chat = LlmChat(
        api_key=api_key,
        session_id=f"read-scale-{uuid.uuid4().hex[:8]}",
        system_message=system_msg,
    ).with_model("gemini", "gemini-2.5-flash")
    try:
        msg = UserMessage(
            text="Read the weight on this scale and return JSON only.",
            file_contents=[ImageContent(image_base64=image_b64)],
        )
        raw = await chat.send_message(msg)
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1] if "\n" in text else text
            if text.endswith("```"): text = text[:-3]
        import json as _json
        start = text.find("{"); end = text.rfind("}")
        if start == -1 or end == -1:
            return {"ok": False, "weightKg": None, "confidenceLevel": "low", "notes": "AI did not return JSON"}
        data = _json.loads(text[start:end+1])
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "weightKg": None, "confidenceLevel": "low", "notes": f"AI error: {str(e)[:120]}"}


@api.post("/count-chicks")
async def count_chicks(payload: dict):
    """Count day-old chicks visible in a crate photo using Gemini vision.

    Body: { imageBase64: str, mimeType?: str, cratesInPhoto?: int }
    Returns: { ok, count, confidenceLevel: 'high'|'medium'|'low', notes }
    """
    image_b64 = (payload or {}).get("imageBase64")
    if not image_b64:
        raise HTTPException(400, "imageBase64 required")
    if "," in image_b64 and image_b64.startswith("data:"):
        image_b64 = image_b64.split(",", 1)[1]
    crates_hint = (payload or {}).get("cratesInPhoto")
    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(503, "LLM key not configured")
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    except Exception as e:
        raise HTTPException(503, f"LLM lib missing: {e}")

    crate_line = f"The photo shows {crates_hint} crate(s). Australian chick crates are typically packed at 100 chicks per crate — use that as a sanity anchor." if crates_hint else "If you can identify the number of crates, note it. Australian chick crates typically hold 100 chicks each."
    system_msg = f"""You are counting day-old broiler chicks in a crate photograph from an Australian poultry farm. Return ONLY a JSON object, no markdown.

{crate_line}

JSON schema:
{{
  "count": <integer, best-estimate total chicks visible>,
  "cratesDetected": <integer or null>,
  "confidenceLevel": "high" | "medium" | "low",
  "notes": "One short sentence: counting method + what makes this confidence rating"
}}

Counting method: try to detect distinct chicks. Where chicks overlap, estimate. Where they are so densely packed that individual counting is impossible, use crateCount × 100 as a sanity estimate and set confidenceLevel to "medium".

If the photo does NOT show chicks, return:
{{ "count": 0, "cratesDetected": null, "confidenceLevel": "low", "notes": "No chicks detected in photo" }}
""" + _lang_directive(payload)
    chat = LlmChat(
        api_key=api_key,
        session_id=f"count-chicks-{uuid.uuid4().hex[:8]}",
        system_message=system_msg,
    ).with_model("gemini", "gemini-2.5-flash")
    try:
        msg = UserMessage(
            text="Count the day-old chicks visible in this photo and return JSON only.",
            file_contents=[ImageContent(image_base64=image_b64)],
        )
        raw = await chat.send_message(msg)
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1] if "\n" in text else text
            if text.endswith("```"): text = text[:-3]
        import json as _json
        start = text.find("{"); end = text.rfind("}")
        if start == -1 or end == -1:
            return {"ok": False, "count": None, "confidenceLevel": "low", "notes": "AI did not return JSON"}
        data = _json.loads(text[start:end+1])
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "count": None, "confidenceLevel": "low", "notes": f"AI error: {str(e)[:120]}"}


@api.post("/parse-mort-sheet")
async def parse_mort_sheet(payload: dict):
    """OCR an Australian broiler mort sheet photo and return per-shed dead-bird & cull counts for TODAY.

    Body: { imageBase64: str, mimeType?: str, totalSheds?: int }
    Returns: { ok, date?, sheds: [{shed:int, morts:int, culls:int}], confidenceLevel, notes }
    """
    image_b64 = (payload or {}).get("imageBase64")
    if not image_b64:
        raise HTTPException(400, "imageBase64 required")
    if "," in image_b64 and image_b64.startswith("data:"):
        image_b64 = image_b64.split(",", 1)[1]
    total_sheds = int((payload or {}).get("totalSheds") or 8)
    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(503, "LLM key not configured")
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    except Exception as e:
        raise HTTPException(503, f"LLM lib missing: {e}")

    system_msg = f"""You are reading a paper mortality (mort) sheet from an Australian broiler poultry farm. The farm has {total_sheds} sheds numbered 1..{total_sheds}.

The mort sheet is a hand-written or printed daily log where the grower records DEAD BIRDS (morts) and CULLED BIRDS per shed per day.

Common layouts:
- Column per shed (Shed 1, Shed 2, ...) with rows for each day. Values may be split as "morts/culls" or in separate M and C columns.
- Row per shed with columns for each day of the batch.
- Free-form paper docket with shed# and death count scribbled next to each other.

Return ONLY a JSON object, no markdown. Extract the MOST RECENT day visible (the latest date on the sheet, or the last non-empty row).

JSON schema:
{{
  "date": "YYYY-MM-DD or null if not readable",
  "sheds": [
    {{"shed": <int 1..{total_sheds}>, "morts": <int>, "culls": <int>}}
  ],
  "confidenceLevel": "high" | "medium" | "low",
  "notes": "One short sentence describing what you extracted and any uncertainty."
}}

Rules:
- Include ONE entry per shed you can see, even if the value is 0.
- If a value is unreadable, set it to 0 and lower the confidenceLevel.
- If the photo is NOT a mort sheet, return {{"date": null, "sheds": [], "confidenceLevel": "low", "notes": "Photo does not appear to be a mort sheet"}}.
- morts = dead birds found in the shed. culls = birds humanely culled. Both are separate columns/values on the sheet.
""" + _lang_directive(payload)
    chat = LlmChat(
        api_key=api_key,
        session_id=f"parse-mort-sheet-{uuid.uuid4().hex[:8]}",
        system_message=system_msg,
    ).with_model("gemini", "gemini-2.5-flash")
    try:
        msg = UserMessage(
            text=f"Extract today's per-shed morts and culls from this mort sheet photo. Farm has {total_sheds} sheds. Return JSON only.",
            file_contents=[ImageContent(image_base64=image_b64)],
        )
        raw = await chat.send_message(msg)
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1] if "\n" in text else text
            if text.endswith("```"): text = text[:-3]
        import json as _json
        start = text.find("{"); end = text.rfind("}")
        if start == -1 or end == -1:
            return {"ok": False, "sheds": [], "confidenceLevel": "low", "notes": "AI did not return JSON"}
        data = _json.loads(text[start:end+1])
        # Sanitise
        cleaned = []
        for row in (data.get("sheds") or []):
            try:
                s = int(row.get("shed"))
                m = int(row.get("morts") or 0)
                c = int(row.get("culls") or 0)
                if 1 <= s <= total_sheds and m >= 0 and c >= 0:
                    cleaned.append({"shed": s, "morts": m, "culls": c})
            except Exception:
                continue
        return {
            "ok": True,
            "date": data.get("date"),
            "sheds": cleaned,
            "confidenceLevel": data.get("confidenceLevel") or "medium",
            "notes": data.get("notes") or "",
        }
    except Exception as e:
        return {"ok": False, "sheds": [], "confidenceLevel": "low", "notes": f"AI error: {str(e)[:120]}"}



@api.post("/bootstrap/first-operator")
async def first_operator():
    return {"ok": True}


# ── Previous reading lookup (per silo) ───────────────────────────────────
@api.get("/readings/previous")
async def previous_reading(siloId: str = Query(...)):
    """Most recent reading for a given silo (excludes today)."""
    today_start, _ = aest_today_range()
    row = await readings_col.find_one(
        {"siloId": siloId, "readingDate": {"$lt": today_start}},
        sort=[("readingDate", -1)],
    )
    if row is None:
        return {"siloId": siloId, "amountRemaining": None, "feedType": None, "unit": None, "readingDate": None}
    return {
        "siloId": siloId,
        "amountRemaining": float(row["amountRemaining"]),
        "feedType": row["feedType"],
        "unit": row["unit"],
        "readingDate": row["readingDate"].isoformat(),
    }


# ── Docket scanner (Ingham/Baiada) ───────────────────────────────────────
class ScanDocketBody(BaseModel):
    imageData: str
    mimeType: str
    docketType: Optional[str] = "ingham"  # "ingham" | "baiada"


INGHAM_PROMPT = """You are reading an Ingham's Despatch Docket — an Australian poultry feed delivery document.

Extract these fields and return ONLY a valid JSON object (no markdown, no explanation):

{
  "supplier": "Ingham's",
  "feedType": "Product name only, without product code — e.g. 'Gourmet Broiler Grower' from 'F116 GOURMET BROILER GROWER'. Capitalise properly.",
  "productCode": "The product code, e.g. 'F116'",
  "amount": <Net Weight in tonnes as a number, e.g. 28.16>,
  "unit": "t",
  "deliveryDate": "The 'Date Req' field as YYYY-MM-DD, e.g. '2026-05-18'",
  "orderNumber": "The Order No / Dispatch No / Ticket No value",
  "customerName": "The Customer Name",
  "siteCode": "The Site code",
  "deliveryInstructions": "The Delivery Instructions text exactly as printed, e.g. '5 B 10, 6 B 5, 7 B 13'",
  "truckRego": "The Truck Rego value",
  "outloadingBin": "The Outloading Bin value"
}

Use null for any field you cannot read clearly."""


BAIADA_PROMPT = """You are reading a Baiada feed delivery docket — an Australian poultry feed delivery document.

Extract these fields and return ONLY a valid JSON object (no markdown, no explanation):

{
  "supplier": "Baiada",
  "feedType": "Product name (e.g. 'Starter', 'Grower', 'Finisher', 'Withdrawal')",
  "productCode": "Product code if shown, else null",
  "amount": <Net Weight in tonnes as a number>,
  "unit": "t",
  "deliveryDate": "Delivery date as YYYY-MM-DD",
  "orderNumber": "The order/docket number",
  "customerName": "Farm/customer name",
  "siteCode": "Site code if shown, else null",
  "deliveryInstructions": "Shed/silo allocation text, e.g. '3A 12t, 3B 16t'",
  "truckRego": "Truck registration if shown",
  "outloadingBin": "Outloading bin if shown"
}

Use null for any field you cannot read clearly."""


AUTO_PROMPT = """You are reading an Australian poultry feed delivery docket. The docket may come from any of these suppliers:
- Ingham's ("Despatch Docket")
- Baiada
- BPL Adelaide Pty Ltd
- Multiquip / other feed mills

Identify the supplier from the heading, then extract these fields. Return ONLY a valid JSON object (no markdown, no commentary):

{
  "supplier": "Best-fit supplier name from the docket header. Use 'Ingham\\'s' / 'Baiada' / 'BPL Adelaide' or whatever brand is printed at the top.",
  "feedType": "Product / Formula Name only, properly capitalised (e.g. 'Broiler Grower', 'Gourmet Broiler Grower', 'Starter', 'Finisher'). Strip product codes from the name.",
  "productCode": "Product code or Feed Formula No (e.g. 'F116', 'S110')",
  "amount": <Net Weight as a number IN TONNES. If the docket shows kilograms (e.g. '43,740 Kg'), divide by 1000 to convert to tonnes (43.74). If already in tonnes (e.g. '28.16 tonnes'), use as-is.>,
  "unit": "t",
  "deliveryDate": "Delivery / Date Req / Ticket date as YYYY-MM-DD. Australian dates are DD/MM/YYYY (or DD/MM/YY where YY is 20YY). e.g. '02/04/26' = '2026-04-02', '18/05/2026' = '2026-05-18'.",
  "orderNumber": "Best single identifier — prefer 'Ticket No' if present, else 'Dispatch No', else 'Order No', else 'Despatch No'.",
  "customerName": "Customer / Farm Name (e.g. 'Double B Farm', 'GP FARMS PTY LIMITED')",
  "siteCode": "Site code / Destination Flock / Account number",
  "deliveryInstructions": "Shed-silo allocation text exactly as written (e.g. '5 B 10, 6 B 5, 7 B 13'). If absent, use null.",
  "truckRego": "Truck Rego / Vehicle No (the registration plate, e.g. 'SB84EF', 'XS07IQ')",
  "outloadingBin": "Outloading Bin value (e.g. 'BN8103'). If absent, use null."
}

Important conversion rules:
- WEIGHT: If the docket shows 'Net' or 'Net Weight' in Kg (e.g. '43,740 Kg'), convert to tonnes by dividing by 1000.
- WEIGHT: If shown in tonnes (e.g. '28.16 tonnes'), use the number directly.
- DATE: Australian docket dates are DD/MM/YYYY. 2-digit years (DD/MM/YY) are 20YY.
- Use null for any field you genuinely cannot read."""


import base64
import json
import uuid as _uuid


@api.post("/scan-docket/ingham")
async def scan_docket_ingham(body: ScanDocketBody):
    return await _scan_docket(body, INGHAM_PROMPT)


@api.post("/scan-docket/baiada")
async def scan_docket_baiada(body: ScanDocketBody):
    return await _scan_docket(body, BAIADA_PROMPT)


# ── Farm Config ──────────────────────────────────────────────────────────
class FarmConfigBody(BaseModel):
    farmName: Optional[str] = None
    totalSheds: Optional[int] = None
    enabledGroupIds: Optional[List[str]] = None
    logoData: Optional[str] = None  # base64 PNG/JPEG, or empty string to reset


@api.get("/farm/trial-status")
async def get_farm_trial_status(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Lightweight trial/subscription status for the day-25/day-30 in-app nudge
    banners. Mar 2026 — Jason: banner-only, never locks anything; auto-hides
    once the Stripe webhook marks the farm subscriptionStatus 'active'."""
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    doc = await farms_col.find_one({"slug": farm}, {"_id": 0, "subscriptionStatus": 1, "trialStartedAt": 1, "trialExpiresAt": 1})
    if not doc:
        return {"subscriptionStatus": None, "trialStartedAt": None, "trialExpiresAt": None}
    def _iso_utc(dt):
        # Mongo stores naive UTC datetimes — stamp tzinfo back on before
        # formatting so the client never mis-parses this as local time.
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return {
        "subscriptionStatus": doc.get("subscriptionStatus"),
        "trialStartedAt": _iso_utc(doc.get("trialStartedAt")),
        "trialExpiresAt": _iso_utc(doc.get("trialExpiresAt")),
    }


@api.get("/farm-config")
async def get_farm_config(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    doc = await farm_config_col.find_one({"id": farm})
    if not doc:
        # Back-compat: migrate the legacy single-tenant "default" config if it has the old id="default" and no farmId
        if farm == DEFAULT_FARM_ID:
            legacy = await farm_config_col.find_one({"id": "default"})
            if legacy:
                return clean(legacy)
        all_groups = await shed_groups_col.find(_farm_filter(farm)).to_list(length=200)
        doc = {
            "id": farm,
            "farmId": farm,
            "farmName": "My Farm" if farm != DEFAULT_FARM_ID else "Double B Farm",
            "totalSheds": 20,
            "enabledGroupIds": [g["id"] for g in all_groups],
        }
        await farm_config_col.insert_one(doc)
    return clean(doc)


@api.get("/farm-token")
async def get_farm_token(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Lets the already-logged-in owner fetch their own farm's SEC-006 token,
    to build the Staff QR link for /morts-entry (staff never gets a login,
    just this farm's own secret token embedded in the QR). Session-only —
    see _require_owner_session."""
    await _require_owner_session(request, farm)
    doc = await farms_col.find_one({"slug": farm}, {"farmToken": 1})
    if not doc:
        raise HTTPException(404, "Farm not found")
    return {"token": doc.get("farmToken", "")}


@api.patch("/farm-config")
async def patch_farm_config(body: FarmConfigBody, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-001
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not patch:
        raise HTTPException(400, "Nothing to update")
    # Treat empty logoData as "reset to default" (remove the field)
    unset: dict = {}
    if "logoData" in patch and patch["logoData"] == "":
        unset["logoData"] = ""
        del patch["logoData"]
    op: dict = {"$setOnInsert": {"id": farm, "farmId": farm}}
    if patch: op["$set"] = patch
    if unset: op["$unset"] = unset
    await farm_config_col.update_one({"id": farm}, op, upsert=True)
    doc = await farm_config_col.find_one({"id": farm})
    return clean(doc)


# ── Feed-Program State (placement dates, bird counts, mortality — all spreadsheet edits) ───
# Persists the React Feed-Program's `edits` map (serialized) + sheet names to MongoDB
# per farm, so placement dates and batch info survive computer shutdown / different browser.
# The state is kept as an opaque string blob; the frontend serializes/deserializes it.
class FeedProgramStateBody(BaseModel):
    edits: str  # serializeEdits() output — opaque JSON string of cell edits per sheet
    sheetNames: List[str]
    # Client can set to True to bypass the anti-corruption guard (e.g. a legitimate
    # "reset for new batch" that intentionally clears all cells).
    forceOverwrite: Optional[bool] = False


# Anti-corruption threshold: reject writes that drop more than this fraction of
# the previous state's byte size, unless forceOverwrite=True. 30 % catches most
# accidental hydration-bug wipes (where 50–100 % of cells vanish) while allowing
# ordinary batch progression (users add / edit / occasionally delete cells).
_FEED_STATE_CORRUPTION_THRESHOLD = 0.30
_FEED_STATE_MIN_SIZE_TO_GUARD = 500  # bytes — don't guard tiny new farms
_FEED_STATE_HISTORY_LIMIT = 30  # keep last 30 snapshots per farm


@api.get("/feed-program/state")
async def get_feed_program_state(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    doc = await feed_program_state_col.find_one({"farmId": farm})
    if not doc:
        return {"edits": None, "sheetNames": None, "updatedAt": None}
    return {
        "edits": doc.get("edits"),
        "sheetNames": doc.get("sheetNames") or [],
        "updatedAt": doc.get("updatedAt"),
    }


@api.put("/feed-program/state")
async def put_feed_program_state(body: FeedProgramStateBody, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-001
    now = datetime.now(timezone.utc).isoformat()

    # ── Anti-corruption guard ────────────────────────────────────────────────
    # If the incoming edits string is dramatically smaller than what's currently
    # stored, refuse the write and return a 409. Prevents silent data-loss from
    # hydration bugs or race conditions in the client. Client can retry with
    # forceOverwrite=True (used for legitimate "new batch" resets).
    existing = await feed_program_state_col.find_one({"farmId": farm})
    existing_edits = (existing or {}).get("edits") or ""
    existing_size = len(existing_edits)
    new_size = len(body.edits or "")
    if (
        not body.forceOverwrite
        and existing_size >= _FEED_STATE_MIN_SIZE_TO_GUARD
        and new_size < existing_size * (1 - _FEED_STATE_CORRUPTION_THRESHOLD)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "corruption_guard",
                "message": (
                    "Save blocked: incoming state is much smaller than the "
                    "current saved state. This usually means the browser hasn't "
                    "finished loading. Reload the page and try again — if the "
                    "problem persists, use Cloud Rewind to restore an earlier "
                    "snapshot."
                ),
                "existingSize": existing_size,
                "incomingSize": new_size,
                "dropPct": round((1 - new_size / existing_size) * 100, 1),
            },
        )

    # ── Capture existing state to history BEFORE overwriting ─────────────────
    # Only snapshot if there's something worth saving (non-empty existing state)
    # and the incoming write is meaningfully different. Skips duplicate saves.
    if existing_edits and existing_edits != (body.edits or ""):
        try:
            await feed_program_state_history_col.insert_one({
                "id": str(uuid.uuid4()),
                "farmId": farm,
                "edits": existing_edits,
                "sheetNames": existing.get("sheetNames") or [],
                "capturedAt": now,  # ISO string — matches getFeedProgramState updatedAt
                "capturedSize": existing_size,
                "reason": "pre-write-backup",
            })
            # Trim history to the last N snapshots for this farm.
            all_snaps = await feed_program_state_history_col.find({"farmId": farm}).sort("capturedAt", -1).to_list(length=200)
            if len(all_snaps) > _FEED_STATE_HISTORY_LIMIT:
                overflow_ids = [s["_id"] for s in all_snaps[_FEED_STATE_HISTORY_LIMIT:]]
                await feed_program_state_history_col.delete_many({"_id": {"$in": overflow_ids}})
        except Exception:
            # History is a safety net — never fail the write because of it.
            pass

    await feed_program_state_col.update_one(
        {"farmId": farm},
        {"$set": {"edits": body.edits, "sheetNames": body.sheetNames, "updatedAt": now},
         "$setOnInsert": {"farmId": farm}},
        upsert=True,
    )
    return {"ok": True, "updatedAt": now}


# ── Catches cloud sync ────────────────────────────────────────────────────
# Mar 2026 — Jason: "Save imported pickup catches to the server so they
# survive a new phone or browser." catchMap/weighPlanMap previously lived in
# localStorage only (BATCH_CATCHES_KEY / WEIGH_PLAN_KEY) — this mirrors them
# to Mongo the same way /api/feed-program/state already does for edits.
class FeedProgramCatchesBody(BaseModel):
    catchMap: Dict[str, Any] = {}
    weighPlanMap: Dict[str, Any] = {}


@api.get("/feed-program/catches")
async def get_feed_program_catches(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    doc = await feed_program_catches_col.find_one({"farmId": farm})
    if not doc:
        return {"catchMap": {}, "weighPlanMap": {}, "updatedAt": None}
    return {
        "catchMap": doc.get("catchMap") or {},
        "weighPlanMap": doc.get("weighPlanMap") or {},
        "updatedAt": doc.get("updatedAt"),
    }


@api.put("/feed-program/catches")
async def put_feed_program_catches(body: FeedProgramCatchesBody, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-001
    now = datetime.now(timezone.utc).isoformat()
    await feed_program_catches_col.update_one(
        {"farmId": farm},
        {"$set": {"catchMap": body.catchMap, "weighPlanMap": body.weighPlanMap, "updatedAt": now},
         "$setOnInsert": {"farmId": farm}},
        upsert=True,
    )
    return {"ok": True, "updatedAt": now}


# ── Feed-program history (Cloud Rewind) ──────────────────────────────────
# Server-side snapshot list — always available regardless of which device / browser
# the user is on. Restores are non-destructive: the current state is snapshotted
# before it's replaced so an accidental rewind is itself rewind-able.


@api.get("/feed-program/history")
async def list_feed_program_history(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    """Return metadata for up to 30 recent snapshots (excludes the edits blob to keep response small)."""
    snaps = await feed_program_state_history_col.find(
        {"farmId": farm},
        {"edits": 0},  # exclude blob
    ).sort("capturedAt", -1).limit(_FEED_STATE_HISTORY_LIMIT).to_list(length=_FEED_STATE_HISTORY_LIMIT)
    return [
        {
            "id": s.get("id"),
            "capturedAt": s.get("capturedAt"),
            "capturedSize": s.get("capturedSize"),
            "sheetNames": s.get("sheetNames") or [],
            "reason": s.get("reason") or "pre-write-backup",
        }
        for s in snaps
    ]


@api.get("/feed-program/history/{snap_id}")
async def get_feed_program_history_item(snap_id: str, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    """Return the full snapshot (edits + sheetNames) so the client can preview or restore."""
    snap = await feed_program_state_history_col.find_one({"id": snap_id, "farmId": farm})
    if not snap:
        raise HTTPException(404, "Snapshot not found")
    return {
        "id": snap.get("id"),
        "capturedAt": snap.get("capturedAt"),
        "edits": snap.get("edits"),
        "sheetNames": snap.get("sheetNames") or [],
    }


@api.post("/feed-program/history/{snap_id}/restore")
async def restore_feed_program_history(snap_id: str, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Restore a snapshot as the current state. Snapshots current state first so the
    restore itself is rewind-able. Returns the restored state so the client can
    immediately re-hydrate without a second GET."""
    await _require_farm_access(request, farm)   # SEC-001
    snap = await feed_program_state_history_col.find_one({"id": snap_id, "farmId": farm})
    if not snap:
        raise HTTPException(404, "Snapshot not found")
    now = datetime.now(timezone.utc).isoformat()
    existing = await feed_program_state_col.find_one({"farmId": farm})
    existing_edits = (existing or {}).get("edits") or ""
    if existing_edits:
        try:
            await feed_program_state_history_col.insert_one({
                "id": str(uuid.uuid4()),
                "farmId": farm,
                "edits": existing_edits,
                "sheetNames": existing.get("sheetNames") or [],
                "capturedAt": now,
                "capturedSize": len(existing_edits),
                "reason": "pre-rewind-backup",
            })
        except Exception:
            pass
    await feed_program_state_col.update_one(
        {"farmId": farm},
        {"$set": {
            "edits": snap.get("edits") or "",
            "sheetNames": snap.get("sheetNames") or [],
            "updatedAt": now,
        }, "$setOnInsert": {"farmId": farm}},
        upsert=True,
    )
    return {
        "ok": True,
        "restoredFrom": snap.get("capturedAt"),
        "edits": snap.get("edits"),
        "sheetNames": snap.get("sheetNames") or [],
        "updatedAt": now,
    }


# ── Photos (Mort Sheet + Bird Weight) ────────────────────────────────────
class CreatePhotoBody(BaseModel):
    category: str  # "mort_sheet" | "bird_weight"
    shedNumber: Optional[int] = None  # for bird_weight only
    imageData: str  # base64 jpeg
    notes: Optional[str] = None


@api.get("/photos")
async def list_photos(request: Request, category: Optional[str] = None, shedNumber: Optional[int] = None, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-006 read isolation
    q: dict = dict(_farm_filter(farm))
    if category:
        q = _and(q, {"category": category})
    if shedNumber is not None:
        q = _and(q, {"shedNumber": shedNumber})
    rows = await photos_col.find(q).sort("createdAt", -1).limit(200).to_list(length=200)
    return [clean(r) for r in rows]


@api.post("/photos", status_code=201)
async def create_photo(body: CreatePhotoBody, request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    await _require_farm_access(request, farm)   # SEC-001
    if body.category not in ("mort_sheet", "bird_weight"):
        raise HTTPException(400, "Invalid category")
    if body.category == "bird_weight" and body.shedNumber is None:
        raise HTTPException(400, "shedNumber required for bird_weight")
    doc = {
        "id": str(uuid.uuid4()),
        "farmId": farm,
        "category": body.category,
        "shedNumber": body.shedNumber,
        "imageData": body.imageData,
        "notes": body.notes,
        "createdAt": datetime.now(timezone.utc),
    }
    await photos_col.insert_one(doc)
    return clean(doc)


@api.delete("/photos/{photo_id}", status_code=204)
async def delete_photo(photo_id: str, request: Request):
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    existing = await photos_col.find_one({"id": photo_id}, {"farmId": 1, "_id": 0})
    if existing is None:
        raise HTTPException(404, "Photo not found")
    await _require_farm_access(request, existing.get("farmId") or DEFAULT_FARM_ID)   # SEC-001
    res = await photos_col.delete_one({"id": photo_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Photo not found")
    return JSONResponse(content=None, status_code=204)


@api.post("/scan-docket/auto")
async def scan_docket_auto(body: ScanDocketBody):
    """Auto-detect supplier (Ingham's, Baiada, BPL Adelaide, etc.) and extract."""
    return await _scan_docket(body, AUTO_PROMPT)


async def _scan_docket(body: "ScanDocketBody", prompt: str):
    """Extract structured fields from a docket photo using Gemini multimodal."""
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    except Exception as e:
        raise HTTPException(503, f"AI integration not installed: {e}")

    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(503, "EMERGENT_LLM_KEY not configured")

    chat = LlmChat(
        api_key=api_key,
        session_id=f"docket-{_uuid.uuid4().hex}",
        system_message="You are an OCR + extraction assistant. Return ONLY a JSON object — no markdown, no commentary.",
    ).with_model("gemini", "gemini-2.5-flash")

    image = ImageContent(image_base64=body.imageData)
    user_msg = UserMessage(text=prompt, file_contents=[image])

    try:
        raw = await chat.send_message(user_msg)
    except Exception as e:
        raise HTTPException(500, f"AI call failed: {e}")

    text = raw if isinstance(raw, str) else getattr(raw, "text", str(raw))
    cleaned = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if not m:
            raise HTTPException(422, "Could not parse AI response")
        parsed = json.loads(m.group(0))

    return {"ok": True, "fields": parsed}


app.include_router(api)


# ─── Stripe Checkout ─────────────────────────────────────────────────────
# Server-side fixed packages — frontend must NEVER send amounts.
#
# The Ops Manager bundle is priced dynamically (see `_price_ops_bundle`
# below) so its `amount` here is only a legacy fallback for callers that
# somehow POST without a `farms` array.
PACKAGES = {
    # Standard broiler subscription plans (AUD, monthly, LIVE Stripe)
    "bronze_monthly":      {"label": "Bronze",   "amount": 20.0,  "kind": "subscription"},
    "silver_monthly":      {"label": "Silver",   "amount": 25.0,  "kind": "subscription"},
    "gold_monthly":        {"label": "Gold",     "amount": 30.0,  "kind": "subscription"},
    "platinum_monthly":    {"label": "Platinum", "amount": 40.0,  "kind": "subscription"},
    # Annual variants (~15% off, ~10 months for the price of 12)
    "bronze_annual":       {"label": "Bronze Annual",   "amount": 204.0,  "kind": "subscription"},
    "silver_annual":       {"label": "Silver Annual",   "amount": 255.0,  "kind": "subscription"},
    "gold_annual":         {"label": "Gold Annual",     "amount": 306.0,  "kind": "subscription"},
    "platinum_annual":     {"label": "Platinum Annual", "amount": 408.0,  "kind": "subscription"},
    # Operation Manager Pack — multi-farm bundles (priced dynamically from `farms` list).
    # The `amount` here is a legacy fallback only.
    "ops_bronze":          {"label": "Ops Manager Pack",  "amount": 20.0, "kind": "ops_bundle"},
    "ops_silver":          {"label": "Ops Manager Pack",  "amount": 25.0, "kind": "ops_bundle"},
    "ops_gold":            {"label": "Ops Manager Pack",  "amount": 30.0, "kind": "ops_bundle"},
    "ops_platinum":        {"label": "Ops Manager Pack",  "amount": 40.0, "kind": "ops_bundle"},
    # Sponsor tiers
    "sponsor_10":          {"label": "Sponsor — $5/mo",   "amount": 5.0,   "kind": "sponsor"},
    "sponsor_25":          {"label": "Sponsor — $12.50/mo", "amount": 12.50, "kind": "sponsor"},
    "sponsor_50":          {"label": "Sponsor — $25/mo",  "amount": 25.0,  "kind": "sponsor"},
    # One-off donations
    "back_seed":           {"label": "Seed Supporter",       "amount": 50.0,   "kind": "donation"},
    "back_project":        {"label": "Project Backer",       "amount": 250.0,  "kind": "donation"},
    "back_foundation":     {"label": "Foundation Partner",   "amount": 500.0,  "kind": "donation"},
}

# Single source of truth for Ops-bundle per-farm pricing.
# Must stay in sync with the landing page's `TIERS` object at
# `/app/backend/static/landing.html` (search `const TIERS`). When one changes,
# update the other and add a test in `/app/backend/tests/test_ops_pricing.py`.
_OPS_TIER_PRICE = {"bronze": 20.0, "silver": 25.0, "gold": 30.0, "platinum": 40.0}


def _ops_volume_discount(n_farms: int) -> float:
    """Return the multiplier applied AFTER summing tier prices. Mirrors the
    landing-page `volumeDiscount()` percentages exactly."""
    if n_farms >= 10: return 0.75  # 25% off
    if n_farms >=  7: return 0.80  # 20% off
    if n_farms >=  4: return 0.85  # 15% off
    if n_farms >=  2: return 0.90  # 10% off
    return 1.0


def _price_ops_bundle(farms: List["CheckoutFarmConfig"], billing_period: str) -> float:
    """Deterministic server-side price for an Ops bundle.

    Formula (matches `computeTotal()` in landing.html):
        subtotal = sum(TIER_PRICES[f.tier] for f in farms)
        monthlyAfter = subtotal * volumeDiscountMultiplier(len(farms))
        total = monthlyAfter * (12 * 0.85 if billing_period == 'annual' else 1)
        total is rounded to the nearest whole dollar (same as landing rendering).
    """
    if not farms:
        return 0.0
    subtotal = sum(_OPS_TIER_PRICE.get((f.tier or "bronze").lower(), _OPS_TIER_PRICE["bronze"]) for f in farms)
    monthly_after = subtotal * _ops_volume_discount(len(farms))
    if billing_period == "annual":
        return float(round(monthly_after * 12 * 0.85))
    return float(round(monthly_after))


class CheckoutFarmConfig(BaseModel):
    name: str
    tier: Optional[str] = None  # "bronze" | "silver" | "gold" | "platinum"


class CheckoutRequest(BaseModel):
    packageId: str
    originUrl: str
    email: Optional[str] = None
    farms: Optional[List[CheckoutFarmConfig]] = None  # for ops_* bundles
    buyerName: Optional[str] = None
    ref: Optional[str] = None  # referral code (farm slug) of the grower who referred this buyer
    billingPeriod: Optional[str] = None  # "monthly" | "annual" — ops-bundle only


# ── True Stripe Subscriptions (mode="subscription") ─────────────────────
# The 4 tiers × {monthly, annual} = 8 recurring prices. `lookup_key` lets us
# fetch/create the Stripe Price idempotently on first checkout so we never
# hard-code Price IDs in env vars.
_SUB_PRICE_MAP = {
    # package_id           -> (product_name, unit_amount_cents, interval, lookup_key, plan_label)
    "bronze_monthly":   ("Broiler Base Mate — Bronze",   2000,  "month", "bbm_bronze_monthly_aud",   "Bronze"),
    "silver_monthly":   ("Broiler Base Mate — Silver",   2500,  "month", "bbm_silver_monthly_aud",   "Silver"),
    "gold_monthly":     ("Broiler Base Mate — Gold",     3000,  "month", "bbm_gold_monthly_aud",     "Gold"),
    "platinum_monthly": ("Broiler Base Mate — Platinum", 4000,  "month", "bbm_platinum_monthly_aud", "Platinum"),
    "bronze_annual":    ("Broiler Base Mate — Bronze",   20400, "year",  "bbm_bronze_annual_aud",    "Bronze"),
    "silver_annual":    ("Broiler Base Mate — Silver",   25500, "year",  "bbm_silver_annual_aud",    "Silver"),
    "gold_annual":      ("Broiler Base Mate — Gold",     30600, "year",  "bbm_gold_annual_aud",      "Gold"),
    "platinum_annual":  ("Broiler Base Mate — Platinum", 40800, "year",  "bbm_platinum_annual_aud",  "Platinum"),
}
# In-process cache of Stripe Price IDs by package_id — populated lazily.
_STRIPE_PRICE_CACHE: Dict[str, str] = {}


def _ensure_stripe_price(package_id: str) -> str:
    """Return the Stripe Price ID for a subscription package, creating the
    Product + recurring Price in Stripe on first request. Idempotent: uses
    `lookup_key` so we never create duplicates across restarts.
    """
    if package_id in _STRIPE_PRICE_CACHE:
        return _STRIPE_PRICE_CACHE[package_id]
    if package_id not in _SUB_PRICE_MAP:
        raise HTTPException(400, f"Unknown subscription package: {package_id}")
    product_name, amount, interval, lookup_key, plan_label = _SUB_PRICE_MAP[package_id]

    import stripe
    stripe.api_key = os.environ["STRIPE_API_KEY"]

    # 1) Look up existing price by lookup_key (idempotent)
    existing = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1, expand=["data.product"])
    if existing.data:
        price_id = existing.data[0].id
        _STRIPE_PRICE_CACHE[package_id] = price_id
        return price_id

    # 2) Create Product + Price
    product = stripe.Product.create(
        name=product_name,
        metadata={"plan": plan_label, "interval": interval, "app": "broilerbasemate"},
    )
    price = stripe.Price.create(
        product=product.id,
        unit_amount=amount,
        currency="aud",
        recurring={"interval": interval},
        lookup_key=lookup_key,
        nickname=f"{plan_label} {interval}",
        metadata={"plan": plan_label, "package_id": package_id},
    )
    _STRIPE_PRICE_CACHE[package_id] = price.id
    return price.id


@app.post("/api/farm/upgrade-checkout")
async def upgrade_farm_checkout(request: Request, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Real Stripe checkout for an EXISTING trial farm to convert to paid —
    Mar 2026, Jason: the landing-page tier buttons + trial-nudge "Upgrade Now"
    only ever opened the no-card trial signup again, so no customer could
    ever actually pay; nothing showed up in Stripe. This is the missing link:
    one click from inside the app → real Stripe Checkout → card charged NOW
    (no free-trial period here — the grower already had 30 free days) →
    webhook/status-poll flips this SAME farm to subscriptionStatus=active.
    """
    await _require_farm_access(request, farm)   # SEC-006
    api_key = os.environ.get("STRIPE_API_KEY")
    if not api_key:
        raise HTTPException(503, "STRIPE_API_KEY not configured")
    farm_doc = await farms_col.find_one({"slug": farm})
    if not farm_doc:
        raise HTTPException(404, "Farm not found")
    if farm_doc.get("subscriptionStatus") == "active" and farm_doc.get("stripeSubscriptionId"):
        raise HTTPException(409, "This farm is already on a paid, active subscription")
    package_id = farm_doc.get("tier") if farm_doc.get("tier") in _SUB_PRICE_MAP else "bronze_monthly"
    owner_email = farm_doc.get("ownerEmail")
    if not owner_email:
        raise HTTPException(400, "No owner email on file for this farm — can't start checkout")

    import stripe
    stripe.api_key = api_key
    price_id = _ensure_stripe_price(package_id)
    public_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    token = farm_doc.get("farmToken", "")
    _q = f"farm={farm}&t={token}" if token else f"farm={farm}"
    success_url = f"{(public_url or '')}/?{_q}&upgraded=1"
    cancel_url = f"{(public_url or '')}/?{_q}"

    meta = {"upgrade_farm_slug": farm, "package_id": package_id, "kind": "upgrade"}
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        subscription_data={"metadata": meta},  # no trial_period_days — charged immediately, they already had 30 free days
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=meta,
        customer_email=owner_email,
        allow_promotion_codes=True,
    )
    await payments_col.insert_one({
        "id": str(uuid.uuid4()),
        "session_id": session.id,
        "package_id": package_id,
        "amount": PACKAGES[package_id]["amount"],
        "currency": "aud",
        "kind": "upgrade",
        "mode": "subscription",
        "stripe_price_id": price_id,
        "email": owner_email,
        "farm_slug": farm,
        "provisioned": False,
        "payment_status": "initiated",
        "status": "open",
        "createdAt": datetime.now(timezone.utc),
    })
    return {"url": session.url, "session_id": session.id}


@app.post("/api/checkout")
async def create_checkout(body: CheckoutRequest):
    if body.packageId not in PACKAGES:
        raise HTTPException(400, "Invalid package")
    pkg = PACKAGES[body.packageId]
    api_key = os.environ.get("STRIPE_API_KEY")
    if not api_key:
        raise HTTPException(503, "STRIPE_API_KEY not configured")

    origin = body.originUrl.rstrip("/")
    success_url = f"{origin}/landing/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin}/landing"

    meta = {"package_id": body.packageId, "kind": pkg["kind"], "label": pkg["label"]}
    if body.email:     meta["email"] = body.email
    if body.buyerName: meta["buyer_name"] = body.buyerName
    if body.ref:       meta["ref"] = body.ref.strip().lower()

    # ── SUBSCRIPTION MODE (Bronze/Silver/Gold/Platinum × monthly/annual) ──
    # True recurring billing via Stripe Subscriptions with 30-day free trial.
    if pkg["kind"] == "subscription":
        import stripe
        stripe.api_key = api_key
        price_id = _ensure_stripe_price(body.packageId)

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            subscription_data={
                "trial_period_days": 30,
                "metadata": meta,   # copied onto the subscription for auditing
            },
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=meta,
            customer_email=body.email or None,
            allow_promotion_codes=True,
            billing_address_collection="auto",
            # Payment method collected upfront; card charged only if the buyer doesn't cancel before day 30.
            payment_method_collection="always",
        )
        amount = pkg["amount"]

        await payments_col.insert_one({
            "id": str(uuid.uuid4()),
            "session_id": session.id,
            "package_id": body.packageId,
            "amount": amount,
            "currency": "aud",
            "kind": pkg["kind"],
            "mode": "subscription",
            "stripe_price_id": price_id,
            "email": body.email,
            "buyerName": body.buyerName,
            "ref": (body.ref.strip().lower() if body.ref else None),
            "provisioned": False,
            "payment_status": "initiated",
            "status": "open",
            "createdAt": datetime.now(timezone.utc),
        })
        return {"url": session.url, "session_id": session.id}

    # ── OPS BUNDLE MODE (multi-farm, dynamic pricing) ────────────────────
    # Kept on emergent lib one-time payment for now (matches the volume-discount
    # math from `_price_ops_bundle`). Roadmap: convert to subscription too.
    try:
        from emergentintegrations.payments.stripe.checkout import StripeCheckout, CheckoutSessionRequest
    except Exception as e:
        raise HTTPException(503, f"Stripe lib missing: {e}")

    webhook_url = f"{origin}/api/webhook/stripe"
    checkout = StripeCheckout(api_key=api_key, webhook_url=webhook_url)

    amount = float(pkg["amount"])
    if pkg["kind"] == "ops_bundle" and body.farms:
        billing = (body.billingPeriod or "monthly").lower()
        amount = _price_ops_bundle(body.farms, billing)
        meta["billing_period"] = billing
        meta["farm_count"]     = str(len(body.farms))

    req = CheckoutSessionRequest(
        amount=amount, currency="aud",
        success_url=success_url, cancel_url=cancel_url, metadata=meta,
    )
    session = await checkout.create_checkout_session(req)

    await payments_col.insert_one({
        "id": str(uuid.uuid4()),
        "session_id": session.session_id,
        "package_id": body.packageId,
        "amount": amount,
        "currency": "aud",
        "kind": pkg["kind"],
        "mode": "payment",
        "email": body.email,
        "buyerName": body.buyerName,
        "ref": (body.ref.strip().lower() if body.ref else None),
        "farms": [f.model_dump() for f in body.farms] if body.farms else None,
        "provisioned": False,
        "payment_status": "initiated",
        "status": "open",
        "createdAt": datetime.now(timezone.utc),
    })
    return {"url": session.url, "session_id": session.session_id}


# ─── 30-day "no card needed" trial signup ────────────────────────────
class TrialStartRequest(BaseModel):
    name: str
    email: str
    farmName: Optional[str] = None
    packageId: Optional[str] = "silver_monthly"


class WaitlistJoinRequest(BaseModel):
    product: str      # "breeders", "layer", etc
    email: str
    name: Optional[str] = None


@app.post("/api/waitlist/join", status_code=201)
async def waitlist_join(body: WaitlistJoinRequest):
    """Capture a waitlist lead (Breeders, Layer, etc). No auth required."""
    email = (body.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Valid email required")
    product = (body.product or "").strip().lower() or "unknown"
    now = datetime.now(timezone.utc)
    # Idempotent — one row per (product, email)
    await db["waitlist"].update_one(
        {"product": product, "email": email},
        {"$setOnInsert": {"id": str(uuid.uuid4()), "product": product, "email": email, "name": body.name, "createdAt": now}},
        upsert=True,
    )
    # Best-effort admin ping
    admin = os.environ.get("ADMIN_EMAIL")
    if admin:
        try:
            from email_service import send_email
            await send_email(
                to=admin,
                subject=f"📋 Waitlist join: {product} · {email}",
                html=f"<p>{email} joined the <b>{product}</b> waitlist.</p>",
            )
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/trial/start", status_code=201)
async def start_free_trial(body: TrialStartRequest):
    """Create a 30-day free trial farm. No Stripe / no card required.

    Idempotent per email: if that email already has a trial farm, resend the welcome
    link instead of creating a duplicate.
    """
    from email_service import send_email
    email = (body.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Valid email required")
    name = (body.name or "").strip() or email.split("@")[0]
    farm_name = (body.farmName or "").strip() or f"{name}'s Farm"
    package_id = body.packageId if body.packageId in PACKAGES else "silver_monthly"

    now = datetime.now(timezone.utc)
    trial_expires = now + timedelta(days=30)

    # Idempotency: reuse existing trial farm for this email
    existing = await farms_col.find_one({"ownerEmail": email, "subscriptionStatus": "trialing"})
    if existing:
        slug = existing["slug"]
    else:
        # New trial → allocate unique slug
        base = _slugify(farm_name) or "my-farm"
        slug = base
        n = 2
        while await farms_col.find_one({"slug": slug}):
            slug = f"{base}-{n}"
            n += 1
        await farms_col.insert_one({
            "id": str(uuid.uuid4()),
            "slug": slug,
            "name": farm_name,
            "ownerEmail": email,
            "ownerName": name,
            "tier": package_id,
            "subscriptionStatus": "trialing",
            "trialStartedAt": now,
            "trialExpiresAt": trial_expires,
            "createdAt": now,
            "isDefault": False,
            "source": "trial-signup",
            "farmToken": secrets.token_urlsafe(24),  # SEC-006: bulletproof per-farm read-access token
        })
        await _seed_farm(slug, farm_name)

    # Fetch back to get farmToken for URL-building
    farm_doc = await farms_col.find_one({"slug": slug})
    farm_token = farm_doc.get("farmToken") if farm_doc else ""
    public_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    _q = f"farm={slug}&t={farm_token}" if farm_token else f"farm={slug}"
    reader_url = f"{public_url}/reader?{_q}" if public_url else f"/reader?{_q}"
    program_url = f"{public_url}/?{_q}&onboarding=1" if public_url else f"/?{_q}&onboarding=1"

    # Welcome email — magic-link style (login by clicking the reader URL)
    trial_end_fmt = trial_expires.strftime("%A %d %B %Y")
    _tier_price = {"bronze": "A$20", "silver": "A$25", "gold": "A$30", "platinum": "A$40"}.get(
        (package_id or "").lower(), "A$20"
    )
    _tier_label = (package_id or "bronze").capitalize()
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:600px;margin:0 auto;padding:24px;background:#f7fbf4;">
      <div style="background:#0f3d24;color:#fff;padding:28px 26px;border-radius:14px 14px 0 0;border-bottom:3px solid #C9A227;">
        <div style="color:#C9A227;letter-spacing:2px;font-size:11px;font-weight:900;margin-bottom:6px;">APPCOVI · BROILER BASE MATE</div>
        <h1 style="margin:0;font-size:24px;font-weight:900;">G'day {name} — welcome to Broiler Base Mate 🎉</h1>
      </div>
      <div style="background:#fff;padding:24px 26px;border-radius:0 0 14px 14px;border:1px solid #e0e8dd;border-top:0;">
        <p style="font-size:15px;color:#1a3d24;line-height:1.6;margin:0 0 14px;">
          Your farm <b style="color:#0f3d24;">{farm_name}</b> is ready. You've got full access to every feature for the next <b>30 days</b> — <b>no card, no charge</b>.
        </p>
        <div style="background:#f0f5eb;border-left:4px solid #C9A227;padding:14px 16px;border-radius:8px;margin:18px 0;">
          <div style="color:#0f3d24;font-weight:800;font-size:13px;letter-spacing:0.5px;margin-bottom:4px;">📅 YOUR FREE TRIAL</div>
          <div style="font-size:14px;color:#1a3d24;">Runs until <b>{trial_end_fmt}</b>. After that, keep going for just <b>{_tier_price}/month</b> on the <b>{_tier_label}</b> plan (only if you love it).</div>
        </div>
        <h3 style="color:#0f3d24;margin:24px 0 8px;font-size:15px;letter-spacing:0.5px;">🚀 OPEN YOUR FARM</h3>
        <p style="margin:0 0 12px;">
          <a href="{program_url}" style="background:#0f3d24;color:#C9A227;text-decoration:none;padding:13px 26px;border-radius:99px;font-weight:900;font-size:14px;display:inline-block;letter-spacing:0.3px;">📊 Open Feed Program (desktop)</a>
        </p>
        <p style="margin:0 0 20px;">
          <a href="{reader_url}" style="background:#C9A227;color:#0f3d24;text-decoration:none;padding:13px 26px;border-radius:99px;font-weight:900;font-size:14px;display:inline-block;letter-spacing:0.3px;">📱 Open Field Reader (phone)</a>
        </p>
        <div style="background:#fffbe6;border:1px solid #ffe08a;padding:12px 14px;border-radius:8px;font-size:13px;color:#4a3800;line-height:1.55;">
          💡 <b>Tip:</b> Open the Field Reader on your phone and tap "Add to Home Screen" — it lives on your phone like a real app, works offline, and syncs the moment you get reception.
        </div>
        <h3 style="color:#0f3d24;margin:24px 0 8px;font-size:15px;letter-spacing:0.5px;">✅ WHAT'S IN YOUR TRIAL</h3>
        <p style="font-size:13px;color:#3d5450;line-height:1.7;margin:0;">
          Silo Tracker · AI Docket Scanner · Feed Program dashboard · Farm Buddy AI advisor · AI Weigh Birds · AI Mort Sheet parser · AI Chick Counter · Real-time FCR &amp; cFCR · One-tap End-of-Batch reports. All 15 languages available.
        </p>
        <div style="margin-top:26px;padding-top:18px;border-top:1px solid #e0e8dd;font-size:13px;color:#5a7268;line-height:1.6;">
          <b>Reply to this email anytime</b> — it comes to my personal inbox and I'll get back to you fast.<br>
          — <b>Jason Coverdale</b><br>
          <span style="color:#8aa094;font-size:12px;">Founder, Appcovi · 3rd-generation Aussie broiler grower</span>
        </div>
      </div>
    </div>
    """
    email_result = await send_email(
        to=email,
        subject=f"🎉 Welcome to Broiler Base Mate — your 30-day free trial for {farm_name} is live",
        html=html,
        reply_to="appcovi2026@gmail.com",
    )

    # Notify admin
    admin = os.environ.get("ADMIN_EMAIL")
    if admin:
        try:
            await send_email(
                to=admin,
                subject=f"🌱 New trial: {email} · {farm_name}",
                html=f"<p><b>{name}</b> ({email}) started a 30-day trial for <b>{farm_name}</b> (slug: <code>{slug}</code>).<br>Package intent: <code>{package_id}</code><br>Trial expires: {trial_end_fmt}</p>",
            )
        except Exception:
            pass

    return {
        "ok": True,
        "farmSlug": slug,
        "readerUrl": reader_url,
        "programUrl": program_url,
        "trialExpiresAt": trial_expires.isoformat(),
        "emailSent": bool(email_result and email_result.get("ok")),
    }



# ──────────── Referral stats ──────────────────────────────────────────────
# Each successful paid checkout that carries metadata.ref == <farm-slug>
# credits that farm A$10 (one-month sponsor_10 equivalent). The Reader's
# Settings tile shows the count + total credit + the shareable referral link.

REFERRAL_CREDIT_AUD = 10.0  # A$ per successful referral

@app.get("/api/referrals/stats")
async def referral_stats(farm: str = "default"):
    code = farm.strip().lower()
    # Count paid transactions whose stored `ref` matches this farm's code.
    cur = payments_col.find({
        "ref": code,
        "payment_status": {"$in": ["paid", "complete"]},
    }, {"_id": 0, "email": 1, "createdAt": 1, "amount": 1, "package_id": 1})
    signups = []
    async for d in cur:
        signups.append({
            "email": d.get("email") or "—",
            "package": d.get("package_id"),
            "createdAt": (d.get("createdAt").isoformat() if d.get("createdAt") else None),
        })
    count = len(signups)
    return {
        "referralCode": code,
        "signupCount": count,
        "creditAud": round(count * REFERRAL_CREDIT_AUD, 2),
        "signups": signups[-10:],  # last 10 only, keep payload tiny
        "shareUrl": f"https://broilerbasemate.com.au?ref={code}",
    }


@app.get("/api/checkout/status/{session_id}")
async def checkout_status(session_id: str):
    api_key = os.environ.get("STRIPE_API_KEY")
    if not api_key:
        raise HTTPException(503, "STRIPE_API_KEY not configured")

    cur = await payments_col.find_one({"session_id": session_id})
    session_mode = (cur or {}).get("mode")

    # For subscription sessions (Bronze/Silver/Gold/Platinum), the "paid" moment
    # is when the checkout completes and the trial subscription is created —
    # not when a card is charged (that only happens at day 30).
    if session_mode == "subscription":
        import stripe
        stripe.api_key = api_key
        s = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
        status = s.get("status") or "open"
        # A subscription checkout with `payment_status=no_payment_required` +
        # `status=complete` means the trial subscription was created OK.
        # We consider that "paid" for provisioning purposes (Stripe holds the
        # card and will auto-charge at trial end unless the customer cancels).
        raw_pay_status = s.get("payment_status") or "unpaid"
        if status == "complete":
            payment_status = "paid"
        else:
            payment_status = raw_pay_status
        amount_total = s.get("amount_total") or 0
        currency = s.get("currency") or "aud"
        metadata = s.get("metadata") or {}
        sub_id = None
        if s.get("subscription"):
            sub_id = s["subscription"] if isinstance(s["subscription"], str) else s["subscription"].get("id")
    else:
        # Legacy one-time payment session (ops_bundle) via emergent lib
        try:
            from emergentintegrations.payments.stripe.checkout import StripeCheckout
        except Exception as e:
            raise HTTPException(503, f"Stripe lib missing: {e}")
        checkout = StripeCheckout(api_key=api_key, webhook_url="")
        s = await checkout.get_checkout_status(session_id)
        status = s.status
        payment_status = s.payment_status
        amount_total = s.amount_total
        currency = s.currency
        metadata = s.metadata or {}
        sub_id = None

    # Update DB only if status changed (idempotent)
    if cur and (cur.get("payment_status") != payment_status or (sub_id and cur.get("stripe_subscription_id") != sub_id)):
        updates: Dict[str, Any] = {
            "payment_status": payment_status, "status": status,
            "updatedAt": datetime.now(timezone.utc),
        }
        if sub_id: updates["stripe_subscription_id"] = sub_id
        await payments_col.update_one({"session_id": session_id}, {"$set": updates})

    # Auto-onboarding: when payment first becomes 'paid', provision farms + email buyer
    onboarding = None
    if cur and payment_status == "paid" and not cur.get("provisioned"):
        try:
            onboarding = await _provision_purchase(session_id)
        except Exception as prov_err:
            import logging as _logging
            _logging.exception("Status-check provisioning failed: %s", prov_err)
            try:
                await log_error(
                    db,
                    category="stripe_provision",
                    message=f"Status-poll provisioning failed for session {session_id}: {prov_err}",
                    details={"session_id": session_id},
                )
            except Exception:
                pass
            onboarding = {"error": "Provisioning will retry — admin notified."}

    return {"status": status, "payment_status": payment_status, "amount_total": amount_total,
            "currency": currency, "metadata": metadata, "onboarding": onboarding,
            "subscription_id": sub_id}


async def _provision_purchase(session_id: str) -> Optional[dict]:
    """When a Stripe session lands as 'paid', create farms + email buyer.

    Atomic idempotency guard: `update_one({session_id, provisioned:False}, $set: provisioned:True)`
    only succeeds for the FIRST concurrent caller — subsequent races (poll +
    webhook firing within seconds) short-circuit at the guard and return None.
    Prevents duplicate farms + duplicate welcome emails.
    """
    from email_service import send_email
    # Atomic check-and-claim. matched_count == 1 means we won the race.
    claim = await payments_col.update_one(
        {"session_id": session_id, "provisioned": {"$ne": True}},
        {"$set": {"provisioned": True, "provisioningStartedAt": datetime.now(timezone.utc)}},
    )
    if claim.matched_count == 0:
        return None
    cur = await payments_col.find_one({"session_id": session_id})
    if not cur:
        return None

    kind = cur.get("kind")
    buyer_email = cur.get("email")
    buyer_name = cur.get("buyerName") or "there"
    public_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    created_farms: List[dict] = []

    if kind == "upgrade":
        # Existing trial farm converting to paid — update the SAME farm,
        # never create a new one. Mar 2026 fix (see /api/farm/upgrade-checkout).
        slug = cur.get("farm_slug")
        sub_id = cur.get("stripe_subscription_id")
        farm_doc = await farms_col.find_one({"slug": slug}) if slug else None
        if not farm_doc:
            return None
        await farms_col.update_one(
            {"slug": slug},
            {"$set": {
                "subscriptionStatus": "active",
                "tier": cur.get("package_id"),
                "stripeSubscriptionId": sub_id,
                "upgradedAt": datetime.now(timezone.utc),
            }},
        )
        email_result = None
        if buyer_email:
            plan_label = PACKAGES.get(cur.get("package_id"), {}).get("label", "your plan")
            html = f"""
            <div style="font-family:system-ui,-apple-system,sans-serif;max-width:580px;margin:0 auto;padding:20px;">
              <h2 style="color:#0f3d24;margin:0 0 10px;">🎉 You're upgraded, {buyer_name}!</h2>
              <p style="font-size:15px;color:#1a3d24;line-height:1.6;">
                <b>{farm_doc.get('name', 'Your farm')}</b> is now on the <b>{plan_label}</b> plan — thanks for sticking with Broiler Base Mate.
                Everything keeps running exactly as it was, no re-setup needed.
              </p>
              <p style="text-align:center;margin:28px 0;">
                <a href="{public_url or ''}/?farm={slug}" style="background:#C9A227;color:#000;text-decoration:none;padding:14px 28px;border-radius:99px;font-weight:900;font-size:15px;display:inline-block;">📊 Open Feed Program</a>
              </p>
            </div>
            """
            from email_service import send_email
            email_result = await send_email(to=buyer_email, subject=f"🎉 You're upgraded — {plan_label} is live", html=html)
        await payments_col.update_one(
            {"session_id": session_id},
            {"$set": {"provisionedAt": datetime.now(timezone.utc)}},
        )
        admin = os.environ.get("ADMIN_EMAIL")
        if admin:
            try:
                from email_service import send_email
                await send_email(
                    to=admin,
                    subject=f"💰 Trial upgraded to paid: {slug} · {buyer_email} · ${cur.get('amount')}",
                    html=f"<p>Farm <b>{slug}</b> ({buyer_email}) upgraded to {cur.get('package_id')}. Session: {session_id}</p>",
                )
            except Exception:
                pass
        return {"upgradedFarm": slug, "emailSent": (email_result or {}).get("ok", False) if email_result else False}

    if kind == "ops_bundle" and cur.get("farms"):
        # Multi-farm Ops Pack: create one farm per configured name
        for f in cur["farms"]:
            name = (f.get("name") or "Farm").strip()
            base_slug = _slugify(name)
            # Ensure unique slug
            slug = base_slug
            n = 2
            while await farms_col.find_one({"slug": slug}):
                slug = f"{base_slug}-{n}"
                n += 1
            doc = {
                "id": str(uuid.uuid4()),
                "slug": slug,
                "name": name,
                "ownerEmail": buyer_email,
                "tier": f.get("tier"),
                "createdAt": datetime.now(timezone.utc),
                "isDefault": False,
                "stripeSessionId": session_id,
                "farmToken": secrets.token_urlsafe(24),  # SEC-006: unique per-farm read token so each buyer's QR/link is theirs alone
            }
            await farms_col.insert_one(doc)
            await _seed_farm(slug, name)
            farm_token = doc["farmToken"]
            _q = f"farm={slug}&t={farm_token}"
            reader_url = f"{public_url}/reader?{_q}" if public_url else f"/reader?{_q}"
            program_url = f"{public_url}/?{_q}&onboarding=1" if public_url else f"/?{_q}&onboarding=1"
            created_farms.append({"slug": slug, "name": name, "tier": f.get("tier"), "readerUrl": reader_url, "programUrl": program_url})

    elif kind == "subscription":
        # Single-farm plans (Bronze/Silver/Gold/Platinum/Integrator)
        slug_base = _slugify(buyer_name) if buyer_name and buyer_name != "there" else "my-farm"
        slug = slug_base
        n = 2
        while await farms_col.find_one({"slug": slug}):
            slug = f"{slug_base}-{n}"
            n += 1
        doc = {
            "id": str(uuid.uuid4()),
            "slug": slug,
            "name": (buyer_name if buyer_name != "there" else "My Farm"),
            "ownerEmail": buyer_email,
            "tier": cur.get("package_id"),
            "createdAt": datetime.now(timezone.utc),
            "isDefault": False,
            "stripeSessionId": session_id,
            "farmToken": secrets.token_urlsafe(24),  # SEC-006: unique per-farm read token so each buyer's QR/link is theirs alone
        }
        await farms_col.insert_one(doc)
        await _seed_farm(slug, doc["name"])
        farm_token = doc["farmToken"]
        _q = f"farm={slug}&t={farm_token}"
        reader_url = f"{public_url}/reader?{_q}" if public_url else f"/reader?{_q}"
        program_url = f"{public_url}/?{_q}&onboarding=1" if public_url else f"/?{_q}&onboarding=1"
        created_farms.append({"slug": slug, "name": doc["name"], "tier": doc["tier"], "readerUrl": reader_url, "programUrl": program_url})

    ops_dashboard_url = f"{public_url}/ops-dashboard" if public_url else "/ops-dashboard"

    # Send buyer email (graceful-degrade)
    email_result: Optional[dict] = None
    if buyer_email and created_farms:
        farm_rows = "".join([
            f"<tr><td style='padding:8px 12px;background:#f7fbf4;font-weight:700'>{fc['name']}</td>"
            f"<td style='padding:8px 12px;background:#fff;border:1px solid #e8ecea'><a href='{fc['readerUrl']}'>{fc['readerUrl']}</a></td></tr>"
            for fc in created_farms
        ])
        is_ops = kind == "ops_bundle"
        html = f"""
        <div style="font-family:system-ui,-apple-system,sans-serif;max-width:580px;margin:0 auto;padding:20px;">
          <h2 style="color:#0f3d24;margin:0 0 10px;">🎉 You're all set, {buyer_name}!</h2>
          <p style="font-size:15px;color:#1a3d24;line-height:1.6;">
            Your <b>{cur.get('kind','subscription')}</b> on Broiler Base Mate is active.
            We've created <b>{len(created_farms)} farm{'s' if len(created_farms) != 1 else ''}</b> for you and pre-seeded each with 10 shed-groups × 3 silos so you can start tracking immediately.
          </p>
          <h3 style="color:#0f3d24;margin:24px 0 8px;font-size:16px;">Your Field Reader links (give these to your workers):</h3>
          <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px;">{farm_rows}</table>
          { f'<p style="text-align:center;margin:28px 0;"><a href="{ops_dashboard_url}" style="background:#C9A227;color:#000;text-decoration:none;padding:14px 28px;border-radius:99px;font-weight:900;font-size:15px;display:inline-block;">📊 Open Ops Dashboard</a></p>' if is_ops else f'<p style="text-align:center;margin:28px 0;"><a href="{public_url or ""}/" style="background:#C9A227;color:#000;text-decoration:none;padding:14px 28px;border-radius:99px;font-weight:900;font-size:15px;display:inline-block;">📊 Open Feed Program</a></p>' }
          <p style="font-size:12px;color:#5a7268;border-top:1px solid #e8ecea;padding-top:16px;margin-top:24px;">
            Welcome aboard — reply to this email any time if you need a hand.
          </p>
        </div>
        """
        email_result = await send_email(
            to=buyer_email,
            subject=f"🎉 Welcome to Broiler Base Mate — your {len(created_farms)} farm{'s are' if len(created_farms) != 1 else ' is'} ready",
            html=html,
        )

    # Persist final result (provisioned flag was already set atomically at the top)
    await payments_col.update_one(
        {"session_id": session_id},
        {"$set": {"createdFarms": created_farms, "provisionedAt": datetime.now(timezone.utc)}},
    )

    # Also notify admin
    admin = os.environ.get("ADMIN_EMAIL")
    if admin and buyer_email:
        try:
            await send_email(
                to=admin,
                subject=f"💰 New sale: {cur.get('kind')} · {buyer_email} · ${cur.get('amount')}",
                html=(
                    f"<p>New paid checkout:<br>"
                    f"<b>Buyer:</b> {buyer_name} &lt;{buyer_email}&gt;<br>"
                    f"<b>Package:</b> {cur.get('package_id')} ({cur.get('kind')})<br>"
                    f"<b>Amount:</b> ${cur.get('amount')} {cur.get('currency','aud').upper()}<br>"
                    f"<b>Farms created:</b> {', '.join(fc['slug'] for fc in created_farms) or 'none'}<br>"
                    f"<b>Session:</b> {session_id}</p>"
                ),
            )
        except Exception:
            pass

    return {
        "createdFarms": created_farms,
        "opsDashboardUrl": ops_dashboard_url,
        "emailSent": (email_result or {}).get("ok", False) if email_result else False,
        "emailSkipped": (email_result or {}).get("skipped", False) if email_result else False,
    }


@app.post("/api/checkout/resend-welcome/{session_id}")
async def resend_welcome_email(session_id: str):
    """Resend the welcome email for an already-provisioned Stripe session.

    Used by the success-page "didn't get it? resend" button so growers don't
    get stuck waiting for the first email + churn silently.
    """
    from email_service import send_email
    cur = await payments_col.find_one({"session_id": session_id})
    if not cur:
        raise HTTPException(status_code=404, detail="Session not found")
    if not cur.get("provisioned"):
        raise HTTPException(status_code=409, detail="Payment not yet fully provisioned — try again in a moment")
    buyer_email = cur.get("email")
    if not buyer_email:
        raise HTTPException(status_code=400, detail="No buyer email on file — pass it back via Stripe if you paid without an account")
    farms = cur.get("createdFarms") or []
    if not farms:
        raise HTTPException(status_code=404, detail="No farms found for this session")
    # Basic rate-limit: max 3 resends per session, min 20s between sends
    now = datetime.now(timezone.utc)
    resends = cur.get("welcomeResends", [])
    if len(resends) >= 3:
        raise HTTPException(status_code=429, detail="Resend limit reached — email support if you still can't find it")
    if resends:
        last = resends[-1]
        if isinstance(last, dict) and last.get("at"):
            last_at = last["at"] if isinstance(last["at"], datetime) else datetime.fromisoformat(str(last["at"]).replace("Z", "+00:00"))
            if (now - last_at).total_seconds() < 20:
                raise HTTPException(status_code=429, detail="Please wait 20 seconds between resends")

    public_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    ops_dashboard_url = f"{public_url}/ops-dashboard" if public_url else "/ops-dashboard"
    is_ops = cur.get("kind") == "ops_bundle"
    buyer_name = cur.get("buyerName") or "there"
    farm_rows = "".join([
        f"<tr><td style='padding:8px 12px;background:#f7fbf4;font-weight:700'>{fc['name']}</td>"
        f"<td style='padding:8px 12px;background:#fff;border:1px solid #e8ecea'><a href='{fc['readerUrl']}'>{fc['readerUrl']}</a></td></tr>"
        for fc in farms
    ])
    html = f"""
    <div style="font-family:system-ui,-apple-system,sans-serif;max-width:580px;margin:0 auto;padding:20px;">
      <h2 style="color:#0f3d24;margin:0 0 10px;">🔗 Your Broiler Base Mate links, {buyer_name}</h2>
      <p style="font-size:15px;color:#1a3d24;line-height:1.6;">
        Here they are again — bookmark them or share with your team.
      </p>
      <h3 style="color:#0f3d24;margin:24px 0 8px;font-size:16px;">Your Field Reader links:</h3>
      <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px;">{farm_rows}</table>
      { f'<p style="text-align:center;margin:28px 0;"><a href="{ops_dashboard_url}" style="background:#C9A227;color:#000;text-decoration:none;padding:14px 28px;border-radius:99px;font-weight:900;font-size:15px;display:inline-block;">📊 Open Ops Dashboard</a></p>' if is_ops else f'<p style="text-align:center;margin:28px 0;"><a href="{public_url or ""}/" style="background:#C9A227;color:#000;text-decoration:none;padding:14px 28px;border-radius:99px;font-weight:900;font-size:15px;display:inline-block;">📊 Open Feed Program</a></p>' }
    </div>
    """
    result = await send_email(
        to=buyer_email,
        subject=f"🔗 Your Broiler Base Mate access links (resend)",
        html=html,
    )
    await payments_col.update_one(
        {"session_id": session_id},
        {"$push": {"welcomeResends": {"at": now, "ok": bool(result.get("ok"))}}},
    )
    return {"ok": bool(result.get("ok")), "skipped": bool(result.get("skipped")), "email": buyer_email}


@app.post("/api/webhook/stripe")
async def stripe_webhook(request: Request):
    """Handle Stripe events natively:
      • checkout.session.completed          → provision farms + welcome email (both subscription & payment mode)
      • customer.subscription.updated       → sync farms.subscriptionStatus (trialing / active / past_due / canceled)
      • customer.subscription.deleted       → mark farms canceled
      • invoice.payment_failed              → alert admin (buyer has broken card)
    Signature is verified if STRIPE_WEBHOOK_SECRET is set; otherwise we parse
    the payload directly (dev / initial setup mode) and log a warning.
    """
    import stripe as _stripe, json as _json
    _stripe.api_key = os.environ.get("STRIPE_API_KEY", "")
    raw = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # ── Verification path A: signing secret configured → strict HMAC check
    # ── Verification path B: no secret → parse payload, then double-check
    #     the event by fetching it from Stripe's API (requires STRIPE_API_KEY).
    #     Forged events won't exist in Stripe's own records, so the fetch
    #     fails and we reject. Keeps the app secure while the deployer
    #     hasn't wired STRIPE_WEBHOOK_SECRET yet.
    event: Optional[Dict[str, Any]] = None
    verification_mode: str = ""
    if secret:
        try:
            event = _stripe.Webhook.construct_event(raw, sig, secret)
            verification_mode = "hmac"
        except Exception as e:
            try:
                await log_error(db, category="stripe_webhook",
                                message=f"HMAC verify failed: {e}", request=request)
            except Exception:
                pass
            return JSONResponse({"ok": False, "error": "signature check failed"}, status_code=200)
    else:
        # No secret — parse-then-verify-with-Stripe-API fallback.
        if not _stripe.api_key:
            try:
                await log_error(db, category="stripe_webhook",
                                message="Webhook REJECTED — neither STRIPE_WEBHOOK_SECRET nor STRIPE_API_KEY set. Cannot verify events.",
                                request=request)
            except Exception:
                pass
            return JSONResponse({"ok": False, "error": "server missing Stripe credentials"}, status_code=200)
        try:
            parsed = _json.loads(raw.decode("utf-8"))
            event_id = parsed.get("id")
            if not event_id or not str(event_id).startswith("evt_"):
                raise ValueError("payload has no valid Stripe event id")
            # Fetch the event fresh from Stripe using our secret key. A
            # forged payload won't exist in Stripe's records → this throws.
            authoritative = _stripe.Event.retrieve(event_id)
            event = _json.loads(_json.dumps(authoritative))
            verification_mode = "api-fetch"
        except Exception as e:
            try:
                await log_error(db, category="stripe_webhook",
                                message=f"API-fetch verify failed: {e}", request=request)
            except Exception:
                pass
            import logging as _log
            _log.warning("Stripe webhook API-fetch verify failed: %s", e)
            return JSONResponse({"ok": False, "error": "event could not be verified against Stripe"}, status_code=200)

    # Log successfully-verified events so admin can audit
    try:
        import logging as _log
        _log.info("Stripe webhook verified (%s): %s", verification_mode, event.get("type"))
    except Exception:
        pass

    event_type = event.get("type") if isinstance(event, dict) else event["type"]
    obj = (event.get("data") or {}).get("object") if isinstance(event, dict) else event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            session_id = obj.get("id") if isinstance(obj, dict) else obj["id"]
            mode = obj.get("mode") if isinstance(obj, dict) else obj["mode"]
            sub_id = obj.get("subscription") if isinstance(obj, dict) else obj.get("subscription")

            # Update our payments row
            update = {
                "payment_status": "paid",
                "status": "complete",
                "webhook_event": event_type,
                "updatedAt": datetime.now(timezone.utc),
            }
            if sub_id: update["stripe_subscription_id"] = sub_id
            await payments_col.update_one({"session_id": session_id}, {"$set": update})

            # Provision farms + welcome email (idempotent — noop if already done)
            try:
                await _provision_purchase(session_id)
            except Exception as prov_err:
                import logging as _logging
                _logging.exception("Auto-onboarding from webhook failed: %s", prov_err)
                try:
                    await log_error(
                        db, category="stripe_provision",
                        message=f"Webhook provisioning failed for session {session_id}: {prov_err}",
                        details={"session_id": session_id, "event_type": event_type, "mode": mode},
                    )
                except Exception:
                    pass

        elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
            sub_id = obj["id"]
            status = obj.get("status")  # trialing | active | past_due | canceled | incomplete | unpaid
            # Sync all farms tied to this subscription/session
            payment_row = await payments_col.find_one({"stripe_subscription_id": sub_id})
            if payment_row:
                await farms_col.update_many(
                    {"stripeSessionId": payment_row.get("session_id")},
                    {"$set": {"subscriptionStatus": status, "updatedAt": datetime.now(timezone.utc)}},
                )
                await payments_col.update_one(
                    {"stripe_subscription_id": sub_id},
                    {"$set": {"subscription_status": status, "updatedAt": datetime.now(timezone.utc)}},
                )

        elif event_type == "customer.subscription.deleted":
            sub_id = obj["id"]
            payment_row = await payments_col.find_one({"stripe_subscription_id": sub_id})
            if payment_row:
                await farms_col.update_many(
                    {"stripeSessionId": payment_row.get("session_id")},
                    {"$set": {"subscriptionStatus": "canceled", "canceledAt": datetime.now(timezone.utc)}},
                )
                await payments_col.update_one(
                    {"stripe_subscription_id": sub_id},
                    {"$set": {"subscription_status": "canceled", "updatedAt": datetime.now(timezone.utc)}},
                )

        elif event_type == "invoice.payment_failed":
            # Card declined at trial end or renewal — surface to admin.
            admin = os.environ.get("ADMIN_EMAIL")
            if admin:
                try:
                    from email_service import send_email
                    cust_email = obj.get("customer_email") or "(unknown)"
                    amt = (obj.get("amount_due") or 0) / 100.0
                    await send_email(
                        to=admin,
                        subject=f"⚠️ Stripe payment failed — {cust_email} · AUD {amt:.2f}",
                        html=f"<p>Invoice payment failed for <b>{cust_email}</b> (AUD {amt:.2f}). Subscription ID: {obj.get('subscription')}</p>",
                    )
                except Exception:
                    pass

    except Exception as e:
        try:
            await log_error(
                db, category="stripe_webhook",
                message=f"Webhook handler failed on {event_type}: {e}",
                request=request,
            )
        except Exception:
            pass
        # Always 200 so Stripe doesn't retry-storm
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    return {"ok": True}


# ─── Landing page (static) ────────────────────────────────────────────────
@app.get("/landing")
async def landing():
    return FileResponse(os.path.join(STATIC_DIR, "landing.html"))


@app.get("/landing/success")
async def landing_success():
    return FileResponse(os.path.join(STATIC_DIR, "success.html"))


class DemoRequest(BaseModel):
    name: str
    email: str
    farm: Optional[str] = None
    sheds: Optional[str] = None
    message: Optional[str] = None


@app.post("/api/demo-request")
async def demo_request(body: DemoRequest):
    from email_service import send_email, render_demo_request_email
    doc = {
        "id": str(uuid.uuid4()),
        "name": body.name, "email": body.email, "farm": body.farm,
        "sheds": body.sheds, "message": body.message,
        "createdAt": datetime.now(timezone.utc),
    }
    await db["demo_requests"].insert_one(doc)
    # Fire-and-forget admin notification (graceful-degrade if no Resend key)
    admin = os.environ.get("ADMIN_EMAIL")
    if admin:
        try:
            await send_email(
                to=admin,
                subject=f"📅 New demo request — {body.name} ({body.farm or 'no farm'})",
                html=render_demo_request_email(
                    name=body.name, email=body.email, farm=body.farm,
                    sheds=body.sheds, message=body.message,
                ),
            )
        except Exception:
            pass  # never block the form on email failures
    return {"ok": True}


@app.get("/api/demo-request")
async def list_demo_requests():
    rows = await db["demo_requests"].find().sort("createdAt", -1).limit(100).to_list(length=100)
    return [clean(r) for r in rows]


# ─── Partnership enquiries (resellers / integrators / equipment / feed mills) ─
class PartnerRequest(BaseModel):
    name: str
    email: str
    company: str
    partner_type: str  # equipment_supplier | integrator | feed_mill | other
    farms_count: Optional[str] = None
    message: Optional[str] = None


@app.post("/api/partner-request")
async def partner_request(body: PartnerRequest):
    from email_service import send_email
    doc = {
        "id": str(uuid.uuid4()),
        "name":         body.name,
        "email":        body.email,
        "company":      body.company,
        "partner_type": body.partner_type,
        "farms_count":  body.farms_count,
        "message":      body.message,
        "createdAt":    datetime.now(timezone.utc),
    }
    await db["partner_requests"].insert_one(doc)
    admin = os.environ.get("ADMIN_EMAIL")
    type_label = {
        "equipment_supplier": "🏭 Equipment supplier",
        "integrator":         "🏢 Poultry integrator",
        "feed_mill":          "🌾 Feed mill / consultant",
        "other":              "Other",
    }.get(body.partner_type, body.partner_type)
    if admin:
        try:
            await send_email(
                to=admin,
                subject=f"🤝 New partnership enquiry — {body.company} ({type_label})",
                html=f"""<div style="font-family:system-ui,sans-serif;max-width:560px;padding:24px;color:#1a2622;">
                  <h2 style="color:#1a5c36;margin:0 0 16px;">🤝 New partnership enquiry</h2>
                  <table style="width:100%;border-collapse:collapse;font-size:14px;">
                    <tr><td style="padding:6px 0;color:#666;width:140px;">Name:</td><td style="font-weight:700;">{body.name}</td></tr>
                    <tr><td style="padding:6px 0;color:#666;">Company:</td><td style="font-weight:700;">{body.company}</td></tr>
                    <tr><td style="padding:6px 0;color:#666;">Email:</td><td><a href="mailto:{body.email}" style="color:#1a5c36;">{body.email}</a></td></tr>
                    <tr><td style="padding:6px 0;color:#666;">Partner type:</td><td style="font-weight:700;">{type_label}</td></tr>
                    <tr><td style="padding:6px 0;color:#666;">Farms count:</td><td>{body.farms_count or '—'}</td></tr>
                  </table>
                  {'<div style="margin-top:18px;padding:14px;background:#f7fbf4;border-left:4px solid #C9A227;border-radius:6px;font-size:14px;line-height:1.5;">' + (body.message or '') + '</div>' if body.message else ''}
                  <p style="font-size:12px;color:#888;margin-top:18px;">Reply directly to this email to respond to {body.name}.</p>
                </div>""",
                reply_to=body.email,
            )
        except Exception:
            pass
    return {"ok": True}


@app.get("/api/partner-request")
async def list_partner_requests():
    rows = await db["partner_requests"].find().sort("createdAt", -1).limit(100).to_list(length=100)
    return [clean(r) for r in rows]


# ─── Chat (per-farm + group) ──────────────────────────────────────────────
class ChatPostBody(BaseModel):
    text: str


def _chat_clean(doc: dict) -> dict:
    """Strip Mongo internals + format dates for the wire."""
    doc.pop("_id", None)
    for k, v in list(doc.items()):
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


async def _resolve_chat_actor(request: Request) -> tuple[dict, str]:
    """Return (user, role) — role is 'ops' if admin/owner, else 'farm'."""
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Sign in required to chat")
    email = (user.get("email") or "").lower()
    raw_admin = (os.environ.get("OUTREACH_ADMIN_EMAILS") or "") + "," + (os.environ.get("SUPERUSER_EMAILS") or "")
    admin_set = {e.strip().lower() for e in raw_admin.split(",") if e.strip()}
    role = "ops" if email in admin_set else "farm"
    return user, role


@app.get("/api/chat/{scope}")
async def chat_list(scope: str, request: Request, farm: Optional[str] = None, limit: int = 200):
    """List the most recent `limit` messages for the given scope.
    `scope` is either 'farm' (requires ?farm=<slug>) or 'group'."""
    user, _ = await _resolve_chat_actor(request)
    if scope not in ("farm", "group"):
        raise HTTPException(400, "scope must be 'farm' or 'group'")
    query: dict = {"scope": scope}
    if scope == "farm":
        if not farm:
            raise HTTPException(400, "?farm=<slug> required for farm-scope chat")
        query["farm_slug"] = farm
    rows = await db["chat_messages"].find(query).sort("created_at", -1).limit(limit).to_list(length=limit)
    rows.reverse()  # oldest first for chronological rendering
    # Mark received messages as read by this user
    me_email = (user.get("email") or "").lower()
    await db["chat_messages"].update_many(
        {**query, "sender_email": {"$ne": me_email}, "read_by": {"$ne": me_email}},
        {"$push": {"read_by": me_email}},
    )
    return [_chat_clean(r) for r in rows]


@app.post("/api/chat/{scope}")
async def chat_post(scope: str, body: ChatPostBody, request: Request, farm: Optional[str] = None):
    user, role = await _resolve_chat_actor(request)
    if scope not in ("farm", "group"):
        raise HTTPException(400, "scope must be 'farm' or 'group'")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Empty message")
    if len(text) > 4000:
        raise HTTPException(400, "Message too long")
    doc = {
        "id":           "msg_" + uuid.uuid4().hex[:14],
        "scope":        scope,
        "farm_slug":    farm if scope == "farm" else None,
        "sender_email": (user.get("email") or "").lower(),
        "sender_name":  user.get("name") or user.get("email") or "User",
        "sender_role":  role,
        "text":         text,
        "read_by":      [(user.get("email") or "").lower()],
        "created_at":   datetime.now(timezone.utc),
    }
    if scope == "farm" and not farm:
        raise HTTPException(400, "?farm=<slug> required for farm-scope chat")
    await db["chat_messages"].insert_one(doc)
    return _chat_clean({**doc})


@app.get("/api/chat-unread")
async def chat_unread(request: Request):
    """Return unread message counts so the dashboard can show badges per-farm + group."""
    user, _ = await _resolve_chat_actor(request)
    me_email = (user.get("email") or "").lower()
    out: dict = {"group": 0, "farms": {}}
    cursor = db["chat_messages"].find({
        "read_by": {"$ne": me_email},
        "sender_email": {"$ne": me_email},
    })
    async for m in cursor:
        if m.get("scope") == "group":
            out["group"] += 1
        elif m.get("scope") == "farm":
            slug = m.get("farm_slug") or ""
            out["farms"][slug] = out["farms"].get(slug, 0) + 1
    return out


# ─── Farms (multi-farm Ops layer) ────────────────────────────────────────
import re as _re_farms


def _slugify(name: str) -> str:
    s = _re_farms.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "farm"


class CreateFarmBody(BaseModel):
    name: str
    slug: Optional[str] = None
    ownerEmail: Optional[str] = None


class InviteOperatorBody(BaseModel):
    operatorEmail: str
    operatorName: Optional[str] = None
    opsManagerName: Optional[str] = None


@app.get("/api/farms")
async def list_farms(request: Request):
    """List farms this user can access — their OWNED farms + any farms they've
    been INVITED to via the Ops Manager. Admins see everything.

    Bug fix (Feb 2026): previously this endpoint returned ALL farms in the DB
    to any caller, which leaked farm names (e.g. "Takhar Farm 1") into the
    farm-switcher of unrelated growers. Now scoped to the caller's own /
    invited farms.
    """
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    farms_info = await _list_user_farms(db, user["email"])
    role = farms_info.get("role")
    if role == "admin":
        # Admins keep the full-DB view (used by /admin/health)
        rows = await farms_col.find().sort("createdAt", 1).to_list(length=500)
    else:
        allowed_slugs = {f["slug"] for f in farms_info["owned"]} | {f["slug"] for f in farms_info["invited"]}
        if not allowed_slugs:
            return []
        rows = await farms_col.find({"slug": {"$in": list(allowed_slugs)}}).sort("createdAt", 1).to_list(length=500)
    out = []
    for r in rows:
        slug = r.get("slug")
        f_filter = _farm_filter(slug) if slug else {}
        readings_count = await readings_col.count_documents(f_filter)
        deliveries_count = await deliveries_col.count_documents(f_filter)
        out.append({
            "id": r["id"],
            "slug": slug,
            "name": r.get("name"),
            "ownerEmail": r.get("ownerEmail"),
            "farmToken": r.get("farmToken"),  # SEC-006: needed by ops-dashboard to build /?farm=…&t=… login QR
            "isDefault": bool(r.get("isDefault")),
            "createdAt": r["createdAt"].isoformat() if r.get("createdAt") else None,
            "stats": {"readings": readings_count, "deliveries": deliveries_count},
        })
    return out


@app.post("/api/farms", status_code=201)
async def create_farm(body: CreateFarmBody):
    slug = _slugify(body.slug or body.name)
    if not slug or slug in {"api", "reader", "landing", "ops-dashboard"}:
        raise HTTPException(400, "Invalid slug")
    if await farms_col.find_one({"slug": slug}):
        raise HTTPException(409, f"Farm '{slug}' already exists")
    doc = {
        "id": str(uuid.uuid4()),
        "slug": slug,
        "name": body.name,
        "ownerEmail": body.ownerEmail,
        "createdAt": datetime.now(timezone.utc),
        "isDefault": False,
    }
    await farms_col.insert_one(doc)
    await _seed_farm(slug, body.name)
    return clean(doc)


@app.delete("/api/farms/{slug}", status_code=204)
async def delete_farm(slug: str):
    if slug == DEFAULT_FARM_ID:
        raise HTTPException(400, "Cannot delete the default farm")
    f = await farms_col.find_one({"slug": slug})
    if not f:
        raise HTTPException(404, "Farm not found")
    # Cascade: delete this farm's data (only docs explicitly tagged with this slug — never touches default's untagged legacy data)
    await readings_col.delete_many({"farmId": slug})
    await deliveries_col.delete_many({"farmId": slug})
    await photos_col.delete_many({"farmId": slug})
    await shed_groups_col.delete_many({"farmId": slug})
    await silos_col.delete_many({"farmId": slug})
    await farm_config_col.delete_many({"farmId": slug})
    await farms_col.delete_one({"slug": slug})
    return JSONResponse(content=None, status_code=204)


@app.post("/api/farms/{slug}/invite")
async def invite_operator(slug: str, body: InviteOperatorBody):
    from email_service import send_email, render_farm_invite_email
    farm = await farms_col.find_one({"slug": slug})
    if not farm:
        raise HTTPException(404, "Farm not found")
    public_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    reader_url = f"{public_url}/reader?farm={slug}" if public_url else f"/reader?farm={slug}"

    invite_doc = {
        "id": str(uuid.uuid4()),
        "farmSlug": slug,
        "farmName": farm.get("name"),
        "operatorEmail": body.operatorEmail,
        "operatorName": body.operatorName,
        "opsManagerName": body.opsManagerName,
        "readerUrl": reader_url,
        "createdAt": datetime.now(timezone.utc),
    }
    await db["farm_invites"].insert_one(invite_doc)

    result = await send_email(
        to=body.operatorEmail,
        subject=f"📲 You've been added to {farm.get('name')} on Broiler Base Mate",
        html=render_farm_invite_email(
            operator_name=body.operatorName or "",
            farm_name=farm.get("name") or slug,
            reader_url=reader_url,
            ops_manager=body.opsManagerName or "Your Ops Manager",
        ),
    )
    return {"ok": True, "readerUrl": reader_url, "email": result}


@app.get("/ops-dashboard")
async def ops_dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "ops-dashboard.html"))


@app.get("/sw.js")
async def install_only_service_worker():
    """Install-only service worker.

    Satisfies Android Chrome's PWA install criteria (which require a SW present)
    WITHOUT caching anything — so we don't reintroduce the stale-cache bug that
    bit us with the previous Workbox setup. Also dumps any caches a previous SW
    left behind on activate, so old browsers get a clean slate automatically.
    """
    js = (
        "self.addEventListener('install',  () => { self.skipWaiting(); });\n"
        "self.addEventListener('activate', (e) => {\n"
        "  e.waitUntil((async () => {\n"
        "    try {\n"
        "      const keys = await caches.keys();\n"
        "      await Promise.all(keys.map(k => caches.delete(k)));\n"
        "    } catch (_) {}\n"
        "    try { await self.clients.claim(); } catch (_) {}\n"
        "  })());\n"
        "});\n"
        "self.addEventListener('fetch', () => { /* pass-through, never cache */ });\n"
    )
    return Response(content=js, media_type="application/javascript",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/admin/health")
@app.get("/admin")
async def admin_health_page():
    """Admin-only health dashboard. Auth gating happens client-side via /api/auth/me."""
    return FileResponse(os.path.join(STATIC_DIR, "admin-health.html"))


# ─── Legacy Stripe price migration ──────────────────────────────────────
# Bulk-move any active broiler subscribers still on old prices onto the
# current A$20 / A$25 / A$30 AUD monthly (or A$204 / A$255 / A$306 annual)
# tiers. Uses Stripe API directly (no reliance on stored Price IDs — creates
# ad-hoc price_data by amount + interval, same shape as our checkout flow).
_TARGET_PRICES = {
    # (currency, interval, plan_name) -> amount_cents
    ("aud", "month", "Bronze"):   2000,
    ("aud", "month", "Silver"):   2500,
    ("aud", "month", "Gold"):     3000,
    ("aud", "month", "Platinum"): 4000,
    ("aud", "year",  "Bronze"):   20400,
    ("aud", "year",  "Silver"):   25500,
    ("aud", "year",  "Gold"):     30600,
    ("aud", "year",  "Platinum"): 40800,
}


def _guess_plan_from_metadata(sub) -> Optional[str]:
    """Read plan tier from subscription/item metadata written at checkout time."""
    md = (sub.get("metadata") or {})
    for k in ("plan", "tier", "package_id"):
        v = (md.get(k) or "").lower()
        if "platinum" in v: return "Platinum"
        if "bronze"   in v: return "Bronze"
        if "silver"   in v: return "Silver"
        if "gold"     in v: return "Gold"
    # Fall back: check the first item's metadata
    items = ((sub.get("items") or {}).get("data") or [])
    if items:
        imd = (items[0].get("metadata") or {})
        for k in ("plan", "tier", "package_id"):
            v = (imd.get(k) or "").lower()
            if "platinum" in v: return "Platinum"
            if "bronze"   in v: return "Bronze"
            if "silver"   in v: return "Silver"
            if "gold"     in v: return "Gold"
    return None


@app.post("/api/admin/migrate-legacy-subs")
async def migrate_legacy_subs(request: Request, dry_run: bool = True):
    """Bulk-migrate active Stripe subscriptions onto the current A$20/25/30
    price tiers. Set ?dry_run=false to actually apply changes.

    Returns a per-subscription report so the admin can audit the migration.
    """
    await _require_admin(request)
    import stripe as _stripe
    _stripe.api_key = os.environ["STRIPE_API_KEY"]

    report = {"scanned": 0, "already_current": 0, "migrated": 0, "skipped": 0,
              "failed": 0, "dry_run": dry_run, "changes": []}

    # Page through all active subscriptions
    starting_after = None
    while True:
        kwargs = {"status": "active", "limit": 100, "expand": ["data.items"]}
        if starting_after:
            kwargs["starting_after"] = starting_after
        subs = _stripe.Subscription.list(**kwargs)
        for sub in subs.data:
            report["scanned"] += 1
            sub_dict = sub.to_dict() if hasattr(sub, "to_dict") else dict(sub)
            items = ((sub_dict.get("items") or {}).get("data") or [])
            if not items:
                report["skipped"] += 1
                report["changes"].append({"sub_id": sub.id, "action": "skipped", "reason": "no items"})
                continue
            item = items[0]
            price = item.get("price") or {}
            cur = (price.get("currency") or "aud").lower()
            interval = ((price.get("recurring") or {}).get("interval") or "month").lower()
            amt = price.get("unit_amount") or 0
            plan = _guess_plan_from_metadata(sub_dict)
            if not plan:
                report["skipped"] += 1
                report["changes"].append({"sub_id": sub.id, "action": "skipped",
                                          "reason": "unknown plan tier (missing metadata)",
                                          "current_amount": amt, "currency": cur})
                continue
            target_amt = _TARGET_PRICES.get((cur, interval, plan))
            if target_amt is None:
                report["skipped"] += 1
                report["changes"].append({"sub_id": sub.id, "action": "skipped",
                                          "reason": f"no target for {cur}/{interval}/{plan}"})
                continue
            if amt == target_amt:
                report["already_current"] += 1
                continue
            # Migrate: build new price_data and update the subscription item
            change = {
                "sub_id": sub.id, "customer": sub.customer, "plan": plan, "interval": interval,
                "currency": cur, "from_amount": amt, "to_amount": target_amt, "action": "migrate",
            }
            if dry_run:
                report["migrated"] += 1
                report["changes"].append(change)
                continue
            try:
                _stripe.Subscription.modify(
                    sub.id,
                    items=[{
                        "id": item["id"],
                        "price_data": {
                            "currency": cur,
                            "product": price.get("product"),
                            "unit_amount": target_amt,
                            "recurring": {"interval": interval},
                        },
                    }],
                    proration_behavior="none",  # don't retroactively charge or credit
                    metadata={**(sub_dict.get("metadata") or {}), "migrated_at": datetime.now(timezone.utc).isoformat(),
                              "migrated_from_amount": str(amt), "migrated_to_amount": str(target_amt),
                              "plan": plan},
                )
                report["migrated"] += 1
                change["ok"] = True
                report["changes"].append(change)
            except Exception as e:
                report["failed"] += 1
                change["ok"] = False
                change["error"] = str(e)
                report["changes"].append(change)
                import logging as _lg
                _lg.getLogger("stripe_migration").exception("Stripe migration failed for %s", sub.id)
        if not subs.has_more:
            break
        starting_after = subs.data[-1].id if subs.data else None

    return report


# ─── Static field reader ─────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/reader-assets", StaticFiles(directory=STATIC_DIR), name="reader-assets")
    # Production fallback: in production, nginx intercepts /reader-assets/* with SPA-fallback,
    # so the same files are also accessible under /api/static-asset/* (everything under /api/*
    # is reliably proxied to this FastAPI backend).
    app.mount("/api/static-asset", StaticFiles(directory=STATIC_DIR), name="static-asset")


# Page routing for production — see notes in /app/frontend/src/main.tsx bootloader.
# nginx in production serves the React SPA index.html for /landing, /reader, /ops-dashboard.
# So the React app, on those URLs, fetches /api/page/<name> to render the real content.
@app.get("/api/page/{page_name:path}")
async def api_page(page_name: str):
    """Return static HTML for landing/reader/ops-dashboard, with asset URLs rewritten
    to /api/static-asset/ so production nginx can still load them."""
    # Whitelist of accessible pages (no directory traversal)
    allowed = {
        "landing": "landing.html",
        "landing/success": "success.html",
        "reader": "reader.html",
        "morts-entry": "morts-entry.html",
        "ops-dashboard": "ops-dashboard.html",
        "ops-outreach": "ops-outreach.html",
        "admin": "admin-health.html",
        "admin/health": "admin-health.html",
        "onboarding-guide": "onboarding-guide.html",
        "tools/fcr-calculator": "tools-fcr-calculator.html",
        "tools/grower-payment-calculator": "tools-grower-payment-calculator.html",
        "tools/silo-capacity-calculator": "tools-silo-capacity-calculator.html",
        "ross-308-growth-chart": "ross-308-growth-chart.html",
        "cobb-500-growth-chart": "cobb-500-growth-chart.html",
        "vs/poultrylog": "vs-poultrylog.html",
        "guides/broiler-chicken-growth-stages": "guide-broiler-growth-stages.html",
        "guides/lower-broiler-fcr": "guide-lower-broiler-fcr.html",
        "guides/broiler-chicken-mortality-rates": "guide-broiler-mortality-rates.html",
        "guides/chicken-shed-silo-management": "guide-chicken-shed-silo-management.html",
        "guides/ross-308-vs-cobb-500": "guide-ross-308-vs-cobb-500.html",
        "guides/broiler-grower-payment-explained": "guide-broiler-grower-payment.html",
        "terms": "terms.html",
        "privacy": "privacy.html",
    }
    if page_name not in allowed:
        raise HTTPException(404, "Page not found")
    path = os.path.join(STATIC_DIR, allowed[page_name])
    if not os.path.isfile(path):
        raise HTTPException(404, "File missing")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    # Rewrite asset URLs so they go through /api/* (which nginx proxies to FastAPI)
    html = html.replace("/reader-assets/", "/api/static-asset/")
    # SEO: allow crawlers + browsers to cache marketing pages for 5 minutes.
    # Ops/reader/admin stay uncached (auth-guarded, user-specific).
    is_public = page_name in ("landing", "landing/success", "onboarding-guide", "tools/fcr-calculator", "tools/grower-payment-calculator", "tools/silo-capacity-calculator", "ross-308-growth-chart", "cobb-500-growth-chart", "vs/poultrylog", "guides/broiler-chicken-growth-stages", "guides/lower-broiler-fcr", "guides/broiler-chicken-mortality-rates", "guides/chicken-shed-silo-management", "guides/ross-308-vs-cobb-500", "guides/broiler-grower-payment-explained", "terms", "privacy")
    cache_hdr = "public, max-age=300, s-maxage=600" if is_public else "no-store"
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": cache_hdr})


@app.get("/onboarding-guide")
@app.get("/onboarding-guide/")
async def onboarding_guide_page():
    """Customer onboarding guide — install instructions for iPhone/Android/desktop,
    first-batch walkthrough, Farm Buddy tips. Printable as PDF via the in-page button."""
    return FileResponse(os.path.join(STATIC_DIR, "onboarding-guide.html"))


# ─── SEO magnet pages — public, crawlable, cached ─────────────────────────
@app.get("/tools/fcr-calculator")
@app.get("/tools/fcr-calculator/")
async def fcr_calculator_page():
    """Free FCR + cFCR calculator — SEO magnet targeting 'FCR calculator broiler'
    and long-tail 'how to calculate FCR' queries."""
    path = os.path.join(STATIC_DIR, "tools-fcr-calculator.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=600, s-maxage=1800"})


@app.get("/ross-308-growth-chart")
@app.get("/ross-308-growth-chart/")
async def ross_308_growth_chart_page():
    """Ross 308 target-weight/FCR chart reference page — SEO magnet targeting
    Aviagen Ross 308 keyword cluster."""
    path = os.path.join(STATIC_DIR, "ross-308-growth-chart.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=600, s-maxage=1800"})


# ─── Content hub guides — SEO magnets targeting "broiler"/"chicken" informational searches ───
_GUIDE_FILES = {
    "broiler-chicken-growth-stages": "guide-broiler-growth-stages.html",
    "lower-broiler-fcr": "guide-lower-broiler-fcr.html",
    "broiler-chicken-mortality-rates": "guide-broiler-mortality-rates.html",
    "chicken-shed-silo-management": "guide-chicken-shed-silo-management.html",
    "ross-308-vs-cobb-500": "guide-ross-308-vs-cobb-500.html",
    "broiler-grower-payment-explained": "guide-broiler-grower-payment.html",
}


@app.get("/guides/{slug}")
@app.get("/guides/{slug}/")
async def content_hub_guide_page(slug: str):
    """Content hub guide pages — informational articles targeting broiler/chicken
    searches, each linking into a relevant free calculator or growth chart."""
    if slug not in _GUIDE_FILES:
        raise HTTPException(404, "Guide not found")
    path = os.path.join(STATIC_DIR, _GUIDE_FILES[slug])
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=600, s-maxage=1800"})


@app.get("/reader")
async def reader_page():
    """Mobile field reader UI."""
    return FileResponse(os.path.join(STATIC_DIR, "reader.html"))


@app.get("/reader/")
async def reader_page_slash():
    return FileResponse(os.path.join(STATIC_DIR, "reader.html"))


@app.get("/morts-entry")
async def morts_entry_page():
    """No-login staff morts/culls recording page — opened via the Staff QR
    on BBM's Morts tab, farm-scoped by the ?farm=&t= SEC-006 token."""
    return FileResponse(os.path.join(STATIC_DIR, "morts-entry.html"))


@app.get("/morts-entry/")
async def morts_entry_page_slash():
    return FileResponse(os.path.join(STATIC_DIR, "morts-entry.html"))


@app.get("/terms")
async def terms_page():
    """Public Terms of Service page — linked from landing footer + trial signup."""
    return FileResponse(os.path.join(STATIC_DIR, "terms.html"))


@app.get("/privacy")
async def privacy_page():
    """Public Privacy Policy page — linked from landing footer + trial signup."""
    return FileResponse(os.path.join(STATIC_DIR, "privacy.html"))


# ─── Personalised landing pages from outreach links ───────────────────────
@app.get("/g/{slug}")
async def personalised_landing(slug: str):
    """Tracks a click from an outreach email and redirects to /landing with the
    slug attached so the landing page can personalise the headline. Used for
    direct/non-browser access; the React SPA also has a /g/ shortcut in its
    bootloader for production where nginx falls back to the SPA."""
    from fastapi.responses import RedirectResponse
    await _track_outreach_click(db, slug)
    return RedirectResponse(url=f"/landing?g={slug}", status_code=302)


@app.post("/api/g/{slug}/click")
@app.get("/api/g/{slug}/click")
async def personalised_landing_click(slug: str):
    """Fire-and-forget click bump from the SPA bootloader. Returns a tiny 204."""
    await _track_outreach_click(db, slug)
    return Response(status_code=204)


@app.get("/api/g/{slug}/profile")
async def personalised_landing_profile(slug: str):
    """Used by the landing page JS to look up the personalised name / integrator
    for the visitor (no auth — slug is the only access token)."""
    doc = await db["outreach_contacts"].find_one({"slug": slug})
    if not doc:
        return {"found": False}
    return {
        "found":      True,
        "name":       doc.get("name") or "",
        "email":      doc.get("email") or "",
        "integrator": doc.get("integrator"),
        "farm_name":  doc.get("farm_name") or "",
    }


@app.get("/ops-outreach")
async def ops_outreach_page():
    """Admin-only outreach tracker UI. Auth gating happens client-side via /api/auth/me."""
    return FileResponse(os.path.join(STATIC_DIR, "ops-outreach.html"))
