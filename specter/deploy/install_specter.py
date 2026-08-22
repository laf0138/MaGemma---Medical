#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    SPECTER MONSTER — FIELD INSTALLER                        ║
║                         install_specter.py                                  ║
║                                                                             ║
║  Zero-interaction field deployment script.                                  ║
║  • Probes hardware (CPU, RAM, storage, USB, I2C, network)                   ║
║  • Installs all system packages, Python venv, and SPECTER services          ║
║  • Writes all config files, systemd units, and cron jobs                    ║
║  • Starts all services                                                      ║
║  • Generates post-install report with any issues found                      ║
║                                                                             ║
║  Run as root:  sudo python3 install_specter.py                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .dashboard_tls import nginx_site_config, openssl_certificate_command
except ImportError:  # Direct execution: python3 install_specter.py
    from dashboard_tls import nginx_site_config, openssl_certificate_command

# ─── Version ──────────────────────────────────────────────────────────────────
VERSION = "1.2.0"
SPECTER_USER  = "specter"
SPECTER_GROUP = "specter"

# ─── MQTT broker credentials ──────────────────────────────────────────────────
# The broker (Mosquitto) requires authentication - see docs/MANUAL.md Part 3.3.
# Every node's install run must resolve to the SAME credentials, since there is
# no central secret store on an air-gapped mesh: export SPECTER_MQTT_PASSWORD
# before running this installer on every node (Node 1 and every node cloned
# from it via clone_deploy.py will then agree automatically).
#
# Each service gets its own MQTT username and a password DERIVED from the one
# master password, so independent installer runs still agree without needing
# to distribute ten separate secrets - and so a leaked service credential
# doesn't hand over every other service's credential too. The "operator"
# entry is the broad, human-facing credential used for manual mosquitto_pub/
# mosquitto_sub troubleshooting and the CLI tools (health_check.sh,
# disk_report.sh, specter_trauma_monitor.py) that aren't systemd services.
MQTT_DEFAULT_USERNAME = "specter-operator"
MQTT_DEFAULT_PASSWORD = "specter-change-me"
MQTT_USERNAME = os.environ.get("SPECTER_MQTT_USER", MQTT_DEFAULT_USERNAME)
MQTT_PASSWORD = os.environ.get("SPECTER_MQTT_PASSWORD", MQTT_DEFAULT_PASSWORD)
DASHBOARD_USERNAME = os.environ.get("SPECTER_DASHBOARD_USER", "specter-admin")
DASHBOARD_PASSWORD = os.environ.get("SPECTER_DASHBOARD_PASSWORD", "")

# service key -> (mqtt username, ACL rules). Rules are (permission, topic)
# pairs using standard MQTT ACL wildcards (+ single level, # multi-level).
MQTT_SERVICES: dict[str, dict] = {
    "trauma": {
        "username": "specter-trauma",
        "acl": [
            ("read", "shtf/trauma/command/#"),
            ("write", "shtf/trauma/scene"),
            ("write", "shtf/trauma/casualty/#"),
            ("write", "shtf/trauma/alert"),
            ("write", "shtf/trauma/protocol"),
        ],
    },
    "ward": {
        "username": "specter-ward",
        "acl": [
            ("read", "shtf/ward/command/#"),
            ("write", "shtf/ward/episode"),
            ("write", "shtf/ward/episode/#"),
            ("write", "shtf/ward/alert"),
        ],
    },
    "mesh": {
        "username": "specter-mesh",
        "acl": [
            # Read-only on every alert source it relays - it can observe
            # trauma/ward/system alarms but cannot write into any of them,
            # so a leaked mesh credential can't forge a casualty or ward
            # state. shtf/mesh/command/# is its own narrow inbound channel
            # for operator-composed outbound mesh messages.
            ("read", "shtf/trauma/alert"),
            ("read", "shtf/ward/alert"),
            ("read", "shtf/system/alarm"),
            ("read", "shtf/mesh/command/#"),
            ("write", "shtf/mesh/status"),
            ("write", "shtf/mesh/sent"),
            ("write", "shtf/mesh/inbound"),
        ],
    },
    "medical_ai": {
        "username": "specter-medical-ai",
        "acl": [
            ("read", "shtf/medical/vitals/#"),
            ("read", "shtf/medical/query/#"),
            ("read", "shtf/medical/profile/#"),
            ("write", "shtf/medical/diagnosis/#"),
            ("write", "shtf/medical/ai/status"),
            # MAP/pulse pressure/shock index/NEWS2/qSOFA/fever burden/
            # delta-from-baseline (VitalsCache.derived(), see
            # medical/clinical_scores.py) - a distinct topic from
            # diagnosis/# above, so it needs its own explicit grant.
            ("write", "shtf/medical/derived/#"),
        ],
    },
    "medical_hub": {
        "username": "specter-medical-hub",
        "acl": [
            ("read", "shtf/medical/hub/command/#"),
            ("write", "shtf/medical/vitals/#"),
        ],
    },
    "coordinator": {
        "username": "specter-coordinator",
        "acl": [
            # Coordinating/monitoring the whole mesh is this service's job -
            # broad READ is intentional. It cannot write outside these three
            # topics, so it can't forge a trauma or medical command.
            ("read", "shtf/#"),
            ("write", "shtf/system/alarm"),
            ("write", "shtf/system/state"),
            ("write", "shtf/system/heartbeat"),
        ],
    },
    "sdr_control": {
        "username": "specter-sdr-control",
        "acl": [
            ("read", "shtf/sdr/cmd"),
            ("write", "shtf/sdr/status"),
            ("write", "shtf/system/alarm"),
        ],
    },
    "thermal": {
        "username": "specter-thermal",
        "acl": [
            ("write", "shtf/system/thermal"),
            ("write", "shtf/system/alarm"),
        ],
    },
    "dashboard": {
        "username": "specter-dashboard",
        "acl": [
            # Broad READ drives the UI. The one narrow WRITE exception is
            # WARD's authenticated, allowlisted browser command path.
            ("read", "shtf/#"),
            ("write", "shtf/ward/command/#"),
        ],
    },
    "library_api": {
        "username": "specter-library-api",
        "acl": [
            ("read", "shtf/library/ask"),
            ("write", "shtf/library/response"),
            ("write", "shtf/library/status"),
        ],
    },
    "rx_buffer": {
        "username": "specter-rx-buffer",
        "acl": [
            ("read", "shtf/rx/trigger"),
            ("write", "shtf/rx/status"),
            ("write", "shtf/rx/event"),
            ("write", "shtf/rx/recording"),
            ("write", "shtf/system/alarm"),
        ],
    },
}


