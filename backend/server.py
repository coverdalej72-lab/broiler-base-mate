"""
Broiler Base Mate - Backend stub.

The original Replit app's API server depends on PostgreSQL + Clerk + Stripe + AI
integrations. The /feed-program/ UI is largely client-side (uses localStorage,
xlsx parsing in the browser). This stub provides graceful responses for the few
endpoints the feed-program calls so the UI works without those external services.
"""
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Broiler Base Mate API")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")


@api.get("/")
def root():
    return {"ok": True, "service": "broiler-base-mate", "ts": datetime.now(timezone.utc).isoformat()}


@api.get("/health")
def health():
    return {"ok": True}


# --- Feed Program endpoints (stubs) ----------------------------------------
# The original API requires Clerk auth and PostgreSQL.
# These stubs return empty/safe payloads so the client-side spreadsheet works
# while running off-Replit without those external services.

@api.get("/readings/today")
def readings_today():
    """Latest reading per silo (used by feed-program to show SILO USAGE column)."""
    return {"readings": []}


@api.get("/deliveries")
def deliveries():
    """Feed deliveries (used by End-of-Batch summary)."""
    return {"deliveries": []}


class WeighBirdIn(BaseModel):
    shedGroupId: str | int | None = None
    weight: float | None = None
    age: int | None = None
    date: str | None = None


@api.post("/weigh-bird")
def weigh_bird(payload: WeighBirdIn):
    """Bird weighing endpoint (no-op stub)."""
    return {"ok": True, "stored": False, "reason": "running off-Replit standalone"}


@api.delete("/batch/reset")
def batch_reset():
    """Reset batch endpoint (no-op stub)."""
    return {"ok": True}


# --- Bootstrap (Clerk auth replacement) ------------------------------------
@api.get("/bootstrap")
def bootstrap():
    """Originally returned user/farm context from Clerk. Standalone mode = anonymous."""
    return {
        "user": {"id": "local-user", "email": "local@offline", "name": "Local User"},
        "farms": [{"id": "local-farm", "name": "My Farm"}],
        "currentFarmId": "local-farm",
        "mode": "standalone",
    }


app.include_router(api)
