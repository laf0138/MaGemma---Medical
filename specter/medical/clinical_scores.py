#!/usr/bin/env python3
"""
SPECTER shared clinical scoring — pure functions, no I/O.

These are the derived values referenced in docs/SPECTER_MEDICAL_UI_BRIEF.md
Part 1.3 ("Derived — computed, never entered"). They are used from two very
different data sources:

  - specter/ward/specter_ward.py: manual bedside charting, where an
    operator enters every NEWS2 parameter (including respiration rate and
    consciousness) by hand. NEWS2 there is usually complete.

  - specter/medical/specter_medical_ai.py: automated BLE device readings
    (BP cuff, pulse oximeter, thermometer). No connected device measures
    respiration rate or level of consciousness, so NEWS2/qSOFA computed
    from that path are ALWAYS partial - see the per-function docstrings.

Shared here so both call sites score identically and a fix to one doesn't
drift from the other.

A parameter that is missing from the input is NEVER scored as 0 (normal) -
that would silently manufacture a "this looks fine" signal out of no data
at all, exactly the kind of fabricated-but-plausible number this whole
project exists to avoid. Missing parameters are excluded from the total and
named explicitly so a caller can render "NEWS2 3 (partial - no
respiration rate)" rather than a bare, falsely-complete "NEWS2 3".
"""

from typing import Any, Dict, Optional


# ===========================================================================
# MAP / pulse pressure / shock index
# ===========================================================================

def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def calculate_map(sbp: Any, dbp: Any) -> Optional[float]:
    """Mean arterial pressure: DBP + (SBP - DBP) / 3.

    < 65 is graft-perfusion-threshold territory for a transplant recipient
    (docs/SPECTER_MEDICAL_UI_BRIEF.md 1.3). Returns None if either input is
    missing or non-numeric - never guesses a value.
    """
    s, d = _as_float(sbp), _as_float(dbp)
    if s is None or d is None:
        return None
    return round(d + (s - d) / 3, 1)


def calculate_pulse_pressure(sbp: Any, dbp: Any) -> Optional[float]:
    """SBP - DBP. Narrow (< 25) suggests falling stroke volume before SBP drops."""
    s, d = _as_float(sbp), _as_float(dbp)
    if s is None or d is None:
        return None
    return round(s - d, 1)


def calculate_shock_index(hr: Any, sbp: Any) -> Optional[float]:
    """HR / SBP. > 0.9 concerning, > 1.0 suggests shock; rises before BP falls."""
    h, s = _as_float(hr), _as_float(sbp)
    if h is None or s is None or s == 0:
        return None
    return round(h / s, 2)


# ===========================================================================
# NEWS2 (National Early Warning Score 2, Royal College of Physicians UK,
# 2017) - a standard, publicly documented hospital early-warning score.
# ===========================================================================
#
# This implements Scale 1 only (the scale used for the general population).
# NEWS2 also defines Scale 2 for patients with known chronic hypercapnic
# respiratory failure (e.g. severe COPD), which uses different SpO2
# thresholds and requires knowing the patient's individually prescribed
# target saturation range - that per-patient clinical judgment call isn't
# something this module can safely default, so Scale 2 is not implemented.
# Applying Scale 1 to a chronic hypercapnic patient will under-score their
# true risk; this is a real, documented limitation, not an oversight.

NEWS2_RESPIRATORY_FAILURE_SCALE_NOTE = (
    "Scale 1 only (general population) - not valid for patients with known "
    "chronic hypercapnic respiratory failure (Scale 2), which needs an "
    "individually prescribed target SpO2 range this module does not track."
)


def _news2_rr(rr: float) -> int:
    if rr <= 8:
        return 3
    if rr <= 11:
        return 1
    if rr <= 20:
        return 0
    if rr <= 24:
        return 2
    return 3


def _news2_spo2(spo2: float) -> int:
    if spo2 <= 91:
        return 3
    if spo2 <= 93:
        return 2
    if spo2 <= 95:
        return 1
    return 0


def _news2_supplemental_o2(on_o2: bool) -> int:
    return 2 if on_o2 else 0


def _news2_sbp(sbp: float) -> int:
    if sbp <= 90:
        return 3
    if sbp <= 100:
        return 2
    if sbp <= 110:
        return 1
    if sbp <= 219:
        return 0
    return 3


def _news2_pulse(hr: float) -> int:
    if hr <= 40:
        return 3
    if hr <= 50:
        return 1
    if hr <= 90:
        return 0
    if hr <= 110:
        return 1
    if hr <= 130:
        return 2
    return 3


