"""
Tests for medical/ecg_analysis.py, the NeuroKit2-based QRS/T-wave
measurement layer for the Polar H10's ECG waveform.

Two kinds of tests here, deliberately separated:

1. Tests against REAL neurokit2.ecg_simulate() output - these are what
   justify the module's central, non-obvious design choice (documented
   in its docstring): QRS duration is measured as Q-peak-to-S-peak, not
   NeuroKit2's own ECG_R_Onsets/ECG_R_Offsets markers, because empirically
   the latter measure 130-190ms even on physiologically normal synthetic
   beats - implausibly wide - while Q-to-S reproducibly lands in the
   normal 60-110ms range. These tests pin that finding as a regression
   check: if a future NeuroKit2 version changes this behavior, the tests
   should catch it rather than silently start producing wrong QRS
   durations again.

2. Tests of this module's own aggregation/flagging logic in isolation,
   using crafted stand-ins for neurokit2.ecg_peaks/ecg_delineate's return
   shapes (confirmed against real neurokit2 0.2.13 output via inspection,
   the same discipline used for bleakheart) - these test SPECTER's code,
   not NeuroKit2's signal processing, which is a maintained external
   library this module trusts rather than re-verifies every call.
"""
import numpy as np
import neurokit2 as nk
import pytest

import medical.ecg_analysis as ecg_analysis
from medical.ecg_analysis import (
    QRS_WIDENED_THRESHOLD_MS,
    T_R_RATIO_ADVISORY_THRESHOLD,
    analyze_ecg_waveform,
)


def simulate_uv(duration=15, sampling_rate=130, heart_rate=75, seed=1, noise=0.01):
    """A synthetic ECG in a microvolt-like magnitude, matching what
    bleakheart delivers from a real Polar H10."""
    ecg_mv = nk.ecg_simulate(
        duration=duration, sampling_rate=sampling_rate, heart_rate=heart_rate,
        noise=noise, random_state=seed,
    )
    return [int(round(x * 1000)) for x in ecg_mv]


class TestRealSyntheticEcgQrsMeasurement:
    """Pins the Q-to-S vs R_Onset/R_Offset finding across a parameter
    sweep - see module docstring point 1."""

    @pytest.mark.parametrize("sampling_rate", [130, 250, 500])
    @pytest.mark.parametrize("heart_rate", [60, 75, 100])
    def test_qrs_duration_is_physiologically_plausible(self, sampling_rate, heart_rate):
        samples = simulate_uv(sampling_rate=sampling_rate, heart_rate=heart_rate, seed=7)
        result = analyze_ecg_waveform(samples, sampling_rate=sampling_rate)
        assert "error" not in result
        assert "qrs_duration_ms" in result
        # Normal QRS is textbook 60-100ms; allow generous margin either
        # side for a synthetic waveform rather than a real patient, but
        # this must stay well clear of the 120ms widened threshold -
        # that's the whole point of using Q-to-S instead of R_On/R_Off.
        assert 40.0 <= result["qrs_duration_ms"] <= 110.0
        assert result["qrs_duration_ms"] < QRS_WIDENED_THRESHOLD_MS
        assert result["flags"] == []

    def test_r_onset_offset_would_have_falsely_flagged_normal_beats(self):
        """Documents WHY Q-to-S was chosen: if this module used
        ECG_R_Onsets/ECG_R_Offsets directly, it would flag ordinary
        synthetic beats as widened. Exercises the real neurokit2 API
        directly (not this module) to keep that claim honest."""
        sr = 130
        ecg_mv = nk.ecg_simulate(duration=15, sampling_rate=sr, heart_rate=75, noise=0.01, random_state=7)
        cleaned = nk.ecg_clean(ecg_mv, sampling_rate=sr)
        _, info = nk.ecg_peaks(cleaned, sampling_rate=sr, correct_artifacts=True)
        _, waves = nk.ecg_delineate(cleaned, rpeaks=info, sampling_rate=sr, method="dwt")
        r_on = np.array(waves["ECG_R_Onsets"], dtype=float)
        r_off = np.array(waves["ECG_R_Offsets"], dtype=float)
        naive_qrs_ms = np.nanmedian((r_off - r_on) / sr * 1000)
        assert naive_qrs_ms > QRS_WIDENED_THRESHOLD_MS, (
            "If this assertion ever fails, NeuroKit2's R_Onset/R_Offset "
            "semantics may have changed - re-evaluate whether Q-to-S is "
            "still the right measurement in ecg_analysis.py."
        )


