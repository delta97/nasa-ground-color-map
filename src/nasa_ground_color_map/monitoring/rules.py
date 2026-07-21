"""Pure monitor rule transition semantics."""

from __future__ import annotations

QUALITY_RANK = {"unusable": 0, "suspect": 1, "usable": 2}
RULES = {"above", "below", "absolute_change", "percentage_change", "quality_transition"}


def validate_rule(rule_type: str, threshold):
    if rule_type not in RULES: raise ValueError(f"rule_type must be one of {sorted(RULES)}")
    if rule_type != "quality_transition" and threshold is None: raise ValueError("threshold is required for this rule")
    if rule_type == "percentage_change" and float(threshold) < 0: raise ValueError("percentage-change threshold must be non-negative")


def evaluate(*, rule_type: str, threshold: float | None, value: float | None,
             previous_value: float | None, quality: str, previous_quality: str | None,
             minimum_quality: str = "usable", was_active: bool = False) -> dict:
    validate_rule(rule_type, threshold)
    accepted = QUALITY_RANK.get(quality, -1) >= QUALITY_RANK.get(minimum_quality, 2)
    if rule_type == "quality_transition":
        matches = previous_quality is not None and quality != previous_quality
        accepted = True
    elif not accepted or value is None:
        return {"accepted": False, "matches": False, "event": None, "active": was_active}
    elif rule_type == "above": matches = value > threshold
    elif rule_type == "below": matches = value < threshold
    elif previous_value is None: matches = False
    elif rule_type == "absolute_change": matches = abs(value - previous_value) >= threshold
    else:
        matches = previous_value != 0 and abs((value - previous_value) / previous_value * 100) >= threshold
    event = "triggered" if matches and not was_active else ("recovered" if not matches and was_active else None)
    return {"accepted": accepted, "matches": matches, "event": event, "active": matches}
