"""
Tests for medical/clinical_scores.py - the shared MAP / pulse pressure /
shock index / NEWS2 / qSOFA scoring functions used by both specter_ward.py
(manual bedside charting) and specter_medical_ai.py (automated BLE device
readings, always partial for NEWS2/qSOFA - see the module docstring).
"""
import pytest

from medical.clinical_scores import (
    calculate_map,
    calculate_news2,
    calculate_pulse_pressure,
    calculate_qsofa,
    calculate_shock_index,
)


class TestCalculateMap:
    def test_known_values(self):
        assert calculate_map(120, 80) == pytest.approx(93.3)

    def test_missing_sbp_returns_none(self):
        assert calculate_map(None, 80) is None

    def test_missing_dbp_returns_none(self):
        assert calculate_map(120, None) is None

    def test_non_numeric_input_returns_none(self):
        assert calculate_map("high", 80) is None


class TestCalculatePulsePressure:
    def test_known_value(self):
        assert calculate_pulse_pressure(120, 80) == 40.0

    def test_missing_input_returns_none(self):
        assert calculate_pulse_pressure(None, None) is None


class TestCalculateShockIndex:
    def test_known_value(self):
        assert calculate_shock_index(72, 120) == 0.6

    def test_zero_sbp_does_not_divide_by_zero(self):
        assert calculate_shock_index(72, 0) is None

    def test_missing_hr_returns_none(self):
        assert calculate_shock_index(None, 120) is None


class TestCalculateNews2:
    def test_all_normal_scores_zero_low_risk(self):
        result = calculate_news2({
            "rr": 16, "spo2": 98, "supplemental_o2": False,
            "bp_systolic": 120, "pulse": 72, "avpu": "A", "temperature_c": 37.0,
        })
        assert result["total"] == 0
        assert result["risk"] == "low"
        assert result["partial"] is False
        assert result["missing_parameters"] == []

    def test_missing_parameter_excluded_not_scored_zero(self):
        result = calculate_news2({"spo2": 98, "bp_systolic": 120})
        assert "rr" in result["missing_parameters"]
        assert "rr" not in result["per_parameter"]
        assert result["partial"] is True

    def test_no_parameters_at_all_is_unknown_risk_not_zero(self):
        result = calculate_news2({})
        assert result["risk"] == "unknown"
        assert result["total"] == 0
        assert result["partial"] is True

    def test_single_parameter_scoring_3_is_at_least_low_medium(self):
        result = calculate_news2({"spo2": 88})  # scores 3
        assert result["risk"] in ("low-medium", "medium")

    def test_high_risk_when_total_at_least_7(self):
        result = calculate_news2({
            "rr": 5, "spo2": 88, "bp_systolic": 85, "pulse": 145,
        })
        assert result["total"] >= 7
        assert result["risk"] == "high"

    def test_none_value_treated_as_missing_not_crash(self):
        result = calculate_news2({"rr": None, "spo2": 98})
        assert "rr" in result["missing_parameters"]

    def test_scale_note_always_present(self):
        result = calculate_news2({})
        assert "Scale 1" in result["scale_note"]


class TestCalculateQsofa:
    def test_all_present_and_normal_scores_zero(self):
        result = calculate_qsofa({"rr": 16, "bp_systolic": 120, "altered_mentation": False})
        assert result["total"] == 0
        assert result["positive"] is False
        assert result["partial"] is False

    def test_two_positive_criteria_is_positive(self):
        result = calculate_qsofa({"rr": 24, "bp_systolic": 90, "altered_mentation": False})
        assert result["total"] == 2
        assert result["positive"] is True

    def test_missing_input_makes_positive_unknown_not_false(self):
        result = calculate_qsofa({"bp_systolic": 120})
        assert result["partial"] is True
        assert result["positive"] is None  # never asserted "not positive" when incomplete
        assert "rr" in result["missing_parameters"]
        assert "altered_mentation" in result["missing_parameters"]

    def test_empty_input_is_fully_missing_not_a_crash(self):
        result = calculate_qsofa({})
        assert result["total"] == 0
        assert result["positive"] is None
        assert set(result["missing_parameters"]) == {"rr", "bp_systolic", "altered_mentation"}