class TestRealSyntheticEcgOverall:
    def test_normal_synthetic_beat_produces_no_flags(self):
        samples = simulate_uv(seed=3)
        result = analyze_ecg_waveform(samples, sampling_rate=130)
        assert result["flags"] == []
        assert "error" not in result

    def test_heart_rate_is_close_to_simulated_target(self):
        samples = simulate_uv(heart_rate=80, seed=4)
        result = analyze_ecg_waveform(samples, sampling_rate=130)
        assert 70 <= result["heart_rate_bpm"] <= 90

    def test_t_r_ratio_is_below_advisory_threshold_for_normal_beat(self):
        samples = simulate_uv(seed=5)
        result = analyze_ecg_waveform(samples, sampling_rate=130)
        assert result["t_r_ratio"] < T_R_RATIO_ADVISORY_THRESHOLD

    def test_disclaimer_always_present_on_success(self):
        samples = simulate_uv(seed=6)
        result = analyze_ecg_waveform(samples, sampling_rate=130)
        assert "not a diagnosis" in result["disclaimer"]

    def test_beats_analyzed_is_positive_and_plausible(self):
        samples = simulate_uv(duration=15, heart_rate=75, seed=8)
        result = analyze_ecg_waveform(samples, sampling_rate=130)
        # ~15s at 75bpm is roughly 18 beats; delineation may drop a few.
        assert 5 <= result["beats_analyzed"] <= 20


class TestInsufficientData:
    def test_too_few_samples_returns_error_without_crashing(self):
        result = analyze_ecg_waveform([1, 2, 3], sampling_rate=130)
        assert "error" in result
        assert "disclaimer" in result
        assert "qrs_duration_ms" not in result

    def test_empty_samples_returns_error(self):
        result = analyze_ecg_waveform([], sampling_rate=130)
        assert "error" in result

    def test_flat_signal_returns_error_not_exception(self):
        # No R-peaks detected at all - must degrade gracefully.
        flat = [0] * 1300
        result = analyze_ecg_waveform(flat, sampling_rate=130)
        assert "error" in result

    def test_too_few_beats_for_delineation_returns_error(self):
        # A couple of seconds is enough for ecg_peaks to find 1-2 beats
        # but neurokit2.ecg_delineate raises ValueError on windows this
        # short - confirms the MIN_BEATS_FOR_DELINEATION guard actually
        # prevents that exception from reaching the caller.
        samples = simulate_uv(duration=2, heart_rate=75, sampling_rate=130, seed=1)
        result = analyze_ecg_waveform(samples, sampling_rate=130)
        assert "error" in result
        assert "beat" in result["error"].lower()


# ---------------------------------------------------------------------------
# Aggregation/flagging logic, isolated from NeuroKit2's own signal
# processing via crafted (but shape-verified) stand-ins.
# ---------------------------------------------------------------------------

def _patch_neurokit(monkeypatch, rpeaks, q_peaks, s_peaks, t_peaks, signal_len=1000,
                     r_amplitude=1000.0, t_amplitude=200.0):
    """Craft a signal and NeuroKit2 return values with known peak
    amplitudes/positions, so this module's own aggregation and threshold
    logic can be tested deterministically."""
    signal = np.zeros(signal_len)
    for idx in rpeaks:
        signal[idx] = r_amplitude
    for idx in t_peaks:
        signal[idx] = t_amplitude

    monkeypatch.setattr(ecg_analysis.nk, "ecg_clean", lambda sig, sampling_rate: signal)
    monkeypatch.setattr(
        ecg_analysis.nk, "ecg_peaks",
        lambda cleaned, sampling_rate, correct_artifacts=False: (
            None, {"ECG_R_Peaks": np.array(rpeaks, dtype=float)}
        ),
    )
    monkeypatch.setattr(
        ecg_analysis.nk, "ecg_delineate",
        lambda cleaned, rpeaks, sampling_rate, method="dwt": (
            None,
            {
                "ECG_Q_Peaks": np.array(q_peaks, dtype=float),
                "ECG_S_Peaks": np.array(s_peaks, dtype=float),
                "ECG_T_Peaks": np.array(t_peaks, dtype=float),
            },
        ),
    )


