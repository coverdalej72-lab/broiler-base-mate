"""
Broiler Base Mate — Backend (MongoDB-backed).

Provides the API endpoints the Feed Program polls (`/api/readings/today` every
2 minutes) so silo readings entered on a phone in `/reader` flow automatically
into the Feed Program's "FEED USAGE" column.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
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
                "name": s["name"],
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
    # Subscription plans (charged as one-off first-month for v1; user upgrades to recurring in Stripe dashboard)
    "bronze_monthly":      {"label": "Bronze",   "amount": 50.0,   "kind": "subscription"},
    "silver_monthly":      {"label": "Silver",   "amount": 75.0,   "kind": "subscription"},
    "gold_monthly":        {"label": "Gold",     "amount": 100.0,  "kind": "subscription"},
    "platinum_monthly":    {"label": "Platinum", "amount": 150.0,  "kind": "subscription"},
    "ops_bronze":          {"label": "Ops Pack (Bronze farms)",   "amount": 50.0,  "kind": "subscription"},
    "ops_silver":          {"label": "Ops Pack (Silver farms)",   "amount": 75.0,  "kind": "subscription"},
    "ops_gold":            {"label": "Ops Pack (Gold farms)",     "amount": 100.0, "kind": "subscription"},
    "ops_platinum":        {"label": "Ops Pack (Platinum farms)", "amount": 150.0, "kind": "subscription"},
    # Annual variants (~15% off)
    "bronze_annual":       {"label": "Bronze Annual",   "amount": 510.0,   "kind": "subscription"},
    "silver_annual":       {"label": "Silver Annual",   "amount": 1020.0,  "kind": "subscription"},
    "gold_annual":         {"label": "Gold Annual",     "amount": 1530.0,  "kind": "subscription"},
    # Operation Manager Pack — multi-farm bundles for ops managers
    "ops_bronze":          {"label": "Ops Manager — Bronze (≤6 sheds/farm)",  "amount": 50.0,  "kind": "ops_bundle"},
    "ops_silver":          {"label": "Ops Manager — Silver (7-12 sheds/farm)", "amount": 90.0, "kind": "ops_bundle"},
    "ops_gold":            {"label": "Ops Manager — Gold (12+ sheds/farm)",   "amount": 150.0, "kind": "ops_bundle"},
    # Sponsor tiers
    "sponsor_10":          {"label": "Sponsor — $10/mo",  "amount": 10.0,  "kind": "sponsor"},
    "sponsor_25":          {"label": "Sponsor — $25/mo",  "amount": 25.0,  "kind": "sponsor"},
    "sponsor_50":          {"label": "Sponsor — $50/mo",  "amount": 50.0,  "kind": "sponsor"},
    # One-off donations
    "back_seed":           {"label": "Seed Supporter",       "amount": 100.0,  "kind": "donation"},
    "back_project":        {"label": "Project Backer",       "amount": 500.0,  "kind": "donation"},
    "back_foundation":     {"label": "Foundation Partner",   "amount": 1000.0, "kind": "donation"},
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

    # Calculate dynamic amount for ops_bundle if farms list is provided
    amount = float(pkg["amount"])
    if pkg["kind"] == "ops_bundle" and body.farms:
        tier_prices = {"bronze": 50.0, "silver": 90.0, "gold": 150.0}
        amount = sum(tier_prices.get((f.tier or "bronze").lower(), 50.0) for f in body.farms)

    req = CheckoutSessionRequest(
        amount=amount, currency="usd",
        success_url=success_url, cancel_url=cancel_url, metadata=meta,
    )
    session = await checkout.create_checkout_session(req)

    await payments_col.insert_one({
        "id": str(uuid.uuid4()),
        "session_id": session.session_id,
        "package_id": body.packageId,
        "amount": amount,
        "currency": "usd",
        "kind": pkg["kind"],
        "email": body.email,
        "buyerName": body.buyerName,
        "farms": [f.model_dump() for f in body.farms] if body.farms else None,
        "provisioned": False,
        "payment_status": "initiated",
        "status": "open",
        "createdAt": datetime.now(timezone.utc),
    })
    return {"url": session.url, "session_id": session.session_id}


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
                    f"<b>Amount:</b> ${cur.get('amount')} {cur.get('currency','usd').upper()}<br>"
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
async def kill_service_worker():
    """Self-destruct service worker.
    Any browser that previously installed the old Workbox SW will fetch this on
    its next update check. The body unregisters itself and clears all caches,
    permanently freeing users from the old cached bundle.
    """
    js = (
        "self.addEventListener('install', () => { self.skipWaiting(); });\n"
        "self.addEventListener('activate', (e) => {\n"
        "  e.waitUntil((async () => {\n"
        "    try {\n"
        "      const keys = await caches.keys();\n"
        "      await Promise.all(keys.map(k => caches.delete(k)));\n"
        "    } catch (_) {}\n"
        "    try { await self.registration.unregister(); } catch (_) {}\n"
        "    const clientsList = await self.clients.matchAll({ type: 'window' });\n"
        "    clientsList.forEach(c => { try { c.navigate(c.url); } catch (_) {} });\n"
        "  })());\n"
        "});\n"
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
