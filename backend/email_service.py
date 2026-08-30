"""Email helper for Broiler Base Mate.

Provider priority — first one with credentials wins:
  1. Gmail SMTP (App Password) via aiosmtplib — GMAIL_USER + GMAIL_APP_PASSWORD
  2. Resend API — RESEND_API_KEY (legacy fallback)
  3. No-op with warning if neither is set

Chosen at each send call so a live-swap of env vars takes effect on next email
without a server restart. Both paths return the same shape:
  {"ok": bool, "id": str|None, "skipped": bool, "provider": "gmail"|"resend"|"none"}
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from email.message import EmailMessage
from typing import Optional

import aiosmtplib
import resend

logger = logging.getLogger("email_service")


# ─── Provider selection ─────────────────────────────────────────────────

def _gmail_creds() -> Optional[tuple[str, str]]:
    user = os.environ.get("GMAIL_USER", "").strip()
    pwd  = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if user and pwd:
        return user, pwd
    return None


def _resend_key() -> Optional[str]:
    k = os.environ.get("RESEND_API_KEY", "").strip()
    return k or None


def _sender() -> str:
    """Preferred From header. Defaults to Gmail user if set (so From matches auth
    and Gmail won't rewrite it), otherwise falls back to SENDER_EMAIL or Resend
    sandbox address."""
    gmail = _gmail_creds()
    if gmail:
        env_sender = os.environ.get("SENDER_EMAIL", "").strip()
        # If SENDER_EMAIL is set to something Gmail-compatible (contains @gmail
        # or matches the auth user), honour it — Gmail requires From to match
        # the authenticated account, so anything else would get rewritten.
        if env_sender and (gmail[0] in env_sender):
            return env_sender
        return f"Broiler Base Mate <{gmail[0]}>"
    return os.environ.get("SENDER_EMAIL", "onboarding@resend.dev").strip() or "onboarding@resend.dev"


def _reply_to_default() -> Optional[str]:
    r = os.environ.get("REPLY_TO_EMAIL", "").strip()
    return r or None


# ─── Gmail SMTP path ────────────────────────────────────────────────────

async def _send_via_gmail(*, to: str, subject: str, html: str, reply_to: Optional[str],
                          attachments: Optional[list]) -> dict:
    user, pwd = _gmail_creds()  # type: ignore[misc]
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))

    msg = EmailMessage()
    msg["From"] = _sender()
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    # Plain-text fallback — strip tags cheaply so a text-only client still shows
    # something sensible.
    import re
    plain = re.sub(r"<[^>]+>", "", html)
    plain = re.sub(r"\s+\n", "\n", plain).strip()
    msg.set_content(plain or "See HTML version.")
    msg.add_alternative(html, subtype="html")

    # Attachments: same shape as before {filename, content} where content is
    # base64-encoded bytes.
    if attachments:
        for a in attachments:
            fn = a.get("filename") or "attachment"
            raw = a.get("content")
            if not raw:
                continue
            try:
                data = base64.b64decode(raw) if isinstance(raw, str) else raw
            except Exception:
                data = raw if isinstance(raw, (bytes, bytearray)) else b""
            # Best-effort MIME guess by extension
            import mimetypes
            ctype, _ = mimetypes.guess_type(fn)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fn)

    try:
        await aiosmtplib.send(
            msg,
            hostname=host,
            port=port,
            username=user,
            password=pwd,
            start_tls=True,
            timeout=30,
        )
        return {"ok": True, "id": None, "skipped": False, "provider": "gmail"}
    except aiosmtplib.SMTPAuthenticationError as e:
        logger.exception("Gmail SMTP authentication failed for %s: %s", user, e)
        return {"ok": False, "id": None, "skipped": False, "provider": "gmail",
                "error": "gmail_auth_failed"}
    except aiosmtplib.SMTPException as e:
        logger.exception("Gmail SMTP rejected/failed to %s: %s", to, e)
        return {"ok": False, "id": None, "skipped": False, "provider": "gmail",
                "error": str(e)}
    except Exception as e:
        logger.exception("Gmail SMTP unexpected error to %s: %s", to, e)
        return {"ok": False, "id": None, "skipped": False, "provider": "gmail",
                "error": str(e)}


# ─── Resend path (legacy) ───────────────────────────────────────────────

async def _send_via_resend(*, to: str, subject: str, html: str, reply_to: Optional[str],
                           attachments: Optional[list]) -> dict:
    resend.api_key = _resend_key()
    params: dict = {"from": _sender(), "to": [to], "subject": subject, "html": html}
    if reply_to:
        params["reply_to"] = reply_to
    if attachments:
        params["attachments"] = attachments
    try:
        email = await asyncio.to_thread(resend.Emails.send, params)
        return {"ok": True, "id": email.get("id") if isinstance(email, dict) else None,
                "skipped": False, "provider": "resend"}
    except Exception as e:
        logger.exception("Resend send failed to %s: %s", to, e)
        return {"ok": False, "id": None, "skipped": False, "provider": "resend",
                "error": str(e)}


# ─── Public API ─────────────────────────────────────────────────────────

async def send_email(*, to: str, subject: str, html: str, reply_to: Optional[str] = None,
                     attachments: Optional[list] = None) -> dict:
    """Send an email using the first configured provider (Gmail SMTP → Resend).
    Returns {"ok": bool, "id": str|None, "skipped": bool, "provider": str}."""
    rt = reply_to or _reply_to_default()

    if _gmail_creds():
        return await _send_via_gmail(to=to, subject=subject, html=html,
                                     reply_to=rt, attachments=attachments)

    if _resend_key():
        return await _send_via_resend(to=to, subject=subject, html=html,
                                      reply_to=rt, attachments=attachments)

    logger.warning("No email provider configured (set GMAIL_APP_PASSWORD or "
                   "RESEND_API_KEY) — skipping email to %s (subject=%r)", to, subject)
    return {"ok": True, "id": None, "skipped": True, "provider": "none",
            "reason": "no email provider credentials configured"}


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
