#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              SHTF SPECTER — CLONE & DEPLOY KIT  v1.0.0                     ║
║                          clone_deploy.py                                    ║
║                                                                              ║
║  Offline field cloning tool. No internet required.                           ║
║  Copies the fully-built SPECTER system from a master node                   ║
║  or from bundled tarballs to a new target machine.                          ║
║                                                                              ║
║  Packages available:                                                         ║
║    [1] SPECTER SDR Package     — 4× Pi 5 SDR command center                ║
║    [2] SPECTER AI Library      — Jetson Orin offline AI + Kiwix library     ║
║    [3] BOTH                    — Full SPECTER Monster deployment             ║
║                                                                              ║
║  Clone sources (auto-detected in priority order):                           ║
║    A.  Bundled tarballs in ./payloads/  (fastest, fully offline)            ║
║    B.  Network rsync from master node   (requires LAN, no internet)         ║
║                                                                              ║
║  Run as root on the TARGET machine:                                          ║
║    sudo python3 clone_deploy.py                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

VERSION      = "1.0.0"
SCRIPT_DIR   = Path(__file__).parent
PAYLOAD_DIR  = SCRIPT_DIR / "payloads"

# ─── Known master node addresses (tried in order) ─────────────────────────────
MASTER_NODES = [
    "192.168.1.1",   # Pi 1 — SPECTER SDR master
    "192.168.1.5",   # Jetson — AI library node
]

# ─── Package definitions ──────────────────────────────────────────────────────
PACKAGES = {
    "sdr": {
        "name":        "SPECTER SDR Package",
        "description": "4× Pi 5 SDR command center (HackRF, Pluto, KrakenSDR, RTL-SDR)",
        "tarball":     "specter_sdr.tar.gz",
        "rsync_src":   "192.168.1.1",
        "rsync_paths": [
            "/opt/specter/",
            "/etc/specter/",
            "/etc/systemd/system/specter-*.service",
            "/etc/systemd/system/specter-*.timer",
            "/etc/mosquitto/conf.d/specter.conf",
            "/etc/udev/rules.d/99-specter-sdr.rules",
            "/etc/modprobe.d/specter-rtlsdr.conf",
            "/etc/cron.d/specter",
        ],
        "apt_packages": [
            "mosquitto", "mosquitto-clients",
            "python3", "python3-pip", "python3-venv",
            "rtl-sdr", "librtlsdr-dev", "libusb-1.0-0-dev",
            "gnuradio", "gr-osmosdr",
            "portaudio19-dev", "alsa-utils",
            "gpsd", "gpsd-clients", "chrony",
            "git", "curl", "wget", "usbutils", "i2c-tools",
            "nginx",
        ],
        "pip_packages": [
            "numpy", "soundfile", "pyaudio", "paho-mqtt",
            "flask", "flask-socketio", "eventlet", "requests",
        ],
        "services": [
            "specter-mqtt.service",
            "specter-dashboard.service",
            "specter-rx-buffer.service",
            "specter-mqtt-coordinator.service",
            "specter-sdr-control.service",
            "specter-thermal.service",
        ],
        "venv_dir":    Path("/opt/specter/venv"),
        "app_dir":     Path("/opt/specter"),
        "user":        "specter",
        "min_ram_gb":  4,
        "min_disk_gb": 16,
    },
    "library": {
        "name":        "SPECTER AI Library",
        "description": "Jetson Orin offline AI + Kiwix (Wikipedia, WikiMed, Khan Academy, PDFs)",
        "tarball":     "specter_library.tar.gz",
        "rsync_src":   "192.168.1.5",
        "rsync_paths": [
            "/opt/specter/",
            "/etc/specter/library.json",
            "/etc/systemd/system/specter-kiwix.service",
            "/etc/systemd/system/specter-ollama.service",
            "/etc/systemd/system/specter-library-api.service",
            "/etc/systemd/system/specter-index-builder.*",
            "/mnt/specter/library/",          # ZIMs, PDFs, vector index
        ],
        "apt_packages": [
            "python3", "python3-pip", "python3-venv",
            "wget", "curl", "git", "nodejs", "npm",
            "mosquitto-clients",
            "chromium-browser",
        ],
        "pip_packages": [
            "flask", "flask-socketio", "eventlet", "paho-mqtt",
            "requests", "numpy", "chromadb",
            "sentence-transformers", "langchain",
            "langchain-community", "pypdf2", "pdfplumber",
        ],
        "services": [
            "specter-kiwix.service",
            "specter-ollama.service",
            "specter-library-api.service",
            "specter-index-builder.timer",
        ],
        "venv_dir":    Path("/opt/specter/venv"),
        "app_dir":     Path("/opt/specter"),
        "user":        "specter",
        "min_ram_gb":  6,
        "min_disk_gb": 300,   # library is large
        "extra_check": "jetson",   # warn if not a Jetson
    },
}

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("specter.clone")