def _derive_service_password(master_password: str, service: str) -> str:
    """Deterministic per-service password derived from the one master
    secret, so every node's independent install run agrees without a
    central secret store. Not a substitute for a real per-node secret
    manager - see docs/MANUAL.md Part 7.2."""
    import hashlib
    return hashlib.sha256(f"{master_password}:{service}".encode()).hexdigest()[:24]


def _hash_dashboard_password(password: str) -> str:
    """Create the PBKDF2 format consumed by dashboard_server.py."""
    import hashlib
    if len(password) < 12:
        raise ValueError("dashboard password must contain at least 12 characters")
    iterations = 600_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"

# ─── Install paths ────────────────────────────────────────────────────────────
BASE_DIR    = Path("/opt/specter")
CONFIG_DIR  = Path("/etc/specter")
LOG_DIR     = Path("/var/log/specter")
RUN_DIR     = Path("/run/specter")
RECORD_DIR  = Path("/mnt/specter/live/recordings")
ARCHIVE_DIR = Path("/mnt/specter/archive")
VENV_DIR    = BASE_DIR / "venv"
SCRIPTS_DIR = BASE_DIR / "scripts"
TLS_DIR     = CONFIG_DIR / "tls"
DASHBOARD_CERT = TLS_DIR / "dashboard.crt"
DASHBOARD_KEY  = TLS_DIR / "dashboard.key"
DASHBOARD_FINGERPRINT = TLS_DIR / "dashboard.sha256"
NGINX_SITE = Path("/etc/nginx/sites-available/specter-dashboard")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled/specter-dashboard")

SRC_DIR = Path(__file__).parent   # directory containing this installer

# ─── Minimum hardware requirements ────────────────────────────────────────────
MIN_RAM_GB   = 4
MIN_DISK_GB  = 16
MIN_CPU_CORES = 2

# ─── System packages ──────────────────────────────────────────────────────────
APT_PACKAGES = [
    # Audio
    "portaudio19-dev", "alsa-utils", "pulseaudio",
    # SDR
    "rtl-sdr", "librtlsdr-dev", "libusb-1.0-0-dev",
    "gnuradio", "gr-osmosdr",
    # Python build deps
    "python3", "python3-pip", "python3-venv", "python3-dev",
    "python3-numpy", "python3-scipy",
    # Network / MQTT
    "mosquitto", "mosquitto-clients",
    # GPS
    "gpsd", "gpsd-clients", "chrony",
    # Utilities
    "git", "curl", "wget", "usbutils", "i2c-tools",
    "screen", "tmux", "htop", "iotop", "lsof",
    "jq", "bc", "rsync",
    # Web server (dashboard)
    "nginx", "openssl",
    # Build tools
    "build-essential", "cmake", "pkg-config",
    # SoapySDR
    "libsoapysdr-dev", "soapysdr-tools",
]

# Pinned where this repo's own test suite (requirements-dev.txt) actually
# exercises the package - an unpinned install months from now can pull a
# materially different, untested version onto a field kit. soundfile,
# pyaudio, pyserial, and gps3 are real Pi-hardware dependencies this repo's
# test suite does not exercise, so they remain explicitly unpinned pending
# a real-hardware verified-version pass.
PIP_PACKAGES = [
    "numpy==2.4.6", "scipy==1.17.1", "soundfile", "pyaudio",
    "paho-mqtt==2.1.0", "flask==3.1.3", "flask-socketio==5.6.1",
    "requests==2.33.1",
    "pyserial", "gps3",
    "matplotlib==3.11.1",
    "meshtastic==2.7.11",
]

# ─── Logger ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("specter.install")

# ─── Data classes ─────────────────────────────────────────────────────────────
@dataclass
class HardwareReport:
    cpu_model:      str  = "unknown"
    cpu_cores:      int  = 0
    ram_gb:         float = 0.0
    disk_gb:        float = 0.0
    disk_free_gb:   float = 0.0
    os_info:        str  = "unknown"
    hostname:       str  = "unknown"
    ip_address:     str  = "unknown"
    pi_model:       str  = "unknown"
    # SDR hardware
    hackrf_present:   bool = False
    pluto_present:    bool = False
    kraken_present:   bool = False
    rtlsdr_present:   bool = False
    # Peripherals
    gps_present:      bool = False
    i2c_devices:      list = field(default_factory=list)
    usb_devices:      list = field(default_factory=list)
    audio_devices:    list = field(default_factory=list)
    network_ifaces:   list = field(default_factory=list)
    # Flags
    meets_minimum:    bool = False
    warnings:         list = field(default_factory=list)
    missing_hardware: list = field(default_factory=list)

@dataclass
class InstallReport:
    hardware:           HardwareReport = field(default_factory=HardwareReport)
    packages_installed: list = field(default_factory=list)
    packages_failed:    list = field(default_factory=list)
    services_started:   list = field(default_factory=list)
    services_failed:    list = field(default_factory=list)
    files_deployed:     list = field(default_factory=list)
    warnings:           list = field(default_factory=list)
    errors:             list = field(default_factory=list)
    duration_sec:       float = 0.0

# ─── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd: list[str], check: bool = True, capture: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture,
                          text=True, timeout=timeout)

def run_shell(cmd: str, check: bool = False) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip()

def banner(text: str) -> None:
    width = 70
    print("\n" + "═" * width)
    print(f"  {text}")
    print("═" * width)

def step(text: str) -> None:
    print(f"  ▶  {text}")

def ok(text: str) -> None:
    print(f"  ✓  {text}")

