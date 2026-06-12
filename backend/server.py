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
    """New Batch: wipe all readings AND all deliveries so EOB starts empty."""
    r = await readings_col.delete_many({})
    d = await deliveries_col.delete_many({})
    return {"ok": True, "readingsDeleted": r.deleted_count, "deliveriesDeleted": d.deleted_count}


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
async def create_delivery(body: CreateDeliveryBody):
    date = datetime.fromisoformat(body.deliveryDate.replace("Z", "+00:00")) if body.deliveryDate else datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    # Normalise feed type so the Feed Program's End-of-Batch matcher always finds a column
    # ("Broiler Grower" → "Grower", "Gourmet Broiler Starter" → "Starter", etc.)
    normalised_feed = _normalise_feed_type(body.feedType)
    doc = {
        "id": str(uuid.uuid4()),
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