# ─── Report ───────────────────────────────────────────────────────────────────
@dataclass
class CloneReport:
    target_packages: list  = field(default_factory=list)
    clone_method:    str   = "unknown"
    clone_source:    str   = "unknown"
    packages_ok:     list  = field(default_factory=list)
    packages_fail:   list  = field(default_factory=list)
    services_ok:     list  = field(default_factory=list)
    services_fail:   list  = field(default_factory=list)
    warnings:        list  = field(default_factory=list)
    errors:          list  = field(default_factory=list)
    duration_sec:    float = 0.0

# ─── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd: list[str], check: bool = True, timeout: int = 600,
        capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, timeout=timeout,
                          capture_output=capture, text=True)

def run_shell(cmd: str, timeout: int = 30) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()

def banner(text: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {text}")
    print("═" * 70)

def step(t: str)  -> None: print(f"  ▶  {t}")
def ok(t: str)    -> None: print(f"  ✓  {t}")
def warn(t: str)  -> None: print(f"  ⚠  {t}"); log.warning(t)
def fail(t: str)  -> None: print(f"  ✗  {t}"); log.error(t)

def hr() -> None:
    print("  " + "─" * 66)

# ─── MENU ─────────────────────────────────────────────────────────────────────

MENU = """
╔══════════════════════════════════════════════════════════════════╗
║          SHTF SPECTER — CLONE & DEPLOY KIT  v{ver}               ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  What would you like to install on this machine?                 ║
║                                                                  ║
║   [1]  SPECTER SDR Package                                       ║
║        Pi 5 · HackRF · Pluto · KrakenSDR · RTL-SDR              ║
║        MQTT · Dashboard · RX Buffer · Thermal Monitor            ║
║        Requires: 4+ GB RAM · 16+ GB disk                        ║
║                                                                  ║
║   [2]  SPECTER AI Library                                        ║
║        Jetson Orin · Ollama LLaMA 3.2 3B · Kiwix               ║
║        Wikipedia · WikiMed · Khan Academy · 20+ PDFs            ║
║        Requires: 6+ GB RAM · 300+ GB disk                       ║
║                                                                  ║
║   [3]  BOTH  (full SPECTER Monster deployment)                   ║
║                                                                  ║
║   [Q]  Quit                                                      ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

SOURCE_MENU = """
╔══════════════════════════════════════════════════════════════════╗
║  Clone source:                                                   ║
║                                                                  ║
║   [A]  Bundled tarballs  (./payloads/ folder — fully offline)    ║
║   [B]  Network rsync     (from master node on LAN — no internet) ║
║   [C]  Auto-detect       (try bundled first, then network)       ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

def get_menu_choice(prompt: str, valid: list[str]) -> str:
    while True:
        choice = input(f"\n{prompt} ").strip().upper()
        if choice in [v.upper() for v in valid]:
            return choice.upper()
        print(f"  Invalid choice. Enter one of: {', '.join(valid)}")

def show_main_menu() -> list[str]:
    print(MENU.format(ver=VERSION))
    choice = get_menu_choice("Enter choice [1/2/3/Q]:", ["1", "2", "3", "Q"])
    if choice == "Q":
        print("\n  Goodbye.\n")
        sys.exit(0)
    mapping = {
        "1": ["sdr"],
        "2": ["library"],
        "3": ["sdr", "library"],
    }
    return mapping[choice]

def show_source_menu() -> str:
    print(SOURCE_MENU)
    return get_menu_choice("Enter choice [A/B/C]:", ["A", "B", "C"])

# ─── Hardware probe ───────────────────────────────────────────────────────────

def probe_hardware() -> dict:
    hw: dict = {}
    try:
        m = re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text())
        hw["ram_gb"] = int(m.group(1)) / (1024**2) if m else 0
    except Exception:
        hw["ram_gb"] = 0

    try:
        st = os.statvfs("/")
        hw["disk_gb"] = (st.f_blocks * st.f_frsize) / (1024**3)
        hw["disk_free_gb"] = (st.f_bfree * st.f_frsize) / (1024**3)
    except Exception:
        hw["disk_gb"] = hw["disk_free_gb"] = 0

    hw["hostname"]  = socket.gethostname()
    hw["cpu_cores"] = os.cpu_count() or 0

    try:
        model = Path("/proc/device-tree/model").read_text().rstrip("\x00")
        hw["board_model"] = model
        hw["is_pi"]     = "raspberry pi" in model.lower()
        hw["is_jetson"] = "jetson" in model.lower()
    except Exception:
        hw["board_model"] = platform.machine()
        hw["is_pi"]     = False
        hw["is_jetson"] = False

    return hw

