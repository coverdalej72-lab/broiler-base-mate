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

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
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
    # ── Weight & density (wired Feb 2026) ─────────────────────────────
    # Hydrated by the frontend from: today's-weight override > latest
    # catch aveWgt > latest weigh-in. Lets Farm Buddy speak about
    # density automatically without the grower opening the density panel.
    currentAvgWeightKg: Optional[float] = None
    weightSource: Optional[str] = None      # "override" | "catch" | "weighin"
    floorAreaM2: Optional[float] = None     # summed for the shed group
    kgPerM2: Optional[float] = None         # currentAvgWeightKg × birdsPlaced / floorAreaM2
    densityStatus: Optional[str] = None     # "ok" (<30) | "warn" (30-34) | "over" (≥34) — Baiada max 34


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
    "\n\n"
    "── HOW THE WHOLE SYSTEM WORKS (read carefully) ──\n"
    "Each shed has FOUR movers that decide how many birds are alive right now, and each one "
    "affects daily feed usage. LIVE BIRDS = Placement − Morts − Culls − Catches.\n"
    "  • PLACEMENT: the starting count from the hatchery (fixed, on the Feed Program's shed sheet).\n"
    "  • MORTS: birds that died. Recorded in three places — daily on the shed tab (col 13), on the "
    "    Morts log tab, and rolled up on the End-of-Batch sheet. The processor's Weight Sheet has "
    "    RED-highlighted final-pickup rows that also imply morts (Placement − sum of catches).\n"
    "  • CULLS: birds the grower removed (sick, weak). Recorded on the Culls log tab.\n"
    "  • CATCHES (aka pickups): birds the processor collected on catch nights. On the Weight Sheet, "
    "    the FINAL catch row for a shed is highlighted RED — after that catch, the shed is empty.\n"
    "The number in birdsPlaced sent to you is already the LIVE reconciled count "
    "(Placement − MAX(all mort sources) − Catches). birdsOriginalPlaced is the day-one number.\n"
    "\n"
    "── COMMON REAL-WORLD DISCREPANCIES ──\n"
    "  1. Weight sheet says fewer birds left than shed sheet — probably morts under-recorded by the grower.\n"
    "  2. Catches > placement — probably a data-entry duplicate OR the processor over-counted.\n"
    "  3. Big mortality spike (heat wave, disease) — feed usage should also drop fast; if it doesn't, "
    "     the birds may not actually be dying, they may be being caught early.\n"
    "  4. Feed rate per bird looks too high for the age (>180 g/bird/day) — morts likely under-recorded, "
    "     so the sheet thinks there are more birds than reality.\n"
    "  5. Feed rate per bird too low (<80 g/bird/day for post-day-14 birds) — either birds dying "
    "     unnoticed, or the placement was over-stated.\n"
    "  6. DUPLICATE CATCHES: two catch rows on the same shed with the SAME avg weight (± 0.005 kg) — "
    "     the odds of two natural catch nights hitting identical avg weight are near zero. This is "
    "     almost always a copy-paste-twice or a re-uploaded weight sheet double-counting. Flag it "
    "     explicitly: `Shed X has duplicate catches at Y.YYY kg — delete one before it bleeds into "
    "     Batch Results and inflates birds-out.` Same rule applies if two rows share the same date + "
    "     same avg weight, or same avg weight + same bird count.\n"
    "  7. NEW WEIGHT SHEET RE-UPLOAD: when a fresh Baiada weight sheet arrives it is usually an "
    "     AMENDMENT (updated bird count for a shed that was already caught), NOT a set of net-new "
    "     catches. If the client asks about a re-upload, remind them that duplicate detection matches "
    "     on (shed + age + avg weight) or (shed + date + avg weight) so amendments overwrite rather "
    "     than double-count.\n"
    "When you see any of these, call it out plainly, name the shed, and suggest what to check or fix.\n"
    "\n"
    "── FEED ORDER RECOMMENDATIONS ──\n"
    "Use LIVE birds × standard g/bird/day for the age (Ross 308 curve), then multiply by days until "
    "the next catch (upcomingCatches field). Subtract feed already on hand (siloTotal). If daily "
    "usage is measured (dailyUsageT), trust it over standard curves. NEVER recommend more feed than "
    "the sheds can consume before their next scheduled catch — that's wasted feed.\n"
    "\n"
    "DENSITY: when a shed's context includes kgPerM2, factor it into your advice. Baiada max is "
    "34 kg/m² — flag any shed at densityStatus='over' as URGENT ('shed X over Baiada max, thin-out "
    "or depop now'), 'warn' (30-34 kg/m²) as WATCH ('shed X approaching Baiada max — plan thin-out'), "
    "and 'ok' as fine. If both feed-runout AND density are concerns for the same shed, mention both. "
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
            parts.append(f"{int(s.birdsPlaced):,} LIVE birds (reconciled)")
        if s.birdsOriginalPlaced and s.birdsCaught:
            impliedMorts = int(s.birdsOriginalPlaced) - int(s.birdsCaught) - int(s.birdsPlaced or 0)
            parts.append(f"placement {int(s.birdsOriginalPlaced):,} − caught {int(s.birdsCaught):,} − morts/culls ~{max(0,impliedMorts):,}")
        elif s.birdsOriginalPlaced and s.mortsToDate:
            parts.append(f"placement {int(s.birdsOriginalPlaced):,} − morts/culls {int(s.mortsToDate):,}")
        # Feed per bird per day sanity check (helps AI flag under-recorded morts)
        if s.dailyUsageT and s.birdsPlaced and s.birdsPlaced > 0:
            gPerBirdPerDay = (s.dailyUsageT * 1_000_000) / s.birdsPlaced
            parts.append(f"feed rate {gPerBirdPerDay:.0f} g/bird/day")
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
        # Density (Feb 2026): let Farm Buddy speak about kg/m² automatically.
        if s.currentAvgWeightKg is not None and s.currentAvgWeightKg > 0:
            src = f" via {s.weightSource}" if s.weightSource else ""
            parts.append(f"avg weight {s.currentAvgWeightKg:.3f} kg/bird{src}")
        if s.kgPerM2 is not None and s.kgPerM2 > 0:
            status_lbl = {"over": "OVER Baiada max", "warn": "approaching max", "ok": "under max"}.get(s.densityStatus or "", "")
            floor_str = f" ({s.floorAreaM2:.0f} m² floor)" if s.floorAreaM2 else ""
            parts.append(f"density {s.kgPerM2:.1f} kg/m²{floor_str} — {status_lbl} (Baiada max 34)")
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

    # ── SEC-003 — Per-IP rate limiter (in-memory, sliding window) ──────
    # Prevents anonymous LLM cost bombs + upload DoS. 30 requests / min /
    # IP is generous for a real grower using the app but crushes a bot.
    _rate_state: dict[str, list[float]] = {}
    _RATE_WINDOW_S = 60.0
    _RATE_LIMIT = 30

    def _rate_check(request: Request) -> None:
        ip = (request.client.host if request.client else "?") or "?"
        now = datetime.now(timezone.utc).timestamp()
        bucket = _rate_state.get(ip, [])
        bucket = [t for t in bucket if now - t < _RATE_WINDOW_S]
        if len(bucket) >= _RATE_LIMIT:
            raise HTTPException(429, "Too many requests — slow down for a minute.")
        bucket.append(now)
        _rate_state[ip] = bucket
        if len(_rate_state) > 500:  # prune old
            for k in list(_rate_state.keys()):
                if not _rate_state[k] or now - _rate_state[k][-1] > _RATE_WINDOW_S * 3:
                    _rate_state.pop(k, None)

    @app.post("/api/farm-buddy/recommend", response_model=FarmBuddyResponse)
    async def farm_buddy_recommend(req: FarmBuddyRequest, request: Request):
        _rate_check(request)
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