def warn(text: str) -> None:
    print(f"  ⚠  {text}")
    log.warning(text)

def err(text: str) -> None:
    print(f"  ✗  {text}")
    log.error(text)

# ─── Phase 1: Hardware Probe ──────────────────────────────────────────────────

def probe_hardware() -> HardwareReport:
    banner("PHASE 1 — HARDWARE PROBE")
    hw = HardwareReport()

    # OS / hostname
    hw.os_info  = platform.platform()
    hw.hostname = socket.gethostname()
    hw.ip_address = run_shell("hostname -I | awk '{print $1}'")

    # Pi model
    try:
        model_path = Path("/proc/device-tree/model")
        if model_path.exists():
            hw.pi_model = model_path.read_text().rstrip("\x00")
    except Exception:
        pass

    step(f"Host:    {hw.hostname} ({hw.ip_address})")
    step(f"OS:      {hw.os_info}")
    step(f"Model:   {hw.pi_model or 'Not a Pi / unknown'}")

    # CPU
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
        models = re.findall(r"model name\s+:\s+(.+)", cpuinfo)
        hw.cpu_model = models[0].strip() if models else platform.processor()
        hw.cpu_cores = os.cpu_count() or 0
    except Exception:
        hw.cpu_cores = os.cpu_count() or 0
    step(f"CPU:     {hw.cpu_model}  ({hw.cpu_cores} cores)")

    # RAM
    try:
        meminfo = Path("/proc/meminfo").read_text()
        m = re.search(r"MemTotal:\s+(\d+)", meminfo)
        if m:
            hw.ram_gb = int(m.group(1)) / (1024 ** 2)
    except Exception:
        pass
    step(f"RAM:     {hw.ram_gb:.1f} GB")

    # Disk
    try:
        st = os.statvfs("/")
        hw.disk_gb      = (st.f_blocks * st.f_frsize) / (1024 ** 3)
        hw.disk_free_gb = (st.f_bfree  * st.f_frsize) / (1024 ** 3)
    except Exception:
        pass
    step(f"Disk:    {hw.disk_gb:.1f} GB total, {hw.disk_free_gb:.1f} GB free")

    # Network interfaces
    try:
        iface_out = run_shell("ip -o link show | awk '{print $2}' | tr -d ':'")
        hw.network_ifaces = [i for i in iface_out.splitlines() if i not in ("lo",)]
    except Exception:
        pass
    step(f"Network: {', '.join(hw.network_ifaces) or 'none detected'}")

    # USB devices
    try:
        lsusb = run_shell("lsusb 2>/dev/null")
        hw.usb_devices = [l.strip() for l in lsusb.splitlines() if l.strip()]
    except Exception:
        pass

    # SDR detection via USB IDs
    usb_blob = "\n".join(hw.usb_devices).lower()

    # HackRF One: 1d50:6089
    hw.hackrf_present = "1d50:6089" in usb_blob or "hackrf" in usb_blob
    # PlutoSDR / ADALM-Pluto: 0456:b673
    hw.pluto_present  = "0456:b673" in usb_blob or "pluto" in usb_blob or "analog devices" in usb_blob
    # KrakenSDR: built on RTL2832 — 5 × 0bda:2838
    kraken_count = usb_blob.count("0bda:2838")
    hw.kraken_present = kraken_count >= 5
    # RTL-SDR v4: 0bda:2838 (at least 1, not necessarily 5)
    hw.rtlsdr_present = "0bda:2838" in usb_blob or "realtek" in usb_blob

    ok(f"HackRF:    {'FOUND' if hw.hackrf_present else 'NOT DETECTED'}")
    ok(f"PlutoSDR:  {'FOUND' if hw.pluto_present  else 'NOT DETECTED'}")
    ok(f"KrakenSDR: {'FOUND (%d RTL devices)' % kraken_count if hw.kraken_present else 'NOT DETECTED'}")
    ok(f"RTL-SDR:   {'FOUND' if hw.rtlsdr_present else 'NOT DETECTED'}")

    # GPS (gpsd socket or /dev/ttyACM0 / /dev/ttyUSB0)
    gps_devs = list(Path("/dev").glob("ttyACM*")) + list(Path("/dev").glob("ttyUSB*"))
    hw.gps_present = len(gps_devs) > 0
    step(f"GPS:       {'FOUND (' + str(gps_devs[0]) + ')' if hw.gps_present else 'NOT DETECTED'}")

    # I2C devices
    try:
        i2c_out = run_shell("i2cdetect -y 1 2>/dev/null || true")
        # Extract hex addresses
        hw.i2c_devices = re.findall(r"\b([0-9a-f]{2})\b", i2c_out)
    except Exception:
        pass
    if hw.i2c_devices:
        step(f"I2C:       {', '.join(hw.i2c_devices)}")

    # Audio devices
    try:
        aplay = run_shell("aplay -l 2>/dev/null")
        hw.audio_devices = re.findall(r"card \d+.*", aplay)
    except Exception:
        pass

    # ── Minimum requirements check ─────────────────────────────────────────
    hw.meets_minimum = True

    if hw.cpu_cores < MIN_CPU_CORES:
        hw.meets_minimum = False
        hw.warnings.append(f"CPU cores {hw.cpu_cores} < minimum {MIN_CPU_CORES}")

    if hw.ram_gb < MIN_RAM_GB:
        hw.meets_minimum = False
        hw.warnings.append(f"RAM {hw.ram_gb:.1f}GB < minimum {MIN_RAM_GB}GB")

    if hw.disk_gb < MIN_DISK_GB:
        hw.meets_minimum = False
        hw.warnings.append(f"Disk {hw.disk_gb:.1f}GB < minimum {MIN_DISK_GB}GB")

    if not hw.hackrf_present:
        hw.missing_hardware.append("HackRF One — required for HF coverage (no substitution)")
    if not hw.pluto_present:
        hw.missing_hardware.append("PlutoSDR (ADALM-Pluto) — required for 70MHz–6GHz TX/RX")
    if not hw.kraken_present:
        hw.missing_hardware.append("KrakenSDR — required for 5-channel coherent direction finding")
    if not hw.rtlsdr_present:
        hw.missing_hardware.append("RTL-SDR v4 — required for general monitoring")
    if not hw.gps_present:
        hw.missing_hardware.append("GPS (u-blox MAX-M8Q) — required for FT8 time sync (±1s)")

    return hw