def check_requirements(pkg_key: str, hw: dict, report: CloneReport) -> bool:
    pkg = PACKAGES[pkg_key]
    ok_flag = True

    if hw["ram_gb"] < pkg["min_ram_gb"]:
        warn(f"RAM {hw['ram_gb']:.1f} GB < required {pkg['min_ram_gb']} GB for {pkg['name']}")
        report.warnings.append(f"Low RAM for {pkg_key}")
        ok_flag = False

    if hw["disk_free_gb"] < pkg["min_disk_gb"]:
        warn(f"Free disk {hw['disk_free_gb']:.0f} GB < required {pkg['min_disk_gb']} GB for {pkg['name']}")
        report.warnings.append(f"Low disk for {pkg_key}")
        ok_flag = False

    if pkg.get("extra_check") == "jetson" and not hw.get("is_jetson"):
        warn(f"AI Library is optimized for Jetson Orin — this machine is: {hw['board_model']}")
        warn("Install will proceed but GPU inference may not be available")
        report.warnings.append("Not a Jetson — AI Library performance reduced")

    return ok_flag

# ─── Clone: bundled tarball ───────────────────────────────────────────────────

def clone_from_tarball(pkg_key: str, report: CloneReport) -> bool:
    pkg     = PACKAGES[pkg_key]
    tarball = PAYLOAD_DIR / pkg["tarball"]

    if not tarball.exists():
        warn(f"Tarball not found: {tarball}")
        return False

    step(f"Extracting {tarball.name} ({tarball.stat().st_size / (1024**3):.1f} GB) ...")
    try:
        run(["tar", "-xzf", str(tarball), "-C", "/"], timeout=7200)
        ok(f"Extracted: {tarball.name}")
        report.clone_method = "tarball"
        report.clone_source = str(tarball)
        return True
    except Exception as e:
        fail(f"Tarball extract failed: {e}")
        report.errors.append(f"TARBALL: {pkg_key} — {e}")
        return False

# ─── Clone: network rsync ─────────────────────────────────────────────────────

def find_master_node(pkg_key: str) -> str | None:
    """Ping known master nodes, return first reachable one."""
    src_ip = PACKAGES[pkg_key]["rsync_src"]
    candidates = [src_ip] + [n for n in MASTER_NODES if n != src_ip]
    for ip in candidates:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ip],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            step(f"Master node found: {ip}")
            return ip
    return None

def clone_from_network(pkg_key: str, report: CloneReport) -> bool:
    pkg    = PACKAGES[pkg_key]
    master = find_master_node(pkg_key)

    if not master:
        fail(f"No master node reachable for {pkg['name']}")
        fail(f"  Tried: {PACKAGES[pkg_key]['rsync_src']} and {MASTER_NODES}")
        report.errors.append(f"NETWORK: {pkg_key} — no master reachable")
        return False

    step(f"rsyncing {pkg['name']} from {master} ...")
    success = True

    for src_path in pkg["rsync_paths"]:
        # Handle glob patterns
        if "*" in src_path:
            src = f"root@{master}:{src_path}"
        else:
            src = f"root@{master}:{src_path}"

        # Determine local destination
        dest = str(Path(src_path).parent) + "/"

        # Create destination
        Path(dest).mkdir(parents=True, exist_ok=True)

        step(f"  rsync: {src_path}")
        try:
            subprocess.run(
                [
                    "rsync", "-avz",
                    "--timeout=300",
                    "--progress",
                    "-e", "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10",
                    src,
                    dest,
                ],
                check=True,
                timeout=14400,   # 4 hours for library
            )
            ok(f"  ✓ {src_path}")
        except Exception as e:
            warn(f"  rsync failed: {src_path} — {e}")
            report.warnings.append(f"RSYNC: {src_path} — {e}")
            # Non-fatal — continue with remaining paths

    report.clone_method = "rsync"
    report.clone_source = master
    return success

