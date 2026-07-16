"""Mother Hen: a rules-based anomaly detector + LLM reasoner.

The rules engine works entirely offline and produces immediate decisions.
The LLM (Claude Sonnet 4.5) is used on demand for deep reasoning summaries
and open-ended explanations. Rules always run first so the system keeps
working even if the LLM is unreachable.
"""
from __future__ import annotations

import os
import json
from typing import List, Optional

from breed_profiles import get_standard, BREED_STANDARDS
from models import Reading, Decision, new_id, now_iso


# ---------- Rules engine ----------
def analyze_reading(reading: Reading, batch_breed: str, grow_day: int, bird_count: int, capacity: int) -> List[dict]:
    """Return a list of raw findings. Each finding becomes a Decision candidate."""
    findings = []

    # Temperature drift
    if reading.temp_c is not None:
        target = get_standard(batch_breed, "temp_c", grow_day)
        drift = reading.temp_c - target
        if abs(drift) >= 2.0:
            findings.append({
                "action_type": "temp_setpoint",
                "urgency": "high" if abs(drift) >= 3.0 else "medium",
                "title": f"Temperature {'above' if drift > 0 else 'below'} target by {abs(drift):.1f}°C",
                "delta": round(drift, 2),
                "current": reading.temp_c,
                "target": round(target, 1),
                "recommendation": (
                    f"Reduce setpoint by {min(abs(drift)/2, 1.5):.1f}°C and open damper +5%"
                    if drift > 0 else
                    f"Raise setpoint by {min(abs(drift)/2, 1.5):.1f}°C, engage heater group"
                ),
                "confidence": 0.85,
            })
        elif abs(drift) >= 1.0:
            findings.append({
                "action_type": "temp_setpoint",
                "urgency": "low",
                "title": f"Temperature drifting {'high' if drift > 0 else 'low'} ({drift:+.1f}°C)",
                "delta": round(drift, 2),
                "current": reading.temp_c,
                "target": round(target, 1),
                "recommendation": "Monitor next 30 min; small nudge recommended.",
                "confidence": 0.65,
            })

    # Humidity
    if reading.humidity_pct is not None:
        if reading.humidity_pct > 75:
            findings.append({
                "action_type": "ventilation",
                "urgency": "medium",
                "title": f"Humidity high ({reading.humidity_pct:.0f}%)",
                "current": reading.humidity_pct,
                "target": 65,
                "recommendation": "Increase minimum ventilation +10% until <70%.",
                "confidence": 0.8,
            })
        elif reading.humidity_pct < 40 and grow_day > 7:
            findings.append({
                "action_type": "ventilation",
                "urgency": "low",
                "title": f"Humidity low ({reading.humidity_pct:.0f}%)",
                "current": reading.humidity_pct,
                "target": 55,
                "recommendation": "Reduce ventilation slightly; check evaporative cooling.",
                "confidence": 0.7,
            })

    # Ammonia
    if reading.ammonia_ppm is not None and reading.ammonia_ppm > 25:
        findings.append({
            "action_type": "ventilation",
            "urgency": "critical" if reading.ammonia_ppm > 40 else "high",
            "title": f"Ammonia elevated ({reading.ammonia_ppm:.0f} ppm)",
            "current": reading.ammonia_ppm,
            "target": 20,
            "recommendation": "Boost ventilation +20% immediately; inspect litter.",
            "confidence": 0.9,
        })

    # Water consumption (if given daily total)
    if reading.water_liters is not None and bird_count > 0:
        per_bird_ml = (reading.water_liters * 1000) / max(bird_count, 1)
        target_ml = get_standard(batch_breed, "water_ml_per_bird", grow_day)
        if target_ml > 0:
            ratio = per_bird_ml / target_ml
            if ratio < 0.7:
                findings.append({
                    "action_type": "alert",
                    "urgency": "high",
                    "title": f"Water intake low ({per_bird_ml:.0f} ml/bird vs {target_ml:.0f})",
                    "current": round(per_bird_ml, 1),
                    "target": round(target_ml, 1),
                    "recommendation": "Inspect drinker lines for blockage or pressure drop.",
                    "confidence": 0.88,
                })
            elif ratio > 1.4:
                findings.append({
                    "action_type": "alert",
                    "urgency": "medium",
                    "title": f"Water intake unusually high ({per_bird_ml:.0f} ml/bird)",
                    "current": round(per_bird_ml, 1),
                    "target": round(target_ml, 1),
                    "recommendation": "Check for leak or heat-stress driven consumption.",
                    "confidence": 0.72,
                })

    # Mortality spike
    if reading.mortality_today is not None and bird_count > 0:
        pct = (reading.mortality_today / max(bird_count, 1)) * 100
        if pct > 0.5:
            findings.append({
                "action_type": "alert",
                "urgency": "critical" if pct > 1.0 else "high",
                "title": f"Mortality spike ({reading.mortality_today} birds, {pct:.2f}%)",
                "current": reading.mortality_today,
                "target": 0,
                "recommendation": "Physical inspection required; check temp, water, disease signs.",
                "confidence": 0.95,
            })

    # Fan / damper sanity
    if reading.fans_on is not None and reading.temp_c is not None:
        target = get_standard(batch_breed, "temp_c", grow_day)
        if reading.temp_c > target + 2 and reading.fans_on == 0:
            findings.append({
                "action_type": "fan_speed",
                "urgency": "high",
                "title": "Fans idle while temperature above target",
                "current": 0,
                "target": 2,
                "recommendation": "Engage tunnel fan group 1 immediately.",
                "confidence": 0.9,
            })

    return findings