# ─── Grower Pickup Report reconciliation ────────────────────────────────
# Feb 2026 — Jason uploaded a Baiada "Grower Pickup Report" .docx and asked:
# "can buddy audit this and compare his records and this". The reconciler
# accepts the pickup-report rows (parsed client-side or via a raw text
# paste that the server best-effort parses) plus the current app catchMap
# and returns:
#   • diff  → matched / missing (in report but not app) / extra (in app
#              but not report) / mismatched (same shed+date but bird count
#              or weight differ significantly)
#   • narrative → plain-english Farm Buddy audit summary
#
# NO new deps — we regex-parse the pasted table text (Word tables copy as
# tab or newline-separated rows). Everything happens in-process.
# ────────────────────────────────────────────────────────────────────────

class PickupReportRow(BaseModel):
    pickupDate:  Optional[str] = None   # "DD/MM/YYYY" as printed in the doc
    pickupNum:   Optional[str] = None   # "ADUBB01-2603-01" — last digits = shed
    farmName:    Optional[str] = None
    shedNum:     Optional[int] = None   # derived from pickupNum if not supplied
    age:         Optional[int] = None
    birdsCaught: Optional[float] = None
    liveWeightKg: Optional[float] = None
    aveWeightKg: Optional[float] = None


class AppCatchRow(BaseModel):
    shedNum:   int
    date:      Optional[str] = None     # DD/MM/YYYY
    age:       Optional[int] = None
    birds:     Optional[float] = None
    aveWgt:    Optional[float] = None
    totalWgt:  Optional[float] = None