# ─── Auto-detect clone source ─────────────────────────────────────────────────

def clone_package(pkg_key: str, source_choice: str, report: CloneReport) -> bool:
    pkg = PACKAGES[pkg_key]
    banner(f"CLONING: {pkg['name']}")

    if source_choice == "A":
        ok_flag = clone_from_tarball(pkg_key, report)
        if not ok_flag:
            fail(f"Tarball clone failed for {pkg_key}")
            return False

    elif source_choice == "B":
        ok_flag = clone_from_network(pkg_key, report)
        if not ok_flag:
            return False

    else:  # C — auto
        step("Auto-detecting clone source ...")
        tarball = PAYLOAD_DIR / pkg["tarball"]
        if tarball.exists():
            step(f"Found bundled tarball: {tarball.name}")
            ok_flag = clone_from_tarball(pkg_key, report)
        else:
            step("No bundled tarball — trying network rsync ...")
            ok_flag = clone_from_network(pkg_key, report)
        if not ok_flag:
            fail(f"Auto clone failed for {pkg_key}")
            return False

    return True

# ─── System user ──────────────────────────────────────────────────────────────

def ensure_user(username: str, report: CloneReport) -> None:
    result = subprocess.run(["id", username], capture_output=True)
    if result.returncode != 0:
        step(f"Creating system user: {username}")
        try:
            run(["useradd", "-r", "-s", "/bin/false",
                 "-G", "audio,dialout,plugdev,gpio", username])
            ok(f"User created: {username}")
        except Exception as e:
            warn(f"useradd {username}: {e}")
    else:
        ok(f"User exists: {username}")

# ─── Apt packages ─────────────────────────────────────────────────────────────

def install_apt_packages(pkg_list: list[str], report: CloneReport) -> None:
    step("Installing system packages ...")
    try:
        run(["apt-get", "update", "-qq"], timeout=300)
    except Exception as e:
        warn(f"apt-get update: {e}")

    for pkg in pkg_list:
        try:
            run(["apt-get", "install", "-y", "-qq", pkg], timeout=300)
            report.packages_ok.append(f"apt:{pkg}")
        except Exception as e:
            warn(f"apt: {pkg} — {e}")
            report.packages_fail.append(f"apt:{pkg}")

# ─── Python venv ──────────────────────────────────────────────────────────────

def ensure_venv(venv_dir: Path, pip_packages: list[str],
                report: CloneReport) -> None:
    if not (venv_dir / "bin" / "python").exists():
        step(f"Creating venv: {venv_dir}")
        run([sys.executable, "-m", "venv", str(venv_dir)], timeout=120)

    pip = venv_dir / "bin" / "pip"
    run([str(pip), "install", "--upgrade", "pip"], timeout=120)

    for pkg in pip_packages:
        try:
            run([str(pip), "install", pkg], timeout=600)
            report.packages_ok.append(f"pip:{pkg}")
        except Exception as e:
            warn(f"pip: {pkg} — {e}")
            report.packages_fail.append(f"pip:{pkg}")

# ─── Ollama (library package only) ───────────────────────────────────────────

def ensure_ollama(report: CloneReport) -> None:
    if shutil.which("ollama"):
        ok("Ollama binary present")
        # Check if models exist — if rsync copied them they're already there
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and "llama" in result.stdout.lower():
            ok("Ollama models present")
            return
        else:
            warn("Ollama present but no models — may need to pull if rsync missed model dir")
            report.warnings.append("Ollama models may be missing — check: ollama list")
    else:
        warn("Ollama binary not found after clone — attempting install")
        try:
            subprocess.run(
                "curl -fsSL https://ollama.com/install.sh | sh",
                shell=True, check=True, timeout=300,
            )
            ok("Ollama installed")
        except Exception as e:
            fail(f"Ollama install failed: {e}")
            report.errors.append(f"OLLAMA: {e}")

# ─── udev rules ──────────────────────────────────────────────────────────────

def reload_udev() -> None:
    try:
        run(["udevadm", "control", "--reload-rules"], check=False)
        run(["udevadm", "trigger"], check=False)
        ok("udev rules reloaded")
    except Exception as e:
        warn(f"udev reload: {e}")

# ─── Systemd ──────────────────────────────────────────────────────────────────

