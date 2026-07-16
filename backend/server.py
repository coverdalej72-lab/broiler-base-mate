"""Coop Overwatch backend."""
from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, APIRouter, HTTPException, Query
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

from models import (
    Farm, FarmCreate, Shed, ShedCreate, ShedUpdate,
    Batch, BatchCreate, BatchClose, Reading, ReadingIngest,
    Alert, Decision, DecisionApproval, Policy, PolicyUpdate,
    now_iso,
)
from breed_profiles import BREED_STANDARDS, get_full_curve, get_standard
from mother_hen import analyze_reading, compute_shed_status, finding_to_decision, llm_deep_analysis
from seed import seed_if_empty


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

app = FastAPI(title="Coop Overwatch")
api = APIRouter(prefix="/api")


@api.get("/")
async def root():
    return {"app": "Coop Overwatch", "status": "watching"}


@api.post("/seed")
async def seed():
    return await seed_if_empty(db)


@api.get("/breeds")
async def list_breeds():
    return {
        "breeds": list(BREED_STANDARDS.keys()),
        "targets": {
            b: {
                "target_fcr_at_42d": v["target_fcr_at_42d"],
                "target_mortality_pct": v["target_mortality_pct"],
            }
            for b, v in BREED_STANDARDS.items()
        },
    }


@api.get("/breeds/{breed}/curve")
async def breed_curve(breed: str, metric: str = Query("weight_g"), max_day: int = 49):
    if breed not in BREED_STANDARDS:
        raise HTTPException(404, "Breed not found")
    if metric not in ("weight_g", "temp_c", "water_ml_per_bird", "cum_feed_g_per_bird"):
        raise HTTPException(400, "Invalid metric")
    return {"breed": breed, "metric": metric, "curve": get_full_curve(breed, metric, max_day)}


# ---------- FARMS ----------
@api.get("/farms", response_model=List[Farm])
async def list_farms():
    return await db.farms.find({}, {"_id": 0}).to_list(1000)


@api.post("/farms", response_model=Farm)
async def create_farm(payload: FarmCreate):
    farm = Farm(**payload.model_dump())
    await db.farms.insert_one(farm.model_dump())
    return farm


@api.delete("/farms/{farm_id}")
async def delete_farm(farm_id: str):
    await db.farms.delete_one({"id": farm_id})
    await db.sheds.delete_many({"farm_id": farm_id})
    return {"deleted": farm_id}


# ---------- SHEDS ----------
@api.get("/sheds", response_model=List[Shed])
async def list_sheds(farm_id: Optional[str] = None):
    q = {"farm_id": farm_id} if farm_id else {}
    return await db.sheds.find(q, {"_id": 0}).to_list(1000)


