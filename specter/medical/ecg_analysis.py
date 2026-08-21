#!/usr/bin/env python3
"""
SPECTER ECG Waveform Analysis
Turns a raw single-lead ECG waveform (as streamed from the Polar H10 via
bleakheart - see specter_medical_hub.py) into measured QRS/T-wave metrics,
using NeuroKit2 for signal delineation.

TRAINING GATE / SCOPE
----------------------
This module reports MEASURED values and hedged advisory flags. It does
not diagnose hyperkalemia or anything else. A trained responder (or
MedGemma, prompted with appropriate uncertainty) interprets these numbers
in context - vitals, history, symptoms - the same way every other reading
in this system is decision support, not a finding.

Two things this module is explicitly NOT confident about, and says so in
its output:

1. QRS duration is measured as Q-peak-to-S-peak distance, NOT NeuroKit2's
   ECG_R_Onsets/ECG_R_Offsets delineation markers. Empirically (see the
   parameter sweep in tests/test_ecg_analysis.py), R_Onsets/R_Offsets on
   ecg_simulate()-generated waveforms measure 130-190ms even for
   physiologically normal synthetic beats - implausibly wide, and not
   consistent with the standard clinical QRS-duration definition. Q-peak-
   to-S-peak reproducibly lands in the 75-95ms range across heart rates,
   sample rates, and random seeds, which is textbook-normal. This appears
   to be a semantic mismatch in what NeuroKit2's 'dwt' delineation method
   calls "R onset/offset" versus the classical QRS complex boundary,
   rather than a bug in this module - but it has only been checked against
   NeuroKit2 0.2.13 and synthetic waveforms, never a real ECG with a
   clinically confirmed QRS duration. Re-verify if NeuroKit2 is upgraded.

2. The T-wave "peaked" flag is a T-wave/R-wave amplitude ratio threshold,
   which is a much weaker, less-standardized proxy for the published
   12-lead criteria (tall, narrow, symmetric T waves, often described in
   absolute mm on calibrated ECG paper) than QRS duration is. Treat it as
   considerably less trustworthy than the QRS flag until validated against
   real hyperkalemic and normal ECGs.

Neither flag has been checked against a single real hyperkalemic ECG.
Both are pending real-hardware verification exactly like the BLE device
parsers this module's caller is gated behind - see docs/MANUAL.md Part 7.4.
"""

import logging
from typing import Any, Dict, List, Sequence

import numpy as np
import neurokit2 as nk

logger = logging.getLogger("specter.ecg_analysis")

# Minimum detected heartbeats before attempting delineation. Below this,
# NeuroKit2's delineate() can raise ValueError("data length too small to
# be segmented") outright, and any per-beat median would be unreliable
# regardless.
MIN_BEATS_FOR_DELINEATION = 3

# Widened QRS: a well-established, widely-cited clinical cutoff (standard
# 12-lead ECG interpretation), not specific to this pipeline. See module
# docstring point 1 for what this module's measurement methodology can
# and can't promise about matching it.
QRS_WIDENED_THRESHOLD_MS = 120.0

# T-wave/R-wave amplitude ratio above which the T wave is flagged as
# advisory-tall. This is a heuristic proxy, not a sourced clinical
# threshold - see module docstring point 2. Chosen well above the ~0.34
# median T:R ratio measured on normal synthetic beats (see test suite)
# to bias toward under- rather than over-flagging until validated.
T_R_RATIO_ADVISORY_THRESHOLD = 0.75

DISCLAIMER = (
    "Measured values only, not a diagnosis. QRS duration uses a "
    "validated-on-synthetic-data methodology (see module docstring); the "
    "T-wave ratio is a weaker heuristic. Neither has been checked against "
    "a real ECG. Correlate with symptoms, other vitals, and labs; "
    "recommend physician contact whenever available."
)