# ─── Phase 2: System packages ─────────────────────────────────────────────────

def install_packages(report: InstallReport) -> None:
    banner("PHASE 2 — SYSTEM PACKAGES")
    step("Updating apt package lists ...")
    try:
        run(["apt-get", "update", "-qq"], timeout=180)
    except Exception as e:
        warn(f"apt-get update failed: {e}")

    for pkg in APT_PACKAGES:
        try:
            step(f"Installing {pkg} ...")
            run(["apt-get", "install", "-y", "-qq", pkg], timeout=300)
            report.packages_installed.append(pkg)
        except Exception as e:
            warn(f"Failed to install {pkg}: {e}")
            report.packages_failed.append(pkg)

    ok(f"Packages: {len(report.packages_installed)} installed, "
       f"{len(report.packages_failed)} failed")

# ─── Phase 3: User and directories ────────────────────────────────────────────

def create_user_and_dirs(report: InstallReport) -> None:
    banner("PHASE 3 — USER & DIRECTORIES")

    # Create specter system user
    result = subprocess.run(["id", SPECTER_USER], capture_output=True)
    if result.returncode != 0:
        run(["useradd", "-r", "-s", "/bin/false",
             "-G", "audio,dialout,plugdev,gpio", SPECTER_USER])
        ok(f"Created system user: {SPECTER_USER}")
    else:
        ok(f"User exists: {SPECTER_USER}")

    # Create directories
    dirs = [
        BASE_DIR, CONFIG_DIR, LOG_DIR, SCRIPTS_DIR,
        RECORD_DIR, ARCHIVE_DIR,
        Path("/mnt/specter/live"),
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        step(f"Dir: {d}")
        report.files_deployed.append(str(d))

    # Ownership
    for d in [BASE_DIR, LOG_DIR, RECORD_DIR, ARCHIVE_DIR]:
        run(["chown", "-R", f"{SPECTER_USER}:{SPECTER_USER}", str(d)])

    ok("Directories created and ownership set")

# ─── Phase 4: Python venv ─────────────────────────────────────────────────────

def create_venv(report: InstallReport) -> None:
    banner("PHASE 4 — PYTHON VENV")
    if not (VENV_DIR / "bin" / "python").exists():
        step(f"Creating venv at {VENV_DIR} ...")
        run([sys.executable, "-m", "venv", str(VENV_DIR)], timeout=60)
    else:
        step("Venv already exists, upgrading ...")

    pip = VENV_DIR / "bin" / "pip"
    step("Upgrading pip ...")
    run([str(pip), "install", "--upgrade", "pip"], timeout=120)

    for pkg in PIP_PACKAGES:
        try:
            step(f"pip install {pkg} ...")
            run([str(pip), "install", pkg], timeout=300)
            report.packages_installed.append(f"pip:{pkg}")
        except Exception as e:
            warn(f"pip install {pkg} failed: {e}")
            report.packages_failed.append(f"pip:{pkg}")

    ok("Python venv ready")

# ─── Phase 5: Deploy SPECTER scripts ─────────────────────────────────────────

def deploy_scripts(report: InstallReport) -> None:
    banner("PHASE 5 — DEPLOYING SPECTER SCRIPTS")

    py_files = list(SRC_DIR.glob("**/*.py")) + \
               list(SRC_DIR.glob("**/*.sh")) + \
               list(SRC_DIR.glob("**/*.html")) + \
               list(SRC_DIR.glob("**/*.js")) + \
               list(SRC_DIR.glob("**/*.json")) + \
               list(SRC_DIR.glob("**/*.md"))

    for src in py_files:
        # Skip the installer itself
        if src.name == "install_specter.py":
            continue
        rel = src.relative_to(SRC_DIR)
        dest = BASE_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if src.suffix == ".py" or src.suffix == ".sh":
            os.chmod(dest, 0o755)
        step(f"Deployed: {rel} → {dest}")
        report.files_deployed.append(str(dest))

    ok(f"Deployed {len(report.files_deployed)} files")

# ─── Phase 6: Config files ────────────────────────────────────────────────────

def write_configs(report: InstallReport) -> None:
    banner("PHASE 6 — WRITING CONFIG FILES")

    # MQTT / Mosquitto
    mqtt_passwd_file = "/etc/mosquitto/specter_passwd"
    mqtt_acl_file    = "/etc/mosquitto/specter_acl"
    mosquitto_conf = CONFIG_DIR / "mosquitto.conf"
    mosquitto_conf.write_text(textwrap.dedent(f"""\
        # SPECTER MQTT Broker Config
        listener 1883 0.0.0.0
        allow_anonymous false
        password_file {mqtt_passwd_file}
        acl_file {mqtt_acl_file}
        persistence true
        persistence_location /var/lib/mosquitto/
        log_dest file /var/log/specter/mosquitto.log
        log_type all
        connection_messages true
    """))
    shutil.copy2(mosquitto_conf, "/etc/mosquitto/conf.d/specter.conf")
    step("Mosquitto config written")

    # Resolve every service's credential up front (operator role + one
    # entry per systemd-managed service), then write the password file and
    # the matching ACL file so each username can only touch its own topics.
    resolved_services = {
        key: {
            "username": svc["username"],
            "password": _derive_service_password(MQTT_PASSWORD, key),
            "acl": svc["acl"],
        }
        for key, svc in MQTT_SERVICES.items()
    }

    try:
        subprocess.run(
            ["mosquitto_passwd", "-b", "-c", mqtt_passwd_file, MQTT_USERNAME, MQTT_PASSWORD],
            check=True, capture_output=True,
        )
        for svc in resolved_services.values():
            subprocess.run(
                ["mosquitto_passwd", "-b", mqtt_passwd_file, svc["username"], svc["password"]],
                check=True, capture_output=True,
            )
        os.chmod(mqtt_passwd_file, 0o640)
        shutil.chown(mqtt_passwd_file, group="mosquitto")
        step(f"Mosquitto password file written ({1 + len(resolved_services)} accounts)")
    except Exception as e:
        warn(f"Could not generate mosquitto password file: {e}")

    acl_lines = [
        "# SPECTER MQTT ACLs - generated by install_specter.py, do not hand-edit.",
        "# Re-run the installer to regenerate after changing MQTT_SERVICES.",
        "",
        f"user {MQTT_USERNAME}",
        "topic readwrite shtf/#",
        "",
    ]
    for svc in resolved_services.values():
        acl_lines.append(f"user {svc['username']}")
        for permission, topic in svc["acl"]:
            acl_lines.append(f"topic {permission} {topic}")
        acl_lines.append("")
    Path(mqtt_acl_file).write_text("\n".join(acl_lines))
    try:
        os.chmod(mqtt_acl_file, 0o640)
        shutil.chown(mqtt_acl_file, group="mosquitto")
    except Exception as e:
        warn(f"Could not set ACL file permissions: {e}")
    step("Mosquitto ACL file written")

    if MQTT_PASSWORD == MQTT_DEFAULT_PASSWORD:
        warn(
            "Using the DEFAULT MQTT password - every node must be installed with "
            "the SAME password, so change it by exporting SPECTER_MQTT_PASSWORD "
            "before running this installer on every node. See docs/MANUAL.md Part 3.3."
        )

    # SPECTER main config
    specter_conf = {
        "version": VERSION,
        "mqtt": {
            "broker": "192.168.1.1",
            "port": 1883,
            "username": MQTT_USERNAME,  # "operator" role - CLI tools, manual troubleshooting
            "password": MQTT_PASSWORD,
            "services": {
                key: {"username": svc["username"], "password": svc["password"]}
                for key, svc in resolved_services.items()
            },
        },
        "network": {
            "pi1_ip": "192.168.1.1",
            "pi2_ip": "192.168.1.2",
            "pi3_ip": "192.168.1.3",
            "pi4_ip": "192.168.1.4",
        },
        "sdr": {
            "hackrf_enabled": True,
            "pluto_enabled": True,
            "kraken_enabled": True,
            "rtlsdr_enabled": True,
        },
        "recording": {
            "output_dir": str(RECORD_DIR),
            "archive_dir": str(ARCHIVE_DIR),
            "buffer_seconds": 5,
            "post_seconds": 15,
            "max_record_seconds": 300,
        },
        "gps": {
            "device": "/dev/ttyACM0",
            "baud": 9600,
            "gpsd_host": "localhost",
            "gpsd_port": 2947,
        },
        "dashboard": {
            # The application server is intentionally unreachable from the
            # LAN. Nginx is the sole public entry point and terminates TLS.
            "host": "127.0.0.1",
            "port": 5000,
            "update_interval_ms": 250,
            "secret_key": secrets.token_hex(32),
            "cookie_secure": True,
            "external_url": f"https://{report.hardware.ip_address}",
            "auth": {
                "username": DASHBOARD_USERNAME,
                "password_hash": _hash_dashboard_password(DASHBOARD_PASSWORD),
            },
        },
        "thermal": {
            "idle_max_c": 45,
            "throttle_c": 75,
            "emergency_shutdown_c": 85,
        },
    }
    conf_path = CONFIG_DIR / "specter.json"
    conf_path.write_text(json.dumps(specter_conf, indent=2))
    # Carries the MQTT password - not world-readable, but owned by the
    # service user so specter-*.service units (which run as User=specter)
    # can still read it. CONFIG_DIR itself is intentionally left out of the
    # Phase 3 chown -R pass, so this file needs its own ownership fix.
    shutil.chown(conf_path, user=SPECTER_USER, group=SPECTER_GROUP)
    os.chmod(conf_path, 0o640)
    step(f"Main config: {conf_path}")
    report.files_deployed.append(str(conf_path))

    # chrony GPS time sync
    chrony_snip = textwrap.dedent("""\
        # SPECTER GPS time source
        refclock SHM 0 offset 0.5 delay 0.2 refid GPS
        refclock SHM 1 offset 0.0 delay 0.2 refid PPS prefer
        allow 192.168.1.0/24
    """)
    chrony_path = Path("/etc/chrony/conf.d/specter-gps.conf")
    chrony_path.parent.mkdir(exist_ok=True)
    chrony_path.write_text(chrony_snip)
    step(f"Chrony GPS config: {chrony_path}")

    ok("Config files written")

# ─── Phase 7: Dashboard TLS ───────────────────────────────────────────────────

def configure_dashboard_tls(report: InstallReport) -> None:
    """Install a stable local certificate and HTTPS-only Nginx proxy.

    The private key never leaves /etc/specter/tls. Existing complete key/cert
    pairs are preserved across reinstalls so operator trust and the published
    fingerprint do not change unexpectedly.
    """
    banner("PHASE 7 — DASHBOARD TLS")
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TLS_DIR, 0o700)

    cert_tmp = DASHBOARD_CERT.with_suffix(".crt.tmp")
    key_tmp = DASHBOARD_KEY.with_suffix(".key.tmp")
    try:
        reuse_existing = DASHBOARD_CERT.exists() and DASHBOARD_KEY.exists()
        if reuse_existing:
            try:
                # Preserve trust only when the pair is valid, matched, not
                # about to expire, and still covers this node's current IP.
                run([
                    "openssl", "x509", "-in", str(DASHBOARD_CERT),
                    "-noout", "-checkend", "86400",
                ])
                run([
                    "openssl", "x509", "-in", str(DASHBOARD_CERT),
                    "-noout", "-checkip", report.hardware.ip_address,
                ])
                cert_public = run([
                    "openssl", "x509", "-in", str(DASHBOARD_CERT),
                    "-pubkey", "-noout",
                ]).stdout
                key_public = run([
                    "openssl", "pkey", "-in", str(DASHBOARD_KEY), "-pubout",
                ]).stdout
                if cert_public != key_public:
                    raise ValueError("certificate and private key do not match")
            except Exception as exc:
                warn(f"Replacing unusable dashboard certificate: {exc}")
                reuse_existing = False

        if not reuse_existing:
            # A partial pair cannot be used safely; generate both into
            # temporary files and replace only after OpenSSL succeeds.
            cert_tmp.unlink(missing_ok=True)
            key_tmp.unlink(missing_ok=True)
            run(
                openssl_certificate_command(
                    cert_tmp, key_tmp, report.hardware.ip_address
                ),
                timeout=180,
            )
            os.chmod(cert_tmp, 0o644)
            os.chmod(key_tmp, 0o600)
            os.replace(cert_tmp, DASHBOARD_CERT)
            os.replace(key_tmp, DASHBOARD_KEY)
            step(f"Generated dashboard certificate for {report.hardware.ip_address}")
        else:
            os.chmod(DASHBOARD_CERT, 0o644)
            os.chmod(DASHBOARD_KEY, 0o600)
            step("Preserved existing dashboard certificate and private key")

        fingerprint = run([
            "openssl", "x509", "-in", str(DASHBOARD_CERT),
            "-noout", "-fingerprint", "-sha256",
        ]).stdout.strip()
        DASHBOARD_FINGERPRINT.write_text(fingerprint + "\n")
        os.chmod(DASHBOARD_FINGERPRINT, 0o644)

        NGINX_SITE.parent.mkdir(parents=True, exist_ok=True)
        NGINX_ENABLED.parent.mkdir(parents=True, exist_ok=True)
        NGINX_SITE.write_text(nginx_site_config(DASHBOARD_CERT, DASHBOARD_KEY))
        os.chmod(NGINX_SITE, 0o644)

        default_site = NGINX_ENABLED.parent / "default"
        if default_site.is_symlink():
            default_site.unlink()
        if NGINX_ENABLED.is_symlink() or NGINX_ENABLED.exists():
            if NGINX_ENABLED.is_symlink() and NGINX_ENABLED.resolve() == NGINX_SITE:
                pass
            else:
                NGINX_ENABLED.unlink()
        if not NGINX_ENABLED.exists():
            NGINX_ENABLED.symlink_to(NGINX_SITE)

        run(["nginx", "-t"])
        run(["systemctl", "enable", "nginx"])
        run(["systemctl", "restart", "nginx"])
        report.files_deployed.extend(
            str(path) for path in (
                DASHBOARD_CERT, DASHBOARD_KEY, DASHBOARD_FINGERPRINT, NGINX_SITE,
            )
        )
        ok(f"HTTPS dashboard configured; verify {DASHBOARD_FINGERPRINT}")
    except Exception as exc:
        cert_tmp.unlink(missing_ok=True)
        key_tmp.unlink(missing_ok=True)
        message = f"Dashboard TLS configuration failed: {exc}"
        err(message)
        report.errors.append(message)

