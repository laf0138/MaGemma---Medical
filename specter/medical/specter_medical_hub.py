#!/usr/bin/env python3
"""
SPECTER Medical Hub — Bluetooth Vital Signs Aggregator
Raspberry Pi Zero 2W

Collects data from multiple Bluetooth medical devices:
- Omron BP7450 (blood pressure)
- Masimo MightySat (pulse oximetry)
- Braun ThermoScan 7 (temperature)
- Contour Next One (glucose)
- Polar H10 (ECG waveform + heart rate, via the bleakheart library)
- AliveCor KardiaMobile 6L (ECG) — REMOVED, see docs/MANUAL.md Part 7.4

Publishes timestamped readings to MQTT broker on Node 1 (192.168.1.1)
MQTT topics: shtf/medical/vitals/* 

Author: SPECTER Build Team
Date: August 2026
Version: 1.2.0
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Dict, Any
import argparse

import paho.mqtt.client as mqtt

# --- paho-mqtt 1.x / 2.x compatibility -------------------------------------
def _mqtt_client(client_id: str = ""):
    """Construct an MQTT client that works on paho-mqtt 1.x and 2.x."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x has no CallbackAPIVersion - fall back to the
        # old-style constructor (deprecated but functional on 2.x too),
        # NOT a recursive call to this same function, which would hit the
        # same AttributeError every time and blow the stack.
        return mqtt.Client(client_id=client_id)
# ---------------------------------------------------------------------------

# --- MQTT auth --------------------------------------------------------------
# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "medical_hub"
# account: it can only read shtf/medical/hub/command/# and write vitals.
# Runtime connections require the dedicated credential and reject the
# installer's placeholder password.
MQTT_SERVICE_KEY      = "medical_hub"
MQTT_DEFAULT_USERNAME = "specter-medical-hub"
MQTT_DEFAULT_PASSWORD = "specter-change-me"


def _mqtt_credentials() -> tuple:
    """Return the dedicated service credential, failing closed if absent."""
    try:
        cfg = json.loads(Path("/etc/specter/specter.json").read_text())
    except Exception as exc:
        raise RuntimeError("MQTT configuration is unreadable") from exc
    service_cfg = cfg.get("mqtt", {}).get("services", {}).get(MQTT_SERVICE_KEY, {})
    username, password = service_cfg.get("username"), service_cfg.get("password")
    if not username or not password or password == MQTT_DEFAULT_PASSWORD:
        raise RuntimeError(f"dedicated MQTT credentials missing for {MQTT_SERVICE_KEY}")
    return username, password
# ---------------------------------------------------------------------------
import asyncio
from bleak import BleakClient, BleakScanner
# Polar H10 ECG/HR uses bleakheart (MPL-2.0), a maintained library for
# Polar's PMD interface, instead of hand-parsed byte offsets - see the
# VERIFIED DEVICE GATE comment above BluetoothDeviceConfig. Delegating the
# protocol to a real, source-checked library is why polar_h10 is a much
# lower-risk candidate to eventually mark verified than the other four
# device types, but it still requires a real-hardware smoke test first.
from bleakheart import PolarMeasurementData, HeartRate

# ecg_analysis.py is a sibling file in this same medical/ directory. When
# this module is run directly as a script (the real deployment: systemd
# launches `python3 /opt/specter/medical/specter_medical_hub.py`), Python
# only puts that script's own directory on sys.path, so the bare import is
# what resolves. Under pytest, specter/ itself is on sys.path (see
# tests/conftest.py) and medical is imported as a package, so the
# package-qualified form is what resolves there instead.
try:
    from medical.ecg_analysis import analyze_ecg_waveform
except ImportError:
    from ecg_analysis import analyze_ecg_waveform

POLAR_H10_ECG_SAMPLE_RATE_HZ = 130  # H10 default per bleakheart's PMD docs;
                                     # only correct as long as start_streaming
                                     # ('ECG') below is called with no explicit
                                     # SAMPLE_RATE override.