def compute_shed_status(findings: List[dict]) -> str:
    if any(f["urgency"] == "critical" for f in findings):
        return "critical"
    if any(f["urgency"] == "high" for f in findings):
        return "critical"
    if any(f["urgency"] == "medium" for f in findings):
        return "warning"
    if findings:
        return "warning"
    return "healthy"


def finding_to_decision(finding: dict, shed_id: str, batch_id: Optional[str], policy_mode: str) -> Decision:
    """Wrap a rules finding into a persistable Decision."""
    if policy_mode == "auto" and finding["urgency"] in ("low", "medium"):
        status = "auto_applied"
    elif policy_mode == "auto":
        status = "pending_approval"  # high/critical still asks even in auto
    elif policy_mode == "recommend":
        status = "pending_approval" if finding["urgency"] in ("high", "critical") else "recommended"
    else:
        status = "recommended"

    return Decision(
        shed_id=shed_id,
        batch_id=batch_id,
        action_type=finding["action_type"],
        recommendation=finding["recommendation"],
        reasoning=finding["title"],
        urgency=finding["urgency"] if finding["urgency"] != "critical" else "high",
        confidence=finding.get("confidence", 0.7),
        status=status,
        data_snapshot={k: v for k, v in finding.items() if k in ("current", "target", "delta")},
        ai_source="rules",
    )


# ---------- LLM deep reasoning ----------
async def llm_deep_analysis(context: dict) -> Optional[str]:
    """Call Claude Sonnet 4.5 for a plain-language shed summary.

    context is a dict with shed/batch/reading/findings/breed_target info.
    Returns markdown text or None if LLM unavailable.
    """
    api_key = os.environ.get("EMERGENT_LLM_KEY")
    if not api_key:
        return None
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except Exception:
        return None

    system_prompt = (
        "You are Mother Hen, a calm, confident, experienced poultry-farm operator AI. "
        "You watch broiler sheds 24/7. Given a JSON snapshot of a shed, its current batch, "
        "the latest sensor reading, and rule-based findings, produce a SHORT briefing "
        "(3-6 sentences) for the human farm owner. Be direct, practical, prioritise the "
        "single most important thing to do next. Reference breed standards when relevant. "
        "Never invent numbers not in the data. Do not use emoji."
    )

    chat = (
        LlmChat(
            api_key=api_key,
            session_id=f"mother-hen-{context.get('shed_id','x')}",
            system_message=system_prompt,
        )
        .with_model("anthropic", "claude-sonnet-4-5-20250929")
    )
    msg = UserMessage(text="Snapshot:\n" + json.dumps(context, default=str, indent=2))
    try:
        response = await chat.send_message(msg)
        # emergentintegrations returns the text content directly for send_message
        if isinstance(response, str):
            return response
        # Fallback: try attribute
        return getattr(response, "content", None) or str(response)
    except Exception as e:
        return f"(LLM unavailable: {type(e).__name__})"
