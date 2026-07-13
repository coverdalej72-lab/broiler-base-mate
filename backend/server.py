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
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, List, Optional

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
payments_col = db["payment_transactions"]
farms_col = db["farms"]

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
from farm_buddy import init_farm_buddy  # noqa: E402
init_farm_buddy(app, db)


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
    def fmt_n(n: Optional[float], suffix: str = "") -> str:
        if n is None or n == 0:
            return "—"
        if isinstance(n, float) and not n.is_integer():
            return f"{n:,.2f}{suffix}"
        return f"{int(n):,}{suffix}"

    def fmt_pct(n: Optional[float]) -> str:
        return "—" if n is None or n == 0 else f"{n:.2f}%"

    # Logo — use the farm's uploaded base64 if provided, else default mark
    logo_src = (
        f"data:image/png;base64,{r.farmLogoData}"
        if r.farmLogoData else
        "https://broilerbasemate.com.au/reader-assets/icon-192.png"
    )
    batch = r.batchName or (f"Batch #{r.batchNumber}" if r.batchNumber else "Batch")
    prev_batch_note = f" · Prev Batch #{r.lastBatchNumber}" if r.lastBatchNumber else ""
    gen   = r.generatedDate or datetime.now(timezone.utc).strftime("%d %b %Y")

    # ── HERO ────────────────────────────────────────────────────────────
    hero = f"""
      <div style="background:linear-gradient(135deg,#0f3d24 0%,#1a5c36 100%);padding:32px 28px;color:#fff;border-radius:16px 16px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td valign="middle" style="padding-right:14px;width:64px;">
            <img src="{logo_src}" alt="" width="56" height="56" style="display:block;border-radius:10px;background:#fff;padding:4px;" />
          </td>
          <td valign="middle">
            <div style="font-size:12px;letter-spacing:2.5px;color:#C9A227;font-weight:700;margin-bottom:2px;">END OF BATCH REPORT</div>
            <div style="font-size:24px;font-weight:800;letter-spacing:-0.4px;line-height:1.15;">{r.farmName or farm_name}</div>
            <div style="font-size:13px;opacity:0.85;margin-top:4px;">{batch}{prev_batch_note} · Generated {gen}</div>
          </td>
        </tr></table>
      </div>
    """

    # ── KPI tiles ───────────────────────────────────────────────────────
    def kpi(label: str, value: str, accent: str = "#0f3d24") -> str:
        return f"""
          <td valign="top" style="padding:6px;">
            <div style="background:#fff;border:1px solid #e3dccb;border-radius:10px;padding:14px 12px;text-align:center;">
              <div style="font-size:22px;font-weight:800;color:{accent};letter-spacing:-0.5px;line-height:1;">{value}</div>
              <div style="font-size:10px;letter-spacing:1.2px;color:#5d6660;text-transform:uppercase;margin-top:6px;font-weight:700;">{label}</div>
            </div>
          </td>
        """
    kpis = f"""
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;">
        <tr>
          {kpi("Birds Placed", fmt_n(r.totalPlaced))}
          {kpi("Birds Caught", fmt_n(r.totalCaught), "#1a5c36")}
          {kpi("Morts", fmt_n(r.totalMorts), "#a83e00")}
          {kpi("Mortality", fmt_pct(r.mortalityPct), "#a83e00")}
        </tr>
        <tr>
          {kpi("Ave Weight", (f"{r.aveWeight:.3f} kg" if r.aveWeight else "—"), "#0f3d24")}
          {kpi("Total KG", (fmt_n(r.totalLiveWeightKg, ' kg') if r.totalLiveWeightKg else "—"), "#0e7c5a")}
          {kpi("Ave Age", (f"{r.actualAge:.1f} d" if r.actualAge else "—"), "#0f3d24")}
          {kpi("Corr. Age", (f"{r.correctedAge:.1f} d" if r.correctedAge else "—"), "#C9A227")}
        </tr>
        <tr>
          {kpi("FCR", (f"{r.fcr:.3f}" if r.fcr else "—"), "#0f3d24")}
          {kpi("cFCR to 2.45", (f"{r.cfcr:.3f}" if r.cfcr else "—"), "#C9A227")}
          {kpi("Total Feed", fmt_n(r.totalPurchased, " kg"), "#C9A227")}
          {kpi("Batch", (r.batchName or "—"), "#5d6660")}
        </tr>
      </table>
    """

    # ── Feed deliveries by feed type ───────────────────────────────────
    def feed_section(ft: EobFeedType) -> str:
        if not ft.rows and not ft.total:
            return ""
        color = ft.color or "#1a5c36"
        rows_html = "".join(
            f"""<tr>
              <td style="padding:7px 12px;border-bottom:1px solid #f0ece1;font-size:13px;color:#1a2320;">{row.date or "—"}</td>
              <td style="padding:7px 12px;border-bottom:1px solid #f0ece1;font-size:13px;color:#5d6660;">{row.docket or "—"}</td>
              <td style="padding:7px 12px;border-bottom:1px solid #f0ece1;font-size:13px;color:#1a2320;text-align:right;font-weight:600;font-variant-numeric:tabular-nums;">{int(row.kg):,} kg</td>
            </tr>"""
            for row in ft.rows if row.kg > 0
        ) or """<tr><td colspan="3" style="padding:14px;text-align:center;color:#9ca3af;font-size:12px;font-style:italic;">No deliveries</td></tr>"""
        return f"""
          <div style="margin-top:16px;border:1px solid #e3dccb;border-radius:10px;overflow:hidden;background:#fff;">
            <div style="background:{color};color:#fff;padding:9px 14px;font-weight:800;letter-spacing:0.5px;font-size:13px;display:flex;justify-content:space-between;align-items:center;">
              <span style="text-transform:uppercase;">{ft.name}</span>
              <span style="font-size:15px;background:rgba(255,255,255,0.18);padding:2px 10px;border-radius:99px;">{int(ft.total):,} kg</span>
            </div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <thead>
                <tr style="background:#faf7ef;">
                  <th style="padding:8px 12px;text-align:left;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Date</th>
                  <th style="padding:8px 12px;text-align:left;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Docket #</th>
                  <th style="padding:8px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Amount</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
          </div>
        """
    feed_sections = "".join(feed_section(ft) for ft in r.feedTypes)
    if not feed_sections:
        feed_sections = """<div style="margin-top:16px;padding:18px;text-align:center;color:#9ca3af;font-size:13px;background:#faf7ef;border:1px dashed #e3dccb;border-radius:10px;">No feed deliveries recorded for this batch.</div>"""

    # ── Per-shed bird table ────────────────────────────────────────────
    if r.sheds:
        # Only show the MTEC column if at least one shed has an MTEC value —
        # keeps the table clean for growers who don't track it.
        show_mtec = any((s.mtec or 0) > 0 for s in r.sheds)
        total_mtec = sum((s.mtec or 0) for s in r.sheds) if show_mtec else 0
        shed_rows = "".join(
            f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #f0ece1;font-weight:700;color:#0f3d24;">Shed {s.shed}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #f0ece1;text-align:right;font-variant-numeric:tabular-nums;">{s.placed:,}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #f0ece1;text-align:right;color:#a83e00;font-variant-numeric:tabular-nums;">{('−' + format(s.morts, ',')) if s.morts > 0 else '—'}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #f0ece1;text-align:right;font-variant-numeric:tabular-nums;">{(format(s.caught, ',') if s.caught > 0 else '—')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #f0ece1;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;color:{'#0f3d24' if s.balance >= 0 else '#a83e00'};">{s.balance:,}</td>
              {f'<td style="padding:8px 12px;border-bottom:1px solid #f0ece1;text-align:right;font-variant-numeric:tabular-nums;color:#5d6660;">{int(s.mtec or 0):,}</td>' if show_mtec else ''}
            </tr>"""
            for s in r.sheds
        )
        totals_row = f"""<tr style="background:#0f3d24;color:#fff;">
            <td style="padding:10px 12px;font-weight:800;letter-spacing:0.5px;text-transform:uppercase;font-size:12px;">Totals</td>
            <td style="padding:10px 12px;text-align:right;font-weight:800;font-variant-numeric:tabular-nums;">{fmt_n(r.totalPlaced)}</td>
            <td style="padding:10px 12px;text-align:right;font-weight:800;color:#ffb3a7;font-variant-numeric:tabular-nums;">{('−' + (fmt_n(r.totalMorts))) if (r.totalMorts or 0) > 0 else '—'}</td>
            <td style="padding:10px 12px;text-align:right;font-weight:800;font-variant-numeric:tabular-nums;">{fmt_n(r.totalCaught)}</td>
            <td style="padding:10px 12px;text-align:right;font-weight:800;font-variant-numeric:tabular-nums;">{fmt_n(r.totalBalance)}</td>
            {f'<td style="padding:10px 12px;text-align:right;font-weight:800;font-variant-numeric:tabular-nums;">{int(total_mtec):,}</td>' if show_mtec else ''}
          </tr>"""
        mtec_header = '<th style="padding:9px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">MTEC</th>' if show_mtec else ''
        bird_section = f"""
          <h3 style="margin:28px 0 10px;color:#0f3d24;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🐔 Bird Summary</h3>
          <div style="background:#fff;border:1px solid #e3dccb;border-radius:10px;overflow:hidden;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:13px;">
              <thead>
                <tr style="background:#faf7ef;">
                  <th style="padding:9px 12px;text-align:left;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Shed</th>
                  <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Placed</th>
                  <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Morts</th>
                  <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Caught</th>
                  <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Balance</th>
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
      <h3 style="margin:28px 0 10px;color:#0f3d24;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🌾 Feed Summary</h3>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fff;border:1px solid #e3dccb;border-radius:10px;overflow:hidden;font-size:13px;">
        <tr><td style="padding:9px 14px;border-bottom:1px solid #f0ece1;color:#5d6660;">Last Batch Left</td><td style="padding:9px 14px;border-bottom:1px solid #f0ece1;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{fmt_n(r.lastBatchLeft, ' kg')}</td></tr>
        <tr><td style="padding:9px 14px;border-bottom:1px solid #f0ece1;color:#5d6660;">Total Delivered</td><td style="padding:9px 14px;border-bottom:1px solid #f0ece1;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{fmt_n(r.totalDelivered, ' kg')}</td></tr>
        <tr><td style="padding:9px 14px;border-bottom:1px solid #f0ece1;color:#5d6660;">Total Used</td><td style="padding:9px 14px;border-bottom:1px solid #f0ece1;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{fmt_n(r.totalUsed, ' kg')}</td></tr>
        <tr><td style="padding:9px 14px;border-bottom:1px solid #f0ece1;color:#5d6660;">Feed Left</td><td style="padding:9px 14px;border-bottom:1px solid #f0ece1;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;">{fmt_n(r.feedLeft, ' kg')}</td></tr>
        <tr style="background:#fff8e2;"><td style="padding:11px 14px;color:#0f3d24;font-weight:800;letter-spacing:0.4px;text-transform:uppercase;font-size:12px;">Net Consumed</td><td style="padding:11px 14px;text-align:right;font-weight:900;font-variant-numeric:tabular-nums;color:#0f3d24;font-size:14px;">{fmt_n(r.netConsumed, ' kg')}</td></tr>
      </table>
    """

    # ── Final assembly ─────────────────────────────────────────────────
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8" />
<title>End of Batch — {farm_name}</title></head>
<body style="margin:0;padding:24px 12px;background:#f3f0e8;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Roboto,sans-serif;color:#1a2320;-webkit-font-smoothing:antialiased;">
  <div style="max-width:680px;margin:0 auto;background:#faf7ef;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px -12px rgba(15,61,36,0.18);">
    {hero}
    <div style="padding:20px 24px 28px;">
      <h3 style="margin:0 0 4px;color:#0f3d24;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">📊 Batch Performance</h3>
      {kpis}
      <h3 style="margin:28px 0 10px;color:#0f3d24;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🚚 Feed Deliveries</h3>
      {feed_sections}
      {bird_section}
      {feed_summary}
      <div style="margin-top:32px;padding:18px;background:#fff;border-radius:10px;border:1px dashed #e3dccb;text-align:center;color:#5d6660;font-size:12px;line-height:1.6;">
        Generated by <b style="color:#0f3d24;">Broiler Base Mate™</b> · <a href="https://broilerbasemate.com.au" style="color:#1a5c36;text-decoration:none;font-weight:700;">broilerbasemate.com.au</a><br />
        <span style="opacity:0.7;">Sent by {sender}</span>
      </div>
    </div>
  </div>
</body></html>"""


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
    if req.report:
        html = _render_eob_html(req.report, req.farmName or "Broiler Base Mate", user.get("email") or "")
    else:
        # Legacy fallback — plain text dump (for backwards compat)
        safe_body = (
            req.body
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        html = (
            "<div style=\"font-family:system-ui,sans-serif;max-width:700px;margin:0 auto;\">"
            f"<h2 style=\"color:#0f3d24;margin-bottom:4px;\">End of Batch Report</h2>"
            f"<p style=\"color:#64748b;font-size:13px;margin:0 0 14px;\">{farm_label} · sent by {user.get('email')}</p>"
            "<pre style=\"background:#f7faf6;border:1px solid #d4e0d8;border-radius:8px;padding:16px;"
            "font-family:monospace;font-size:12px;line-height:1.5;white-space:pre-wrap;color:#1f2937;\">"
            f"{safe_body}"
            "</pre>"
            "<p style=\"color:#9ca3af;font-size:11px;margin-top:18px;\">"
            "Sent by Broiler Base Mate — broilerbasemate.com.au"
            "</p>"
            "</div>"
        )

    sent_to: list[str] = []
    failed_to: list[str] = []
    for addr in req.to:
        addr = addr.strip()
        if not addr or "@" not in addr:
            failed_to.append(addr)
            continue
        try:
            await send_email(
                to=addr,
                subject=req.subject,
                html=html,
                reply_to=user.get("email"),
            )
            sent_to.append(addr)
        except Exception as e:
            _log.warning("EOB email send failed for %s: %s", addr, e)
            failed_to.append(addr)

    if not sent_to:
        raise HTTPException(500, "Could not send to any of the recipients. Check the email service is configured.")

    return {"ok": True, "sent": sent_to, "failed": failed_to}


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
                          totals: dict, farm_logo_data: Optional[str]) -> str:
    """Premium branded history email — silo readings + deliveries + totals."""
    logo_src = (
        f"data:image/png;base64,{farm_logo_data}"
        if farm_logo_data else
        "https://broilerbasemate.com.au/reader-assets/icon-192.png"
    )
    period = "today" if days == 1 else f"last {days} days"

    # KPI tiles
    def kpi(label: str, val: str, accent: str = "#0f3d24") -> str:
        return f"""<td valign="top" style="padding:6px;"><div style="background:#fff;border:1px solid #e3dccb;border-radius:10px;padding:14px 12px;text-align:center;">
          <div style="font-size:22px;font-weight:800;color:{accent};letter-spacing:-0.5px;line-height:1;">{val}</div>
          <div style="font-size:10px;letter-spacing:1.2px;color:#5d6660;text-transform:uppercase;margin-top:6px;font-weight:700;">{label}</div>
        </div></td>"""
    kpis = f"""<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;"><tr>
      {kpi("Total Feed on Farm", f"{totals.get('totalT', 0):,.1f} t", "#0f3d24")}
      {kpi("Sheds Reporting", str(totals.get('shedsReporting', 0)), "#1a5c36")}
      {kpi("Silos Read", str(totals.get('silosRead', 0)), "#C9A227")}
      {kpi("Deliveries", str(len(deliveries)), "#a83e00")}
    </tr></table>"""

    # Silo readings — grouped by shed
    silo_html = ""
    for grp in silos_grouped:
        silo_rows = "".join(
            f"""<tr><td style="padding:7px 12px;border-bottom:1px solid #f0ece1;font-weight:700;color:#0f3d24;">Silo {s['letter']}</td>
              <td style="padding:7px 12px;border-bottom:1px solid #f0ece1;text-align:right;font-variant-numeric:tabular-nums;font-weight:600;">{s['amount']:,.2f} t</td>
              <td style="padding:7px 12px;border-bottom:1px solid #f0ece1;text-align:right;color:#5d6660;font-size:12px;">{s.get('readAt','')}</td></tr>"""
            for s in grp['silos']
        ) or """<tr><td colspan="3" style="padding:14px;text-align:center;color:#9ca3af;font-size:12px;font-style:italic;">No readings yet</td></tr>"""
        silo_html += f"""<div style="margin-top:14px;background:#fff;border:1px solid #e3dccb;border-radius:10px;overflow:hidden;">
          <div style="background:#0f3d24;color:#fff;padding:9px 14px;font-weight:800;letter-spacing:0.5px;font-size:13px;display:flex;justify-content:space-between;">
            <span style="text-transform:uppercase;">{grp['name']}</span>
            <span style="font-size:15px;background:rgba(201,162,39,0.32);padding:2px 10px;border-radius:99px;">{grp.get('groupTotalT', 0):,.1f} t</span>
          </div>
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <thead><tr style="background:#faf7ef;">
              <th style="padding:8px 12px;text-align:left;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Silo</th>
              <th style="padding:8px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Amount</th>
              <th style="padding:8px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Last Read</th>
            </tr></thead>
            <tbody>{silo_rows}</tbody>
          </table></div>"""

    # Deliveries table
    if deliveries:
        delivery_rows = "".join(
            f"""<tr><td style="padding:8px 12px;border-bottom:1px solid #f0ece1;font-size:13px;">{d.get('date','—')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #f0ece1;font-size:13px;color:#5d6660;">{d.get('supplier','—')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #f0ece1;font-size:13px;color:#5d6660;">{d.get('feedType','—')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #f0ece1;font-size:13px;text-align:right;font-variant-numeric:tabular-nums;font-weight:700;">{d.get('amountT', 0):,.2f} t</td></tr>"""
            for d in deliveries
        )
        delivery_section = f"""<h3 style="margin:28px 0 10px;color:#0f3d24;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🚚 Feed Deliveries ({period})</h3>
        <div style="background:#fff;border:1px solid #e3dccb;border-radius:10px;overflow:hidden;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <thead><tr style="background:#faf7ef;">
              <th style="padding:9px 12px;text-align:left;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Date</th>
              <th style="padding:9px 12px;text-align:left;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Supplier</th>
              <th style="padding:9px 12px;text-align:left;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Feed Type</th>
              <th style="padding:9px 12px;text-align:right;font-size:10px;color:#5d6660;letter-spacing:1px;text-transform:uppercase;font-weight:700;border-bottom:1px solid #e3dccb;">Amount</th>
            </tr></thead>
            <tbody>{delivery_rows}</tbody>
          </table></div>"""
    else:
        delivery_section = ""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8" /><title>Farm History — {farm_name}</title></head>
<body style="margin:0;padding:24px 12px;background:#f3f0e8;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Roboto,sans-serif;color:#1a2320;-webkit-font-smoothing:antialiased;">
  <div style="max-width:680px;margin:0 auto;background:#faf7ef;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px -12px rgba(15,61,36,0.18);">
    <div style="background:linear-gradient(135deg,#0f3d24 0%,#1a5c36 100%);padding:32px 28px;color:#fff;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        <td valign="middle" style="padding-right:14px;width:64px;">
          <img src="{logo_src}" alt="" width="56" height="56" style="display:block;border-radius:10px;background:#fff;padding:4px;" />
        </td>
        <td valign="middle">
          <div style="font-size:12px;letter-spacing:2.5px;color:#C9A227;font-weight:700;margin-bottom:2px;">FARM HISTORY SNAPSHOT</div>
          <div style="font-size:24px;font-weight:800;letter-spacing:-0.4px;line-height:1.15;">{farm_name}</div>
          <div style="font-size:13px;opacity:0.85;margin-top:4px;">Covering the {period}</div>
        </td>
      </tr></table>
    </div>
    <div style="padding:20px 24px 28px;">
      <h3 style="margin:0 0 4px;color:#0f3d24;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">📊 At a Glance</h3>
      {kpis}
      <h3 style="margin:28px 0 10px;color:#0f3d24;font-size:14px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800;border-bottom:2px solid #C9A227;padding-bottom:6px;">🌾 Silo Readings (latest per silo)</h3>
      {silo_html or '<div style="margin-top:14px;padding:18px;text-align:center;color:#9ca3af;font-size:13px;background:#faf7ef;border:1px dashed #e3dccb;border-radius:10px;">No silo readings yet.</div>'}
      {delivery_section}
      <div style="margin-top:32px;padding:18px;background:#fff;border-radius:10px;border:1px dashed #e3dccb;text-align:center;color:#5d6660;font-size:12px;line-height:1.6;">
        Generated by <b style="color:#0f3d24;">Broiler Base Mate™</b> · <a href="https://broilerbasemate.com.au" style="color:#1a5c36;text-decoration:none;font-weight:700;">broilerbasemate.com.au</a><br />
        <span style="opacity:0.7;">Sent by {sender}</span>
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

    # ── Latest silo reading per silo (within window), grouped by shed group
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
    html = _render_history_html(farm_name, days, user.get("email") or "", silos_grouped, deliveries, totals, req.farmLogoData)

    sent, failed = [], []
    subject = f"📊 Farm History — {farm_name} ({'today' if days == 1 else f'last {days} days'})"
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
    subj = f"📊 Monthly Snapshot — {farm_name} ({now.strftime('%b %Y')})"
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
    """Ensure the current user can access this farm slug. Returns user dict; raises 401/403."""
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
async def batch_reset(farm: str = Query(default=DEFAULT_FARM_ID)):
    """New Batch: wipe this farm's readings, deliveries, and photos so app starts empty."""
    r = await readings_col.delete_many(_farm_filter(farm))
    d = await deliveries_col.delete_many(_farm_filter(farm))
    p = await photos_col.delete_many(_farm_filter(farm))
    return {"ok": True, "readingsDeleted": r.deleted_count, "deliveriesDeleted": d.deleted_count, "photosDeleted": p.deleted_count}


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
async def list_shed_groups(farm: str = Query(default=DEFAULT_FARM_ID)):
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
                    name=s["name"],
                    defaultFeedType=s.get("defaultFeedType"),
                )
                for s in silos if s.get("shedGroupId") == g["id"]
            ],
        ))
    return out