# Contour Next One glucose parsing (Bluetooth SIG Glucose Service 0x1808,
# Glucose Measurement characteristic 0x2A18) - see parse_contour_glucometer
# and the VERIFIED DEVICE GATE comment above BluetoothDeviceConfig.
# Glucose (C6H12O6) molar mass, IUPAC 2021 standard atomic weights
# (C 12.011, H 1.008, O 15.999): 6*12.011 + 12*1.008 + 6*15.999 = 180.156 g/mol.
GLUCOSE_MOLAR_MASS_G_PER_MOL = 180.156
# The device reports concentration in mol/L when the units flag is set (see
# parse_contour_glucometer), not mmol/L, so this is derived directly from
# the molar mass rather than borrowed from any particular app's mmol/L<->
# mg/dL display constant (xDrip's own codebase has two slightly different
# ones for that unrelated conversion - not applicable here).
# mg/dL = (mol/L) * molar_mass(g/mol) * 1000(mg/g) / 10(dL/L)
GLUCOSE_MOLL_TO_MGDL = GLUCOSE_MOLAR_MASS_G_PER_MOL * 100
# mg/dL = (kg/L) * 1e6(mg/kg) / 10(dL/L)
GLUCOSE_KGL_TO_MGDL = 100_000

# Logging configuration
# SPECTER_LOG lets this be overridden (tests/conftest.py points it at a
# tmp file) - every other module in this project already does this
# (specter_trauma.py, specter_medical_ai.py). This one hardcoded
# /var/log/specter_medical_hub.log directly, which the real install
# writes fine as root but any non-root import (including every GitHub
# Actions run on this repo, checked - every single CI run on this branch
# failed at collection with PermissionError on this exact line) cannot
# create at all, since /var/log isn't writable and the file doesn't exist.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.environ.get('SPECTER_LOG', '/var/log/specter_medical_hub.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class VitalSign:
    """Single vital sign reading with timestamp and metadata"""
    device_name: str
    reading_type: str  # bp_systolic, bp_diastolic, pulse, spo2, temp, glucose, ecg_rhythm
    value: float
    unit: str
    timestamp_utc: str
    rssi: Optional[int] = None  # Signal strength
    
    def to_mqtt_payload(self) -> str:
        """Convert to JSON for MQTT publishing"""
        return json.dumps(asdict(self))


@dataclass
class PatientVitals:
    """Complete vital signs snapshot"""
    patient_id: str
    timestamp_utc: str
    readings: list  # List of VitalSign objects
    
    def to_mqtt_payload(self) -> str:
        """Convert to JSON for MQTT publishing"""
        payload = {
            'patient_id': self.patient_id,
            'timestamp_utc': self.timestamp_utc,
            'readings': [asdict(r) for r in self.readings]
        }
        return json.dumps(payload)


# ============================================================================
# VERIFIED DEVICE GATE
# ============================================================================
#
# The service/characteristic UUIDs and byte layouts below were checked
# against Bluetooth SIG specifications and public reverse-engineering
# research (August 2026) and do not match how these specific devices are
# documented to actually communicate:
#
#   - omron_bp7450: service_uuid 180a is the generic Device Information
#     Service (manufacturer/model/serial strings), not a data service.
#     The characteristic UUIDs given (2a6e, 2a6f, 2a3c) are the real
#     Bluetooth SIG assignments for Temperature, Humidity, and Alert
#     Category ID respectively - nothing to do with blood pressure or
#     pulse. Independent reverse-engineering (userx14/omblepy,
#     evnleong/open-BPM) shows Omron devices actually use a proprietary
#     EEPROM read/write command protocol, not a single flags+value
#     notification at all.
#   - masimo_mightyssat: parse_masimo_oximeter's own docstring claims to
#     read the "Standard BLE Heart Rate Measurement" characteristic
#     (0x2A37) and extracts an SpO2 byte from it - but that characteristic
#     has no SpO2 field under the Bluetooth spec. SpO2 lives in a
#     separate standard characteristic (PLX Continuous Measurement,
#     0x2A5F) with a different structure entirely.
#   - braun_thermoscan: worse than a wrong parser - the physical device
#     this entry names, the plain "Braun ThermoScan 7" (IRT6520), has NO
#     Bluetooth radio at all. It is a basic ear thermometer with a
#     9-reading on-device memory button and no app or wireless sync.
#     Braun sells a visually similar but distinct SKU, "ThermoScan 7+
#     Connect", that does have BLE 5.0 and syncs to the Braun Family Care
#     app - but SPECTER's kit names the non-Connect model, which cannot
#     produce any BLE traffic to parse, real or otherwise (confirmed
#     August 2026 against Braun's own US/UK product pages and independent
#     reviews - see docs/MANUAL.md Part 7.4). This entry stays hard-blocked
#     permanently unless the kit's hardware is swapped for the Connect
#     model, which would need its own from-scratch protocol verification.
#   - contour_next_one: FIXED August 2026. Confirmed via multiple
#     independent sources (weliem/blessed-android, NightscoutFoundation/
#     xDrip, Chakib-Temal/Android_BLE_Usb_Sensors) that the Contour Next
#     One genuinely implements the standard Bluetooth SIG Glucose Service
#     (0x1808) / Glucose Measurement characteristic (0x2A18) - unlike
#     Omron, this one isn't proprietary. parse_contour_glucometer now
#     decodes the real flags+SFLOAT structure (ported from xDrip's
#     GlucoseReadingRx.java / BluetoothCHelper.java, cross-checked against
#     the Bluetooth SIG GATT Specification Supplement). Still gated below:
#     a correct decoder for a confirmed-standard protocol is lower risk
#     than the other four, but "should be right" is not "hardware
#     confirmed" - same standard SPECTER already held polar_h10 to.
#   - collect_from_device() also only reads the FIRST characteristic
#     listed per device ("Simplified: use first reading") and feeds its
#     raw bytes to a parser expecting several characteristics' worth of
#     combined data - a second, independent bug on top of the protocol
#     mismatch above. Doesn't affect braun_thermoscan/contour_next_one
#     (each lists exactly one characteristic), but still applies to
#     omron_bp7450 and masimo_mightyssat.
#
# This is the same class of problem as the AliveCor KardiaMobile parser
# that was removed in v1.1.0 for fabricating a characteristic that never
# existed (see docs/MANUAL.md Part 7.4) - a plausible-looking wrong vital
# sign is worse than no reading at all, because nothing about a parsed
# number by itself reveals that it's wrong.
#
# Collection is hard-blocked per device type until someone captures real
# traffic from the actual hardware and confirms (or replaces) the parser
# against it - see docs/MANUAL.md Part 7.2. Verifying a device does NOT
# require a code change: set SPECTER_VERIFIED_BLE_DEVICES to a
# comma-separated list of the dev_type keys below (e.g.
# "omron_bp7450,contour_next_one") once confirmed.
VERIFIED_DEVICE_TYPES = frozenset(
    d.strip() for d in os.environ.get("SPECTER_VERIFIED_BLE_DEVICES", "").split(",") if d.strip()
)


