#!/usr/bin/env python3
"""Conservative single-lead ECG measurements for the Polar H10 stream.

This module deliberately does not diagnose an arrhythmia, hyperkalemia, or
any other condition. NeuroKit2's DWT delineator returns Q and S *peak*
locations separately from its R-onset/R-offset markers. The interval reported
here is therefore named ``q_s_peak_interval_ms``. It must not be presented as
clinical QRS duration or compared with clinical QRS-duration thresholds.

The amplitude values and T:R ratio are also experimental. They have been
exercised against synthetic ECGs, but not validated against a Polar H10 and a
simultaneously recorded, clinician-measured reference ECG. Until that
validation exists, the safest useful output is a clearly labelled measurement
plus completeness metadata -- not an automated clinical advisory.
"""

import logging
import math
from typing import Any, Dict, Sequence

import neurokit2 as nk
import numpy as np

logger = logging.getLogger("specter.ecg_analysis")

MIN_BEATS_FOR_DELINEATION = 3
MIN_VALID_MORPHOLOGY_BEATS = 3

ANALYSIS_STATUS = "experimental_not_clinically_validated"
DISCLAIMER = (
    "Experimental single-lead measurements only, not a diagnosis. The Q-to-S "
    "peak interval is not clinical QRS duration, and the absolute T:R amplitude "
    "ratio is an unvalidated heuristic. Do not apply diagnostic thresholds to "
    "either value. Confirm waveform findings with a diagnostic ECG and a "
    "qualified clinician whenever available."
)


def _failure(message: str) -> Dict[str, Any]:
    return {
        "error": message,
        "analysis_status": ANALYSIS_STATUS,
        "disclaimer": DISCLAIMER,
    }


def analyze_ecg_waveform(
    samples_uv: Sequence[int], sampling_rate: int
) -> Dict[str, Any]:
    """Measure a finite, one-dimensional ECG sample window.

    Successful results may contain ``heart_rate_bpm``,
    ``q_s_peak_interval_ms``, R/T absolute amplitudes, ``t_r_abs_ratio``, and
    beat-completeness counts. Morphology values are omitted unless at least
    three valid, correctly ordered beat tuples are available. Expected input
    and signal-processing failures are returned as an ``error`` field rather
    than raised into the collection loop.
    """
    if isinstance(sampling_rate, bool) or not isinstance(sampling_rate, (int, float)):
        return _failure("Sampling rate must be a positive finite number")
    if not math.isfinite(float(sampling_rate)) or sampling_rate <= 0:
        return _failure("Sampling rate must be a positive finite number")

    try:
        signal = np.asarray(samples_uv, dtype=float)
    except (TypeError, ValueError):
        return _failure("ECG samples must be a one-dimensional numeric sequence")

    if signal.ndim != 1:
        return _failure("ECG samples must be a one-dimensional numeric sequence")
    if signal.size < math.ceil(float(sampling_rate)):
        return _failure("Insufficient samples for analysis")
    if not np.all(np.isfinite(signal)):
        return _failure("ECG samples contain non-finite values")

    try:
        cleaned = np.asarray(
            nk.ecg_clean(signal, sampling_rate=sampling_rate), dtype=float
        )
        _, peak_info = nk.ecg_peaks(
            cleaned, sampling_rate=sampling_rate, correct_artifacts=True
        )
        rpeaks = np.asarray(peak_info["ECG_R_Peaks"], dtype=float)
    except Exception as exc:
        logger.warning("ECG peak detection failed: %s", exc)
        return _failure(f"Peak detection failed: {exc}")

    result: Dict[str, Any] = {
        "analysis_status": ANALYSIS_STATUS,
        "detected_beats": int(np.count_nonzero(np.isfinite(rpeaks))),
        "warnings": [],
    }

    valid_rpeaks = rpeaks[np.isfinite(rpeaks)]
    rr_intervals_s = np.diff(valid_rpeaks) / float(sampling_rate)
    rr_intervals_s = rr_intervals_s[np.isfinite(rr_intervals_s) & (rr_intervals_s > 0)]
    if rr_intervals_s.size:
        result["heart_rate_bpm"] = round(
            60.0 / float(np.median(rr_intervals_s)), 1
        )

    if valid_rpeaks.size < MIN_BEATS_FOR_DELINEATION:
        result["error"] = (
            f"Only {valid_rpeaks.size} beat(s) detected - need at least "
            f"{MIN_BEATS_FOR_DELINEATION} for morphology delineation"
        )
        result["disclaimer"] = DISCLAIMER
        return result

    try:
        _, waves = nk.ecg_delineate(
            cleaned,
            rpeaks=peak_info,
            sampling_rate=sampling_rate,
            method="dwt",
        )
    except Exception as exc:
        logger.warning("ECG delineation failed: %s", exc)
        result["error"] = f"Delineation failed: {exc}"
        result["disclaimer"] = DISCLAIMER
        return result

    q_peaks = np.asarray(waves.get("ECG_Q_Peaks", []), dtype=float)
    s_peaks = np.asarray(waves.get("ECG_S_Peaks", []), dtype=float)
    t_peaks = np.asarray(waves.get("ECG_T_Peaks", []), dtype=float)

    q_s_intervals = _valid_qrs_peak_intervals(
        q_peaks, rpeaks, s_peaks, len(cleaned), float(sampling_rate)
    )
    result["morphology_beats_analyzed"] = int(q_s_intervals.size)
    if q_s_intervals.size >= MIN_VALID_MORPHOLOGY_BEATS:
        result["q_s_peak_interval_ms"] = round(
            float(np.median(q_s_intervals)), 1
        )
    else:
        result["warnings"].append(
            "Q-to-S peak interval withheld: fewer than 3 valid Q-R-S beat tuples"
        )

    amplitude_rows = _valid_rt_amplitudes(cleaned, rpeaks, t_peaks)
    result["amplitude_beats_analyzed"] = len(amplitude_rows)
    if len(amplitude_rows) >= MIN_VALID_MORPHOLOGY_BEATS:
        r_amplitudes = np.asarray([row[0] for row in amplitude_rows])
        t_amplitudes = np.asarray([row[1] for row in amplitude_rows])
        nonzero_r = r_amplitudes > 0
        ratios = t_amplitudes[nonzero_r] / r_amplitudes[nonzero_r]

        result["r_wave_abs_amplitude_uv"] = round(
            float(np.median(r_amplitudes)), 2
        )
        result["t_wave_abs_amplitude_uv"] = round(
            float(np.median(t_amplitudes)), 2
        )
        if ratios.size >= MIN_VALID_MORPHOLOGY_BEATS:
            result["t_r_abs_ratio"] = round(float(np.median(ratios)), 3)
        else:
            result["warnings"].append(
                "Absolute T:R ratio withheld: fewer than 3 beats had non-zero R amplitude"
            )
    else:
        result["warnings"].append(
            "Wave amplitudes withheld: fewer than 3 valid R-T beat pairs"
        )

    result["disclaimer"] = DISCLAIMER
    return result


