"""Pydantic models for Coop Overwatch. UUID string ids, no ObjectId leakage."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Literal

from pydantic import BaseModel, Field, ConfigDict


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


Breed = Literal["Ross 308", "Cobb 500"]
Status = Literal["healthy", "warning", "critical", "offline"]
PolicyMode = Literal["auto", "recommend", "manual"]


# ---------- FARM ----------
class Farm(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    name: str
    location: Optional[str] = None
    vendor_system: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class FarmCreate(BaseModel):
    name: str
    location: Optional[str] = None
    vendor_system: Optional[str] = None


# ---------- SHED ----------
class Shed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    farm_id: str
    name: str
    capacity: int = 20000
    vendor_system: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class ShedCreate(BaseModel):
    farm_id: str
    name: str
    capacity: int = 20000
    vendor_system: Optional[str] = None


class ShedUpdate(BaseModel):
    name: Optional[str] = None
    capacity: Optional[int] = None
    vendor_system: Optional[str] = None


# ---------- BATCH ----------
class Batch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    shed_id: str
    breed: Breed
    start_date: str  # ISO date
    bird_count_start: int
    bird_count_current: int
    mortality_total: int = 0
    status: Literal["active", "closed"] = "active"
    closed_at: Optional[str] = None
    final_weight_g: Optional[float] = None
    final_fcr: Optional[float] = None
    notes: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class BatchCreate(BaseModel):
    shed_id: str
    breed: Breed
    start_date: str
    bird_count_start: int
    notes: Optional[str] = None


class BatchClose(BaseModel):
    final_weight_g: Optional[float] = None
    final_fcr: Optional[float] = None
    notes: Optional[str] = None


# ---------- READING ----------
class Reading(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    shed_id: str
    batch_id: Optional[str] = None
    timestamp: str = Field(default_factory=now_iso)
    # environment
    temp_c: Optional[float] = None
    temp_required_c: Optional[float] = None
    temp_outside_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    static_pressure_pa: Optional[float] = None
    ammonia_ppm: Optional[float] = None
    # mechanical
    airflow_pct: Optional[float] = None
    damper_pct: Optional[float] = None
    cool_flap_pct: Optional[float] = None
    curtain_pct: Optional[float] = None
    heaters_on: Optional[int] = None
    fans_on: Optional[int] = None
    lighting_pct: Optional[float] = None
    # consumables
    water_liters: Optional[float] = None  # cumulative for the day
    feed_kg: Optional[float] = None
    # bird
    mortality_today: Optional[int] = None
    avg_weight_g: Optional[float] = None
    grow_day: Optional[int] = None


class ReadingIngest(BaseModel):
    shed_id: str
    timestamp: Optional[str] = None
    temp_c: Optional[float] = None
    temp_required_c: Optional[float] = None
    temp_outside_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    static_pressure_pa: Optional[float] = None
    ammonia_ppm: Optional[float] = None
    airflow_pct: Optional[float] = None
    damper_pct: Optional[float] = None
    cool_flap_pct: Optional[float] = None
    curtain_pct: Optional[float] = None
    heaters_on: Optional[int] = None
    fans_on: Optional[int] = None
    lighting_pct: Optional[float] = None
    water_liters: Optional[float] = None
    feed_kg: Optional[float] = None
    mortality_today: Optional[int] = None
    avg_weight_g: Optional[float] = None


# ---------- ALERT ----------
class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    shed_id: str
    batch_id: Optional[str] = None
    severity: Literal["info", "warning", "critical"]
    category: str  # temperature, humidity, water, ammonia, mortality, ventilation, ai
    title: str
    message: str
    acknowledged: bool = False
    acknowledged_at: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


# ---------- DECISION (Mother Hen audit log) ----------
class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    shed_id: str
    batch_id: Optional[str] = None
    action_type: str  # temp_setpoint, fan_speed, damper, lighting, alert, none
    recommendation: str
    reasoning: str
    urgency: Literal["low", "medium", "high"] = "low"
    confidence: float = 0.7  # 0-1
    status: Literal["auto_applied", "pending_approval", "recommended", "approved", "rejected"] = "recommended"
    data_snapshot: dict = Field(default_factory=dict)
    approved_at: Optional[str] = None
    rejected_at: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)
    ai_source: Literal["rules", "llm"] = "rules"


class DecisionApproval(BaseModel):
    decision_id: str
    approve: bool


# ---------- POLICY ----------
class Policy(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    action_type: str  # temp_setpoint, fan_speed, damper, cool_flap, lighting, ventilation
    mode: PolicyMode = "recommend"
    max_change_pct: float = 5.0
    updated_at: str = Field(default_factory=now_iso)


class PolicyUpdate(BaseModel):
    mode: PolicyMode
    max_change_pct: Optional[float] = None