# ─── Phase 8: Systemd units ───────────────────────────────────────────────────

SYSTEMD_UNITS: dict[str, str] = {}

SYSTEMD_UNITS["specter-mqtt.service"] = textwrap.dedent("""\
    [Unit]
    Description=SPECTER MQTT Broker (Mosquitto)
    After=network.target

    [Service]
    Type=simple
    ExecStart=/usr/sbin/mosquitto -c /etc/specter/mosquitto.conf
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=multi-user.target
""")

SYSTEMD_UNITS["specter-dashboard.service"] = textwrap.dedent("""\
    [Unit]
    Description=SPECTER Dashboard Server
    After=network.target specter-mqtt.service

    [Service]
    Type=simple
    User=specter
    WorkingDirectory=/opt/specter
    ExecStart=/opt/specter/venv/bin/python /opt/specter/dashboard/dashboard_server.py
    Restart=always
    RestartSec=5
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=specter-dashboard

    [Install]
    WantedBy=multi-user.target
""")

SYSTEMD_UNITS["specter-ward.service"] = textwrap.dedent("""\
    [Unit]
    Description=SPECTER Ward Module (sustained bed care)
    After=network.target specter-mqtt.service

    [Service]
    Type=simple
    User=specter
    WorkingDirectory=/opt/specter
    ExecStart=/opt/specter/venv/bin/python /opt/specter/ward/specter_ward.py --mqtt-host 192.168.1.1 --persist /var/lib/specter/ward.json
    Restart=always
    RestartSec=5
    StateDirectory=specter
    StateDirectoryMode=0755
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=specter-ward

    [Install]
    WantedBy=multi-user.target
""")

