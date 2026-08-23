"""Safety and regression tests for the experimental Polar H10 ECG analyzer."""

import numpy as np
import neurokit2 as nk
import pytest

import medical.ecg_analysis as ecg_analysis
from medical.ecg_analysis import ANALYSIS_STATUS, analyze_ecg_waveform


def simulate_uv(duration=15, sampling_rate=130, heart_rate=75, seed=1, noise=0.01):
    ecg_mv = nk.ecg_simulate(
        duration=duration,
        sampling_rate=sampling_rate,
        heart_rate=heart_rate,
        noise=noise,
        random_state=seed,
    )
    return [int(round(value * 1000)) for value in ecg_mv]


def _patch_neurokit(
    monkeypatch,
    rpeaks,
    q_peaks,
    s_peaks,
    t_peaks,
    *,
    signal_len=1300,
    r_amplitude=-1000.0,
    t_amplitude=200.0,
):
    """Install deterministic stand-ins matching NeuroKit2's return shapes."""
    signal = np.zeros(signal_len)
    for index in rpeaks:
        if np.isfinite(index) and 0 <= int(index) < signal_len:
            signal[int(index)] = r_amplitude
    for index in t_peaks:
        if np.isfinite(index) and 0 <= int(index) < signal_len:
            signal[int(index)] = t_amplitude

    monkeypatch.setattr(
        ecg_analysis.nk, "ecg_clean", lambda samples, sampling_rate: signal
    )
    monkeypatch.setattr(
        ecg_analysis.nk,
        "ecg_peaks",
        lambda cleaned, sampling_rate, correct_artifacts=False: (
            None,
            {"ECG_R_Peaks": np.asarray(rpeaks, dtype=float)},
        ),
    )
    monkeypatch.setattr(
        ecg_analysis.nk,
        "ecg_delineate",
        lambda cleaned, rpeaks, sampling_rate, method="dwt": (
            None,
            {
                "ECG_Q_Peaks": np.asarray(q_peaks, dtype=float),
                "ECG_S_Peaks": np.asarray(s_peaks, dtype=float),
                "ECG_T_Peaks": np.asarray(t_peaks, dtype=float),
            },
        ),
    )


class TestRealSyntheticEcg:
    @pytest.mark.parametrize("sampling_rate", [130, 250, 500])
    @pytest.mark.parametrize("heart_rate", [60, 75, 100])
    def test_q_s_peak_interval_is_plausible_and_precisely_named(
        self, sampling_rate, heart_rate
    ):
        result = analyze_ecg_waveform(
            simulate_uv(
                sampling_rate=sampling_rate,
                heart_rate=heart_rate,
                seed=7,
            ),
            sampling_rate=sampling_rate,
        )

        assert "error" not in result
        assert 40.0 <= result["q_s_peak_interval_ms"] <= 110.0
        assert "qrs_duration_ms" not in result
        assert "flags" not in result
        assert result["analysis_status"] == ANALYSIS_STATUS
        assert "not clinical QRS duration" in result["disclaimer"]

    def test_r_onset_offset_is_a_distinct_neurokit_output(self):
        """Pin the upstream semantic distinction that caused the old bug."""
        sampling_rate = 130
        signal = nk.ecg_simulate(
            duration=15,
            sampling_rate=sampling_rate,
            heart_rate=75,
            noise=0.01,
            random_state=7,
        )
        cleaned = nk.ecg_clean(signal, sampling_rate=sampling_rate)
        _, info = nk.ecg_peaks(
            cleaned, sampling_rate=sampling_rate, correct_artifacts=True
        )
        _, waves = nk.ecg_delineate(
            cleaned, rpeaks=info, sampling_rate=sampling_rate, method="dwt"
        )

        q_s_ms = np.nanmedian(
            (np.asarray(waves["ECG_S_Peaks"]) - np.asarray(waves["ECG_Q_Peaks"]))
            / sampling_rate
            * 1000
        )
        onset_offset_ms = np.nanmedian(
            (
                np.asarray(waves["ECG_R_Offsets"])
                - np.asarray(waves["ECG_R_Onsets"])
            )
            / sampling_rate
            * 1000
        )
        assert q_s_ms != pytest.approx(onset_offset_ms)

    def test_heart_rate_is_close_to_simulated_target(self):
        result = analyze_ecg_waveform(
            simulate_uv(heart_rate=80, seed=4), sampling_rate=130
        )
        assert 70 <= result["heart_rate_bpm"] <= 90