class ReconcileRequest(BaseModel):
    pickupText:   Optional[str] = None        # raw pasted text from the doc
    pickupRows:   Optional[list[PickupReportRow]] = None  # already-parsed rows
    appCatches:   list[AppCatchRow]
    farmName:     Optional[str] = None
    farmSlug:     Optional[str] = None
    generateNarrative: Optional[bool] = True


class ReconcileDiffRow(BaseModel):
    status:  str        # "match" | "missing_in_app" | "extra_in_app" | "mismatch"
    shedNum: int
    date:    Optional[str] = None
    age:     Optional[int] = None
    reportBirds:   Optional[float] = None
    reportWeight:  Optional[float] = None
    reportAveKg:   Optional[float] = None
    appBirds:      Optional[float] = None
    appAveKg:      Optional[float] = None
    note:    Optional[str] = None


class ReconcileResponse(BaseModel):
    matched:     list[ReconcileDiffRow]
    missing:     list[ReconcileDiffRow]
    extra:       list[ReconcileDiffRow]
    mismatched:  list[ReconcileDiffRow]
    totals: dict[str, Any]
    narrative:   Optional[str] = None
    parsedReport: list[PickupReportRow]


_SHED_FROM_PICKUP_RE = re.compile(r"[-\s](\d{1,2})\s*$")
_ROW_KG_RE           = re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.IGNORECASE)
_ROW_DATE_RE         = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")
_ROW_PICKUP_RE       = re.compile(r"([A-Z]{2,}[A-Z0-9]*-\d+-\s*\d{1,2})", re.IGNORECASE)


def _shed_from_pickup(pickup_num: Optional[str]) -> Optional[int]:
    if not pickup_num:
        return None
    m = _SHED_FROM_PICKUP_RE.search(str(pickup_num).strip())
    if m:
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


