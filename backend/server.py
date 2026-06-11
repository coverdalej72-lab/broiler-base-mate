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
from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

# ─── App ───────────────────────────────────────────────────────────────────
app = FastAPI(title="Broiler Base Mate API")
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# ─── Seed default farm (10 shed groups × 3 silos) ──────────────────────────
async def seed_if_empty() -> None:
    if await shed_groups_col.count_documents({}) > 0:
        return
    groups = []
    silos = []
    for i in range(1, 11):
        gid = str(uuid.uuid4())
        groups.append({
            "id": gid,
            "name": f"Sheds {2 * i - 1} & {2 * i}",
            "displayOrder": i,
        })
        for letter in ("A", "B", "C"):
            silos.append({
                "id": str(uuid.uuid4()),
                "shedGroupId": gid,
                "letter": letter,
                "name": f"Silo {letter}",
                "defaultFeedType": None,
            })
    if groups:
        await shed_groups_col.insert_many(groups)
    if silos:
        await silos_col.insert_many(silos)


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
async def batch_reset():
    await readings_col.delete_many({})
    return {"ok": True}


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
async def list_shed_groups():
    groups = await shed_groups_col.find().sort("displayOrder", 1).to_list(length=200)
    silos = await silos_col.find().sort("letter", 1).to_list(length=1000)
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
async def list_silos():
    silos = await silos_col.find().sort("letter", 1).to_list(length=1000)
    return [clean(s) for s in silos]


@api.post("/silos", status_code=201)
async def create_silo(body: CreateSiloBody):
    doc = {
        "id": str(uuid.uuid4()),
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
async def readings_today(localDate: Optional[str] = Query(default=None)):
    """Today's readings, grouped by shed for the Feed Program auto-sync."""
    if localDate and len(localDate) == 10:
        d = datetime.fromisoformat(localDate)
        start = datetime.combine(d.date(), datetime.min.time(), tzinfo=timezone.utc) - AEST_OFFSET
        end = datetime.combine(d.date(), datetime.max.time(), tzinfo=timezone.utc) - AEST_OFFSET
        date_str = localDate
    else:
        start, end = aest_today_range()
        date_str = aest_today()

    groups = await shed_groups_col.find().sort("displayOrder", 1).to_list(length=200)
    silos = await silos_col.find().sort("letter", 1).to_list(length=1000)
    todays = await readings_col.find({
        "readingDate": {"$gte": start, "$lte": end}
    }).to_list(length=5000)

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
async def batch_create_readings(body: BatchCreateReadingsBody):
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
            await readings_col.delete_many({
                "siloId": r.siloId,
                "readingDate": {"$gte": today_start, "$lte": today_end},
            })
        doc = {
            "id": str(uuid.uuid4()),
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
async def list_readings(limit: int = Query(default=100, le=1000), siloId: Optional[str] = None):
    q = {}
    if siloId:
        q["siloId"] = siloId
    rows = await readings_col.find(q).sort("readingDate", -1).limit(limit).to_list(length=limit)
    silos = await silos_col.find().to_list(length=1000)
    groups = await shed_groups_col.find().to_list(length=200)
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
async def list_deliveries(limit: int = Query(default=100, le=1000)):
    rows = await deliveries_col.find().sort("deliveryDate", -1).limit(limit).to_list(length=limit)
    silos = await silos_col.find().to_list(length=1000)
    groups = await shed_groups_col.find().to_list(length=200)
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
            "amount": float(r["amount"]),
            "unit": r["unit"],
            "notes": r.get("notes"),
            "deliveryDate": r["deliveryDate"].isoformat(),
            "createdAt": r["createdAt"].isoformat(),
        })
    return out


@api.post("/deliveries", status_code=201)
async def create_delivery(body: CreateDeliveryBody):
    date = datetime.fromisoformat(body.deliveryDate.replace("Z", "+00:00")) if body.deliveryDate else datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    doc = {
        "id": str(uuid.uuid4()),
        "shedGroupId": body.shedGroupId,
        "siloId": body.siloId,
        "feedType": body.feedType,
        "amount": float(body.amount),
        "unit": body.unit,
        "notes": body.notes,
        "deliveryDate": date,
        "createdAt": datetime.now(timezone.utc),
    }
    await deliveries_col.insert_one(doc)
    return clean(doc)


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


app.include_router(api)


# ─── Static field reader ─────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/reader-assets", StaticFiles(directory=STATIC_DIR), name="reader-assets")


@app.get("/reader")
async def reader_page():
    """Mobile field reader UI."""
    return FileResponse(os.path.join(STATIC_DIR, "reader.html"))


@app.get("/reader/")
async def reader_page_slash():
    return FileResponse(os.path.join(STATIC_DIR, "reader.html"))