# ============================================================================
# BLUETOOTH DEVICE DEFINITIONS
# ============================================================================

class BluetoothDeviceConfig:
    """Configuration for Bluetooth medical devices"""

    # Device UUIDs and characteristic UUIDs (standard medical device specs)
    DEVICES = {
        'omron_bp7450': {
            'name_pattern': 'Omron',
            'service_uuid': '180a',  # Device Information Service
            'characteristic_uuids': {
                'bp_systolic': '2a6e',
                'bp_diastolic': '2a6f',
                'pulse': '2a3c'
            },
            'parser': 'parse_omron_bp'
        },
        'masimo_mightyssat': {
            'name_pattern': 'MightySat',
            'service_uuid': '180d',  # Heart Rate Service
            'characteristic_uuids': {
                'spo2': '2a60',
                'pulse': '2a37'
            },
            'parser': 'parse_masimo_oximeter'
        },
        'braun_thermoscan': {
            'name_pattern': 'ThermoScan',
            'service_uuid': '180a',
            'characteristic_uuids': {
                'temperature': '2a1c'
            },
            'parser': 'parse_braun_thermometer'
        },
        'contour_next_one': {
            'name_pattern': 'Contour',
            'service_uuid': '1808',  # Glucose Service (was 180a - wrong)
            'characteristic_uuids': {
                'glucose': '2a18'  # Glucose Measurement
            },
            'parser': 'parse_contour_glucometer'
        },
        'polar_h10': {
            'name_pattern': 'Polar H10',
            # Streams via bleakheart's PMD/HeartRate interfaces rather than
            # a single characteristic_uuids/parser read - see
            # collect_from_device() and _collect_polar_h10_stream().
            'streaming': True,
        },
    }


# ============================================================================
# BLUETOOTH DATA PARSERS
# ============================================================================