class TestQrsFlagThreshold:
    def test_wide_qrs_triggers_flag(self, monkeypatch):
        sr = 130
        # gap of 20 samples at 130Hz = 153.8ms, above the 120ms threshold
        rpeaks = [100, 230, 360, 490, 620]
        q_peaks = [95, 225, 355, 485, 615]
        s_peaks = [q + 20 for q in q_peaks]
        t_peaks = [140, 270, 400, 530, 660]
        _patch_neurokit(monkeypatch, rpeaks, q_peaks, s_peaks, t_peaks)

        result = analyze_ecg_waveform(list(range(1300)), sampling_rate=sr)

        assert result["qrs_duration_ms"] == pytest.approx(20 / sr * 1000, abs=0.1)
        assert result["qrs_duration_ms"] > QRS_WIDENED_THRESHOLD_MS
        assert any("QRS duration" in f for f in result["flags"])

    def test_normal_qrs_does_not_trigger_flag(self, monkeypatch):
        sr = 130
        # gap of 10 samples at 130Hz = 76.9ms, well under threshold
        rpeaks = [100, 230, 360, 490, 620]
        q_peaks = [95, 225, 355, 485, 615]
        s_peaks = [q + 10 for q in q_peaks]
        t_peaks = [140, 270, 400, 530, 660]
        _patch_neurokit(monkeypatch, rpeaks, q_peaks, s_peaks, t_peaks)

        result = analyze_ecg_waveform(list(range(1300)), sampling_rate=sr)

        assert result["qrs_duration_ms"] < QRS_WIDENED_THRESHOLD_MS
        assert not any("QRS duration" in f for f in result["flags"])


class TestTWaveRatioFlag:
    def test_tall_t_wave_triggers_advisory_flag(self, monkeypatch):
        rpeaks = [100, 230, 360, 490, 620]
        q_peaks = [95, 225, 355, 485, 615]
        s_peaks = [q + 10 for q in q_peaks]
        t_peaks = [140, 270, 400, 530, 660]
        _patch_neurokit(monkeypatch, rpeaks, q_peaks, s_peaks, t_peaks,
                         r_amplitude=1000.0, t_amplitude=800.0)  # ratio 0.8

        result = analyze_ecg_waveform(list(range(1300)), sampling_rate=130)

        assert result["t_r_ratio"] == pytest.approx(0.8)
        assert any("T-wave amplitude" in f for f in result["flags"])
        assert "considerably less reliable" in result["flags"][-1] or \
               any("considerably less reliable" in f for f in result["flags"])

    def test_normal_t_wave_does_not_trigger_flag(self, monkeypatch):
        rpeaks = [100, 230, 360, 490, 620]
        q_peaks = [95, 225, 355, 485, 615]
        s_peaks = [q + 10 for q in q_peaks]
        t_peaks = [140, 270, 400, 530, 660]
        _patch_neurokit(monkeypatch, rpeaks, q_peaks, s_peaks, t_peaks,
                         r_amplitude=1000.0, t_amplitude=300.0)  # ratio 0.3

        result = analyze_ecg_waveform(list(range(1300)), sampling_rate=130)

        assert result["t_r_ratio"] == pytest.approx(0.3)
        assert not any("T-wave amplitude" in f for f in result["flags"])


class TestBeatsAnalyzedCount:
    def test_counts_beats_with_both_q_and_s_detected(self, monkeypatch):
        rpeaks = [100, 230, 360, 490, 620]
        q_peaks = [95, 225, 355, float("nan"), 615]
        s_peaks = [105, 235, 365, 495, float("nan")]
        t_peaks = [140, 270, 400, 530, 660]
        _patch_neurokit(monkeypatch, rpeaks, q_peaks, s_peaks, t_peaks)

        result = analyze_ecg_waveform(list(range(1300)), sampling_rate=130)

        # Beats 3 and 4 (0-indexed) are missing Q or S respectively.
        assert result["beats_analyzed"] == 3
