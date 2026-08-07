"""
Farm Buddy — AI assistant that watches the Feed Program and recommends
where to drop the next feed loads, flags risky sheds, and answers natural
language questions about the farm.

Wired into the existing FastAPI app via `init_farm_buddy(app)`.

Uses the same emergentintegrations LLM pattern as docket scanning (Gemini Flash).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger("farm_buddy")


# ─── Request / Response shapes ──────────────────────────────────────────

class ShedSnapshot(BaseModel):
    shedNum: int
    name: Optional[str] = None
    birdsPlaced: Optional[float] = None       # LIVE count (placement − morts − caught)
    birdsOriginalPlaced: Optional[float] = None
    mortsToDate: Optional[float] = None
    birdsCaught: Optional[float] = None
    upcomingCatches: Optional[list[dict]] = None  # [{date, birds}, ...] next 7-14 days
    dayAge: Optional[int] = None
    mortalityPct: Optional[float] = None
    siloA: Optional[float] = None  # tonnes
    siloB: Optional[float] = None
    siloC: Optional[float] = None
    siloTotal: Optional[float] = None
    dailyUsageT: Optional[float] = None  # avg t/day
    daysOfFeedLeft: Optional[float] = None


class DeliverySnapshot(BaseModel):
    date: str
    feedType: Optional[str] = None
    amountT: Optional[float] = None
    shedTargeted: Optional[str] = None


class FarmContext(BaseModel):
    farmName: Optional[str] = "Your farm"
    farmSlug: Optional[str] = None
    placementDate: Optional[str] = None
    breed: Optional[str] = None
    sheds: list[ShedSnapshot] = []
    recentDeliveries: list[DeliverySnapshot] = []
    pendingLoadT: Optional[float] = None  # how much feed is queued to arrive
    pendingLoadType: Optional[str] = None
    notes: Optional[str] = None


class FarmBuddyRequest(BaseModel):
    farmContext: FarmContext
    userQuestion: Optional[str] = None  # If set, conversational mode. Else: proactive overview.
    language: Optional[str] = None      # 2-letter code (en/vi/zh/pt/es/id/th/ko/tl/hi/ar/fr/de/ja/ms). Default = English.


class FarmBuddyResponse(BaseModel):
    headline: str
    detail: list[str]            # bullet-point explanations
    actions: list[dict]          # [{shedNum, recommendedT, reason}, ...]
    riskLevel: str               # "ok" | "watch" | "urgent"
    source: Optional[str] = None # "llm" | "heuristic"
    raw: Optional[str] = None    # raw model text, useful for debugging


# ─── Prompt building ───────────────────────────────────────────────────

SYSTEM_MSG = (
    "You are Farm Buddy, an expert AI advisor for Australian broiler chicken farmers. "
    "Your job: look at the feed program data the user gives you, then recommend WHERE the "
    "next feed loads should be dropped, flag any sheds at risk of running out, and answer "
    "questions in clear, practical, farmer-friendly language (no jargon, no preachy disclaimers). "
    "Always think in TONNES (t). Use shed numbers like 'Shed 5&6'. Be specific and concise. "
    "If the question is a general one (e.g. 'what's going on?'), give a 1-line headline, 2-4 short bullets, "
    "and (if relevant) a recommended split of the next load. "
    "RETURN VALID JSON ONLY — no markdown fences, no commentary outside the JSON object."
)


def _build_user_prompt(ctx: FarmContext, question: Optional[str]) -> str:
    """Compress the farm context into a compact JSON-like brief for the model."""
    sheds_compact = []
    for s in ctx.sheds:
        line = f"Shed {s.shedNum}"
        if s.name and s.name != f"Shed {s.shedNum}":
            line += f" ({s.name})"
        parts = []
        if s.birdsPlaced:
            parts.append(f"{int(s.birdsPlaced):,} live birds")
        if s.birdsOriginalPlaced and s.birdsCaught:
            parts.append(f"({int(s.birdsCaught):,} already caught of {int(s.birdsOriginalPlaced):,} placed)")
        elif s.birdsOriginalPlaced and s.mortsToDate:
            parts.append(f"({int(s.birdsOriginalPlaced):,} placed − {int(s.mortsToDate):,} morts)")
        if s.upcomingCatches:
            uc_str = ", ".join(f"{int(c.get('birds') or 0):,} on {c.get('date')}" for c in s.upcomingCatches[:5])
            parts.append(f"upcoming catches: {uc_str}")
        if s.dayAge is not None:
            parts.append(f"day {s.dayAge}")
        if s.mortalityPct is not None:
            parts.append(f"{s.mortalityPct:.1f}% mort")
        # Silos
        silo_bits = []
        if s.siloA is not None: silo_bits.append(f"A {s.siloA:.1f}t")
        if s.siloB is not None: silo_bits.append(f"B {s.siloB:.1f}t")
        if s.siloC is not None: silo_bits.append(f"C {s.siloC:.1f}t")
        if silo_bits:
            parts.append("silos: " + ", ".join(silo_bits))
        if s.siloTotal is not None:
            parts.append(f"total feed on hand: {s.siloTotal:.1f}t")
        if s.dailyUsageT:
            parts.append(f"using {s.dailyUsageT:.1f}t/day")
        if s.daysOfFeedLeft is not None:
            parts.append(f"~{s.daysOfFeedLeft:.1f} days left")
        sheds_compact.append(line + " — " + "; ".join(parts) if parts else line)

    deliveries_compact = []
    for d in ctx.recentDeliveries[:8]:
        parts = [d.date]
        if d.feedType: parts.append(d.feedType)
        if d.amountT:  parts.append(f"{d.amountT:.1f}t")
        if d.shedTargeted: parts.append(f"→ {d.shedTargeted}")
        deliveries_compact.append(" · ".join(parts))

    brief = {
        "farm": ctx.farmName,
        "breed": ctx.breed or "unknown",
        "placement": ctx.placementDate,
        "sheds": sheds_compact,
        "recentDeliveries": deliveries_compact,
        "pendingLoad": (
            f"{ctx.pendingLoadT:.1f}t {ctx.pendingLoadType or ''}".strip()
            if ctx.pendingLoadT else None
        ),
        "notes": ctx.notes,
    }

    if question:
        # Conversational mode
        return (
            f"User question: {question}\n\n"
            f"Current farm state:\n{json.dumps(brief, indent=2)}\n\n"
            f"Reply with a JSON object: {{\"headline\": \"<one line answer>\", "
            f"\"detail\": [\"<bullet1>\", \"<bullet2>\"], "
            f"\"actions\": [{{\"shedNum\": <num>, \"recommendedT\": <tonnes>, \"reason\": \"<why>\"}}], "
            f"\"riskLevel\": \"ok\"|\"watch\"|\"urgent\"}}"
        )

    # Proactive overview
    return (
        f"Look over this farm and give me your top recommendation: WHERE should the next "
        f"feed load(s) be dropped? Flag any shed at risk of running out within 48 hours.\n\n"
        f"Current farm state:\n{json.dumps(brief, indent=2)}\n\n"
        f"Reply with a JSON object: {{\"headline\": \"<one-line action you'd take right now>\", "
        f"\"detail\": [\"<bullet1>\", \"<bullet2>\", \"<bullet3>\"], "
        f"\"actions\": [{{\"shedNum\": <num>, \"recommendedT\": <tonnes>, \"reason\": \"<why>\"}}], "
        f"\"riskLevel\": \"ok\"|\"watch\"|\"urgent\"}}"
    )


# ─── Cache + LLM call ───────────────────────────────────────────────────

# Per-farm last response cache so we don't call the LLM on every keystroke.
_cache: dict[str, dict] = {}
_CACHE_TTL_S = 60  # one minute


def _cache_key(req: FarmBuddyRequest) -> str:
    # Cache only proactive overviews; questions vary per call
    if req.userQuestion:
        return ""
    slug = req.farmContext.farmSlug or req.farmContext.farmName or "default"
    # Hash the sheds tuple so any data change busts cache
    payload = json.dumps([s.model_dump() for s in req.farmContext.sheds], sort_keys=True)
    return f"{slug}|{hash(payload)}"


# ─── Local heuristic recommender (LLM fallback) ─────────────────────────
# Calculates feed allocation purely from the data — no AI needed. Used when:
#   • Emergent LLM budget is exhausted
#   • Network call fails
#   • Response parsing fails
# This is actually quite good on its own — feed allocation is mostly arithmetic.

def _heuristic_recommend(ctx: FarmContext) -> dict:
    if not ctx.sheds:
        return {
            "headline": "No shed data yet — add some readings and I'll watch the silos for you.",
            "detail": [],
            "actions": [],
            "riskLevel": "ok",
            "source": "heuristic",
        }

    # Score each shed by how soon it'll run out (lower = more urgent)
    scored = []
    for s in ctx.sheds:
        days_left = s.daysOfFeedLeft
        if days_left is None and s.siloTotal and s.dailyUsageT and s.dailyUsageT > 0:
            days_left = s.siloTotal / s.dailyUsageT
        scored.append({
            "shed":          s,
            "days_left":     days_left if days_left is not None else 999.0,
            "silo_total":    s.siloTotal or 0,
            "daily_usage":   s.dailyUsageT or 0,
        })

    scored.sort(key=lambda x: x["days_left"])

    # Risk classification
    most_urgent = scored[0]
    if most_urgent["days_left"] < 1.5:
        risk = "urgent"
    elif most_urgent["days_left"] < 3:
        risk = "watch"
    else:
        risk = "ok"

    # Build the recommendation
    pending = ctx.pendingLoadT or 30.0  # default to a 30t load if none specified
    actions = []
    detail = []
    headline = ""

    # Approach: allocate next load to top sheds in priority order until depleted
    remaining = pending
    for entry in scored:
        if remaining <= 0:
            break
        shed = entry["shed"]
        days_left = entry["days_left"]
        usage = entry["daily_usage"]
        if days_left >= 7:
            continue  # plenty of feed, skip
        # How much to drop here: cover the next 5 days of usage (or whatever fills the silo headroom),
        # capped at remaining tonnes available.
        target_days = 5.0
        need = max(0, (target_days - days_left) * usage) if usage else min(remaining, 24.0)
        # Cap to a reasonable single-shed drop (most silos hold 24-30t total)
        drop = min(need, remaining, 30.0)
        if drop < 1.0:
            continue
        drop = round(drop, 1)
        actions.append({
            "shedNum":       shed.shedNum,
            "recommendedT":  drop,
            "reason": (
                f"~{days_left:.1f} days left, using {usage:.1f}t/day"
                if usage else f"silo total {entry['silo_total']:.1f}t"
            ),
        })
        remaining -= drop

    # If nothing scored as needy, suggest holding the load
    if not actions:
        urgent_shed_obj = most_urgent["shed"]
        urgent_label = urgent_shed_obj.name or f"Shed {urgent_shed_obj.shedNum}"
        headline = f"All sheds are comfortable — earliest run-out is ~{most_urgent['days_left']:.1f} days away in {urgent_label}."
        detail = [f"No shed under 7 days of feed. Hold the {pending:.0f}t load or schedule a flexible delivery."]
    else:
        top = actions[0]
        top_shed = next(s for s in ctx.sheds if s.shedNum == top["shedNum"])
        shed_label = top_shed.name or f"Shed {top['shedNum']}"
        headline = f"Drop {top['recommendedT']:.1f}t into {shed_label} first — {top['reason']}"
        for a in actions:
            sd = next(s for s in ctx.sheds if s.shedNum == a["shedNum"])
            label = sd.name or f"Shed {a['shedNum']}"
            detail.append(f"{label}: {a['recommendedT']:.1f}t — {a['reason']}")
        if remaining > 1.0:
            detail.append(f"Hold back {remaining:.1f}t for next load or top up the comfortable sheds.")

    # Flag any urgent shed not in actions for visibility
    for entry in scored:
        if entry["days_left"] < 1.5 and not any(a["shedNum"] == entry["shed"].shedNum for a in actions):
            label = entry["shed"].name or f"Shed {entry['shed'].shedNum}"
            detail.append(f"⚠ {label} only has ~{entry['days_left']:.1f} days of feed — schedule a top-up.")

    return {
        "headline":  headline,
        "detail":    detail,
        "actions":   actions,
        "riskLevel": risk,
        "source":    "heuristic",
    }


async def _call_llm(req: FarmBuddyRequest) -> dict:
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except Exception as e:
        raise HTTPException(503, f"AI integration not installed: {e}")

    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        raise HTTPException(503, "EMERGENT_LLM_KEY not configured")

    # Append language directive to system message if non-English
    _LANG_NAMES = {
        "en":"English","vi":"Vietnamese","zh":"Simplified Chinese","zh-CN":"Simplified Chinese",
        "pt":"Brazilian Portuguese","es":"Spanish","id":"Indonesian","th":"Thai",
        "ko":"Korean","tl":"Filipino/Tagalog","hi":"Hindi","ar":"Arabic",
        "fr":"French","de":"German","ja":"Japanese","ms":"Malay",
    }
    lang_code = (req.language or "en").lower()
    lang_name = _LANG_NAMES.get(lang_code, "English")
    sys_msg = SYSTEM_MSG
    if lang_code != "en" and lang_name != "English":
        sys_msg += f" IMPORTANT: Write ALL headline, detail bullets and reason text in {lang_name}. Keep JSON keys and shed identifiers (like 'Shed 5&6') in English."

    chat = LlmChat(
        api_key=api_key,
        session_id=f"farm-buddy-{uuid.uuid4().hex}",
        system_message=sys_msg,
    ).with_model("gemini", "gemini-2.5-flash")

    user_prompt = _build_user_prompt(req.farmContext, req.userQuestion)
    try:
        raw = await chat.send_message(UserMessage(text=user_prompt))
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
            # Soft-fail rather than 500 — return raw so UI can still render something
            return {
                "headline": "Couldn't read AI response",
                "detail": [cleaned[:300] or "Empty AI response"],
                "actions": [],
                "riskLevel": "ok",
                "raw": text[:800],
            }
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return {
                "headline": "Couldn't parse AI response",
                "detail": [cleaned[:300]],
                "actions": [],
                "riskLevel": "ok",
                "raw": text[:800],
            }

    # Defensive defaults
    parsed.setdefault("headline", "All good for now.")
    parsed.setdefault("detail", [])
    parsed.setdefault("actions", [])
    parsed.setdefault("riskLevel", "ok")
    if not isinstance(parsed.get("detail"), list): parsed["detail"] = [str(parsed["detail"])]
    if not isinstance(parsed.get("actions"), list): parsed["actions"] = []
    return parsed


# ─── Wire-up ────────────────────────────────────────────────────────────

def init_farm_buddy(app: FastAPI, db) -> None:
    """Mount /api/farm-buddy/recommend onto the FastAPI app."""

    @app.post("/api/farm-buddy/recommend", response_model=FarmBuddyResponse)
    async def farm_buddy_recommend(req: FarmBuddyRequest, request: Request):
        # Lightweight short-term cache so the proactive banner doesn't hammer the LLM
        key = _cache_key(req)
        now = datetime.now(timezone.utc).timestamp()
        if key:
            hit = _cache.get(key)
            if hit and (now - hit["ts"]) < _CACHE_TTL_S:
                return hit["response"]

        # Try LLM first for natural language smarts; fall back to deterministic
        # math if budget exhausted, network down, or any other LLM failure.
        parsed: dict
        used_source = "llm"
        try:
            parsed = await _call_llm(req)
        except HTTPException as he:
            log.warning("Farm Buddy LLM unavailable (%s) — using local heuristic", he.detail)
            parsed = _heuristic_recommend(req.farmContext)
            used_source = "heuristic"
        except Exception as e:
            log.warning("Farm Buddy LLM crashed (%s) — using local heuristic", e)
            parsed = _heuristic_recommend(req.farmContext)
            used_source = "heuristic"

        parsed.setdefault("source", used_source)

        if key:
            _cache[key] = {"ts": now, "response": parsed}
            if len(_cache) > 100:
                oldest = min(_cache.items(), key=lambda kv: kv[1]["ts"])[0]
                _cache.pop(oldest, None)

        # Log for ops insight
        try:
            await db["farm_buddy_log"].insert_one({
                "farmSlug":   req.farmContext.farmSlug,
                "farmName":   req.farmContext.farmName,
                "shedsCount": len(req.farmContext.sheds),
                "question":   req.userQuestion,
                "headline":   parsed.get("headline"),
                "riskLevel":  parsed.get("riskLevel"),
                "source":     parsed.get("source"),
                "createdAt":  datetime.now(timezone.utc),
            })
        except Exception:
            pass

        return parsed
