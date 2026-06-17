"""Outreach tracker — manage cold-email lists, drip follow-ups, click/open tracking,
and personalised landing pages.

Admin-only (gated by SUPERUSER_EMAILS in .env). Drip cadence: day 3 / day 7 / day 14
follow-ups after the initial "sent" timestamp, with templated emails via Resend.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr

logger = logging.getLogger("outreach")

# ─── Status & integrator detection helpers ─────────────────────────────────
STATUSES = ("cold", "sent", "opened", "replied", "trialled", "won", "lost")

INTEGRATOR_MAP = [
    ("proten.com.au",            "ProTen"),
    ("baiada.com.au",            "Baiada"),
    ("cerespoultry.com.au",      "Ceres Poultry"),
    ("scfaustralia.com.au",      "SCF Australia"),
    ("baqerifarming.com.au",     "Baqeri Farming"),
    ("southernfreerange.com.au", "Southern Free Range"),
    ("southernbarn.com.au",      "Southern Barn"),
    ("feratiholdings.com.au",    "Ferati Holdings"),
    ("aussiebb.com.au",          "Aussie BB"),
    ("ingham",                   "Ingham's"),
    ("ridley",                   "Ridley"),
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NAME_BEFORE_EMAIL_RE = re.compile(r'"?([^<"\n]+?)"?\s*<\s*([^>]+)\s*>')


def _detect_integrator(email: str) -> Optional[str]:
    e = (email or "").lower()
    for key, label in INTEGRATOR_MAP:
        if key in e:
            return label
    return None


def _slug_from_email(email: str) -> str:
    local, _, dom = email.partition("@")
    safe_local = re.sub(r"[^a-z0-9]+", "-", local.lower()).strip("-")[:24]
    safe_dom   = re.sub(r"[^a-z0-9]+", "-", dom.lower().split(".")[0]).strip("-")[:16]
    # Short random suffix so the slug isn't guessable
    rand = uuid.uuid4().hex[:6]
    return f"{safe_local}-{safe_dom}-{rand}".strip("-")


def _parse_email_list(raw: str) -> list[dict]:
    """Parse a pasted blob into a list of {email, name} dicts. Handles 'Name <email>',
    quoted, comma-separated, newline-separated, semicolon-separated all at once."""
    if not raw:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    # First, pull out 'Name <email>' pairs
    for m in NAME_BEFORE_EMAIL_RE.finditer(raw):
        name = (m.group(1) or "").strip().strip('"').strip("'").strip(",").strip(";").strip()
        email = (m.group(2) or "").strip().lower()
        if not email or email in seen:
            continue
        if "@" not in email:
            continue
        seen.add(email)
        # Discard if "name" is actually a bare email (e.g. duplicated)
        clean_name = "" if (not name or "@" in name) else name
        out.append({"email": email, "name": clean_name})
    # Then pull out remaining bare emails
    for m in EMAIL_RE.finditer(raw):
        email = m.group(0).strip().lower()
        if email in seen:
            continue
        seen.add(email)
        out.append({"email": email, "name": ""})
    return out


def _backend_base_url() -> str:
    """Public URL to use in email links (for tracking + landing pages)."""
    return (os.environ.get("PUBLIC_BACKEND_URL") or
            os.environ.get("FRONTEND_URL") or
            "https://broilerbasemate.com.au").rstrip("/")


# ─── Email templates ───────────────────────────────────────────────────────
SENDER_NAME = os.environ.get("OUTREACH_SENDER_NAME", "Broiler Base Mate")
SENDER_FIRST = os.environ.get("OUTREACH_SENDER_FIRST", "the team at Broiler Base Mate")


def _greeting(contact: dict) -> str:
    nm = (contact.get("name") or "").strip()
    if nm and " " in nm:
        nm = nm.split()[0]
    if nm:
        return f"G'day {nm}"
    integ = contact.get("integrator")
    if integ:
        return f"G'day from one {integ} grower to another"
    return "G'day"


def _tracking_pixel(contact_id: str) -> str:
    return f'<img src="{_backend_base_url()}/api/ops/outreach/{contact_id}/open.gif" width="1" height="1" alt="" style="display:block;border:0;">'


def _personal_link(contact: dict) -> str:
    return f"{_backend_base_url()}/g/{contact['slug']}"


def _wrap(body_html: str, contact_id: str) -> str:
    return f"""<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:560px;margin:0 auto;padding:24px;line-height:1.55;color:#1a1a1a;">
  {body_html}
  <hr style="border:0;border-top:1px solid #e0e8e4;margin:24px 0 12px;">
  <div style="font-size:11px;color:#888;text-align:center;">
    Broiler Base Mate · poultry farm management built by an Aussie grower<br>
    <a href="{_backend_base_url()}/" style="color:#1a5c36;">broilerbasemate.com.au</a>
  </div>
  {_tracking_pixel(contact_id)}
