"""
Tests for MedicalDeviceParser (medical/specter_medical_hub.py).

docs/MANUAL.md Part 7.2 flags most of these parsers as "written against
published GATT specs but never run against the actual devices" - these
tests lock in the documented byte layouts so a refactor can't silently
change the parsing without a test failing, and confirm short/malformed
input degrades to an empty dict instead of raising.

parse_contour_glucometer is the exception: its real Bluetooth SIG Glucose
Measurement (0x2A18) layout was confirmed against multiple independent
sources (xDrip, blessed-android) - see the VERIFIED DEVICE GATE comment in
medical/specter_medical_hub.py - so these tests build real spec-compliant
byte payloads (flags + sequence + base time + SFLOAT concentration)
instead of just pinning arbitrary bytes.
"""
from medical.specter_medical_hub import MedicalDeviceParser, _decode_sfloat


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


class TestDecodeSfloat:
    def test_positive_integer_zero_exponent(self):
        # mantissa=95, exponent=0 -> 95.0
        assert _decode_sfloat(bytes([0x5F, 0x00])) == 95.0

    def test_negative_exponent_fraction(self):
        # mantissa=55, exponent=-1 -> 5.5
        assert _decode_sfloat(bytes([0x37, 0xF0])) == 5.5

    def test_negative_mantissa(self):
        # mantissa=-5 (two's complement 0xFFB), exponent=0 -> -5.0
        assert _decode_sfloat(bytes([0xFB, 0x0F])) == -5.0

    def test_nan_reserved_value_returns_none(self):
        # mantissa=0x07FF (NaN sentinel), exponent=0
        assert _decode_sfloat(bytes([0xFF, 0x07])) is None

    def test_wrong_length_returns_none(self):
        assert _decode_sfloat(bytes([0x00])) is None
        assert _decode_sfloat(bytes([0x00, 0x00, 0x00])) is None


class TestParseContourGlucometer:
    # Real Bluetooth SIG Glucose Measurement (0x2A18) records: flags(1) +
    # sequence(2 LE) + base time(7: year LE, month, day, hour, min, sec) +
    # [time offset(2 LE)] + [glucose SFLOAT(2 LE) + type/location(1)] +
    # [sensor status(2 LE)].
    BASE_TIME = bytes([0xEA, 0x07, 8, 21, 12, 0, 0])  # 2026-08-21 12:00:00

    def test_parses_kg_per_l_units(self):
        # flags: glucose present (bit1), units=kg/L (bit2 clear)
        flags = 0x02
        seq = bytes([0x01, 0x00])
        # mantissa=959, exponent=-6 -> 0.000959 kg/L -> 95.9 mg/dL
        sfloat = bytes([0xBF, 0xA3])
        type_loc = bytes([0x00])
        data = bytes([flags]) + seq + self.BASE_TIME + sfloat + type_loc
        result = MedicalDeviceParser.parse_contour_glucometer(data)
        assert result == {"glucose_mg_dl": 95.9}

    def test_parses_mol_per_l_units(self):
        # flags: glucose present (bit1) + units=mol/L (bit2 set)
        flags = 0x06
        seq = bytes([0x01, 0x00])
        # mantissa=5, exponent=-3 -> 0.005 mol/L (~90 mg/dL, normal fasting)
        sfloat = bytes([0x05, 0xD0])
        type_loc = bytes([0x00])
        data = bytes([flags]) + seq + self.BASE_TIME + sfloat + type_loc
        result = MedicalDeviceParser.parse_contour_glucometer(data)
        assert result == {"glucose_mg_dl": 90.1}

    def test_time_offset_present_shifts_glucose_field(self):
        # flags: time offset present (bit0) + glucose present (bit1), kg/L
        flags = 0x03
        seq = bytes([0x01, 0x00])
        time_offset = bytes([0x00, 0x00])
        sfloat = bytes([0xBF, 0xA3])  # same 95.9 mg/dL value as above
        type_loc = bytes([0x00])
        data = bytes([flags]) + seq + self.BASE_TIME + time_offset + sfloat + type_loc
        result = MedicalDeviceParser.parse_contour_glucometer(data)
        assert result == {"glucose_mg_dl": 95.9}

    def test_glucose_not_present_returns_empty_dict(self):
        # flags: nothing set - a context-only record, not an error
        flags = 0x00
        seq = bytes([0x01, 0x00])
        data = bytes([flags]) + seq + self.BASE_TIME
        assert MedicalDeviceParser.parse_contour_glucometer(data) == {}

    def test_sensor_error_sfloat_is_discarded_not_fabricated(self):
        # SFLOAT NaN sentinel where the concentration would be - must not
        # silently become a fabricated glucose reading.
        flags = 0x02
        seq = bytes([0x01, 0x00])
        sfloat_nan = bytes([0xFF, 0x07])
        type_loc = bytes([0x00])
        data = bytes([flags]) + seq + self.BASE_TIME + sfloat_nan + type_loc
        assert MedicalDeviceParser.parse_contour_glucometer(data) == {}

    def test_short_payload_returns_empty_dict(self):
        assert MedicalDeviceParser.parse_contour_glucometer(bytes([0x02, 0x01])) == {}

    def test_non_bytes_input_does_not_raise(self):
        assert MedicalDeviceParser.parse_contour_glucometer(None) == {}
