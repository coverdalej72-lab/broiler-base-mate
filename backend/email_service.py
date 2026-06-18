"""Resend email helper for Broiler Base Mate.

Graceful degrade: if RESEND_API_KEY is missing/blank, log + skip (don't crash).
Once the user pastes their Resend API key into /app/backend/.env, emails go live
automatically — no other code changes needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import resend

logger = logging.getLogger("email_service")


def _key() -> Optional[str]:
    k = os.environ.get("RESEND_API_KEY", "").strip()
    return k or None


def _sender() -> str:
    return os.environ.get("SENDER_EMAIL", "onboarding@resend.dev").strip() or "onboarding@resend.dev"


def _reply_to() -> Optional[str]:
    """Optional Reply-To address — when set, replies go here instead of the sender.
    Lets the platform send from a verified Resend domain but route grower replies
    to the owner's personal Gmail."""
    r = os.environ.get("REPLY_TO_EMAIL", "").strip()
    return r or None


async def send_email(*, to: str, subject: str, html: str, reply_to: Optional[str] = None) -> dict:
    """Send an email via Resend. Returns {"ok": bool, "id": str|None, "skipped": bool}.
    If `reply_to` is omitted, falls back to the REPLY_TO_EMAIL env var."""
    key = _key()
    if not key:
        logger.warning("RESEND_API_KEY missing — skipping email to %s (subject=%r)", to, subject)
        return {"ok": True, "id": None, "skipped": True, "reason": "RESEND_API_KEY not set"}

    resend.api_key = key
    params: dict = {"from": _sender(), "to": [to], "subject": subject, "html": html}
    rt = reply_to or _reply_to()
    if rt:
        params["reply_to"] = rt
    try:
        email = await asyncio.to_thread(resend.Emails.send, params)
        return {"ok": True, "id": email.get("id") if isinstance(email, dict) else None, "skipped": False}
    except Exception as e:
        logger.exception("Resend send failed to %s: %s", to, e)
        return {"ok": False, "id": None, "skipped": False, "error": str(e)}


def render_demo_request_email(*, name: str, email: str, farm: Optional[str], sheds: Optional[str], message: Optional[str]) -> str:
    return f"""
    <div style="font-family:system-ui,-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:20px;">
      <h2 style="color:#0f3d24;margin:0 0 16px;">New demo request</h2>
      <table cellpadding="8" style="border-collapse:collapse;width:100%;font-size:14px;">
        <tr><td style="background:#f7fbf4;font-weight:700;width:120px;">Name</td><td style="background:#fff;border:1px solid #e8ecea;">{name}</td></tr>
        <tr><td style="background:#f7fbf4;font-weight:700;">Email</td><td style="background:#fff;border:1px solid #e8ecea;"><a href="mailto:{email}">{email}</a></td></tr>
        <tr><td style="background:#f7fbf4;font-weight:700;">Farm</td><td style="background:#fff;border:1px solid #e8ecea;">{farm or '—'}</td></tr>
        <tr><td style="background:#f7fbf4;font-weight:700;">Sheds</td><td style="background:#fff;border:1px solid #e8ecea;">{sheds or '—'}</td></tr>
        <tr><td style="background:#f7fbf4;font-weight:700;">Message</td><td style="background:#fff;border:1px solid #e8ecea;">{(message or '—').replace(chr(10), '<br>')}</td></tr>
      </table>
      <p style="margin-top:20px;color:#5a7268;font-size:12px;">Sent from Broiler Base Mate landing page.</p>
    </div>
    """


def render_farm_invite_email(*, operator_name: str, farm_name: str, reader_url: str, ops_manager: str) -> str:
    return f"""
    <div style="font-family:system-ui,-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:20px;">
      <h2 style="color:#0f3d24;margin:0 0 12px;">You've been added to <span style="color:#C9A227;">{farm_name}</span></h2>
      <p style="font-size:15px;color:#1a3d24;line-height:1.6;">
        Hi {operator_name or 'there'},<br><br>
        <b>{ops_manager}</b> has set you up on Broiler Base Mate to log silo readings &amp; delivery dockets for <b>{farm_name}</b>.
      </p>
      <p style="text-align:center;margin:32px 0;">
        <a href="{reader_url}" style="background:#C9A227;color:#000;text-decoration:none;padding:14px 28px;border-radius:99px;font-weight:900;font-size:15px;display:inline-block;">📲 Open the Field Reader</a>
      </p>
      <p style="font-size:13px;color:#5a7268;line-height:1.6;">
        Open the link on your phone, then tap your browser's "Add to Home Screen" so it lives on your phone like a real app.
        No login needed. Tap silo readings, photograph dockets — your office spreadsheet updates within 2 minutes.
      </p>
      <p style="font-size:12px;color:#8aa094;margin-top:24px;border-top:1px solid #e8ecea;padding-top:16px;">
        Reader link (bookmark this): <a href="{reader_url}">{reader_url}</a>
      </p>
    </div>
    """