</div>"""


def render_initial(contact: dict) -> tuple[str, str]:
    integ = contact.get("integrator") or "your operation"
    return (
        f"Saw your farm — built something that might save you a few hours a week",
        _wrap(f"""
            <p>{_greeting(contact)},</p>
            <p>I'm a 3rd-generation Aussie poultry grower. Got fed up with the integrator
               Excel spreadsheet so I built <strong>Broiler Base Mate</strong> — a Feed
               Program replacement that auto-syncs silo readings, scans feed dockets with AI,
               and runs the FCR maths for you across the whole farm.</p>
            <p>It's running on my own farm and now a few others around {integ}. Thought
               you might want a look. <strong>14-day free trial, no card needed.</strong></p>
            <p style="text-align:center;margin:24px 0;">
              <a href="{_personal_link(contact)}" style="background:#1a5c36;color:#fff;padding:12px 24px;border-radius:99px;text-decoration:none;font-weight:800;display:inline-block;">See the 60-second demo →</a>
            </p>
            <p>Happy to jump on a quick call if you've got 10 minutes — just hit reply.</p>
            <p>Cheers,<br>{SENDER_FIRST}</p>
        """, contact["id"]),
    )


def render_followup_d3(contact: dict) -> tuple[str, str]:
    return (
        f"Re: did you get a chance to look?",
        _wrap(f"""
            <p>{_greeting(contact)},</p>
            <p>Just bumping this up in case it got buried — sent you a note about
               <strong>Broiler Base Mate</strong>, the Feed Program replacement I've built.</p>
            <p>If you've got 60 seconds, the animated demo on the site shows the whole flow:
               placement → silo readings → AI docket scanning → FCR forecast across all sheds.</p>
            <p style="text-align:center;margin:24px 0;">
              <a href="{_personal_link(contact)}" style="background:#1a5c36;color:#fff;padding:12px 24px;border-radius:99px;text-decoration:none;font-weight:800;display:inline-block;">Watch the 60-sec demo →</a>
            </p>
            <p>If now's not the right time, all good — just hit reply with "not now" and I'll
               quit nudging.</p>
            <p>Cheers,<br>{SENDER_FIRST}</p>
        """, contact["id"]),
    )


def render_followup_d7(contact: dict) -> tuple[str, str]:
    return (
        f"One last thought on Broiler Base Mate",
        _wrap(f"""
            <p>{_greeting(contact)},</p>
            <p>Last note from me, promise. I know cold emails are a pain — but I figured if
               there's one farmer in Australia who'd actually benefit from this, it's someone
               running a serious broiler operation.</p>
            <p>Quick rundown of what it replaces:</p>
            <ul>
              <li>Feed Program Excel (auto-syncs across phone &amp; PC)</li>
              <li>Manual silo tonnage tracking (tap on phone instead)</li>
              <li>Docket data entry (AI extracts feed type, kg, truck rego from a photo)</li>
              <li>FCR / CFCR maths (calculates the whole batch automatically)</li>
              <li>Catch-day report to the integrator (one-click CSV export)</li>
            </ul>
            <p><strong>14-day free trial — no card, no commitment.</strong></p>
            <p style="text-align:center;margin:24px 0;">
              <a href="{_personal_link(contact)}" style="background:#C9A227;color:#000;padding:12px 24px;border-radius:99px;text-decoration:none;font-weight:800;display:inline-block;">Start the free trial →</a>
            </p>
            <p>Cheers,<br>{SENDER_FIRST}</p>
        """, contact["id"]),
    )


def render_followup_d14(contact: dict) -> tuple[str, str]:
    return (
        f"How's the current batch going?",
        _wrap(f"""
            <p>{_greeting(contact)},</p>
            <p>Genuine question, not a pitch — how's your current batch tracking?
               If FCR is anywhere off where you'd want, that's literally the problem
               Broiler Base Mate exists to solve.</p>
            <p>If the timing's wrong, no worries at all. I'll leave you alone after this.</p>
            <p>If it's worth a look:</p>
            <p style="text-align:center;margin:24px 0;">
              <a href="{_personal_link(contact)}" style="background:#1a5c36;color:#fff;padding:12px 24px;border-radius:99px;text-decoration:none;font-weight:800;display:inline-block;">See the demo →</a>
            </p>
            <p>All the best for this batch.<br>{SENDER_FIRST}</p>
        """, contact["id"]),
    )


TEMPLATES = {
    0: render_initial,
    1: render_followup_d3,
    2: render_followup_d7,
    3: render_followup_d14,
}
# Days between followups (cumulative from initial send)
STEP_DELAYS_DAYS = {1: 3, 2: 7, 3: 14}


# ─── Pydantic bodies ────────────────────────────────────────────────────────
class ImportBody(BaseModel):
    raw: str


class UpdateContactBody(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    name: Optional[str] = None
    next_followup_at: Optional[datetime] = None
    farm_name: Optional[str] = None


# ─── Router builder ─────────────────────────────────────────────────────────
def build_router(db, require_admin):
    """`require_admin` is a callable awaited as require_admin(request) -> user|raises."""
    r = APIRouter(prefix="/api/ops/outreach", tags=["outreach"])

    async def _to_public(doc: dict) -> dict:
        if not doc:
            return doc
        doc.pop("_id", None)
        for k, v in list(doc.items()):
            if isinstance(v, datetime):
                doc[k] = v.isoformat()
        return doc

    # ── List + stats ────────────────────────────────────────────────────────
    @r.get("")
    async def list_contacts(request: Request, status: Optional[str] = None):
        await require_admin(request)
        q: dict = {}
        if status and status in STATUSES:
            q["status"] = status
        items: list[dict] = []
        async for c in db["outreach_contacts"].find(q).sort("created_at", -1):
            items.append(await _to_public(c))
        # Compute stats across the whole collection (not filtered)
        stats: dict = {s: 0 for s in STATUSES}
        async for c in db["outreach_contacts"].find({}, {"status": 1}):
            s = c.get("status", "cold")
            stats[s] = stats.get(s, 0) + 1
        stats["total"] = sum(stats.values())
        # Followups due
        now = datetime.now(timezone.utc)
        stats["followups_due"] = await db["outreach_contacts"].count_documents({
            "next_followup_at": {"$lte": now},
            "status": {"$nin": ["replied", "trialled", "won", "lost"]},
        })
        return {"items": items, "stats": stats}

    # ── Import paste blob ────────────────────────────────────────────────────
    @r.post("/import")
    async def import_contacts(body: ImportBody, request: Request):
        await require_admin(request)
        parsed = _parse_email_list(body.raw)
        if not parsed:
            return {"created": 0, "skipped": 0, "items": []}
        now = datetime.now(timezone.utc)
        created = 0
        skipped = 0
        created_docs: list[dict] = []
        for p in parsed:
            existing = await db["outreach_contacts"].find_one({"email": p["email"]})
            if existing:
                skipped += 1
                continue
            doc = {
                "id":         "oc_" + uuid.uuid4().hex[:14],
                "email":      p["email"],
                "name":       p.get("name") or "",
                "farm_name":  "",
                "integrator": _detect_integrator(p["email"]),
                "slug":       _slug_from_email(p["email"]),
                "status":     "cold",
                "notes":      "",
                "followup_step":   0,
                "next_followup_at": None,
                "open_count":   0,
                "click_count":  0,
                "first_opened_at": None,
                "last_activity_at": None,
                "sent_at":      None,
                "last_sent_at": None,
                "source":       "manual_import",
                "created_at":   now,
                "updated_at":   now,
            }
            await db["outreach_contacts"].insert_one(doc)
            created += 1
            created_docs.append(await _to_public({**doc}))
        return {"created": created, "skipped": skipped, "items": created_docs}

    # ── Update a single contact (status / notes) ─────────────────────────────
    @r.patch("/{contact_id}")
    async def update_contact(contact_id: str, body: UpdateContactBody, request: Request):
        await require_admin(request)
        update: dict = {"updated_at": datetime.now(timezone.utc)}
        if body.status is not None:
            if body.status not in STATUSES:
                raise HTTPException(400, f"Invalid status. Must be one of {STATUSES}")
            update["status"] = body.status
        if body.notes is not None:
            update["notes"] = body.notes
        if body.name is not None:
            update["name"] = body.name
        if body.farm_name is not None:
            update["farm_name"] = body.farm_name
        if body.next_followup_at is not None:
            update["next_followup_at"] = body.next_followup_at
        res = await db["outreach_contacts"].update_one({"id": contact_id}, {"$set": update})
        if res.matched_count == 0:
            raise HTTPException(404, "Contact not found")
        doc = await db["outreach_contacts"].find_one({"id": contact_id})
        return await _to_public(doc)

    # ── Delete a contact ─────────────────────────────────────────────────────
    @r.delete("/{contact_id}", status_code=204)
    async def delete_contact(contact_id: str, request: Request):
        await require_admin(request)
        await db["outreach_contacts"].delete_one({"id": contact_id})
        return Response(status_code=204)

    # ── Send (initial or follow-up) for one contact ──────────────────────────
    @r.post("/{contact_id}/send")
    async def send_one(contact_id: str, request: Request):
        await require_admin(request)
        doc = await db["outreach_contacts"].find_one({"id": contact_id})
        if not doc:
            raise HTTPException(404, "Contact not found")
        result = await _send_step(db, doc)
        return result

    # ── Run any drip follow-ups due right now ────────────────────────────────
    @r.post("/run-drip")
    async def run_drip(request: Request):
        await require_admin(request)
        sent, skipped = await run_due_followups(db)
        return {"sent": sent, "skipped": skipped, "ran_at": datetime.now(timezone.utc).isoformat()}

    # ── Open tracking pixel (public, no auth) ────────────────────────────────
    @r.get("/{contact_id}/open.gif")
    async def open_pixel(contact_id: str):
        now = datetime.now(timezone.utc)
        await db["outreach_contacts"].update_one(
            {"id": contact_id, "first_opened_at": None},
            {"$set": {"first_opened_at": now, "status": "opened"}},
        )
        await db["outreach_contacts"].update_one(
            {"id": contact_id},
            {"$set": {"last_activity_at": now}, "$inc": {"open_count": 1}},
        )
        # 1×1 transparent GIF
        gif = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
        return Response(content=gif, media_type="image/gif",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    return r


# ─── Public helper: track a personalised landing click ──────────────────────
async def track_slug_click(db, slug: str) -> Optional[dict]:
    """Called by the /g/{slug} endpoint. Increments click_count and returns the contact dict."""
    now = datetime.now(timezone.utc)
    doc = await db["outreach_contacts"].find_one_and_update(
        {"slug": slug},
        {"$inc": {"click_count": 1},
         "$set": {"last_activity_at": now}},
        return_document=True,
    )
    if not doc:
        return None
    # Promote status from cold/sent/opened → opened (clicked is stronger than opened)
    if doc.get("status") in ("cold", "sent"):
        await db["outreach_contacts"].update_one(
            {"slug": slug}, {"$set": {"status": "opened"}}
        )
        doc["status"] = "opened"
    return doc


# ─── Internal: send the next step and bump counters ─────────────────────────
async def _send_step(db, contact: dict) -> dict:
    from email_service import send_email
    step = int(contact.get("followup_step") or 0)
    if step >= len(TEMPLATES):
        return {"ok": False, "reason": "All steps already sent"}
    renderer = TEMPLATES[step]
    subject, html = renderer(contact)
    result = await send_email(to=contact["email"], subject=subject, html=html)
    now = datetime.now(timezone.utc)
    update = {
        "last_sent_at":   now,
        "updated_at":     now,
        "followup_step":  step + 1,
    }
    if step == 0:
        update["sent_at"] = now
        if contact.get("status") == "cold":
            update["status"] = "sent"
    # Schedule next step
    next_step = step + 1
    if next_step in STEP_DELAYS_DAYS and contact.get("sent_at") is None and step == 0:
        # First send just happened — schedule day-3 from now
        update["next_followup_at"] = now + timedelta(days=STEP_DELAYS_DAYS[next_step])
    elif next_step in STEP_DELAYS_DAYS:
        sent_at = contact.get("sent_at") or now
        update["next_followup_at"] = sent_at + timedelta(days=STEP_DELAYS_DAYS[next_step])
    else:
        update["next_followup_at"] = None
    await db["outreach_contacts"].update_one({"id": contact["id"]}, {"$set": update})
    return {
        "ok": True,
        "skipped": bool(result.get("skipped")),
        "email_id": result.get("id"),
        "step_sent": step,
        "next_step": next_step,
        "next_followup_at": update.get("next_followup_at"),
    }


async def run_due_followups(db) -> tuple[int, int]:
    """Send any follow-ups whose next_followup_at has passed. Returns (sent, skipped)."""
    now = datetime.now(timezone.utc)
    cursor = db["outreach_contacts"].find({
        "next_followup_at": {"$lte": now},
        "status": {"$nin": ["replied", "trialled", "won", "lost"]},
    })
    sent = 0
    skipped = 0
    async for doc in cursor:
        try:
            res = await _send_step(db, doc)
            if res.get("ok"):
                sent += 1
            else:
                skipped += 1
        except Exception as e:
            logger.exception("Follow-up failed for %s: %s", doc.get("email"), e)
            skipped += 1
    return sent, skipped