def start_services(service_list: list[str], report: CloneReport) -> None:
    step("Reloading systemd and starting services ...")
    try:
        run(["systemctl", "daemon-reload"])
    except Exception as e:
        warn(f"daemon-reload: {e}")

    for svc in service_list:
        try:
            run(["systemctl", "enable", svc], check=False)
            run(["systemctl", "restart", svc], check=False)
            time.sleep(1)
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "active":
                ok(f"Running: {svc}")
                report.services_ok.append(svc)
            else:
                warn(f"Not active: {svc} — {result.stdout.strip()}")
                report.services_fail.append(svc)
        except Exception as e:
            warn(f"Service {svc}: {e}")
            report.services_fail.append(svc)

# ─── Ownership fix ────────────────────────────────────────────────────────────

def fix_ownership(paths: list[Path], username: str) -> None:
    for p in paths:
        if p.exists():
            try:
                run(["chown", "-R", f"{username}:{username}", str(p)], check=False)
            except Exception:
                pass

# ─── Deploy one package ───────────────────────────────────────────────────────

def deploy_package(pkg_key: str, source_choice: str,
                   hw: dict, report: CloneReport) -> bool:
    pkg = PACKAGES[pkg_key]

    # Requirements check (warn but don't abort)
    check_requirements(pkg_key, hw, report)

    # Ensure system user
    ensure_user(pkg["user"], report)

    # Clone files
    if not clone_package(pkg_key, source_choice, report):
        return False

    # System packages
    banner(f"PACKAGES: {pkg['name']}")
    install_apt_packages(pkg["apt_packages"], report)

    # Venv
    if not pkg["venv_dir"].exists() or \
       not (pkg["venv_dir"] / "bin" / "python").exists():
        step("Venv not found in clone — rebuilding ...")
        ensure_venv(pkg["venv_dir"], pkg["pip_packages"], report)
    else:
        ok(f"Venv present: {pkg['venv_dir']}")

    # Ollama (AI library only)
    if pkg_key == "library":
        ensure_ollama(report)

    # udev (SDR package)
    if pkg_key == "sdr":
        reload_udev()

    # Fix ownership
    fix_ownership([pkg["app_dir"], Path("/var/log/specter"),
                   Path("/mnt/specter")], pkg["user"])

    # Start services
    banner(f"SERVICES: {pkg['name']}")
    start_services(pkg["services"], report)

    return True

# ─── Validation ───────────────────────────────────────────────────────────────

def validate(target_packages: list[str], report: CloneReport) -> None:
    banner("VALIDATION")

    for pkg_key in target_packages:
        pkg = PACKAGES[pkg_key]
        step(f"Validating: {pkg['name']}")

        # Check app dir
        if pkg["app_dir"].exists():
            ok(f"App dir: {pkg['app_dir']}")
        else:
            fail(f"App dir missing: {pkg['app_dir']}")
            report.errors.append(f"VALIDATE: {pkg_key} app_dir missing")

        # Check venv
        if (pkg["venv_dir"] / "bin" / "python").exists():
            ok(f"Venv: {pkg['venv_dir']}")
        else:
            warn(f"Venv missing: {pkg['venv_dir']}")

        # Check services
        for svc in pkg["services"]:
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            status = result.stdout.strip()
            if status == "active":
                ok(f"{svc}: active")
            else:
                warn(f"{svc}: {status}")

        # Library-specific checks
        if pkg_key == "library":
            zim_count = len(list(Path("/mnt/specter/library/zim").rglob("*.zim"))) \
                if Path("/mnt/specter/library/zim").exists() else 0
            pdf_count = len(list(Path("/mnt/specter/library/pdf").rglob("*.pdf"))) \
                if Path("/mnt/specter/library/pdf").exists() else 0
            ok(f"ZIM files: {zim_count}")
            ok(f"PDF files: {pdf_count}")
            if zim_count == 0:
                warn("No ZIM files found — library content may not have transferred")
            if pdf_count == 0:
                warn("No PDF files found — library content may not have transferred")

        # SDR-specific checks
        if pkg_key == "sdr":
            lsusb = run_shell("lsusb 2>/dev/null")
            hackrf = "1d50:6089" in lsusb
            pluto  = "0456:b673" in lsusb
            ok(f"HackRF One:  {'DETECTED' if hackrf else 'not present'}")
            ok(f"PlutoSDR:    {'DETECTED' if pluto  else 'not present'}")

# ─── Post-install report ──────────────────────────────────────────────────────