SYSTEMD_UNITS["specter-mesh.service"] = textwrap.dedent("""\
    [Unit]
    Description=SPECTER Mesh Relay (LoRa/Meshtastic internal comms + alarm notification)
    After=network.target specter-mqtt.service

    [Service]
    Type=simple
    User=specter
    Group=dialout
    WorkingDirectory=/opt/specter
    ExecStart=/opt/specter/venv/bin/python /opt/specter/mesh/specter_mesh_relay.py --mqtt-host 192.168.1.1
    # If multiple USB-serial devices are attached, pin the radio by adding:
    # --serial-port /dev/ttyUSB0
    Restart=always
    RestartSec=5
    StateDirectory=specter
    StateDirectoryMode=0755
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=specter-mesh

    [Install]
    WantedBy=multi-user.target
""")

SYSTEMD_UNITS["specter-rx-buffer.service"] = textwrap.dedent("""\
    [Unit]
    Description=SPECTER RX Rolling Capture Service
    After=network.target sound.target specter-mqtt.service

    [Service]
    Type=simple
    User=specter
    Group=audio
    WorkingDirectory=/opt/specter
    ExecStart=/opt/specter/venv/bin/python /opt/specter/services/specter_rx_ring_buffer.py run \\
        --output-dir /mnt/specter/live/recordings \\
        --trigger-file /run/specter/sdr_trigger \\
        --mqtt-broker 192.168.1.1 \\
        --log-level INFO
    Restart=always
    RestartSec=5
    RuntimeDirectory=specter
    RuntimeDirectoryMode=0755
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=specter-rx

    [Install]
    WantedBy=multi-user.target
""")