class TestInputAndFailureSafety:
    def test_numpy_array_input_is_supported(self):
        samples = np.asarray(simulate_uv(seed=6), dtype=float)
        result = analyze_ecg_waveform(samples, sampling_rate=130)
        assert "error" not in result

    @pytest.mark.parametrize("sampling_rate", [0, -1, float("nan"), True, "130"])
    def test_invalid_sampling_rate_is_rejected(self, sampling_rate):
        result = analyze_ecg_waveform([0] * 130, sampling_rate=sampling_rate)
        assert "positive finite" in result["error"]

    @pytest.mark.parametrize(
        "samples",
        [
            [0.0] * 129 + [float("nan")],
            [0.0] * 129 + [float("inf")],
            [[0.0] * 130],
            ["not-a-number"] * 130,
        ],
    )
    def test_invalid_sample_windows_are_rejected(self, samples):
        result = analyze_ecg_waveform(samples, sampling_rate=130)
        assert "error" in result
        assert result["analysis_status"] == ANALYSIS_STATUS

    def test_too_few_samples_returns_error(self):
        result = analyze_ecg_waveform([1, 2, 3], sampling_rate=130)
        assert result["error"] == "Insufficient samples for analysis"

    def test_flat_signal_returns_error_not_exception(self):
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert "error" in result

    @pytest.mark.parametrize("failure_stage", ["clean", "peaks"])
    def test_neurokit_peak_detection_failures_are_contained(
        self, monkeypatch, failure_stage
    ):
        if failure_stage == "clean":
            monkeypatch.setattr(
                ecg_analysis.nk,
                "ecg_clean",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    ValueError("cleaning failed")
                ),
            )
        else:
            monkeypatch.setattr(
                ecg_analysis.nk, "ecg_clean", lambda signal, sampling_rate: signal
            )
            monkeypatch.setattr(
                ecg_analysis.nk,
                "ecg_peaks",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("peak detector failed")
                ),
            )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["error"].startswith("Peak detection failed:")
        assert result["analysis_status"] == ANALYSIS_STATUS

    def test_delineation_failure_returns_partial_result(self, monkeypatch):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360],
            [95, 225, 355],
            [105, 235, 365],
            [140, 270, 400],
        )
        monkeypatch.setattr(
            ecg_analysis.nk,
            "ecg_delineate",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad signal")),
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["error"] == "Delineation failed: bad signal"
        assert result["heart_rate_bpm"] == pytest.approx(60.0)


class TestMorphologyCompleteness:
    def test_empty_delineation_arrays_withhold_all_morphology(self, monkeypatch):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360, 490],
            [],
            [],
            [],
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["morphology_beats_analyzed"] == 0
        assert result["amplitude_beats_analyzed"] == 0
        assert "q_s_peak_interval_ms" not in result
        assert "t_r_abs_ratio" not in result
        assert len(result["warnings"]) == 2

    def test_mismatched_arrays_do_not_crash_or_broadcast(self, monkeypatch):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360, 490, 620],
            [95, 225, 355, 485],
            [105, 235, 365],
            [140, 270, 400, 530, 660],
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["morphology_beats_analyzed"] == 3
        assert result["q_s_peak_interval_ms"] == pytest.approx(76.9, abs=0.1)

    def test_invalid_q_r_s_order_is_excluded_and_measurement_withheld(
        self, monkeypatch
    ):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360, 490],
            [95, 235, 355, 485],  # second Q occurs after R
            [105, 225, 350, 495],  # second/third S occur before R
            [140, 270, 400, 530],
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["morphology_beats_analyzed"] == 2
        assert "q_s_peak_interval_ms" not in result
        assert any("fewer than 3" in warning for warning in result["warnings"])

    def test_nan_pairs_are_excluded(self, monkeypatch):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360, 490, 620],
            [95, 225, float("nan"), 485, 615],
            [105, 235, 365, float("nan"), 625],
            [140, 270, 400, 530, 660],
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["morphology_beats_analyzed"] == 3

    def test_absolute_amplitude_ratio_is_polarity_independent(self, monkeypatch):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360, 490, 620],
            [95, 225, 355, 485, 615],
            [105, 235, 365, 495, 625],
            [140, 270, 400, 530, 660],
            r_amplitude=-1000.0,
            t_amplitude=800.0,
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["r_wave_abs_amplitude_uv"] == 1000.0
        assert result["t_wave_abs_amplitude_uv"] == 800.0
        assert result["t_r_abs_ratio"] == pytest.approx(0.8)
        assert "flags" not in result

    def test_zero_r_amplitudes_withhold_ratio(self, monkeypatch):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360, 490],
            [95, 225, 355, 485],
            [105, 235, 365, 495],
            [140, 270, 400, 530],
            r_amplitude=0.0,
            t_amplitude=200.0,
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["r_wave_abs_amplitude_uv"] == 0.0
        assert "t_r_abs_ratio" not in result
        assert any("non-zero R amplitude" in warning for warning in result["warnings"])

    def test_nan_and_out_of_range_peaks_are_excluded(self, monkeypatch):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360, 490, 620],
            [-1, 225, 355, float("nan"), 615],
            [105, 235, 2000, 495, 625],
            [float("nan"), -1, 400, 2000, 660],
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["morphology_beats_analyzed"] == 2
        assert "q_s_peak_interval_ms" not in result
        assert result["amplitude_beats_analyzed"] == 2
        assert "r_wave_abs_amplitude_uv" not in result
        assert any("fewer than 3 valid Q-R-S" in warning for warning in result["warnings"])
        assert any("fewer than 3 valid R-T" in warning for warning in result["warnings"])

    def test_nonfinite_cleaned_peak_amplitude_is_excluded(self, monkeypatch):
        rpeaks = [100, 230, 360, 490]
        tpeaks = [140, 270, 400, 530]
        _patch_neurokit(
            monkeypatch,
            rpeaks,
            [95, 225, 355, 485],
            [105, 235, 365, 495],
            tpeaks,
        )
        original_clean = ecg_analysis.nk.ecg_clean

        def nonfinite_clean(signal, sampling_rate):
            cleaned = original_clean(signal, sampling_rate)
            cleaned[rpeaks[0]] = np.nan
            return cleaned

        monkeypatch.setattr(ecg_analysis.nk, "ecg_clean", nonfinite_clean)
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["amplitude_beats_analyzed"] == 3

    def test_t_peak_outside_its_beat_is_excluded(self, monkeypatch):
        _patch_neurokit(
            monkeypatch,
            [100, 230, 360, 490],
            [95, 225, 355, 485],
            [105, 235, 365, 495],
            [235, 270, 400, 530],  # first T crosses the next R
        )
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert result["amplitude_beats_analyzed"] == 3

    def test_fewer_than_three_detected_beats_skips_delineation(self, monkeypatch):
        called = False

        def delineate(*args, **kwargs):
            nonlocal called
            called = True

        _patch_neurokit(monkeypatch, [100, 230], [], [], [])
        monkeypatch.setattr(ecg_analysis.nk, "ecg_delineate", delineate)
        result = analyze_ecg_waveform([0] * 1300, sampling_rate=130)
        assert "need at least 3" in result["error"]
        assert called is False