@api.get("/sheds/{shed_id}", response_model=Shed)
async def get_shed(shed_id: str):
    doc = await db.sheds.find_one({"id": shed_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Shed not found")
    return doc


@api.post("/sheds", response_model=Shed)
async def create_shed(payload: ShedCreate):
    shed = Shed(**payload.model_dump())
    await db.sheds.insert_one(shed.model_dump())
    return shed


@api.patch("/sheds/{shed_id}", response_model=Shed)
async def update_shed(shed_id: str, payload: ShedUpdate):
    upd = {k: v for k, v in payload.model_dump().items() if v is not None}
    if upd:
        await db.sheds.update_one({"id": shed_id}, {"$set": upd})
    doc = await db.sheds.find_one({"id": shed_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Shed not found")
    return doc


@api.delete("/sheds/{shed_id}")
async def delete_shed(shed_id: str):
    await db.sheds.delete_one({"id": shed_id})
    return {"deleted": shed_id}


# ---------- BATCHES ----------
@api.get("/batches", response_model=List[Batch])
async def list_batches(shed_id: Optional[str] = None, status: Optional[str] = None):
    q = {}
    if shed_id:
        q["shed_id"] = shed_id
    if status:
        q["status"] = status
    return await db.batches.find(q, {"_id": 0}).sort("start_date", -1).to_list(1000)


@api.get("/batches/{batch_id}", response_model=Batch)
async def get_batch(batch_id: str):
    doc = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Batch not found")
    return doc


@api.post("/batches", response_model=Batch)
async def create_batch(payload: BatchCreate):
    await db.batches.update_many(
        {"shed_id": payload.shed_id, "status": "active"},
        {"$set": {"status": "closed", "closed_at": now_iso()}},
    )
    batch = Batch(**payload.model_dump(), bird_count_current=payload.bird_count_start)
    await db.batches.insert_one(batch.model_dump())
    return batch


@api.post("/batches/{batch_id}/close", response_model=Batch)
async def close_batch(batch_id: str, payload: BatchClose):
    upd = {"status": "closed", "closed_at": now_iso()}
    for k, v in payload.model_dump().items():
        if v is not None:
            upd[k] = v
    await db.batches.update_one({"id": batch_id}, {"$set": upd})
    doc = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Batch not found")
    return doc


# ---------- READINGS ----------
async def _get_active_batch(shed_id: str):
    return await db.batches.find_one({"shed_id": shed_id, "status": "active"}, {"_id": 0})


def _grow_day_from(batch: dict) -> int:
    from datetime import date
    try:
        start = date.fromisoformat(batch["start_date"])
        return (date.today() - start).days
    except Exception:
        return 0


@api.post("/readings", response_model=Reading)
async def ingest_reading(payload: ReadingIngest):
    shed = await db.sheds.find_one({"id": payload.shed_id}, {"_id": 0})
    if not shed:
        raise HTTPException(404, "Shed not found")

    batch = await _get_active_batch(payload.shed_id)
    grow_day = _grow_day_from(batch) if batch else 0

    data = payload.model_dump()
    if not data.get("timestamp"):
        data["timestamp"] = now_iso()
    reading = Reading(**data, batch_id=batch["id"] if batch else None, grow_day=grow_day)
    await db.readings.insert_one(reading.model_dump())

    if batch:
        findings = analyze_reading(reading, batch["breed"], grow_day, batch["bird_count_current"], shed["capacity"])
        for f in findings:
            policy_doc = await db.policies.find_one({"action_type": f["action_type"]}, {"_id": 0})
            mode = policy_doc["mode"] if policy_doc else "recommend"
            decision = finding_to_decision(f, shed_id=shed["id"], batch_id=batch["id"], policy_mode=mode)
            await db.decisions.insert_one(decision.model_dump())
            if f["urgency"] in ("high", "critical"):
                alert = Alert(
                    shed_id=shed["id"],
                    batch_id=batch["id"],
                    severity="critical" if f["urgency"] == "critical" else "warning",
                    category=f["action_type"],
                    title=f["title"],
                    message=f["recommendation"],
                )
                await db.alerts.insert_one(alert.model_dump())
        if reading.mortality_today:
            new_current = max(0, batch["bird_count_current"] - reading.mortality_today)
            new_total = batch["mortality_total"] + reading.mortality_today
            await db.batches.update_one(
                {"id": batch["id"]},
                {"$set": {"bird_count_current": new_current, "mortality_total": new_total}},
            )

    return reading


@api.get("/sheds/{shed_id}/readings", response_model=List[Reading])
async def shed_readings(shed_id: str, batch_id: Optional[str] = None, limit: int = 200):
    q = {"shed_id": shed_id}
    if batch_id:
        q["batch_id"] = batch_id
    return await db.readings.find(q, {"_id": 0}).sort("timestamp", -1).to_list(limit)


@api.get("/sheds/{shed_id}/latest")
async def latest_reading(shed_id: str):
    doc = await db.readings.find_one({"shed_id": shed_id}, {"_id": 0}, sort=[("timestamp", -1)])
    return doc


@api.get("/batches/{batch_id}/readings", response_model=List[Reading])
async def batch_readings(batch_id: str, limit: int = 500):
    return await db.readings.find({"batch_id": batch_id}, {"_id": 0}).sort("timestamp", 1).to_list(limit)


# ---------- DASHBOARD ----------
@api.get("/dashboard")
async def dashboard():
    sheds = await db.sheds.find({}, {"_id": 0}).to_list(1000)
    farms = await db.farms.find({}, {"_id": 0}).to_list(1000)
    farms_map = {f["id"]: f for f in farms}

    tiles = []
    counts = {"healthy": 0, "warning": 0, "critical": 0, "offline": 0}
    for shed in sheds:
        batch = await _get_active_batch(shed["id"])
        latest = await db.readings.find_one({"shed_id": shed["id"]}, {"_id": 0}, sort=[("timestamp", -1)])
        status = "offline"
        findings = []
        grow_day = 0
        breed_target = {}
        if batch and latest:
            grow_day = _grow_day_from(batch)
            r_obj = Reading(**latest)
            findings = analyze_reading(r_obj, batch["breed"], grow_day, batch["bird_count_current"], shed["capacity"])
            status = compute_shed_status(findings)
            breed_target = {
                "temp_c": round(get_standard(batch["breed"], "temp_c", grow_day), 1),
                "weight_g": round(get_standard(batch["breed"], "weight_g", grow_day), 1),
                "water_ml_per_bird": round(get_standard(batch["breed"], "water_ml_per_bird", grow_day), 1),
            }
        counts[status] += 1
        tiles.append({
            "shed": shed,
            "farm": farms_map.get(shed["farm_id"]),
            "batch": batch,
            "grow_day": grow_day,
            "latest": latest,
            "status": status,
            "findings_count": len(findings),
            "breed_target": breed_target,
        })

    active_alerts = await db.alerts.count_documents({"acknowledged": False})
    pending_decisions = await db.decisions.count_documents({"status": "pending_approval"})

    return {
        "tiles": tiles,
        "counts": counts,
        "total_sheds": len(sheds),
        "active_alerts": active_alerts,
        "pending_decisions": pending_decisions,
        "watching_since": now_iso(),
    }


# ---------- ALERTS ----------
@api.get("/alerts", response_model=List[Alert])
async def list_alerts(unack_only: bool = False, limit: int = 100):
    q = {"acknowledged": False} if unack_only else {}
    return await db.alerts.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)


@api.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str):
    await db.alerts.update_one({"id": alert_id}, {"$set": {"acknowledged": True, "acknowledged_at": now_iso()}})
    return {"ok": True}


# ---------- DECISIONS ----------
@api.get("/decisions", response_model=List[Decision])
async def list_decisions(shed_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100):
    q = {}
    if shed_id:
        q["shed_id"] = shed_id
    if status:
        q["status"] = status
    return await db.decisions.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)


@api.post("/decisions/approve")
async def approve_decision(payload: DecisionApproval):
    if payload.approve:
        await db.decisions.update_one(
            {"id": payload.decision_id},
            {"$set": {"status": "approved", "approved_at": now_iso()}},
        )
    else:
        await db.decisions.update_one(
            {"id": payload.decision_id},
            {"$set": {"status": "rejected", "rejected_at": now_iso()}},
        )
    return {"ok": True}


# ---------- POLICIES ----------
@api.get("/policies", response_model=List[Policy])
async def list_policies():
    return await db.policies.find({}, {"_id": 0}).to_list(100)


@api.patch("/policies/{action_type}")
async def update_policy(action_type: str, payload: PolicyUpdate):
    upd = {"mode": payload.mode, "updated_at": now_iso()}
    if payload.max_change_pct is not None:
        upd["max_change_pct"] = payload.max_change_pct
    await db.policies.update_one({"action_type": action_type}, {"$set": upd}, upsert=True)
    return await db.policies.find_one({"action_type": action_type}, {"_id": 0})


# ---------- MOTHER HEN DEEP ANALYSIS (LLM) ----------
@api.post("/mother-hen/analyze/{shed_id}")
async def deep_analysis(shed_id: str):
    shed = await db.sheds.find_one({"id": shed_id}, {"_id": 0})
    if not shed:
        raise HTTPException(404, "Shed not found")
    batch = await _get_active_batch(shed_id)
    latest = await db.readings.find_one({"shed_id": shed_id}, {"_id": 0}, sort=[("timestamp", -1)])
    if not batch or not latest:
        return {"summary": "Insufficient data: no active batch or readings yet."}

    grow_day = _grow_day_from(batch)
    r_obj = Reading(**latest)
    findings = analyze_reading(r_obj, batch["breed"], grow_day, batch["bird_count_current"], shed["capacity"])
    context = {
        "shed_name": shed["name"],
        "batch_breed": batch["breed"],
        "grow_day": grow_day,
        "bird_count_current": batch["bird_count_current"],
        "bird_count_start": batch["bird_count_start"],
        "mortality_total": batch["mortality_total"],
        "latest_reading": {k: v for k, v in latest.items() if v is not None and k not in ("id",)},
        "findings": findings,
        "breed_targets": {
            "temp_c": round(get_standard(batch["breed"], "temp_c", grow_day), 1),
            "weight_g": round(get_standard(batch["breed"], "weight_g", grow_day), 1),
            "water_ml_per_bird": round(get_standard(batch["breed"], "water_ml_per_bird", grow_day), 1),
        },
    }
    summary = await llm_deep_analysis(context)
    if summary:
        d = Decision(
            shed_id=shed_id,
            batch_id=batch["id"],
            action_type="analysis",
            recommendation=summary[:800],
            reasoning="LLM deep analysis (Claude Sonnet 4.5)",
            urgency="low",
            confidence=0.75,
            status="recommended",
            ai_source="llm",
            data_snapshot={"findings_count": len(findings)},
        )
        await db.decisions.insert_one(d.model_dump())
    return {"summary": summary or "LLM unavailable", "findings": findings, "context": context}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("coop-overwatch")


@app.on_event("startup")
async def on_startup():
    logger.info("Coop Overwatch starting; ensuring demo data...")
    try:
        result = await seed_if_empty(db)
        logger.info(f"Seed result: {result}")
    except Exception as e:
        logger.exception(f"Seed failed: {e}")


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