SYSTEMD_UNITS["specter-mqtt-coordinator.service"] = textwrap.dedent("""\
    [Unit]
    Description=SPECTER MQTT Coordinator
    After=network.target specter-mqtt.service

    [Service]
    Type=simple
    User=specter
    WorkingDirectory=/opt/specter
    ExecStart=/opt/specter/venv/bin/python /opt/specter/core/mqtt_coordinator.py
    Restart=always
    RestartSec=5
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=specter-coordinator

    [Install]
    WantedBy=multi-user.target
""")

SYSTEMD_UNITS["specter-sdr-control.service"] = textwrap.dedent("""\
    [Unit]
    Description=SPECTER SDR Control Service
    After=network.target specter-mqtt.service

    [Service]
    Type=simple
    User=specter
    Group=plugdev
    WorkingDirectory=/opt/specter
    ExecStart=/opt/specter/venv/bin/python /opt/specter/core/sdr_control.py
    Restart=always
    RestartSec=5
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=specter-sdr

    [Install]
    WantedBy=multi-user.target
""")

SYSTEMD_UNITS["specter-thermal.service"] = textwrap.dedent("""\
    [Unit]
    Description=SPECTER Thermal Monitor
    After=network.target specter-mqtt.service

    [Service]
    Type=simple
    User=specter
    WorkingDirectory=/opt/specter
    ExecStart=/opt/specter/venv/bin/python /opt/specter/core/thermal_monitor.py
    Restart=always
    RestartSec=10
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=specter-thermal

    [Install]
    WantedBy=multi-user.target
""")


def install_systemd_units(report: InstallReport) -> None:
    banner("PHASE 8 — SYSTEMD UNITS")
    systemd_dir = Path("/etc/systemd/system")

    for unit_name, content in SYSTEMD_UNITS.items():
        unit_path = systemd_dir / unit_name
        unit_path.write_text(content)
        step(f"Wrote {unit_path}")
        report.files_deployed.append(str(unit_path))

    run(["systemctl", "daemon-reload"])
    ok("systemd daemon reloaded")

    for unit_name in SYSTEMD_UNITS:
        try:
            run(["systemctl", "enable", unit_name])
            run(["systemctl", "restart", unit_name])
            ok(f"Started: {unit_name}")
            report.services_started.append(unit_name)
        except Exception as e:
            warn(f"Failed to start {unit_name}: {e}")
            report.services_failed.append(unit_name)

# ─── Phase 9: USB rules & kernel mods ────────────────────────────────────────

def configure_udev(report: InstallReport) -> None:
    banner("PHASE 9 — UDEV RULES & KERNEL CONFIG")

    udev_rules = textwrap.dedent("""\
        # SPECTER SDR udev rules

        # HackRF One
        SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6089", MODE="0666", GROUP="plugdev", SYMLINK+="hackrf%n"

        # ADALM-Pluto (PlutoSDR)
        SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", MODE="0666", GROUP="plugdev", SYMLINK+="pluto%n"

        # RTL-SDR / KrakenSDR (RTL2832U)
        SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666", GROUP="plugdev"
        SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", MODE="0666", GROUP="plugdev"

        # u-blox GPS
        SUBSYSTEM=="tty", ATTRS{idVendor}=="1546", MODE="0666", GROUP="dialout", SYMLINK+="gps0"
    """)
    udev_path = Path("/etc/udev/rules.d/99-specter-sdr.rules")
    udev_path.write_text(udev_rules)
    step(f"udev rules: {udev_path}")

    # Blacklist DVB kernel modules that hijack RTL-SDR
    blacklist = textwrap.dedent("""\
        # SPECTER: prevent DVB drivers from claiming RTL-SDR devices
        blacklist dvb_usb_rtl28xxu
        blacklist rtl2832
        blacklist rtl2830
    """)
    bl_path = Path("/etc/modprobe.d/specter-rtlsdr.conf")
    bl_path.write_text(blacklist)
    step(f"Kernel module blacklist: {bl_path}")

    # Reload udev
    try:
        run(["udevadm", "control", "--reload-rules"])
        run(["udevadm", "trigger"])
        ok("udev rules reloaded")
    except Exception as e:
        warn(f"udev reload failed: {e}")

    report.files_deployed.append(str(udev_path))
    report.files_deployed.append(str(bl_path))

# ─── Phase 10: gpsd config ────────────────────────────────────────────────────

def configure_gpsd(report: InstallReport) -> None:
    banner("PHASE 10 — GPSD CONFIGURATION")
    gpsd_default = Path("/etc/default/gpsd")
    gpsd_conf = textwrap.dedent("""\
        # SPECTER gpsd configuration
        START_DAEMON="true"
        USBAUTO="true"
        DEVICES="/dev/ttyACM0"
        GPSD_OPTIONS="-n"
        GPSD_SOCKET="/var/run/gpsd.sock"
    """)
    gpsd_default.write_text(gpsd_conf)
    step(f"gpsd config: {gpsd_default}")
    try:
        run(["systemctl", "enable", "gpsd"])
        run(["systemctl", "restart", "gpsd"])
        ok("gpsd enabled and started")
        report.services_started.append("gpsd")
    except Exception as e:
        warn(f"gpsd start failed (GPS device may not be present yet): {e}")
        report.warnings.append("gpsd not started — connect GPS hardware and run: systemctl start gpsd")

# ─── Phase 11: Cron / rsync archive ──────────────────────────────────────────