def _decode_sfloat(raw_le: bytes) -> Optional[float]:
    """
    Decode a 2-byte IEEE 11073-20601 SFLOAT (used by the Bluetooth SIG
    Glucose Measurement characteristic, among others): a 12-bit signed
    mantissa (two's complement) plus a 4-bit signed exponent (two's
    complement), value = mantissa * 10**exponent, packed little-endian as
    exponent in the top nibble and mantissa in the remaining 12 bits.

    Returns None for the reserved sentinel mantissa values (NaN, NRes,
    +/-INFINITY, reserved) per the spec, rather than silently decoding them
    as a plausible-looking number - a device reporting "sensor error" must
    not turn into a fabricated reading.

    Ported from xDrip's BluetoothCHelper.getSfloat16() (NightscoutFoundation/
    xDrip, GPLv3), cross-checked against the Bluetooth SIG GATT
    Specification Supplement's SFLOAT definition.
    """
    if len(raw_le) != 2:
        return None
    raw = raw_le[0] | (raw_le[1] << 8)
    mantissa_raw = raw & 0x0FFF
    exponent_raw = (raw >> 12) & 0x0F

    if mantissa_raw in (0x07FF, 0x0800, 0x07FE, 0x0802, 0x0801):
        return None

    mantissa = mantissa_raw - 0x1000 if mantissa_raw >= 0x0800 else mantissa_raw
    exponent = exponent_raw - 0x10 if exponent_raw >= 0x08 else exponent_raw
    return mantissa * (10 ** exponent)


class MedicalDeviceParser:
    """Parse raw Bluetooth characteristic data into vital signs"""

    @staticmethod
    def parse_omron_bp(data: bytes) -> Dict[str, Any]:
        """
        Parse Omron BP7450 blood pressure data
        Format: flags (1), systolic (2), diastolic (2), pulse (2)
        """
        try:
            if len(data) < 7:
                return {}
            
            flags = data[0]
            # Systolic/diastolic in mmHg (big-endian)
            systolic = int.from_bytes(data[1:3], 'big')
            diastolic = int.from_bytes(data[3:5], 'big')
            pulse = int.from_bytes(data[5:7], 'big')
            
            return {
                'bp_systolic': systolic,
                'bp_diastolic': diastolic,
                'pulse': pulse
            }
        except Exception as e:
            logger.error(f"Error parsing Omron BP data: {e}")
            return {}
    
    @staticmethod
    def parse_masimo_oximeter(data: bytes) -> Dict[str, Any]:
        """
        Parse Masimo MightySat pulse oximetry data
        Standard BLE Heart Rate Measurement: flags (1), pulse (1), SpO2 (1)
        """
        try:
            if len(data) < 3:
                return {}
            
            flags = data[0]
            pulse = data[1]
            spo2 = data[2]  # Percentage 0-100
            
            return {
                'pulse': pulse,
                'spo2': spo2
            }
        except Exception as e:
            logger.error(f"Error parsing Masimo oximeter data: {e}")
            return {}
    
    @staticmethod
    def parse_braun_thermometer(data: bytes) -> Dict[str, Any]:
        """
        Parse Braun ThermoScan 7 temperature data
        Format: temperature in 0.01°C increments (2 bytes, big-endian)
        """
        try:
            if len(data) < 2:
                return {}
            
            temp_raw = int.from_bytes(data[0:2], 'big')
            temp_celsius = temp_raw / 100.0
            
            return {
                'temperature_c': temp_celsius,
                'temperature_f': (temp_celsius * 9/5) + 32
            }
        except Exception as e:
            logger.error(f"Error parsing Braun thermometer data: {e}")
            return {}
    
    @staticmethod
    def parse_contour_glucometer(data: bytes) -> Dict[str, Any]:
        """
        Parse Contour Next One glucose meter data: the standard Bluetooth
        SIG Glucose Measurement characteristic (0x2A18), confirmed August
        2026 to be what this device actually implements (see the VERIFIED
        DEVICE GATE comment above BluetoothDeviceConfig). Ported from
        xDrip's GlucoseReadingRx.java (NightscoutFoundation/xDrip, GPLv3),
        cross-checked against the Bluetooth SIG GATT Specification
        Supplement.

        Layout (all multi-byte fields little-endian):
          flags (1 byte):
            bit0 time-offset present, bit1 glucose concentration present,
            bit2 units (0=kg/L, 1=mol/L), bit3 sensor status present,
            bit4 context info follows (ignored - context is a separate
            optional characteristic this device kit doesn't read)
          sequence number (uint16)
          base time (7 bytes: year u16, month/day/hour/min/sec u8 each)
          [time offset (sint16)]           - only if bit0 set
          [glucose SFLOAT (2) + type/sample-location (1)] - only if bit1 set
          [sensor status (uint16)]         - only if bit3 set
        """
        try:
            if len(data) < 10:
                logger.warning(f"Contour glucometer payload too short: {len(data)} bytes")
                return {}

            flags = data[0]
            glucose_present = bool(flags & 0x02)
            units_mol_l = bool(flags & 0x04)
            time_offset_present = bool(flags & 0x01)

            if not glucose_present:
                # A valid, well-formed measurement with no glucose reading
                # (e.g. a context-only record) - not an error, just nothing
                # to report this time.
                return {}

            offset = 10
            if time_offset_present:
                offset += 2

            if len(data) < offset + 2:
                logger.warning("Contour glucometer payload missing glucose field per its own flags")
                return {}

            concentration = _decode_sfloat(data[offset:offset + 2])
            if concentration is None:
                logger.warning("Contour glucometer reported an invalid/sensor-error glucose reading (SFLOAT NaN/INFINITY/reserved) - discarding rather than fabricating a value")
                return {}

            if units_mol_l:
                glucose_mg_dl = concentration * GLUCOSE_MOLL_TO_MGDL
            else:
                glucose_mg_dl = concentration * GLUCOSE_KGL_TO_MGDL

            return {
                'glucose_mg_dl': round(glucose_mg_dl, 1)
            }
        except Exception as e:
            logger.error(f"Error parsing Contour glucometer data: {e}")
            return {}
    
    # NOTE: AliveCor KardiaMobile parsing was REMOVED in v1.1.0.
    # The previous implementation fabricated a BLE characteristic that does not
    # exist. The KardiaMobile 6L uses a proprietary BLE protocol and performs
    # rhythm determination inside the Kardia app, not on the device. There is
    # no public GATT characteristic emitting a rhythm classification.
    #
    # See docs/MANUAL.md "ECG" for the two supported paths:
    #   - Polar H10 chest strap (open BLE, continuous HR + RR intervals)
    #   - Original single-lead KardiaMobile via audio capture (waveform)