def _parse_pickup_text(raw: str) -> list[PickupReportRow]:
    """Best-effort parser for pasted Word/Excel table text. Each line is
    treated as a candidate row; we look for a pickup# (…-XX), a date, an
    age (2-digit stand-alone int under 100), a bird count (int > 100),
    a total weight kg, and an average weight kg."""
    if not raw:
        return []
    out: list[PickupReportRow] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or len(s) < 8:
            continue
        # Skip obvious headers
        if re.search(r"pickup\s*date|age.*catch|bird.*count|live.*weight", s, re.IGNORECASE):
            continue
        pickup_m = _ROW_PICKUP_RE.search(s)
        date_m   = _ROW_DATE_RE.search(s)
        kg_matches = _ROW_KG_RE.findall(s)
        # Numbers on the line — strip commas
        nums = [float(n.replace(",", "")) for n in re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", s)]
        if not (pickup_m and date_m and nums):
            continue
        pickup_num = re.sub(r"\s+", "", pickup_m.group(1))
        shed = _shed_from_pickup(pickup_num)
        # Heuristics for age / birds / weight ordering: age is a stand-alone
        # small int (25-60), bird count is a mid int (500-15000), live weight
        # is a big int (1000-50000), ave weight is small float in kg (1.5-4.5)
        age = birds = live_kg = ave_kg = None
        for n in nums:
            if age is None and 20 <= n <= 60 and float(n).is_integer():
                age = int(n); continue
            if ave_kg is None and 1.2 <= n <= 5.0 and abs(n - int(n)) > 0.001:
                ave_kg = n; continue
            if birds is None and 200 <= n <= 20000 and float(n).is_integer():
                birds = n; continue
            if live_kg is None and 1000 <= n <= 60000 and float(n).is_integer():
                live_kg = n; continue
        # If ave wasn't picked but "N.NN kg" strings were, prefer those
        if ave_kg is None and kg_matches:
            for k in kg_matches:
                v = float(k)
                if 1.2 <= v <= 5.0:
                    ave_kg = v; break
        if shed is None:
            continue
        out.append(PickupReportRow(
            pickupDate=date_m.group(1),
            pickupNum=pickup_num,
            shedNum=shed,
            age=age,
            birdsCaught=birds,
            liveWeightKg=live_kg,
            aveWeightKg=ave_kg,
        ))
    return out


def _parse_pickup_docx(file_bytes: bytes) -> tuple[list[PickupReportRow], Optional[str]]:
    """Parse a Baiada / Ingham / mTech-style Grower Pickup Report .docx by
    reading its native tables (falls back to plain text if the doc has no
    tables). Returns (rows, farmName). Never raises — on any error returns
    ([], None) so the caller can retry with the paste-text path."""
    try:
        from docx import Document
        import io
        doc = Document(io.BytesIO(file_bytes))
    except Exception as e:
        log.warning("Could not open .docx: %s", e)
        return [], None

    farm_name: Optional[str] = None
    rows_out: list[PickupReportRow] = []

    # Walk every table — Baiada/mTech report is paginated into multiple
    # tables. Each table has a "Farm Name: …" banner row, a header row
    # (Trans Date · Entity · Farm Ref · Lot No · Age · Farm Head · Farm
    # Weight · Avg LWT · RGB), then data rows. Farm Name lives on the
    # banner row when the header row is `Trans Date …`.
    for tbl in doc.tables:
        if not tbl.rows:
            continue

        # Try to locate the header row — it's whichever row has "date" +
        # ("age" or "weight") in it. Sometimes row 0 is a banner.
        header_row_idx: Optional[int] = None
        for ri, row in enumerate(tbl.rows[:3]):
            joined = " ".join(c.text.strip().lower() for c in row.cells)
            if ("trans date" in joined or "pickup date" in joined or "date" in joined) and \
               ("age" in joined or "weight" in joined):
                header_row_idx = ri
                break
        if header_row_idx is None:
            continue

        # Grab farm name from a "Farm Name: …" banner above the header
        for ri in range(header_row_idx):
            first_cell = tbl.rows[ri].cells[0].text.strip()
            m = re.search(r"farm\s*name\s*:\s*(.+)", first_cell, re.IGNORECASE)
            if m and not farm_name:
                farm_name = m.group(1).strip()
                break

        header_cells = [c.text.strip().lower() for c in tbl.rows[header_row_idx].cells]
        col_idx: dict[str, int] = {}
        for i, h in enumerate(header_cells):
            # Date column: "Trans Date" or "Pickup Date"
            if "date" in h and "date" not in col_idx.get("_", ""):
                col_idx.setdefault("date", i)
            # Pickup number / batch#: "Entity" or "Pickup #" or "Batch"
            if h in ("entity", "pickup #", "pickup no", "batch") or "pickup" in h and "date" not in h:
                col_idx.setdefault("pickup", i)
            if "farm" in h and "name" in h:
                col_idx.setdefault("farm", i)
            if "shed" in h:
                col_idx.setdefault("shed", i)
            if "age" in h:
                col_idx.setdefault("age", i)
            # Bird count: "Farm Head" or "Birds Caught" or "Head"
            if h == "farm head" or "head" in h and "farm" in h or "bird" in h and ("caught" in h or "count" in h or "pick" in h):
                col_idx.setdefault("birds", i)
            # Live weight (total): "Farm Weight" or "Live Weight" or "Total Weight"
            if (h == "farm weight" or "live" in h and "weight" in h or "total" in h and "weight" in h):
                col_idx.setdefault("liveWeight", i)
            # Average weight: "Avg LWT" or "Ave Weight"
            if ("avg" in h or "ave" in h) and ("wt" in h or "weight" in h):
                col_idx.setdefault("aveWeight", i)

        # Skip tables that clearly aren't the pickup roster
        if "pickup" not in col_idx and "date" not in col_idx:
            continue

        for row in tbl.rows[header_row_idx + 1:]:
            cells = [c.text.strip() for c in row.cells]
            # Skip banner rows repeated on subsequent pages
            if cells and re.search(r"farm\s*name\s*:", cells[0], re.IGNORECASE):
                continue
            # Skip repeated header rows
            joined = " ".join(cells).lower()
            if "trans date" in joined or ("date" in joined and "age" in joined and "avg" in joined):
                continue

            def _cell(k: str) -> str:
                i = col_idx.get(k)
                return cells[i] if i is not None and i < len(cells) else ""

            date  = _cell("date")
            pickup = re.sub(r"\s+", "", _cell("pickup"))
            farm  = _cell("farm") or None
            if farm and not farm_name:
                farm_name = farm
            shed_txt = _cell("shed")
            age_txt  = _cell("age")
            birds_txt = _cell("birds").replace(",", "")
            live_txt  = _cell("liveWeight").replace(",", "").replace("kg", "").strip()
            ave_txt   = _cell("aveWeight").replace("kg", "").strip()

            # Skip totals / blank rows
            if not (pickup or date):
                continue

            def _to_float(s: str) -> Optional[float]:
                try:
                    return float(re.sub(r"[^\d.\-]", "", s)) if s else None
                except (TypeError, ValueError):
                    return None
            def _to_int(s: str) -> Optional[int]:
                try:
                    return int(float(re.sub(r"[^\d.\-]", "", s))) if s else None
                except (TypeError, ValueError):
                    return None

            shed = _to_int(shed_txt) or _shed_from_pickup(pickup)
            rows_out.append(PickupReportRow(
                pickupDate=date or None,
                pickupNum=pickup or None,
                farmName=farm,
                shedNum=shed,
                age=_to_int(age_txt),
                birdsCaught=_to_float(birds_txt),
                liveWeightKg=_to_float(live_txt),
                aveWeightKg=_to_float(ave_txt),
            ))

        # NOTE: don't `break` — the Baiada report paginates into multiple
        # tables, and all of them share the same schema. We want ALL rows.

    # Fallback: no tables parsed → try the plain-text extractor
    if not rows_out:
        text = "\n".join(p.text for p in doc.paragraphs)
        rows_out = _parse_pickup_text(text)

    return rows_out, farm_name


def _norm_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", str(s).strip())
    if not m:
        return None
    d, mo, y = m.groups()
    if len(y) == 2:
        y = "20" + y
    return f"{int(d):02d}/{int(mo):02d}/{y}"


def _reconcile(req: ReconcileRequest) -> ReconcileResponse:
    # ── Normalise the pickup report rows ────────────────────────────
    report_rows: list[PickupReportRow] = list(req.pickupRows or [])
    if not report_rows and req.pickupText:
        report_rows = _parse_pickup_text(req.pickupText)
    # Ensure shedNum on every row (derive from pickupNum if missing)
    for r in report_rows:
        if r.shedNum is None:
            r.shedNum = _shed_from_pickup(r.pickupNum)
    # Filter to rows with a resolvable shed
    report_rows = [r for r in report_rows if r.shedNum is not None]

    # ── Buckets ────────────────────────────────────────────────────
    matched:    list[ReconcileDiffRow] = []
    missing:    list[ReconcileDiffRow] = []
    mismatched: list[ReconcileDiffRow] = []
    # Track which app rows got claimed so we can surface unmatched app rows
    claimed: set[int] = set()

    def find_app_match(row: PickupReportRow) -> Optional[int]:
        """Match a pickup-report row to an app catch. Rule: same shed + same date
        is the ONLY reliable signal — the same shed can have multiple pickups
        at similar ages/weights over a 2-week catch window, so matching on age
        or weight alone over-matches and mis-flags rows as "missing" (which
        led to duplicate imports in Batch Results — Feb 2026 Jason)."""
        rdate = _norm_date(row.pickupDate)
        if not rdate:
            return None
        for i, a in enumerate(req.appCatches):
            if i in claimed:
                continue
            if a.shedNum != row.shedNum:
                continue
            adate = _norm_date(a.date)
            if adate and rdate == adate:
                return i
        return None

    for r in report_rows:
        idx = find_app_match(r)
        if idx is None:
            missing.append(ReconcileDiffRow(
                status="missing_in_app",
                shedNum=r.shedNum or 0,
                date=r.pickupDate,
                age=r.age,
                reportBirds=r.birdsCaught,
                reportWeight=r.liveWeightKg,
                reportAveKg=r.aveWeightKg,
                note="Row on processor pickup report but no matching catch in the app.",
            ))
            continue
        claimed.add(idx)
        a = req.appCatches[idx]
        birds_diff = None
        weight_diff = None
        if r.birdsCaught is not None and a.birds is not None:
            birds_diff = r.birdsCaught - a.birds
        if r.aveWeightKg is not None and a.aveWgt is not None:
            weight_diff = r.aveWeightKg - a.aveWgt
        # 5% birds tolerance, 0.05 kg avg-weight tolerance
        is_mismatch = (
            (birds_diff is not None  and abs(birds_diff)  > max(50, 0.05 * (a.birds or 0))) or
            (weight_diff is not None and abs(weight_diff) > 0.05)
        )
        row_out = ReconcileDiffRow(
            status="mismatch" if is_mismatch else "match",
            shedNum=r.shedNum or 0,
            date=r.pickupDate,
            age=r.age,
            reportBirds=r.birdsCaught,
            reportWeight=r.liveWeightKg,
            reportAveKg=r.aveWeightKg,
            appBirds=a.birds,
            appAveKg=a.aveWgt,
            note=(
                f"Birds Δ {int(birds_diff):+,} · Weight Δ {weight_diff:+.2f} kg"
                if is_mismatch and birds_diff is not None and weight_diff is not None
                else None
            ),
        )
        (mismatched if is_mismatch else matched).append(row_out)

    # Extra rows: app catches that never matched anything on the report
    extra: list[ReconcileDiffRow] = []
    for i, a in enumerate(req.appCatches):
        if i in claimed:
            continue
        extra.append(ReconcileDiffRow(
            status="extra_in_app",
            shedNum=a.shedNum,
            date=a.date,
            age=a.age,
            appBirds=a.birds,
            appAveKg=a.aveWgt,
            note="In the app but not on this processor pickup report — likely a stale row, a duplicate paste, or a pickup happening after the report was generated.",
        ))

    # ── Totals for a quick health read-out ─────────────────────────
    def _sum(rows: list[Any], field: str) -> float:
        return float(sum(getattr(r, field, 0) or 0 for r in rows))
    totals = {
        "reportRows":       len(report_rows),
        "appRows":          len(req.appCatches),
        "matched":          len(matched),
        "missing":          len(missing),
        "mismatched":       len(mismatched),
        "extraInApp":       len(extra),
        "reportBirds":      int(_sum(report_rows, "birdsCaught")),
        "reportLiveKg":     int(_sum(report_rows, "liveWeightKg")),
        "appBirds":         int(_sum(req.appCatches, "birds")),
    }

    return ReconcileResponse(
        matched=matched,
        missing=missing,
        extra=extra,
        mismatched=mismatched,
        totals=totals,
        narrative=None,
        parsedReport=report_rows,
    )


async def _reconcile_narrative(resp: ReconcileResponse, farm_name: Optional[str]) -> Optional[str]:
    """Ask the LLM for a plain-english audit summary of the reconciliation.
    Falls back to a deterministic summary if the LLM is unavailable."""
    t = resp.totals
    baseline = (
        f"🐤 Farm Buddy audit for {farm_name or 'your farm'} — "
        f"{t['reportRows']} rows on processor report vs {t['appRows']} in app. "
        f"✓ {t['matched']} matched · "
        f"⚠ {t['mismatched']} mismatched · "
        f"❌ {t['missing']} missing from app · "
        f"➕ {t['extraInApp']} extra in app."
    )
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        api_key = os.environ.get("EMERGENT_LLM_KEY")
        if not api_key:
            return baseline
        payload = {
            "farmName": farm_name,
            "totals": t,
            "sampleMismatches": [r.dict() for r in resp.mismatched[:8]],
            "sampleMissing":    [r.dict() for r in resp.missing[:8]],
            "sampleExtra":      [r.dict() for r in resp.extra[:8]],
        }
        prompt = (
            "You are Farm Buddy auditing a broiler farm's records against a "
            "processor 'Grower Pickup Report'. Write ONE short paragraph "
            "(max 90 words) telling the grower plainly what the diff shows, "
            "which sheds need attention, and one concrete next step. "
            "No JSON, no bullet points — just plain honest text. "
            f"Data: {json.dumps(payload)}"
        )
        chat = LlmChat(api_key=api_key, session_id=f"reconcile-{uuid.uuid4().hex[:8]}",
                       system_message="You are Farm Buddy, a friendly poultry-farm assistant. Reply with plain english only.").with_model("gemini", "gemini-2.5-flash")
        reply = await chat.send_message(UserMessage(text=prompt))
        text = (getattr(reply, "text", None) or str(reply)).strip()
        return text or baseline
    except Exception as e:
        log.warning("Reconcile narrative LLM failed (%s) — using deterministic summary", e)
        return baseline


def init_farm_buddy_reconcile(app: FastAPI, db) -> None:
    # SEC-003: Independent rate limiter for the reconcile endpoints — same
    # sliding-window pattern as the LLM recommender, per-IP.
    _rr_state: dict[str, list[float]] = {}
    _RR_WINDOW_S = 60.0
    _RR_LIMIT = 20  # reconcile is heavier than /recommend — tighter cap
    _MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # SEC-003: 10 MB hard cap on .docx / text uploads

    def _rr_rate_check(request: Request) -> None:
        ip = (request.client.host if request.client else "?") or "?"
        now = datetime.now(timezone.utc).timestamp()
        bucket = [t for t in _rr_state.get(ip, []) if now - t < _RR_WINDOW_S]
        if len(bucket) >= _RR_LIMIT:
            raise HTTPException(429, "Too many reconcile requests — slow down for a minute.")
        bucket.append(now)
        _rr_state[ip] = bucket

    @app.post("/api/farm-buddy/reconcile-pickup-report", response_model=ReconcileResponse)
    async def reconcile_pickup_report(req: ReconcileRequest, request: Request):
        _rr_rate_check(request)
        resp = _reconcile(req)
        if req.generateNarrative:
            resp.narrative = await _reconcile_narrative(resp, req.farmName)
        try:
            await db["farm_buddy_reconcile_log"].insert_one({
                "farmSlug":   req.farmSlug,
                "farmName":   req.farmName,
                "totals":     resp.totals,
                "createdAt":  datetime.now(timezone.utc),
            })
        except Exception:
            pass
        return resp

    @app.post("/api/farm-buddy/reconcile-pickup-report-upload", response_model=ReconcileResponse)
    async def reconcile_pickup_report_upload(
        request: Request,
        file: UploadFile = File(...),
        appCatches: str = Form(...),
        farmName: Optional[str] = Form(None),
        farmSlug: Optional[str] = Form(None),
        generateNarrative: bool = Form(True),
    ):
        """File-upload variant so growers can drop the Baiada / mTech
        Grower Pickup Report .docx (or a plain-text export) straight in
        without pasting. Accepts .docx (parsed via python-docx tables) and
        falls back to plain text for .txt / .csv / unknown formats.

        SEC-003 hardening: per-IP rate limit + 10 MB upload cap +
        `.docx`/`.txt`/`.csv` extension allow-list so bots can't ship
        arbitrary payloads."""
        _rr_rate_check(request)

        fname = (file.filename or "").lower()
        allowed = (".docx", ".txt", ".csv")
        if not any(fname.endswith(ext) for ext in allowed):
            raise HTTPException(415, "Only .docx, .txt, or .csv files are accepted.")

        # Stream-read with a byte cap so a 5 GB "docx" doesn't OOM the pod.
        raw_chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise HTTPException(413, "File too large — max 10 MB.")
            raw_chunks.append(chunk)
        raw = b"".join(raw_chunks)

        rows: list[PickupReportRow] = []
        derived_farm: Optional[str] = None
        if fname.endswith(".docx"):
            rows, derived_farm = _parse_pickup_docx(raw)
        else:
            # Plain text / mTech CSV export → decode + regex parser
            try:
                text = raw.decode("utf-8", errors="ignore")
            except Exception:
                text = raw.decode("latin-1", errors="ignore")
            rows = _parse_pickup_text(text)

        if not rows:
            raise HTTPException(400, "Could not read any pickup rows from that file. Try a .docx or paste the table text.")

        try:
            app_catches = [AppCatchRow.parse_obj(x) for x in json.loads(appCatches)]
        except Exception as e:
            raise HTTPException(400, f"appCatches must be valid JSON array of AppCatchRow objects: {e}")

        req = ReconcileRequest(
            pickupRows=rows,
            appCatches=app_catches,
            farmName=farmName or derived_farm,
            farmSlug=farmSlug,
            generateNarrative=generateNarrative,
        )
        resp = _reconcile(req)
        if req.generateNarrative:
            resp.narrative = await _reconcile_narrative(resp, req.farmName)
        try:
            await db["farm_buddy_reconcile_log"].insert_one({
                "farmSlug":   req.farmSlug,
                "farmName":   req.farmName,
                "sourceFile": file.filename,
                "totals":     resp.totals,
                "createdAt":  datetime.now(timezone.utc),
            })
        except Exception:
            pass
        return resp

