"""
Tests for MedicalDeviceParser (medical/specter_medical_hub.py).

docs/MANUAL.md Part 7.2 flags these parsers as "written against published GATT
specs but never run against the actual devices" - these tests lock in the
documented byte layouts so a refactor can't silently change the parsing
without a test failing, and confirm short/malformed input degrades to an
empty dict instead of raising.
"""
from medical.specter_medical_hub import MedicalDeviceParser


class TestParseOmronBp:
    def test_parses_systolic_diastolic_pulse_big_endian(self):
        # flags=0x00, systolic=120, diastolic=80, pulse=72
        data = bytes([0x00, 0x00, 0x78, 0x00, 0x50, 0x00, 0x48])
        result = MedicalDeviceParser.parse_omron_bp(data)
        assert result == {"bp_systolic": 120, "bp_diastolic": 80, "pulse": 72}

    def test_short_payload_returns_empty_dict(self):
        assert MedicalDeviceParser.parse_omron_bp(bytes([0x00, 0x01])) == {}

    def test_empty_payload_returns_empty_dict(self):
        assert MedicalDeviceParser.parse_omron_bp(b"") == {}

    def test_non_bytes_input_does_not_raise(self):
        assert MedicalDeviceParser.parse_omron_bp(None) == {}


class TestParseMasimoOximeter:
    def test_parses_pulse_and_spo2(self):
        data = bytes([0x00, 72, 98])
        result = MedicalDeviceParser.parse_masimo_oximeter(data)
        assert result == {"pulse": 72, "spo2": 98}

    def test_short_payload_returns_empty_dict(self):
        assert MedicalDeviceParser.parse_masimo_oximeter(bytes([0x00])) == {}

    def test_non_bytes_input_does_not_raise(self):
        assert MedicalDeviceParser.parse_masimo_oximeter(None) == {}


class TestParseBraunThermometer:
    def test_parses_temperature_c_and_f(self):
        # 37.00C -> raw 3700 -> big-endian 0x0E74
        data = bytes([0x0E, 0x74])
        result = MedicalDeviceParser.parse_braun_thermometer(data)
        assert result["temperature_c"] == 37.0
        assert result["temperature_f"] == 98.6

    def test_short_payload_returns_empty_dict(self):
        assert MedicalDeviceParser.parse_braun_thermometer(bytes([0x0E])) == {}

    def test_non_bytes_input_does_not_raise(self):
        assert MedicalDeviceParser.parse_braun_thermometer(None) == {}


class TestParseContourGlucometer:
    def test_parses_glucose_little_endian(self):
        # 95 mg/dL -> little-endian bytes
        data = bytes([95, 0])
        result = MedicalDeviceParser.parse_contour_glucometer(data)
        assert result == {"glucose_mg_dl": 95}

    def test_parses_value_above_255_needs_second_byte(self):
        # 300 mg/dL -> little-endian bytes (0x2C, 0x01)
        data = bytes([0x2C, 0x01])
        result = MedicalDeviceParser.parse_contour_glucometer(data)
        assert result == {"glucose_mg_dl": 300}

    def test_short_payload_returns_empty_dict(self):
        assert MedicalDeviceParser.parse_contour_glucometer(bytes([95])) == {}

    def test_non_bytes_input_does_not_raise(self):
        assert MedicalDeviceParser.parse_contour_glucometer(None) == {}