def write_report(report: CloneReport, hw: dict) -> None:
    banner("CLONE REPORT")
    lines = [
        f"SHTF SPECTER Clone & Deploy Report  v{VERSION}",
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"Duration:  {report.duration_sec:.1f}s  ({report.duration_sec/60:.1f} min)",
        f"Host:      {hw.get('hostname','?')}",
        f"Board:     {hw.get('board_model','?')}",
        f"RAM:       {hw.get('ram_gb',0):.1f} GB",
        f"Disk:      {hw.get('disk_free_gb',0):.0f} GB free",
        "",
        f"Packages deployed: {', '.join(report.target_packages)}",
        f"Clone method:      {report.clone_method}",
        f"Clone source:      {report.clone_source}",
        "",
        "── SERVICES ──────────────────────────────────────────────────────",
    ]
    for s in report.services_ok:
        lines.append(f"  ✓  {s}")
    for s in report.services_fail:
        lines.append(f"  ✗  {s}  ← FAILED")

    if report.packages_fail:
        lines += ["", "── FAILED PACKAGES ───────────────────────────────────────────────"]
        for p in report.packages_fail:
            lines.append(f"  ✗  {p}")

    if report.warnings:
        lines += ["", "── WARNINGS ──────────────────────────────────────────────────────"]
        for w in report.warnings:
            lines.append(f"  ⚠  {w}")

    if report.errors:
        lines += ["", "── ERRORS ────────────────────────────────────────────────────────"]
        for e in report.errors:
            lines.append(f"  ✗  {e}")

    lines += [
        "",
        "── NEXT STEPS ────────────────────────────────────────────────────",
    ]
    if "sdr" in report.target_packages:
        lines += [
            "  SDR Dashboard:   http://192.168.1.1:5000",
            "  Health check:    bash /opt/specter/scripts/health_check.sh",
            "  Logs:            journalctl -u specter-* -f",
        ]
    if "library" in report.target_packages:
        lines += [
            "  Kiwix Library:   http://192.168.1.5:8080",
            "  Library API:     http://192.168.1.5:5001",
            "  AI query:        specter-ask 'your question'",
            "  Library health:  bash /opt/specter/scripts/library_health.sh",
        ]
    lines += ["", "═" * 68]

    text = "\n".join(lines)
    print("\n" + text)

    report_path = Path("/opt/specter/CLONE_REPORT.txt")
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text)
        print(f"\n  Report saved: {report_path}")
    except Exception:
        pass

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print(textwrap.dedent(f"""
    ╔══════════════════════════════════════════════════════════════════╗
    ║        SHTF SPECTER — CLONE & DEPLOY KIT  v{VERSION}               ║
    ║        Field deployment without internet                         ║
    ╚══════════════════════════════════════════════════════════════════╝
    """))

    if os.geteuid() != 0:
        print("[ERROR] Must run as root:  sudo python3 clone_deploy.py")
        return 1

    start = time.time()
    hw    = probe_hardware()

    print(f"\n  Detected hardware:")
    print(f"    Board:  {hw.get('board_model', 'unknown')}")
    print(f"    RAM:    {hw.get('ram_gb', 0):.1f} GB")
    print(f"    Disk:   {hw.get('disk_free_gb', 0):.0f} GB free")
    print(f"    Host:   {hw.get('hostname', 'unknown')}")

    # Package selection
    target_packages = show_main_menu()

    # Source selection
    print(SOURCE_MENU)
    source_choice = get_menu_choice("Enter choice [A/B/C]:", ["A", "B", "C"])

    # Confirm
    print(f"\n  ── Deployment Plan ───────────────────────────────────────────")
    for pk in target_packages:
        print(f"  ▶  {PACKAGES[pk]['name']}")
    src_label = {"A":"Bundled tarball", "B":"Network rsync", "C":"Auto-detect"}[source_choice]
    print(f"  ▶  Source: {src_label}")
    print()

    confirm = input("  Proceed? [Y/N]: ").strip().upper()
    if confirm != "Y":
        print("\n  Aborted.")
        return 0

    report = CloneReport(target_packages=target_packages)

    def _sigint(sig, frame):
        print("\n\n  [INTERRUPTED] Clone paused. Re-run to retry.")
        sys.exit(130)
    signal.signal(signal.SIGINT, _sigint)

    # Deploy each selected package
    for pkg_key in target_packages:
        success = deploy_package(pkg_key, source_choice, hw, report)
        if not success:
            fail(f"Package {pkg_key} failed — see errors above")
            report.errors.append(f"DEPLOY_FAILED: {pkg_key}")

    # Validate
    validate(target_packages, report)

    report.duration_sec = time.time() - start
    write_report(report, hw)

    return 0 if not report.errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
