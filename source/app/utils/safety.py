from __future__ import annotations

from dataclasses import dataclass
from typing import List

from app.core.campaigns import Campaign


MAX_GROUPS_HARD = 200
MAX_MSGS_PER_HOUR_HARD = 60
MIN_SEND_GAP_SEC_HARD = 30


@dataclass
class RiskReport:
    level: str
    score: int
    reasons: List[str]
    guardrails: List[str]


def _estimate_msgs_per_hour(c: Campaign) -> float:
    avg_gap = (c.send_gap_min_sec + c.send_gap_max_sec) / 2
    if avg_gap <= 0:
        return 9999.0
    return 3600.0 / avg_gap


def assess_campaign_risk(c: Campaign) -> RiskReport:
    reasons: List[str] = []
    guardrails: List[str] = []
    score = 0

    targets = len(c.target_refs or [])
    rate = _estimate_msgs_per_hour(c)

    if targets > 50:
        score += 1
        reasons.append(f"Large target count: {targets}")
    if targets > 150:
        score += 2
        reasons.append(f"Very large target count: {targets}")
    if rate > 30:
        score += 1
        reasons.append(f"High send rate ~{rate:.1f} msgs/hour")
    if rate > 60:
        score += 2
        reasons.append(f"Very high send rate ~{rate:.1f} msgs/hour")
    if c.send_gap_min_sec < 60:
        score += 1
        reasons.append(f"Low min gap: {c.send_gap_min_sec}s")
    if c.batch_gap_min_sec < 900:
        score += 1
        reasons.append(f"Low batch gap: {c.batch_gap_min_sec}s")

    if targets > MAX_GROUPS_HARD:
        guardrails.append(f"Targets exceed hard limit ({targets} > {MAX_GROUPS_HARD})")
    if rate > MAX_MSGS_PER_HOUR_HARD:
        guardrails.append(f"Estimated rate exceeds hard limit ({rate:.1f} > {MAX_MSGS_PER_HOUR_HARD}/hour)")
    if c.send_gap_min_sec < MIN_SEND_GAP_SEC_HARD:
        guardrails.append(f"Min gap below hard limit ({c.send_gap_min_sec}s < {MIN_SEND_GAP_SEC_HARD}s)")

    if score >= 5:
        level = "high"
    elif score >= 3:
        level = "medium"
    else:
        level = "low"

    return RiskReport(level=level, score=score, reasons=reasons, guardrails=guardrails)