def configure_cron(report: InstallReport) -> None:
    banner("PHASE 11 — MAINTENANCE CRON JOBS")
    cron_content = textwrap.dedent("""\
        # SPECTER maintenance cron jobs
        # Rotate recordings older than 7 days to archive
        0 3 * * * specter find /mnt/specter/live/recordings -name "*.wav" -mtime +7 -exec mv {} /mnt/specter/archive/ \\;

        # Delete archives older than 30 days
        30 3 * * * specter find /mnt/specter/archive -name "*.wav" -mtime +30 -delete

        # Restart services if dead (belt-and-suspenders)
        */5 * * * * root systemctl is-active specter-rx-buffer.service >/dev/null 2>&1 || systemctl restart specter-rx-buffer.service

        # Log disk usage to MQTT
        */10 * * * * specter /opt/specter/scripts/disk_report.sh
    """)
    cron_path = Path("/etc/cron.d/specter")
    cron_path.write_text(cron_content)
    step(f"Cron file: {cron_path}")
    report.files_deployed.append(str(cron_path))
    ok("Cron jobs installed")

# ─── Phase 12: Post-install report ───────────────────────────────────────────

def write_report(report: InstallReport) -> None:
    banner("SPECTER INSTALL REPORT")

    hw = report.hardware
    lines = [
        f"SPECTER Monster Field Install Report  v{VERSION}",
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"Duration:  {report.duration_sec:.1f}s",
        "",
        "── HARDWARE ─────────────────────────────────────────",
        f"  Host:       {hw.hostname} ({hw.ip_address})",
        f"  Model:      {hw.pi_model or 'unknown'}",
        f"  CPU:        {hw.cpu_model} × {hw.cpu_cores} cores",
        f"  RAM:        {hw.ram_gb:.1f} GB",
        f"  Disk:       {hw.disk_gb:.1f} GB total / {hw.disk_free_gb:.1f} GB free",
        f"  OS:         {hw.os_info}",
        "",
        "── SDR HARDWARE STATUS ───────────────────────────────",
        f"  HackRF One:  {'✓ PRESENT' if hw.hackrf_present else '✗ NOT DETECTED'}",
        f"  PlutoSDR:    {'✓ PRESENT' if hw.pluto_present  else '✗ NOT DETECTED'}",
        f"  KrakenSDR:   {'✓ PRESENT' if hw.kraken_present else '✗ NOT DETECTED'}",
        f"  RTL-SDR v4:  {'✓ PRESENT' if hw.rtlsdr_present else '✗ NOT DETECTED'}",
        f"  GPS:         {'✓ PRESENT' if hw.gps_present    else '✗ NOT DETECTED'}",
        "",
        "── SERVICES ─────────────────────────────────────────",
    ]
    for svc in report.services_started:
        lines.append(f"  ✓ {svc}")
    for svc in report.services_failed:
        lines.append(f"  ✗ {svc}  ← FAILED TO START")

    if report.packages_failed:
        lines += ["", "── FAILED PACKAGES ──────────────────────────────────"]
        for pkg in report.packages_failed:
            lines.append(f"  ✗ {pkg}")

    if hw.missing_hardware:
        lines += ["", "── MISSING / NOT DETECTED HARDWARE ──────────────────",
                  "  Connect hardware and re-run:  sudo python3 install_specter.py --hw-only"]
        for item in hw.missing_hardware:
            lines.append(f"  ⚠  {item}")

    if report.warnings:
        lines += ["", "── WARNINGS ─────────────────────────────────────────"]
        for w in report.warnings:
            lines.append(f"  ⚠  {w}")

    if report.errors:
        lines += ["", "── ERRORS ───────────────────────────────────────────"]
        for e in report.errors:
            lines.append(f"  ✗  {e}")

    lines += [
        "",
        "── NEXT STEPS ───────────────────────────────────────",
        "  Dashboard:    https://" + hw.ip_address,
        "  TLS SHA-256:  " + str(DASHBOARD_FINGERPRINT),
        "  Logs:         journalctl -u specter-* -f",
        "  Trigger RX:   touch /run/specter/sdr_trigger",
        "  Status:       systemctl status specter-*",
        "  Config:       /etc/specter/specter.json",
        "",
        "═" * 54,
    ]

    report_text = "\n".join(lines)
    report_path = Path("/opt/specter/INSTALL_REPORT.txt")
    report_path.write_text(report_text)

    print("\n" + report_text)
    print(f"\n  Full report saved to: {report_path}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print(textwrap.dedent(f"""
    ╔══════════════════════════════════════════════════════════╗
    ║         SPECTER MONSTER — FIELD INSTALLER v{VERSION}        ║
    ║    Emergency Communications Command Center Deployment    ║
    ╚══════════════════════════════════════════════════════════╝
    """))

    if os.geteuid() != 0:
        print("[ERROR] Must be run as root:  sudo python3 install_specter.py")
        return 1

    missing_secrets = []
    if not MQTT_USERNAME:
        missing_secrets.append("SPECTER_MQTT_USER (must not be empty)")
    if len(MQTT_PASSWORD) < 12 or MQTT_PASSWORD == MQTT_DEFAULT_PASSWORD:
        missing_secrets.append(
            "SPECTER_MQTT_PASSWORD (must be non-default and at least 12 characters)"
        )
    if not DASHBOARD_USERNAME:
        missing_secrets.append("SPECTER_DASHBOARD_USER (must not be empty)")
    if len(DASHBOARD_PASSWORD) < 12 or DASHBOARD_PASSWORD == MQTT_DEFAULT_PASSWORD:
        missing_secrets.append(
            "SPECTER_DASHBOARD_PASSWORD (must be non-default and at least 12 characters)"
        )
    if missing_secrets:
        print("[ERROR] Refusing an insecure installation. Set:")
        for item in missing_secrets:
            print(f"  - {item}")
        return 1

    start = time.time()
    report = InstallReport()

    report.hardware = probe_hardware()

    if not report.hardware.meets_minimum:
        print("\n[ABORT] Hardware does not meet minimum requirements:")
        for w in report.hardware.warnings:
            print(f"  ✗ {w}")
        print("\nInstall aborted. Fix hardware and retry.")
        return 1

    install_packages(report)
    create_user_and_dirs(report)
    create_venv(report)
    deploy_scripts(report)
    write_configs(report)
    configure_dashboard_tls(report)
    install_systemd_units(report)
    configure_udev(report)
    configure_gpsd(report)
    configure_cron(report)

    report.duration_sec = time.time() - start
    write_report(report)

    return 0 if not report.errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