def _news2_consciousness(avpu: str) -> int:
    return 0 if (avpu or "").strip().upper() == "A" else 3


def _news2_temp(temp_c: float) -> int:
    if temp_c <= 35.0:
        return 3
    if temp_c <= 36.0:
        return 1
    if temp_c <= 38.0:
        return 0
    if temp_c <= 39.0:
        return 1
    return 2


_NEWS2_PARAMS = {
    # vitals key -> (scorer, display label)
    "rr": (_news2_rr, "Respiration rate"),
    "spo2": (_news2_spo2, "SpO2"),
    "supplemental_o2": (_news2_supplemental_o2, "Supplemental O2"),
    "bp_systolic": (_news2_sbp, "Systolic BP"),
    "pulse": (_news2_pulse, "Pulse"),
    "avpu": (_news2_consciousness, "Consciousness (ACVPU)"),
    "temperature_c": (_news2_temp, "Temperature"),
}


def calculate_news2(vitals: Dict[str, Any]) -> Dict[str, Any]:
    """
    Score present parameters only - see the module-level note above on why
    a missing parameter is never treated as normal/0.

    Returns: total, risk (low/low-medium/medium/high/unknown), per_parameter
    dict, missing_parameters list, partial bool, scale_note.
    """
    per_parameter: Dict[str, int] = {}
    for key, (scorer, _label) in _NEWS2_PARAMS.items():
        if key not in vitals or vitals[key] is None:
            continue
        try:
            per_parameter[key] = scorer(vitals[key])
        except (TypeError, ValueError):
            continue

    missing = [k for k in _NEWS2_PARAMS if k not in per_parameter]
    total = sum(per_parameter.values())
    any_scored_3 = any(v == 3 for v in per_parameter.values())

    if not per_parameter:
        risk = "unknown"
    elif total >= 7:
        risk = "high"
    elif total >= 5 or any_scored_3:
        risk = "low-medium" if (any_scored_3 and total < 5) else "medium"
    else:
        risk = "low"

    return {
        "total": total,
        "risk": risk,
        "per_parameter": per_parameter,
        "missing_parameters": missing,
        "partial": bool(missing),
        "scale_note": NEWS2_RESPIRATORY_FAILURE_SCALE_NOTE,
    }


# ===========================================================================
# qSOFA (quick Sequential Organ Failure Assessment)
# ===========================================================================
#
# 1 point each: RR >= 22, SBP <= 100, altered mentation. >= 2 is a sepsis
# screen positive - an emergency in an immunosuppressed patient (docs/
# SPECTER_MEDICAL_UI_BRIEF.md 1.3). Same missing-parameter discipline as
# NEWS2 above: an input this function never received is never scored 0.

_QSOFA_LABELS = {
    "rr": "Respiration rate",
    "bp_systolic": "Systolic BP",
    "altered_mentation": "Altered mentation",
}


def calculate_qsofa(vitals: Dict[str, Any]) -> Dict[str, Any]:
    """
    vitals: {"rr": float, "bp_systolic": float, "altered_mentation": bool}
    Any key absent or None is excluded from the total and named in
    missing_parameters, not scored as "not altered" / "normal rate".

    Returns: total, positive (bool, total>=2 and not partial-critical),
    per_parameter dict, missing_parameters list, partial bool.
    """
    per_parameter: Dict[str, int] = {}

    rr = vitals.get("rr")
    if rr is not None:
        try:
            per_parameter["rr"] = 1 if float(rr) >= 22 else 0
        except (TypeError, ValueError):
            pass

    sbp = vitals.get("bp_systolic")
    if sbp is not None:
        try:
            per_parameter["bp_systolic"] = 1 if float(sbp) <= 100 else 0
        except (TypeError, ValueError):
            pass

    mentation = vitals.get("altered_mentation")
    if mentation is not None:
        per_parameter["altered_mentation"] = 1 if bool(mentation) else 0

    missing = [k for k in _QSOFA_LABELS if k not in per_parameter]
    total = sum(per_parameter.values())

    return {
        "total": total,
        # A score can only be asserted "positive" (the >=2 sepsis-screen
        # trigger) when it was computed from a complete set of inputs -
        # with parameters missing, an equal-or-higher true total is always
        # possible, so a partial score is never allowed to read as
        # reassuring ("not positive") either.
        "positive": (total >= 2) if not missing else None,
        "per_parameter": per_parameter,
        "missing_parameters": missing,
        "partial": bool(missing),
    }