# ── Silos ────────────────────────────────────────────────────────────────
@api.get("/silos")
async def list_silos(farm: str = Query(default=DEFAULT_FARM_ID)):
    silos = await silos_col.find(_farm_filter(farm)).sort("letter", 1).to_list(length=1000)
    return [clean(s) for s in silos]


@api.post("/silos", status_code=201)
async def create_silo(body: CreateSiloBody, farm: str = Query(default=DEFAULT_FARM_ID)):
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
async def update_silo(silo_id: str, body: UpdateSiloBody):
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not patch:
        raise HTTPException(400, "Nothing to update")
    res = await silos_col.find_one_and_update({"id": silo_id}, {"$set": patch}, return_document=True)
    if res is None:
        raise HTTPException(404, "Silo not found")
    return clean(res)


@api.delete("/silos/{silo_id}", status_code=204)
async def delete_silo(silo_id: str):
    res = await silos_col.delete_one({"id": silo_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Silo not found")
    return JSONResponse(content=None, status_code=204)


# ── Readings ─────────────────────────────────────────────────────────────
@api.get("/readings/today")
async def readings_today(localDate: Optional[str] = Query(default=None), farm: str = Query(default=DEFAULT_FARM_ID)):
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
                "name": s.get("name", s.get("letter", "Silo")),
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
async def farm_buddy_alerts(farm: str = Query(default=DEFAULT_FARM_ID)):
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
async def batch_create_readings(body: BatchCreateReadingsBody, farm: str = Query(default=DEFAULT_FARM_ID)):
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
            "siloName": silo.get("name", "") if silo else "",
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
async def list_readings(limit: int = Query(default=100, le=1000), siloId: Optional[str] = None, farm: str = Query(default=DEFAULT_FARM_ID)):
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
            "siloName": silo.get("name", "") if silo else "",
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
async def delete_reading(reading_id: str):
    res = await readings_col.delete_one({"id": reading_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Reading not found")
    return JSONResponse(content=None, status_code=204)


# ── Deliveries ───────────────────────────────────────────────────────────
@api.get("/deliveries")
async def list_deliveries(limit: int = Query(default=100, le=1000), farm: str = Query(default=DEFAULT_FARM_ID)):
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
async def create_delivery(body: CreateDeliveryBody, farm: str = Query(default=DEFAULT_FARM_ID)):
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
async def delete_delivery(delivery_id: str):
    res = await deliveries_col.delete_one({"id": delivery_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Delivery not found")
    return JSONResponse(content=None, status_code=204)


# ── Stubs (unused in standalone but called by silo-tracker) ───────────────
@api.post("/weigh-bird")
async def weigh_bird(payload: dict):
    return {"ok": True, "stored": False}


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
import re
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


@api.get("/farm-config")
async def get_farm_config(farm: str = Query(default=DEFAULT_FARM_ID)):
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


@api.patch("/farm-config")
async def patch_farm_config(body: FarmConfigBody, farm: str = Query(default=DEFAULT_FARM_ID)):
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
async def get_feed_program_state(farm: str = Query(default=DEFAULT_FARM_ID)):
    doc = await feed_program_state_col.find_one({"farmId": farm})
    if not doc:
        return {"edits": None, "sheetNames": None, "updatedAt": None}
    return {
        "edits": doc.get("edits"),
        "sheetNames": doc.get("sheetNames") or [],
        "updatedAt": doc.get("updatedAt"),
    }


@api.put("/feed-program/state")
async def put_feed_program_state(body: FeedProgramStateBody, farm: str = Query(default=DEFAULT_FARM_ID)):
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


# ── Feed-program history (Cloud Rewind) ──────────────────────────────────
# Server-side snapshot list — always available regardless of which device / browser
# the user is on. Restores are non-destructive: the current state is snapshotted
# before it's replaced so an accidental rewind is itself rewind-able.


@api.get("/feed-program/history")
async def list_feed_program_history(farm: str = Query(default=DEFAULT_FARM_ID)):
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
async def get_feed_program_history_item(snap_id: str, farm: str = Query(default=DEFAULT_FARM_ID)):
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
async def restore_feed_program_history(snap_id: str, farm: str = Query(default=DEFAULT_FARM_ID)):
    """Restore a snapshot as the current state. Snapshots current state first so the
    restore itself is rewind-able. Returns the restored state so the client can
    immediately re-hydrate without a second GET."""
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
async def list_photos(category: Optional[str] = None, shedNumber: Optional[int] = None, farm: str = Query(default=DEFAULT_FARM_ID)):
    q: dict = dict(_farm_filter(farm))
    if category:
        q = _and(q, {"category": category})
    if shedNumber is not None:
        q = _and(q, {"shedNumber": shedNumber})
    rows = await photos_col.find(q).sort("createdAt", -1).limit(200).to_list(length=200)
    return [clean(r) for r in rows]


@api.post("/photos", status_code=201)
async def create_photo(body: CreatePhotoBody, farm: str = Query(default=DEFAULT_FARM_ID)):
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
async def delete_photo(photo_id: str):
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
PACKAGES = {
    # 🚀 LAUNCH SPECIAL — all prices halved to drive sign-ups. Original RRP
    # shown in comments so they can be restored later by doubling the amount.
    # Subscription plans (charged as one-off first-month for v1; user upgrades to recurring in Stripe dashboard)
    "bronze_monthly":      {"label": "Bronze",   "amount": 25.0,  "kind": "subscription"},   # RRP $50
    "silver_monthly":      {"label": "Silver",   "amount": 37.50, "kind": "subscription"},   # RRP $75
    "gold_monthly":        {"label": "Gold",     "amount": 50.0,  "kind": "subscription"},   # RRP $100
    "platinum_monthly":    {"label": "Platinum", "amount": 75.0,  "kind": "subscription"},   # RRP $150
    "ops_bronze":          {"label": "Ops Pack (Bronze farms)",   "amount": 25.0,  "kind": "subscription"},   # RRP $50
    "ops_silver":          {"label": "Ops Pack (Silver farms)",   "amount": 37.50, "kind": "subscription"},   # RRP $75
    "ops_gold":            {"label": "Ops Pack (Gold farms)",     "amount": 50.0,  "kind": "subscription"},   # RRP $100
    "ops_platinum":        {"label": "Ops Pack (Platinum farms)", "amount": 75.0,  "kind": "subscription"},   # RRP $150
    # Annual variants (~15% off RRP, then halved)
    "bronze_annual":       {"label": "Bronze Annual",   "amount": 255.0,  "kind": "subscription"},   # RRP $510
    "silver_annual":       {"label": "Silver Annual",   "amount": 510.0,  "kind": "subscription"},   # RRP $1020
    "gold_annual":         {"label": "Gold Annual",     "amount": 765.0,  "kind": "subscription"},   # RRP $1530
    # Operation Manager Pack — multi-farm bundles for ops managers
    "ops_bronze":          {"label": "Ops Manager — Bronze (≤6 sheds/farm)",  "amount": 25.0,  "kind": "ops_bundle"},   # RRP $50
    "ops_silver":          {"label": "Ops Manager — Silver (7-12 sheds/farm)", "amount": 45.0, "kind": "ops_bundle"},   # RRP $90
    "ops_gold":            {"label": "Ops Manager — Gold (12+ sheds/farm)",   "amount": 75.0,  "kind": "ops_bundle"},   # RRP $150
    # Sponsor tiers
    "sponsor_10":          {"label": "Sponsor — $5/mo",   "amount": 5.0,   "kind": "sponsor"},   # RRP $10
    "sponsor_25":          {"label": "Sponsor — $12.50/mo", "amount": 12.50, "kind": "sponsor"}, # RRP $25
    "sponsor_50":          {"label": "Sponsor — $25/mo",  "amount": 25.0,  "kind": "sponsor"},   # RRP $50
    # One-off donations
    "back_seed":           {"label": "Seed Supporter",       "amount": 50.0,   "kind": "donation"},   # RRP $100
    "back_project":        {"label": "Project Backer",       "amount": 250.0,  "kind": "donation"},   # RRP $500
    "back_foundation":     {"label": "Foundation Partner",   "amount": 500.0,  "kind": "donation"},   # RRP $1000
}


class CheckoutFarmConfig(BaseModel):
    name: str
    tier: Optional[str] = None  # "bronze" | "silver" | "gold"


class CheckoutRequest(BaseModel):
    packageId: str
    originUrl: str
    email: Optional[str] = None
    farms: Optional[List[CheckoutFarmConfig]] = None  # for ops_* bundles
    buyerName: Optional[str] = None
    ref: Optional[str] = None  # referral code (farm slug) of the grower who referred this buyer


@app.post("/api/checkout")
async def create_checkout(body: CheckoutRequest):
    if body.packageId not in PACKAGES:
        raise HTTPException(400, "Invalid package")
    pkg = PACKAGES[body.packageId]
    try:
        from emergentintegrations.payments.stripe.checkout import StripeCheckout, CheckoutSessionRequest
    except Exception as e:
        raise HTTPException(503, f"Stripe lib missing: {e}")

    api_key = os.environ.get("STRIPE_API_KEY")
    if not api_key:
        raise HTTPException(503, "STRIPE_API_KEY not configured")

    origin = body.originUrl.rstrip("/")
    webhook_url = f"{origin}/api/webhook/stripe"
    checkout = StripeCheckout(api_key=api_key, webhook_url=webhook_url)

    success_url = f"{origin}/landing/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin}/landing"

    meta = {"package_id": body.packageId, "kind": pkg["kind"], "label": pkg["label"]}
    if body.email: meta["email"] = body.email
    if body.ref: meta["ref"] = body.ref.strip().lower()

    # Calculate dynamic amount for ops_bundle if farms list is provided
    amount = float(pkg["amount"])
    if pkg["kind"] == "ops_bundle" and body.farms:
        tier_prices = {"bronze": 25.0, "silver": 45.0, "gold": 75.0}  # LAUNCH 50% OFF — RRP $50/$90/$150
        amount = sum(tier_prices.get((f.tier or "bronze").lower(), 50.0) for f in body.farms)

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
    try:
        from emergentintegrations.payments.stripe.checkout import StripeCheckout
    except Exception as e:
        raise HTTPException(503, f"Stripe lib missing: {e}")
    api_key = os.environ.get("STRIPE_API_KEY")
    checkout = StripeCheckout(api_key=api_key, webhook_url="")
    s = await checkout.get_checkout_status(session_id)
    # Update DB only if status changed (idempotent)
    cur = await payments_col.find_one({"session_id": session_id})
    if cur and cur.get("payment_status") != s.payment_status:
        await payments_col.update_one({"session_id": session_id},
            {"$set": {"payment_status": s.payment_status, "status": s.status,
                      "updatedAt": datetime.now(timezone.utc)}})

    # Auto-onboarding: when payment first becomes 'paid', provision farms + email buyer
    onboarding = None
    if cur and s.payment_status == "paid" and not cur.get("provisioned"):
        try:
            onboarding = await _provision_purchase(session_id)
        except Exception as prov_err:
            # Don't fail the status check the buyer is watching — log and let the
            # next status poll / webhook retry. Admin gets emailed once per error.
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

    return {"status": s.status, "payment_status": s.payment_status, "amount_total": s.amount_total,
            "currency": s.currency, "metadata": s.metadata, "onboarding": onboarding}


async def _provision_purchase(session_id: str) -> Optional[dict]:
    """When a Stripe session lands as 'paid', create farms + email buyer (idempotent)."""
    from email_service import send_email
    cur = await payments_col.find_one({"session_id": session_id})
    if not cur or cur.get("provisioned"):
        return None

    kind = cur.get("kind")
    buyer_email = cur.get("email")
    buyer_name = cur.get("buyerName") or "there"
    public_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    created_farms: List[dict] = []

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
            }
            await farms_col.insert_one(doc)
            await _seed_farm(slug, name)
            reader_url = f"{public_url}/reader?farm={slug}" if public_url else f"/reader?farm={slug}"
            created_farms.append({"slug": slug, "name": name, "tier": f.get("tier"), "readerUrl": reader_url})

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
        }
        await farms_col.insert_one(doc)
        await _seed_farm(slug, doc["name"])
        reader_url = f"{public_url}/reader?farm={slug}" if public_url else f"/reader?farm={slug}"
        created_farms.append({"slug": slug, "name": doc["name"], "tier": doc["tier"], "readerUrl": reader_url})

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

    # Mark provisioned
    await payments_col.update_one(
        {"session_id": session_id},
        {"$set": {"provisioned": True, "createdFarms": created_farms, "provisionedAt": datetime.now(timezone.utc)}},
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


@app.post("/api/webhook/stripe")
async def stripe_webhook(request: Request):
    raw = await request.body() if hasattr(request, "body") else b""
    sig = request.headers.get("Stripe-Signature", "") if hasattr(request, "headers") else ""
    try:
        from emergentintegrations.payments.stripe.checkout import StripeCheckout
        api_key = os.environ.get("STRIPE_API_KEY")
        checkout = StripeCheckout(api_key=api_key, webhook_url="")
        evt = await checkout.handle_webhook(raw, sig)
        await payments_col.update_one({"session_id": evt.session_id},
            {"$set": {"payment_status": evt.payment_status, "webhook_event": evt.event_type,
                      "updatedAt": datetime.now(timezone.utc)}})
        # Auto-onboard on first paid webhook (idempotent — _provision_purchase skips if already provisioned)
        if evt.payment_status == "paid":
            try:
                await _provision_purchase(evt.session_id)
            except Exception as prov_err:
                # Never fail the webhook just because onboarding hit an issue —
                # the user has paid, we must ack 200 so Stripe doesn't retry storm.
                # Surface to admin so we can manually fix the missed provisioning.
                import logging as _logging
                _logging.exception("Auto-onboarding from webhook failed: %s", prov_err)
                try:
                    await log_error(
                        db,
                        category="stripe_provision",
                        message=f"Webhook provisioning failed for session {evt.session_id}: {prov_err}",
                        details={"session_id": evt.session_id, "event_type": evt.event_type},
                        request=request,
                    )
                except Exception:
                    pass
    except Exception as e:
        # Webhook signature mismatch or library error — log loudly, but still 200
        # so Stripe doesn't lock us out of future events. (Bad sig = nothing happened.)
        try:
            await log_error(
                db,
                category="stripe_webhook",
                message=f"Webhook handler error: {e}",
                request=request,
            )
        except Exception:
            pass
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
async def list_farms():
    rows = await farms_col.find().sort("createdAt", 1).to_list(length=500)
    out = []
    for r in rows:
        slug = r.get("slug")
        # count rows per farm for stats
        f_filter = _farm_filter(slug) if slug else {}
        readings_count = await readings_col.count_documents(f_filter)
        deliveries_count = await deliveries_col.count_documents(f_filter)
        out.append({
            "id": r["id"],
            "slug": slug,
            "name": r.get("name"),
            "ownerEmail": r.get("ownerEmail"),
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
        "ops-dashboard": "ops-dashboard.html",
        "ops-outreach": "ops-outreach.html",
        "admin": "admin-health.html",
        "admin/health": "admin-health.html",
        "onboarding-guide": "onboarding-guide.html",
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
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.get("/onboarding-guide")
@app.get("/onboarding-guide/")
async def onboarding_guide_page():
    """Customer onboarding guide — install instructions for iPhone/Android/desktop,
    first-batch walkthrough, Farm Buddy tips. Printable as PDF via the in-page button."""
    return FileResponse(os.path.join(STATIC_DIR, "onboarding-guide.html"))


@app.get("/reader")
async def reader_page():
    """Mobile field reader UI."""
    return FileResponse(os.path.join(STATIC_DIR, "reader.html"))


@app.get("/reader/")
async def reader_page_slash():
    return FileResponse(os.path.join(STATIC_DIR, "reader.html"))


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
