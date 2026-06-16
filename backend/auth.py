"""Auth helpers for Broiler Base Mate — Emergent Google Auth.

Flow:
  1. Frontend redirects to https://auth.emergentagent.com/?redirect=<url>
  2. User logs in with Google
  3. Browser returns to <url>#session_id=<id>
  4. Frontend posts session_id to /api/auth/exchange-session
  5. We call Emergent's session-data endpoint, get user+token, set httpOnly cookie
  6. Subsequent requests include cookie; we validate via get_current_user()
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, Cookie, Header

# REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
EMERGENT_SESSION_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"
SESSION_TTL_DAYS = 7
COOKIE_NAME = "session_token"


def _admin_emails() -> set[str]:
    """Emails granted super-admin access (see ALL farms across all customers).

    Deliberately separate from ADMIN_EMAIL (which is just the notification target for
    sale/support emails). This lets the developer receive sale notifications WITHOUT
    being able to peek at customer farm data — a privacy promise.

    By default this is empty (no super-admin). To enable temporary support access,
    add SUPERUSER_EMAILS=you@example.com,backup@example.com to /app/backend/.env.
    """
    raw = os.environ.get("SUPERUSER_EMAILS", "") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


async def fetch_emergent_session(session_id: str) -> dict:
    """Exchange the session_id from Emergent OAuth for full user data + session_token."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(EMERGENT_SESSION_URL, headers={"X-Session-ID": session_id})
        if r.status_code != 200:
            raise HTTPException(401, f"Invalid session_id (Emergent returned {r.status_code})")
        return r.json()


def _user_role(email: str, owned_slugs: list[str], invited_slugs: list[str]) -> str:
    if email.lower() in _admin_emails():
        return "admin"
    if owned_slugs:
        return "owner"
    if invited_slugs:
        return "operator"
    return "none"


async def list_user_farms(db, email: str) -> dict:
    """Returns { owned: [farm_dicts], invited: [farm_dicts], role: str }"""
    email_l = (email or "").lower()
    owned, invited = [], []

    # Admin sees ALL farms (but classified as owned for permission purposes)
    if email_l in _admin_emails():
        async for f in db["farms"].find({}, {"_id": 0}):
            owned.append({"slug": f.get("slug"), "name": f.get("name"), "isDefault": bool(f.get("isDefault"))})
        return {"owned": owned, "invited": [], "role": "admin"}

    # Owner of farms
    async for f in db["farms"].find({"ownerEmail": {"$regex": f"^{email_l}$", "$options": "i"}}, {"_id": 0}):
        owned.append({"slug": f.get("slug"), "name": f.get("name"), "isDefault": bool(f.get("isDefault"))})

    # Invited operator
    seen_slugs = {f["slug"] for f in owned}
    async for inv in db["farm_invites"].find({"operatorEmail": {"$regex": f"^{email_l}$", "$options": "i"}}, {"_id": 0}):
        slug = inv.get("farmSlug")
        if slug and slug not in seen_slugs:
            farm = await db["farms"].find_one({"slug": slug}, {"_id": 0})
            if farm:
                invited.append({"slug": slug, "name": farm.get("name"), "isDefault": bool(farm.get("isDefault"))})
                seen_slugs.add(slug)

    role = _user_role(email_l, [f["slug"] for f in owned], [f["slug"] for f in invited])
    return {"owned": owned, "invited": invited, "role": role}


async def get_current_user(
    db,
    session_token: Optional[str] = None,
    authorization: Optional[str] = None,
) -> Optional[dict]:
    """Returns the current user dict or None if not authenticated. Checks cookie first, then Bearer."""
    token = session_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        return None
    session = await db["user_sessions"].find_one({"session_token": token}, {"_id": 0})
    if not session:
        return None
    # Expiry check (handle naive datetimes from Mongo)
    exp = session.get("expires_at")
    if isinstance(exp, str):
        exp = datetime.fromisoformat(exp)
    if exp and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp and exp < datetime.now(timezone.utc):
        return None
    user = await db["users"].find_one({"user_id": session["user_id"]}, {"_id": 0})
    return user


def build_router(db, app_url: Optional[str] = None) -> APIRouter:
    """Build the FastAPI auth router. db is the motor client database."""
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/exchange-session")
    async def exchange_session(request: Request, response: Response):
        body = await request.json()
        session_id = (body.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(400, "session_id required")
        data = await fetch_emergent_session(session_id)
        email = (data.get("email") or "").strip().lower()
        if not email:
            raise HTTPException(401, "No email returned from auth provider")

        # Upsert user (custom user_id, never expose _id)
        existing = await db["users"].find_one({"email": email}, {"_id": 0})
        if existing:
            user_id = existing["user_id"]
            await db["users"].update_one(
                {"user_id": user_id},
                {"$set": {"name": data.get("name"), "picture": data.get("picture"),
                          "last_login_at": datetime.now(timezone.utc)}},
            )
        else:
            user_id = f"user_{uuid.uuid4().hex[:12]}"
            await db["users"].insert_one({
                "user_id": user_id,
                "email": email,
                "name": data.get("name"),
                "picture": data.get("picture"),
                "created_at": datetime.now(timezone.utc),
                "last_login_at": datetime.now(timezone.utc),
            })

        # Persist session (use the Emergent-issued session_token as the cookie value)
        session_token = data.get("session_token") or f"st_{uuid.uuid4().hex}"
        expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
        await db["user_sessions"].insert_one({
            "user_id": user_id,
            "session_token": session_token,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc),
        })

        # Set httpOnly cookie. samesite=none + secure=True is required for cross-site cookie flows.
        response.set_cookie(
            key=COOKIE_NAME, value=session_token, max_age=SESSION_TTL_DAYS * 86400,
            httponly=True, secure=True, samesite="none", path="/",
        )

        farms_info = await list_user_farms(db, email)
        return {
            "ok": True,
            "user": {"user_id": user_id, "email": email,
                     "name": data.get("name"), "picture": data.get("picture")},
            **farms_info,
        }

    @router.get("/me")
    async def get_me(
        session_token: Optional[str] = Cookie(default=None),
        authorization: Optional[str] = Header(default=None),
    ):
        user = await get_current_user(db, session_token=session_token, authorization=authorization)
        if not user:
            raise HTTPException(401, "Not authenticated")
        farms_info = await list_user_farms(db, user["email"])
        return {
            "user": {"user_id": user["user_id"], "email": user["email"],
                     "name": user.get("name"), "picture": user.get("picture")},
            **farms_info,
        }

    @router.post("/logout")
    async def logout(
        response: Response,
        session_token: Optional[str] = Cookie(default=None),
    ):
        if session_token:
            await db["user_sessions"].delete_one({"session_token": session_token})
        response.delete_cookie(COOKIE_NAME, path="/", samesite="none", secure=True)
        return {"ok": True}

    return router