def _valid_qrs_peak_intervals(
    q_peaks: np.ndarray,
    r_peaks: np.ndarray,
    s_peaks: np.ndarray,
    signal_length: int,
    sampling_rate: float,
) -> np.ndarray:
    """Return Q-to-S peak intervals only for finite, in-range Q<R<S tuples."""
    n = min(len(q_peaks), len(r_peaks), len(s_peaks))
    if n == 0:
        return np.asarray([], dtype=float)
    q = q_peaks[:n]
    r = r_peaks[:n]
    s = s_peaks[:n]
    valid = (
        np.isfinite(q)
        & np.isfinite(r)
        & np.isfinite(s)
        & (q >= 0)
        & (q < r)
        & (r < s)
        & (s < signal_length)
    )
    return (s[valid] - q[valid]) / sampling_rate * 1000.0


def _valid_rt_amplitudes(
    cleaned_signal: np.ndarray,
    r_peaks: np.ndarray,
    t_peaks: np.ndarray,
) -> list[tuple[float, float]]:
    """Return absolute amplitudes for finite R<T pairs within each beat."""
    rows: list[tuple[float, float]] = []
    n = min(len(r_peaks), len(t_peaks))
    for index in range(n):
        r_peak = r_peaks[index]
        t_peak = t_peaks[index]
        if not (np.isfinite(r_peak) and np.isfinite(t_peak)):
            continue
        r_index = int(r_peak)
        t_index = int(t_peak)
        if not (0 <= r_index < t_index < len(cleaned_signal)):
            continue
        if index + 1 < len(r_peaks):
            next_r = r_peaks[index + 1]
            if np.isfinite(next_r) and t_peak >= next_r:
                continue
        r_amplitude = abs(float(cleaned_signal[r_index]))
        t_amplitude = abs(float(cleaned_signal[t_index]))
        if np.isfinite(r_amplitude) and np.isfinite(t_amplitude):
            rows.append((r_amplitude, t_amplitude))
    return rows