def analyze_ecg_waveform(
    samples_uv: Sequence[int], sampling_rate: int
) -> Dict[str, Any]:
    """
    Analyze a raw single-lead ECG waveform.

    Args:
        samples_uv: raw ECG samples in microvolts (as delivered by
            bleakheart's PolarMeasurementData for the H10).
        sampling_rate: sampling rate in Hz (130 for the Polar H10).

    Returns a dict. On success it contains a subset of:
        heart_rate_bpm, qrs_duration_ms, r_wave_amplitude_uv,
        t_wave_amplitude_uv, t_r_ratio, beats_analyzed, flags (list[str]),
        disclaimer (str).
    A key is present only if it could be computed - a noisy or short
    window may yield a heart rate but no reliable QRS duration, for
    example. On failure (too little data, an exception in NeuroKit2)
    returns {"error": "...", "disclaimer": DISCLAIMER} - this is a
    normal, expected outcome for a short or poor-contact window, not
    something the caller needs to treat as exceptional.
    """
    if not samples_uv or len(samples_uv) < sampling_rate:
        return {"error": "Insufficient samples for analysis", "disclaimer": DISCLAIMER}

    try:
        signal = np.asarray(samples_uv, dtype=float)
        cleaned = nk.ecg_clean(signal, sampling_rate=sampling_rate)
        _, peak_info = nk.ecg_peaks(cleaned, sampling_rate=sampling_rate, correct_artifacts=True)
        rpeaks = np.asarray(peak_info["ECG_R_Peaks"], dtype=float)
    except Exception as e:
        logger.error(f"ECG peak detection failed: {e}")
        return {"error": f"Peak detection failed: {e}", "disclaimer": DISCLAIMER}

    result: Dict[str, Any] = {}
    flags: List[str] = []

    if len(rpeaks) >= 2:
        rr_intervals_s = np.diff(rpeaks) / sampling_rate
        mean_rr_s = np.nanmean(rr_intervals_s)
        if mean_rr_s > 0:
            result["heart_rate_bpm"] = round(60.0 / mean_rr_s, 1)

    if len(rpeaks) < MIN_BEATS_FOR_DELINEATION:
        result["error"] = (
            f"Only {len(rpeaks)} beat(s) detected - need at least "
            f"{MIN_BEATS_FOR_DELINEATION} for QRS/T-wave delineation"
        )
        result["disclaimer"] = DISCLAIMER
        return result

    try:
        _, waves = nk.ecg_delineate(cleaned, rpeaks=peak_info, sampling_rate=sampling_rate, method="dwt")
    except Exception as e:
        logger.error(f"ECG delineation failed: {e}")
        result["error"] = f"Delineation failed: {e}"
        result["disclaimer"] = DISCLAIMER
        return result

    q_peaks = np.asarray(waves.get("ECG_Q_Peaks", []), dtype=float)
    s_peaks = np.asarray(waves.get("ECG_S_Peaks", []), dtype=float)
    t_peaks = np.asarray(waves.get("ECG_T_Peaks", []), dtype=float)

    result["beats_analyzed"] = int(np.sum(~np.isnan(q_peaks) & ~np.isnan(s_peaks)))

    qrs_ms = _sample_diff_to_ms(q_peaks, s_peaks, sampling_rate)
    if qrs_ms is not None:
        result["qrs_duration_ms"] = qrs_ms
        if qrs_ms > QRS_WIDENED_THRESHOLD_MS:
            flags.append(
                f"QRS duration {qrs_ms:.0f}ms is above the {QRS_WIDENED_THRESHOLD_MS:.0f}ms "
                f"widened-QRS threshold - non-specific, seen in hyperkalemia among other "
                f"causes. Not a diagnosis."
            )

    r_amp = _median_amplitude(cleaned, rpeaks)
    t_amp = _median_amplitude(cleaned, t_peaks)
    if r_amp is not None:
        result["r_wave_amplitude_uv"] = r_amp
    if t_amp is not None:
        result["t_wave_amplitude_uv"] = t_amp
    if r_amp is not None and t_amp is not None and r_amp != 0:
        t_r_ratio = round(t_amp / r_amp, 3)
        result["t_r_ratio"] = t_r_ratio
        if t_r_ratio > T_R_RATIO_ADVISORY_THRESHOLD:
            flags.append(
                f"T-wave amplitude is {t_r_ratio:.2f}x the R-wave amplitude, above the "
                f"{T_R_RATIO_ADVISORY_THRESHOLD:.2f}x advisory threshold - tall/peaked T waves "
                f"are a weak, non-specific proxy for hyperkalemia on a single lead. "
                f"Treat this flag as considerably less reliable than the QRS one "
                f"(see module docstring)."
            )

    result["flags"] = flags
    result["disclaimer"] = DISCLAIMER
    return result


def _sample_diff_to_ms(start_indices: np.ndarray, end_indices: np.ndarray, sampling_rate: int):
    """Median duration in ms between paired sample indices, ignoring
    beats where either index is NaN (a missed per-beat detection)."""
    if len(start_indices) == 0 or len(end_indices) == 0:
        return None
    n = min(len(start_indices), len(end_indices))
    durations = (end_indices[:n] - start_indices[:n]) / sampling_rate * 1000.0
    durations = durations[~np.isnan(durations)]
    if len(durations) == 0:
        return None
    return round(float(np.median(durations)), 1)


def _median_amplitude(cleaned_signal: np.ndarray, peak_indices: np.ndarray):
    """Median signal amplitude at a set of (possibly NaN-containing) peak
    sample indices."""
    valid = peak_indices[~np.isnan(peak_indices)].astype(int)
    valid = valid[(valid >= 0) & (valid < len(cleaned_signal))]
    if len(valid) == 0:
        return None
    return round(float(np.median(cleaned_signal[valid])), 2)