# ============================================================================
# BLUETOOTH SCANNER & COLLECTOR
# ============================================================================

class MedicalHubBleCollector:
    """Scan for and collect data from Bluetooth medical devices"""
    
    def __init__(self, mqtt_host: str = '192.168.1.1', mqtt_port: int = 1883,
                 polar_stream_seconds: int = 10):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_client = _mqtt_client()
        self.mqtt_client.username_pw_set(*_mqtt_credentials())
        self.mqtt_connected = False
        self.discovered_devices = {}
        self.parser = MedicalDeviceParser()
        self.patient_id = "default"
        self.polar_stream_seconds = polar_stream_seconds
        
        # MQTT callbacks
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
        self.mqtt_client.on_message = self._on_mqtt_message
    
    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        """MQTT connection callback"""
        if reason_code == 0:
            self.mqtt_connected = True
            logger.info(f"MQTT connected to {self.mqtt_host}:{self.mqtt_port}")
            self.mqtt_client.subscribe("shtf/medical/hub/command/#")
        else:
            logger.error("MQTT connection failed: %s", reason_code)
            self.mqtt_connected = False
    
    def _on_mqtt_disconnect(
        self, client, userdata, disconnect_flags_or_reason_code,
        reason_code=None, properties=None,
    ):
        """MQTT disconnection callback"""
        reason_code = (
            disconnect_flags_or_reason_code if reason_code is None else reason_code
        )
        self.mqtt_connected = False
        if reason_code != 0:
            logger.warning("MQTT disconnected unexpectedly: %s", reason_code)
    
    def _on_mqtt_message(self, client, userdata, msg):
        """Handle incoming MQTT commands"""
        try:
            payload = json.loads(msg.payload.decode())
            if msg.topic == "shtf/medical/hub/command/patient_id":
                self.patient_id = payload.get('patient_id', 'default')
                logger.info(f"Patient ID set to: {self.patient_id}")
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")
    
    def connect_mqtt(self):
        """Connect to MQTT broker"""
        try:
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
            self.mqtt_client.loop_start()
            time.sleep(1)  # Wait for connection callback
        except Exception as e:
            logger.error(f"Failed to connect to MQTT: {e}")
    
    def disconnect_mqtt(self):
        """Disconnect from MQTT broker"""
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
    
    async def scan_devices(self, timeout: int = 10):
        """Scan for Bluetooth medical devices"""
        logger.info(f"Scanning for Bluetooth devices ({timeout}s)...")
        
        try:
            scanner = BleakScanner()
            devices = await scanner.discover(timeout=timeout, return_adv=True)
            
            self.discovered_devices = {}
            # Bleak with return_adv=True returns
            # {address: (BLEDevice, AdvertisementData)}.  The previous loop
            # treated each address-string key as the BLEDevice, so every real
            # scan failed at ``device.name`` and was swallowed by the broad
            # exception below.  Accept the documented shape while retaining a
            # list fallback for older/test scanner implementations.
            entries = devices.values() if isinstance(devices, dict) else devices
            for entry in entries:
                if isinstance(entry, tuple) and len(entry) == 2:
                    device, adv_data = entry
                else:
                    device, adv_data = entry, None
                device_name = (
                    getattr(device, 'name', None)
                    or getattr(adv_data, 'local_name', None)
                    or getattr(device, 'address', 'unknown')
                )
                device_address = getattr(device, 'address', device_name)
                rssi = getattr(adv_data, 'rssi', getattr(device, 'rssi', None))
                
                # Check if device matches any medical device patterns
                for dev_type, config in BluetoothDeviceConfig.DEVICES.items():
                    if config['name_pattern'].lower() in device_name.lower():
                        self.discovered_devices[device_address] = {
                            'name': device_name,
                            'address': device_address,
                            'type': dev_type,
                            'rssi': rssi,
                            'object': device
                        }
                        logger.info(
                            f"Found {dev_type}: {device_name} "
                            f"({device_address}) RSSI: {rssi}"
                        )
            
            return self.discovered_devices
        
        except Exception as e:
            logger.error(f"Error during device scan: {e}")
            return {}
    
    async def collect_from_device(self, device_address: str) -> Optional[Dict[str, Any]]:
        """Connect to single device and collect vitals"""
        if device_address not in self.discovered_devices:
            logger.warning(f"Device {device_address} not found in discovered devices")
            return None
        
        device_info = self.discovered_devices[device_address]
        device = device_info['object']
        dev_type = device_info['type']

        if dev_type not in VERIFIED_DEVICE_TYPES:
            logger.error(
                f"Refusing to read {device_info['name']} ({dev_type}): this device's "
                f"parser is unverified against real hardware and may produce a "
                f"plausible-looking WRONG vital sign - see the VERIFIED DEVICE GATE "
                f"comment above BluetoothDeviceConfig and docs/MANUAL.md Part 7.2. "
                f"Once confirmed, add '{dev_type}' to SPECTER_VERIFIED_BLE_DEVICES."
            )
            return None

        try:
            async with BleakClient(device) as client:
                logger.info(f"Connected to {device_info['name']}")

                config = BluetoothDeviceConfig.DEVICES[dev_type]

                if config.get('streaming'):
                    parsed = await self._collect_polar_h10_stream(client, device_info)
                    if parsed:
                        return {
                            'device_name': device_info['name'],
                            'device_type': dev_type,
                            'address': device_address,
                            'rssi': device_info['rssi'],
                            'readings': parsed
                        }
                    return None

                readings = {}

                # Read all characteristics for this device
                for char_name, char_uuid in config['characteristic_uuids'].items():
                    try:
                        data = await client.read_gatt_char(char_uuid)
                        readings[char_name] = data
                        logger.debug(f"Read {char_name} from {device_info['name']}: {data.hex()}")
                    except Exception as e:
                        logger.warning(f"Could not read {char_name} from {device_info['name']}: {e}")

                # Parse all readings
                parser_func = getattr(self.parser, config['parser'], None)
                if parser_func and readings:
                    parsed = parser_func(list(readings.values())[0])  # Simplified: use first reading
                    return {
                        'device_name': device_info['name'],
                        'device_type': dev_type,
                        'address': device_address,
                        'rssi': device_info['rssi'],
                        'readings': parsed
                    }

        except Exception as e:
            logger.error(f"Error collecting from {device_info['name']}: {e}")

        return None

    async def _collect_polar_h10_stream(
        self, client: BleakClient, device_info: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Stream ECG + heart rate from a Polar H10 for self.polar_stream_seconds
        via bleakheart's PMD/HeartRate interfaces - a maintained library
        that talks the real protocol, not hand-parsed byte offsets.

        Returns a dict that may contain:
          'ecg_waveform_uv':  list[int] microvolt samples, 130Hz (H10)
          'pulse':            most recent average heart rate in bpm
          'rr_intervals_ms':  list[int] RR intervals collected in the window
        Any key may be absent if nothing arrived during the window (e.g.
        poor skin contact for HR, or the strap not yet settled) - this is
        normal and the caller (collect_from_device) already treats an
        empty/partial dict as "no reading this cycle", not an error.
        """
        ecg_queue: asyncio.Queue = asyncio.Queue()
        hr_queue: asyncio.Queue = asyncio.Queue()

        pmd = PolarMeasurementData(client, ecg_queue=ecg_queue)
        # unpack=False: each queue item already carries the full RR list
        # for that frame, rather than bleakheart splitting it into one
        # queue item per heartbeat - simpler and less ambiguous to consume.
        hr = HeartRate(client, queue=hr_queue, unpack=False)

        err, err_msg, _raw = await pmd.start_streaming('ECG')
        if err != 0:
            logger.error(f"Polar H10 ECG stream start failed for {device_info['name']}: {err_msg}")
            return {}
        await hr.start_notify()

        ecg_samples: list = []
        rr_intervals: list = []
        latest_hr = None
        deadline = time.monotonic() + self.polar_stream_seconds

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    dtype, _tstamp, payload = await asyncio.wait_for(
                        ecg_queue.get(), timeout=remaining
                    )
                    if dtype == 'ECG':
                        ecg_samples.extend(payload)
                except asyncio.TimeoutError:
                    pass

                while not hr_queue.empty():
                    _dtype, _tstamp, (avg_hr, rr_list), _energy = hr_queue.get_nowait()
                    latest_hr = avg_hr
                    rr_intervals.extend(rr_list)
        finally:
            try:
                await pmd.stop_streaming('ECG')
            except Exception as e:
                logger.warning(f"Error stopping Polar H10 ECG stream: {e}")
            try:
                await hr.stop_notify()
            except Exception as e:
                logger.warning(f"Error stopping Polar H10 HR notifications: {e}")

        result: Dict[str, Any] = {}
        if ecg_samples:
            result['ecg_waveform_uv'] = ecg_samples

            analysis = analyze_ecg_waveform(ecg_samples, sampling_rate=POLAR_H10_ECG_SAMPLE_RATE_HZ)
            if 'error' in analysis:
                logger.info(f"Polar H10 ECG analysis skipped for {device_info['name']}: {analysis['error']}")
            else:
                # These names are intentionally precise.  NeuroKit2 gives us
                # Q/S peak locations, not a validated clinical QRS onset-to-
                # offset duration.  Publishing that proxy as "QRS duration"
                # made it too easy for a UI or model to apply an inapplicable
                # diagnostic threshold.
                analysis_fields = {
                    'q_s_peak_interval_ms': 'ecg_q_s_peak_interval_ms',
                    'r_wave_abs_amplitude_uv': 'ecg_r_wave_abs_amplitude_uv',
                    't_wave_abs_amplitude_uv': 'ecg_t_wave_abs_amplitude_uv',
                    't_r_abs_ratio': 'ecg_t_r_abs_ratio',
                    'morphology_beats_analyzed': 'ecg_morphology_beats_analyzed',
                    'amplitude_beats_analyzed': 'ecg_amplitude_beats_analyzed',
                    'analysis_status': 'ecg_analysis_status',
                }
                for analysis_key, reading_key in analysis_fields.items():
                    if analysis_key in analysis:
                        result[reading_key] = analysis[analysis_key]
                if analysis.get('warnings'):
                    result['ecg_analysis_warnings'] = analysis['warnings']

        if latest_hr is not None:
            result['pulse'] = latest_hr
        if rr_intervals:
            result['rr_intervals_ms'] = rr_intervals
        return result
    
    async def collect_all_devices(self) -> PatientVitals:
        """Collect vitals from all discovered devices, return PatientVitals object"""
        vital_signs = []
        timestamp_utc = datetime.now(timezone.utc).isoformat()
        
        # Collect from each device
        for address in self.discovered_devices:
            result = await self.collect_from_device(address)
            if result and result['readings']:
                # Create VitalSign objects for each reading
                for reading_type, value in result['readings'].items():
                    # Determine unit based on reading type
                    unit_map = {
                        'bp_systolic': 'mmHg',
                        'bp_diastolic': 'mmHg',
                        'pulse': 'bpm',
                        'spo2': '%',
                        'temperature_c': '°C',
                        'temperature_f': '°F',
                        'glucose_mg_dl': 'mg/dL',
                        'ecg_rhythm': 'classification',
                        'ecg_waveform_uv': 'uV',
                        'rr_intervals_ms': 'ms',
                        'ecg_q_s_peak_interval_ms': 'ms',
                        'ecg_r_wave_abs_amplitude_uv': 'uV',
                        'ecg_t_wave_abs_amplitude_uv': 'uV',
                        'ecg_t_r_abs_ratio': 'ratio',
                        'ecg_morphology_beats_analyzed': 'beats',
                        'ecg_amplitude_beats_analyzed': 'beats',
                        'ecg_analysis_status': 'status',
                        'ecg_analysis_warnings': 'text',
                    }
                    unit = unit_map.get(reading_type, 'unknown')
                    
                    vital = VitalSign(
                        device_name=result['device_name'],
                        reading_type=reading_type,
                        value=value,
                        unit=unit,
                        timestamp_utc=timestamp_utc,
                        rssi=result['rssi']
                    )
                    vital_signs.append(vital)
        
        return PatientVitals(
            patient_id=self.patient_id,
            timestamp_utc=timestamp_utc,
            readings=vital_signs
        )
    
    def publish_vitals(self, vitals: PatientVitals):
        """Publish vitals to MQTT"""
        if not self.mqtt_connected:
            logger.warning("MQTT not connected, skipping publish")
            return
        
        try:
            # Publish summary to main vitals topic
            topic = f"shtf/medical/vitals/{self.patient_id}"
            payload = vitals.to_mqtt_payload()
            self.mqtt_client.publish(topic, payload, qos=1)
            logger.info(f"Published {len(vitals.readings)} readings to {topic}")
            
            # Publish individual readings for real-time dashboard
            for reading in vitals.readings:
                sub_topic = f"shtf/medical/vitals/{self.patient_id}/{reading.reading_type}"
                sub_payload = reading.to_mqtt_payload()
                self.mqtt_client.publish(sub_topic, sub_payload, qos=1)
        
        except Exception as e:
            logger.error(f"Error publishing vitals: {e}")


# ============================================================================
# MAIN LOOP
# ============================================================================

async def main_async(args):
    """Main async loop"""
    hub = MedicalHubBleCollector(mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port)
    hub.connect_mqtt()
    
    try:
        while True:
            logger.info("=" * 60)
            logger.info(f"Starting vital signs collection cycle (patient: {hub.patient_id})")
            
            # Scan for devices
            devices = await hub.scan_devices(timeout=args.scan_timeout)
            if not devices:
                logger.warning("No medical devices found, retrying in 30s...")
                await asyncio.sleep(30)
                continue
            
            logger.info(f"Found {len(devices)} medical device(s)")
            
            # Collect vitals
            vitals = await hub.collect_all_devices()
            
            # Publish to MQTT
            if vitals.readings:
                hub.publish_vitals(vitals)
                logger.info(f"Collected {len(vitals.readings)} vital signs")
            else:
                logger.warning("No vital signs collected")
            
            # Wait before next cycle
            logger.info(f"Waiting {args.cycle_interval}s before next collection cycle")
            await asyncio.sleep(args.cycle_interval)
    
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}")
    finally:
        hub.disconnect_mqtt()
        logger.info("Medical Hub shutdown complete")


def main():
    """Entry point"""
    parser = argparse.ArgumentParser(
        description='SPECTER Medical Hub — Bluetooth vital signs aggregator'
    )
    parser.add_argument('--mqtt-host', default='192.168.1.1', 
                        help='MQTT broker host (default: 192.168.1.1)')
    parser.add_argument('--mqtt-port', type=int, default=1883,
                        help='MQTT broker port (default: 1883)')
    parser.add_argument('--scan-timeout', type=int, default=10,
                        help='Bluetooth scan timeout in seconds (default: 10)')
    parser.add_argument('--cycle-interval', type=int, default=60,
                        help='Collection cycle interval in seconds (default: 60)')
    parser.add_argument('--patient-id', default='default',
                        help='Patient ID for MQTT topics (default: default)')
    
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info("SPECTER Medical Hub v1.2.0 starting")
    logger.info(f"MQTT: {args.mqtt_host}:{args.mqtt_port}")
    logger.info(f"Scan timeout: {args.scan_timeout}s")
    logger.info(f"Collection interval: {args.cycle_interval}s")
    all_device_types = set(BluetoothDeviceConfig.DEVICES)
    blocked = sorted(all_device_types - VERIFIED_DEVICE_TYPES)
    if blocked:
        logger.warning(
            f"UNVERIFIED DEVICE PARSERS BLOCKED (no vitals will publish for these): "
            f"{', '.join(blocked)} - see docs/MANUAL.md Part 7.2. Set "
            f"SPECTER_VERIFIED_BLE_DEVICES once confirmed against real hardware."
        )
    if VERIFIED_DEVICE_TYPES:
        logger.info(f"Verified device types active: {', '.join(sorted(VERIFIED_DEVICE_TYPES))}")
    logger.info("=" * 60)
    
    # Run async main loop
    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()
