#!/usr/bin/env python3
"""
SPECTER Medical Hub — Bluetooth Vital Signs Aggregator
Raspberry Pi Zero 2W

Collects data from multiple Bluetooth medical devices:
- Omron BP7450 (blood pressure)
- Masimo MightySat (pulse oximetry)
- Braun ThermoScan 7 (temperature)
- Contour Next One (glucose)
- AliveCor KardiaMobile 6L (ECG)

Publishes timestamped readings to MQTT broker on Node 1 (192.168.1.1)
MQTT topics: shtf/medical/vitals/* 

Author: SPECTER Build Team
Date: August 2026
Version: 1.1.0
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
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except (AttributeError, TypeError):
        return _mqtt_client(client_id)
# ---------------------------------------------------------------------------

# --- MQTT auth --------------------------------------------------------------
# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "medical_hub"
# account: it can only read shtf/medical/hub/command/# and write vitals.
# Fallback values below are used only when specter.json has no
# mqtt.services.medical_hub entry (e.g. running outside a real install).
MQTT_SERVICE_KEY      = "medical_hub"
MQTT_DEFAULT_USERNAME = "specter-medical-hub"
MQTT_DEFAULT_PASSWORD = "specter-change-me"


def _mqtt_credentials() -> tuple:
    """Read this service's MQTT username/password from
    /etc/specter/specter.json (written by the installer) if available,
    else fall back to the documented default."""
    try:
        cfg = json.loads(Path("/etc/specter/specter.json").read_text())
        mqtt_cfg = cfg.get("mqtt", {})
        service_cfg = mqtt_cfg.get("services", {}).get(MQTT_SERVICE_KEY, {})
        return (
            service_cfg.get("username", mqtt_cfg.get("username", MQTT_DEFAULT_USERNAME)),
            service_cfg.get("password", mqtt_cfg.get("password", MQTT_DEFAULT_PASSWORD)),
        )
    except Exception:
        return MQTT_DEFAULT_USERNAME, MQTT_DEFAULT_PASSWORD
# ---------------------------------------------------------------------------
import asyncio
from bleak import BleakClient, BleakScanner

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/specter_medical_hub.log'),
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
#   - braun_thermoscan / contour_next_one: the real standard
#     Temperature Measurement (0x2A1C) and Glucose Measurement (0x2A18)
#     characteristics both use IEEE-11073 float encodings inside a
#     flags-dependent variable-length structure, not the fixed-width raw
#     integers these parsers assume.
#   - collect_from_device() also only reads the FIRST characteristic
#     listed per device ("Simplified: use first reading") and feeds its
#     raw bytes to a parser expecting several characteristics' worth of
#     combined data - a second, independent bug on top of the protocol
#     mismatch above.
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
            'service_uuid': '180a',
            'characteristic_uuids': {
                'glucose': '2a18'
            },
            'parser': 'parse_contour_glucometer'
        },
    }


# ============================================================================
# BLUETOOTH DATA PARSERS
# ============================================================================

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
        Parse Contour Next One glucose meter data
        Format: glucose in mg/dL (2 bytes, little-endian)
        """
        try:
            if len(data) < 2:
                return {}
            
            glucose = int.from_bytes(data[0:2], 'little')
            
            return {
                'glucose_mg_dl': glucose
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
    
    def __init__(self, mqtt_host: str = '192.168.1.1', mqtt_port: int = 1883):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_client = _mqtt_client()
        self.mqtt_client.username_pw_set(*_mqtt_credentials())
        self.mqtt_connected = False
        self.discovered_devices = {}
        self.parser = MedicalDeviceParser()
        self.patient_id = "default"
        
        # MQTT callbacks
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
        self.mqtt_client.on_message = self._on_mqtt_message
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            self.mqtt_connected = True
            logger.info(f"MQTT connected to {self.mqtt_host}:{self.mqtt_port}")
            self.mqtt_client.subscribe("shtf/medical/hub/command/#")
        else:
            logger.error(f"MQTT connection failed with code {rc}")
            self.mqtt_connected = False
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback"""
        self.mqtt_connected = False
        if rc != 0:
            logger.warning(f"MQTT disconnected unexpectedly with code {rc}")
    
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
            for device, adv_data in devices.items():
                device_name = device.name or device.address
                
                # Check if device matches any medical device patterns
                for dev_type, config in BluetoothDeviceConfig.DEVICES.items():
                    if config['name_pattern'].lower() in device_name.lower():
                        self.discovered_devices[device.address] = {
                            'name': device_name,
                            'address': device.address,
                            'type': dev_type,
                            'rssi': device.rssi,
                            'object': device
                        }
                        logger.info(f"Found {dev_type}: {device_name} ({device.address}) RSSI: {device.rssi}")
            
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
                        'ecg_rhythm': 'classification'
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
    logger.info("SPECTER Medical Hub v1.0.0 starting")
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
