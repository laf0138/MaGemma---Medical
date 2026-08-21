#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         SPECTER OFFLINE AI LIBRARY — FIELD INSTALLER                        ║
║                  install_specter_library.py  v1.0.0                         ║
║                                                                              ║
║  Zero-interaction installer for Jetson Orin Nano Super (192.168.1.5)        ║
║                                                                              ║
║  What this does (fully automated):                                           ║
║    1.  System packages + CUDA/cuDNN dependencies                             ║
║    2.  Ollama install + LLaMA 3.2 3B Q4_K_M model pull                     ║
║    3.  kiwix-serve install + systemd unit                                    ║
║    4.  Download ALL manifest ZIM files (Wikipedia, WikiMed, Khan Academy)   ║
║    5.  Download ALL manifest PDFs (MSF, ORNL, USDA, FEMA, Army TMs, etc.)  ║
║    6.  Zimit scrape (Merck Vet Manual, USU Extension)                       ║
║    7.  Clone LDS scriptures structured JSON from GitHub                      ║
║    8.  Build specter-library Flask RAG API                                   ║
║    9.  Configure all systemd services + MQTT integration                     ║
║   10.  Validation pass + post-install report                                 ║
║                                                                              ║
║  Run as root on the Jetson:  sudo python3 install_specter_library.py        ║
║                                                                              ║
║  Expected runtime: 4-12 hours depending on download speed                   ║
║  Expected storage: 150-300 GB (Wikipedia maxi is ~90 GB alone)              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlretrieve
from urllib.error import URLError

# ─── Version & identity ───────────────────────────────────────────────────────
VERSION       = "1.0.0"
SPECTER_USER  = "specter"
JETSON_IP     = "192.168.1.5"
MQTT_BROKER   = "192.168.1.1"

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR      = Path("/opt/specter")
LIBRARY_DIR   = Path("/mnt/specter/library")
ZIM_DIR       = LIBRARY_DIR / "zim"
PDF_DIR       = LIBRARY_DIR / "pdf"
LDS_DIR       = LIBRARY_DIR / "lds"
SCRAPE_DIR    = LIBRARY_DIR / "scraped"
LOG_DIR       = Path("/var/log/specter")
CONFIG_DIR    = Path("/etc/specter")
VENV_DIR      = BASE_DIR / "venv"
OLLAMA_DIR    = Path("/usr/local/bin")
KIWIX_DIR     = Path("/usr/local/bin")

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_FILE = LOG_DIR / "library_install.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("specter.library.install")

# ─── Manifest ─────────────────────────────────────────────────────────────────
# Each entry: (category, filename, url, description, size_hint_gb)
# URLs use the most stable known direct-download endpoints.
# ZIM files use Kiwix mirrors; PDFs use primary government/NGO sources.

ZIM_MANIFEST = [
    # ── [01] Medical / WikiMed ─────────────────────────────────────────────
    (
        "01_MEDICAL_TRAUMA",
        "wikimed_en_all_maxi.zim",
        "https://download.kiwix.org/zim/wikimed/wikimed_en_all_maxi_2024-10.zim",
        "WikiMed — 75,000+ medical articles (Wikipedia WikiProject Medicine)",
        2.5,
    ),
    # ── [17] Wikipedia full ────────────────────────────────────────────────
    (
        "17_MORALE_EDUCATION",
        "wikipedia_en_all_maxi.zim",
        "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2024-11.zim",
        "Wikipedia English — full with images (~90 GB)",
        90.0,
    ),
    # ── [17] Khan Academy ──────────────────────────────────────────────────
    (
        "17_MORALE_EDUCATION",
        "khanacademy_en_all.zim",
        "https://download.kiwix.org/zim/other/khanacademy_en_all_2023-03.zim",
        "Khan Academy — complete K-12 and college tracks",
        15.0,
    ),
    # ── Wikibooks ──────────────────────────────────────────────────────────
    (
        "17_MORALE_EDUCATION",
        "wikibooks_en_all_maxi.zim",
        "https://download.kiwix.org/zim/wikibooks/wikibooks_en_all_maxi_2024-10.zim",
        "Wikibooks — open-content textbooks",
        4.0,
    ),
    # ── Wikiversity ────────────────────────────────────────────────────────
    (
        "17_MORALE_EDUCATION",
        "wikiversity_en_all_maxi.zim",
        "https://download.kiwix.org/zim/wikiversity/wikiversity_en_all_maxi_2024-10.zim",
        "Wikiversity — learning resources and courses",
        2.0,
    ),
    # ── iFixit (repair / fabrication) ─────────────────────────────────────
    (
        "10_REPAIR_FABRICATION",
        "ifixit_en_all.zim",
        "https://download.kiwix.org/zim/ifixit/ifixit_en_all_2024-10.zim",
        "iFixit — repair guides for electronics and equipment",
        3.5,
    ),
    # ── Wikivoyage (navigation / geographic knowledge) ────────────────────
    (
        "17_MORALE_EDUCATION",
        "wikivoyage_en_all_maxi.zim",
        "https://download.kiwix.org/zim/wikivoyage/wikivoyage_en_all_maxi_2024-10.zim",
        "Wikivoyage — travel and geographic reference",
        0.8,
    ),
    # ── Project Gutenberg (general reference / morale) ─────────────────────
    (
        "17_MORALE_EDUCATION",
        "gutenberg_en_all.zim",
        "https://download.kiwix.org/zim/gutenberg/gutenberg_en_all_2023-10.zim",
        "Project Gutenberg — 70,000+ public domain books",
        60.0,
    ),
    # ── StackOverflow (technical reference) ───────────────────────────────
    (
        "10_REPAIR_FABRICATION",
        "stackoverflow_en_all.zim",
        "https://download.kiwix.org/zim/stack_exchange/stackoverflow.com_en_all_2024-10.zim",
        "Stack Overflow — programming and electronics Q&A",
        90.0,
    ),
]

# PDF manifest: (category, filename, url, description)
# All are government, UN, military, or NGO publications — freely distributable
PDF_MANIFEST = [
    # ── [01] Medical / Trauma ──────────────────────────────────────────────
    (
        "01_MEDICAL_TRAUMA",
        "msf_clinical_guidelines_2025.pdf",
        "https://medicalguidelines.msf.org/sites/default/files/2026-05/guideline-170-en.pdf",
        "MSF Clinical Guidelines — Diagnosis and Treatment Manual (June 2025)",
    ),
    (
        "01_MEDICAL_TRAUMA",
        "msf_paediatric_care_2024.pdf",
        "https://medicalguidelines.msf.org/sites/default/files/2024-10/MSF_Paediatric%20care_2024.pdf",
        "MSF Paediatric Care Guidelines 2024",
    ),
    (
        "01_MEDICAL_TRAUMA",
        "who_basic_emergency_care.pdf",
        "https://iris.who.int/bitstream/handle/10665/275635/9789241513081-eng.pdf",
        "WHO Basic Emergency Care — Approach to the Acutely Ill and Injured",
    ),
    (
        "01_MEDICAL_TRAUMA",
        "us_army_sf_medical_handbook.pdf",
        "https://archive.org/download/USArmySFMedicalHandbook/US_Army_Special_Forces_Medical_Handbook.pdf",
        "US Army Special Forces Medical Handbook (ST 31-91B)",
    ),
    (
        "01_MEDICAL_TRAUMA",
        "where_there_is_no_doctor.pdf",
        "https://archive.org/download/WhereThere_Is_No_Doctor/where_there_is_no_doctor.pdf",
        "Where There Is No Doctor — Hesperian Health Guides",
    ),
    (
        "01_MEDICAL_TRAUMA",
        "where_there_is_no_dentist.pdf",
        "https://archive.org/download/where-there-is-no-dentist/where_there_is_no_dentist.pdf",
        "Where There Is No Dentist — Hesperian Health Guides",
    ),

    # ── [02] Radiation / CBRN ─────────────────────────────────────────────
    (
        "02_RADIATION_CBRN",
        "nuclear_war_survival_skills_kearny.pdf",
        "https://www.survivorlibrary.com/library/nuclear-war-survival-skills.pdf",
        "Nuclear War Survival Skills — Cresson Kearny / ORNL (freely distributable)",
    ),
    (
        "02_RADIATION_CBRN",
        "fema_tr87_fallout_shelter.pdf",
        "https://archive.org/download/fema-tr-87/FEMA_TR-87.pdf",
        "FEMA TR-87 — Expeditionary Fallout Shelter Manual",
    ),
    (
        "02_RADIATION_CBRN",
        "army_fm3_11_chemical_ops.pdf",
        "https://archive.org/download/FM3-11/FM3-11.pdf",
        "US Army FM 3-11 — CBRN Operations",
    ),

    # ── [03] Food Preservation ────────────────────────────────────────────
    (
        "03_FOOD_PRESERVATION",
        "usda_complete_guide_home_canning.pdf",
        "https://nchfp.uga.edu/publications/usda/USDA_Complete_Guide_Home_Canning_2015.pdf",
        "USDA Complete Guide to Home Canning (2015 revision)",
    ),

    # ── [04] Agriculture / Crops ─────────────────────────────────────────
    (
        "04_AGRICULTURE_CROPS",
        "fao_seeds_in_emergencies.pdf",
        "https://www.fao.org/3/y5765e/y5765e.pdf",
        "FAO Seeds in Emergencies — Technical Guide",
    ),
    (
        "04_AGRICULTURE_CROPS",
        "fao_crop_production_disaster.pdf",
        "https://www.fao.org/3/i3316e/i3316e.pdf",
        "FAO Crop Production in Disaster-Affected Areas",
    ),

    # ── [10] Repair / Fabrication ─────────────────────────────────────────
    (
        "10_REPAIR_FABRICATION",
        "army_tm5_551a_carpenter_tools.pdf",
        "https://archive.org/download/TM5-551A/TM5-551A.pdf",
        "US Army TM 5-551A — Carpenter Hand-Tool and Timber Engineering",
    ),
    (
        "10_REPAIR_FABRICATION",
        "army_fm5_34_engineer_field_data.pdf",
        "https://archive.org/download/FM5-34/FM5-34.pdf",
        "US Army FM 5-34 — Engineer Field Data",
    ),

    # ── [11] Construction / Shelter ───────────────────────────────────────
    (
        "11_CONSTRUCTION_SHELTER",
        "unhcr_handbook_emergencies_shelter.pdf",
        "https://www.unhcr.org/media/handbook-emergencies-3rd-edition",
        "UNHCR Handbook for Emergencies — Shelter and Infrastructure",
    ),
    (
        "11_CONSTRUCTION_SHELTER",
        "fema_p348_flood_utilities.pdf",
        "https://www.fema.gov/sites/default/files/2020-08/fema_p348.pdf",
        "FEMA P-348 — Protecting Building Utilities from Flood Damage",
    ),
    (
        "11_CONSTRUCTION_SHELTER",
        "army_fm3_34_343_bridging_rigging.pdf",
        "https://archive.org/download/FM3-34.343/FM3-34.343.pdf",
        "US Army FM 3-34.343 — Military Non-Standard Bridging and Rigging",
    ),
    (
        "11_CONSTRUCTION_SHELTER",
        "army_fm5_125_rigging_techniques.pdf",
        "https://archive.org/download/FM5-125/FM5-125.pdf",
        "US Army FM 5-125 — Rigging Techniques, Procedures, and Applications",
    ),

    # ── [15] LDS / Local Utah ─────────────────────────────────────────────
    (
        "15_LOCAL_UTAH_LDS",
        "lds_standard_works_quad.pdf",
        "https://media.ldscdn.org/pdf/lds-scriptures/standard-works/standard-works-83501-eng.pdf",
        "LDS Standard Works — Bible (KJV), Book of Mormon, D&C, Pearl of Great Price",
    ),
]

# Zimit scrape targets (URL → scraped ZIM output filename)
ZIMIT_TARGETS = [
    (
        "01_MEDICAL_TRAUMA",
        "merck_veterinary_manual.zim",
        "https://www.msdvetmanual.com",
        "Merck MSD Veterinary Manual — complete online reference",
        4.0,
    ),
    (
        "15_LOCAL_UTAH_LDS",
        "usu_extension_intermountain.zim",
        "https://extension.usu.edu",
        "USU Extension — Intermountain West agriculture and crop guides",
        1.5,
    ),
    (
        "04_AGRICULTURE_CROPS",
        "usda_nal_collection.zim",
        "https://www.nal.usda.gov/agriculture-information-center",
        "USDA National Agricultural Library — selected collections",
        2.0,
    ),
]

# GitHub / structured text sources
STRUCTURED_SOURCES = [
    (
        "15_LOCAL_UTAH_LDS",
        "lds-scriptures",
        "https://github.com/beandog/lds-scriptures/archive/2020.12.08.zip",
        "LDS Scriptures — structured JSON/SQLite/CSV/HTML export (all formats)",
    ),
    (
        "02_RADIATION_CBRN",
        "nuclear-prep-docs",
        "https://github.com/mgard/nuclear-preparedness/archive/main.zip",
        "Nuclear preparedness structured reference (fallback)",
    ),
]


# ─── Report dataclass ─────────────────────────────────────────────────────────
@dataclass
class InstallReport:
    downloaded_zim:   list = field(default_factory=list)
    downloaded_pdf:   list = field(default_factory=list)
    scraped:          list = field(default_factory=list)
    cloned:           list = field(default_factory=list)
    skipped:          list = field(default_factory=list)
    failed:           list = field(default_factory=list)
    services_started: list = field(default_factory=list)
    warnings:         list = field(default_factory=list)
    duration_sec:     float = 0.0
    ollama_models:    list = field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd: list[str], check: bool = True, timeout: int = 600,
        capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, check=check, timeout=timeout,
        capture_output=capture, text=True
    )

def run_shell(cmd: str, timeout: int = 60) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()

def banner(text: str) -> None:
    print("\n" + "═" * 72)
    print(f"  {text}")
    print("═" * 72)

def step(text: str)  -> None: print(f"  ▶  {text}")
def ok(text: str)    -> None: print(f"  ✓  {text}")
def warn(text: str)  -> None: print(f"  ⚠  {text}"); log.warning(text)
def fail(text: str)  -> None: print(f"  ✗  {text}"); log.error(text)

def bytes_to_human(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"

def _progress_hook(count, block_size, total_size):
    """Simple download progress indicator."""
    if total_size > 0:
        pct = min(100, count * block_size * 100 / total_size)
        done = bytes_to_human(min(count * block_size, total_size))
        total = bytes_to_human(total_size)
        print(f"\r    {pct:5.1f}%  {done} / {total}    ", end="", flush=True)

def download_file(url: str, dest: Path, description: str,
                  report: InstallReport) -> bool:
    """Download a file with resume support and progress display."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        step(f"Already exists, skipping: {dest.name}")
        report.skipped.append(str(dest))
        return True

    step(f"Downloading: {description}")
    step(f"  → {dest.name}")
    try:
        # Use wget for large files — handles resume, progress, retries
        result = subprocess.run(
            [
                "wget",
                "--continue",           # resume partial downloads
                "--tries=5",
                "--waitretry=10",
                "--timeout=60",
                "--progress=dot:mega",
                "-O", str(tmp),
                url,
            ],
            check=True,
            timeout=43200,  # 12 hours max per file
        )
        tmp.rename(dest)
        ok(f"Saved: {dest.name} ({bytes_to_human(dest.stat().st_size)})")
        return True
    except Exception as e:
        fail(f"Download failed: {dest.name} — {e}")
        if tmp.exists():
            tmp.unlink()
        report.failed.append(f"DOWNLOAD: {dest.name} ({url})")
        return False


# ─── Phase 1: System packages ─────────────────────────────────────────────────

APT_PACKAGES = [
    "python3", "python3-pip", "python3-venv", "python3-dev",
    "wget", "curl", "git", "unzip", "jq", "rsync",
    "build-essential", "cmake",
    "nodejs", "npm",               # required by Zimit scraper
    "mosquitto-clients",
    "nginx",
    "chromium-browser",            # headless browser for Zimit
]

PIP_PACKAGES = [
    "flask", "flask-socketio", "eventlet",
    "paho-mqtt", "requests",
    "numpy", "tqdm",
    "pypdf2", "pdfplumber",
    "chromadb",                    # vector store for RAG
    "sentence-transformers",       # local embeddings (no internet required)
    "langchain", "langchain-community",
]

def install_system_packages(report: InstallReport) -> None:
    banner("PHASE 1 — SYSTEM PACKAGES")
    step("Updating apt ...")
    try:
        run(["apt-get", "update", "-qq"], timeout=300)
    except Exception as e:
        warn(f"apt-get update: {e}")

    for pkg in APT_PACKAGES:
        try:
            run(["apt-get", "install", "-y", "-qq", pkg], timeout=300)
            ok(f"apt: {pkg}")
        except Exception as e:
            warn(f"apt install {pkg}: {e}")
            report.warnings.append(f"apt: {pkg} — {e}")


# ─── Phase 2: Python venv ─────────────────────────────────────────────────────

def setup_venv(report: InstallReport) -> None:
    banner("PHASE 2 — PYTHON VENV")
    if not (VENV_DIR / "bin" / "python").exists():
        run([sys.executable, "-m", "venv", str(VENV_DIR)], timeout=120)
    pip = VENV_DIR / "bin" / "pip"
    run([str(pip), "install", "--upgrade", "pip"], timeout=120)
    for pkg in PIP_PACKAGES:
        try:
            run([str(pip), "install", pkg], timeout=600)
            ok(f"pip: {pkg}")
        except Exception as e:
            warn(f"pip: {pkg} — {e}")
            report.warnings.append(f"pip: {pkg} — {e}")


# ─── Phase 3: Ollama + LLM model ─────────────────────────────────────────────

OLLAMA_MODELS = [
    ("llama3.2:3b-instruct-q4_K_M",
     "LLaMA 3.2 3B — Q4_K_M quantized, primary inference model (~2 GB)"),
    ("nomic-embed-text",
     "Nomic Embed Text — local embeddings for RAG vector store (~274 MB)"),
]

def install_ollama(report: InstallReport) -> None:
    banner("PHASE 3 — OLLAMA + LLM MODELS")

    if shutil.which("ollama"):
        ok("Ollama already installed")
    else:
        step("Installing Ollama ...")
        try:
            result = subprocess.run(
                "curl -fsSL https://ollama.com/install.sh | sh",
                shell=True, check=True, timeout=300,
            )
            ok("Ollama installed")
        except Exception as e:
            fail(f"Ollama install failed: {e}")
            report.failed.append("OLLAMA_INSTALL")
            return

    # Start Ollama service
    try:
        run(["systemctl", "enable", "ollama"], check=False)
        run(["systemctl", "start",  "ollama"], check=False)
        time.sleep(5)  # let it initialize
        ok("Ollama service started")
    except Exception as e:
        warn(f"Ollama systemd: {e}")

    # Pull models
    for model_tag, description in OLLAMA_MODELS:
        step(f"Pulling: {description}")
        try:
            subprocess.run(
                ["ollama", "pull", model_tag],
                check=True,
                timeout=7200,  # 2 hours — large model
            )
            ok(f"Model ready: {model_tag}")
            report.ollama_models.append(model_tag)
        except Exception as e:
            fail(f"Model pull failed: {model_tag} — {e}")
            report.failed.append(f"OLLAMA_MODEL: {model_tag}")


# ─── Phase 4: kiwix-serve ────────────────────────────────────────────────────

def install_kiwix(report: InstallReport) -> None:
    banner("PHASE 4 — KIWIX-SERVE")

    kiwix_bin = Path("/usr/local/bin/kiwix-serve")
    if kiwix_bin.exists():
        ok("kiwix-serve already installed")
        return

    step("Downloading kiwix-serve for ARM64 ...")
    # Detect architecture
    arch = run_shell("uname -m")
    if "aarch64" in arch or "arm64" in arch:
        kiwix_url = (
            "https://download.kiwix.org/release/kiwix-tools/"
            "kiwix-tools_linux-aarch64.tar.gz"
        )
    else:
        kiwix_url = (
            "https://download.kiwix.org/release/kiwix-tools/"
            "kiwix-tools_linux-x86_64.tar.gz"
        )

    tmp_tgz = Path("/tmp/kiwix-tools.tar.gz")
    try:
        run(["wget", "-q", "-O", str(tmp_tgz), kiwix_url], timeout=600)
        run(["tar", "-xzf", str(tmp_tgz), "-C", "/tmp/"], timeout=60)
        # Find extracted binary
        found = list(Path("/tmp").glob("**/kiwix-serve"))
        if found:
            shutil.copy2(found[0], "/usr/local/bin/kiwix-serve")
            os.chmod("/usr/local/bin/kiwix-serve", 0o755)
            ok("kiwix-serve installed to /usr/local/bin/")
        else:
            fail("kiwix-serve binary not found in archive")
            report.failed.append("KIWIX_INSTALL")
    except Exception as e:
        fail(f"kiwix-serve install failed: {e}")
        report.failed.append(f"KIWIX_INSTALL: {e}")
    finally:
        if tmp_tgz.exists():
            tmp_tgz.unlink()


# ─── Phase 5: Create directories ─────────────────────────────────────────────

def create_directories(report: InstallReport) -> None:
    banner("PHASE 5 — DIRECTORIES")
    dirs = [
        BASE_DIR, LIBRARY_DIR, LOG_DIR, CONFIG_DIR,
        ZIM_DIR, PDF_DIR, LDS_DIR, SCRAPE_DIR,
    ]
    # Create category subdirectories
    categories = set(c for c, *_ in ZIM_MANIFEST + PDF_MANIFEST + ZIMIT_TARGETS)
    for cat in categories:
        dirs.append(ZIM_DIR / cat)
        dirs.append(PDF_DIR / cat)

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        step(f"Dir: {d}")

    # Ownership
    result = subprocess.run(["id", SPECTER_USER], capture_output=True)
    if result.returncode == 0:
        for d in [LIBRARY_DIR, LOG_DIR]:
            run(["chown", "-R", f"{SPECTER_USER}:{SPECTER_USER}", str(d)], check=False)
    ok("Directories ready")


# ─── Phase 6: Download ZIM files ──────────────────────────────────────────────

def download_zim_files(report: InstallReport) -> None:
    banner("PHASE 6 — ZIM FILE DOWNLOADS")
    step(f"Downloading {len(ZIM_MANIFEST)} ZIM files ...")
    step("NOTE: Wikipedia maxi is ~90 GB — expect many hours on slow links")
    step("wget --continue means partial downloads resume automatically")

    total_gb = sum(size for *_, size in ZIM_MANIFEST)
    step(f"Total estimated: {total_gb:.0f} GB")

    for category, filename, url, description, size_gb in ZIM_MANIFEST:
        dest = ZIM_DIR / category / filename
        ok_flag = download_file(url, dest, f"[{category}] {description}", report)
        if ok_flag and str(dest) not in report.skipped:
            report.downloaded_zim.append(str(dest))


# ─── Phase 7: Download PDFs ───────────────────────────────────────────────────

def download_pdf_files(report: InstallReport) -> None:
    banner("PHASE 7 — PDF DOWNLOADS")
    step(f"Downloading {len(PDF_MANIFEST)} PDF documents ...")

    for category, filename, url, description in PDF_MANIFEST:
        dest = PDF_DIR / category / filename
        ok_flag = download_file(url, dest, f"[{category}] {description}", report)
        if ok_flag and str(dest) not in report.skipped:
            report.downloaded_pdf.append(str(dest))


# ─── Phase 8: Zimit scrapes ───────────────────────────────────────────────────

def run_zimit_scrapes(report: InstallReport) -> None:
    banner("PHASE 8 — ZIMIT WEB SCRAPES")

    # Install zimit via npm if not present
    if not shutil.which("zimit"):
        step("Installing zimit scraper (npm) ...")
        try:
            run(["npm", "install", "-g", "zimit"], timeout=300)
            ok("zimit installed")
        except Exception as e:
            warn(f"zimit npm install failed: {e} — skipping scrapes")
            report.warnings.append(f"ZIMIT_INSTALL: {e}")
            return

    for category, zim_name, url, description, size_gb in ZIMIT_TARGETS:
        dest = ZIM_DIR / category / zim_name
        if dest.exists():
            step(f"Already scraped, skipping: {zim_name}")
            report.skipped.append(str(dest))
            continue

        step(f"Scraping: {description}")
        step(f"  URL: {url}")
        step(f"  Estimated: ~{size_gb:.0f} GB, may take 1-4 hours")

        tmp_dir = SCRAPE_DIR / category
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(
                [
                    "zimit",
                    f"--url={url}",
                    f"--output={tmp_dir}",
                    f"--name={zim_name.replace('.zim','')}",
                    "--limit=0",
                    "--workers=4",
                    "--depth=3",
                ],
                check=True,
                timeout=14400,  # 4 hours
            )
            # Move completed ZIM
            scraped = list(tmp_dir.glob("*.zim"))
            if scraped:
                shutil.move(str(scraped[0]), str(dest))
                ok(f"Scraped: {zim_name}")
                report.scraped.append(str(dest))
            else:
                fail(f"Zimit produced no ZIM for {url}")
                report.failed.append(f"ZIMIT: {zim_name}")
        except Exception as e:
            fail(f"Zimit scrape failed: {zim_name} — {e}")
            report.failed.append(f"ZIMIT: {zim_name} — {e}")


# ─── Phase 9: Structured text sources (LDS, GitHub) ──────────────────────────

def clone_structured_sources(report: InstallReport) -> None:
    banner("PHASE 9 — STRUCTURED TEXT SOURCES (LDS SCRIPTURES + GITHUB)")

    for category, name, url, description in STRUCTURED_SOURCES:
        dest_dir = LDS_DIR / category / name
        dest_zip = LDS_DIR / category / f"{name}.zip"

        if dest_dir.exists():
            step(f"Already present: {name}")
            report.skipped.append(str(dest_dir))
            continue

        step(f"Downloading: {description}")
        dest_dir.parent.mkdir(parents=True, exist_ok=True)

        try:
            run(["wget", "-q", "-O", str(dest_zip), url], timeout=300)
            run(["unzip", "-q", str(dest_zip), "-d", str(dest_dir.parent)], timeout=120)
            dest_zip.unlink()
            ok(f"Cloned: {name}")
            report.cloned.append(str(dest_dir))
        except Exception as e:
            fail(f"Clone failed: {name} — {e}")
            report.failed.append(f"CLONE: {name} — {e}")

    # Also download LDS scriptures directly from Church CDN
    step("Downloading LDS scriptures EPUB from ChurchofJesusChrist.org ...")
    lds_formats = [
        ("lds_book_of_mormon_en.pdf",
         "https://www.churchofjesuschrist.org/bc/content/shared/content/english/pdf/language-materials/34404_eng.pdf"),
        ("lds_doctrine_covenants_en.pdf",
         "https://www.churchofjesuschrist.org/bc/content/shared/content/english/pdf/language-materials/34590_eng.pdf"),
        ("lds_pearl_of_great_price_en.pdf",
         "https://www.churchofjesuschrist.org/bc/content/shared/content/english/pdf/language-materials/34841_eng.pdf"),
    ]
    lds_pdf_dir = PDF_DIR / "15_LOCAL_UTAH_LDS"
    lds_pdf_dir.mkdir(parents=True, exist_ok=True)
    for fname, url in lds_formats:
        dest = lds_pdf_dir / fname
        download_file(url, dest, f"LDS: {fname}", report)


# ─── Phase 10: Build RAG index ───────────────────────────────────────────────

def build_rag_index(report: InstallReport) -> None:
    banner("PHASE 10 — BUILDING RAG VECTOR INDEX")
    step("Indexing PDF library into ChromaDB vector store ...")
    step("This indexes PDFs for semantic search by the LLM.")

    index_script = BASE_DIR / "services" / "build_index.py"
    if not index_script.exists():
        warn("build_index.py not found — RAG index will be built on first service start")
        return

    try:
        python = VENV_DIR / "bin" / "python"
        subprocess.run(
            [str(python), str(index_script),
             "--pdf-dir", str(PDF_DIR),
             "--index-dir", str(LIBRARY_DIR / "vector_index")],
            check=True,
            timeout=7200,  # 2 hours
        )
        ok("RAG vector index built")
    except Exception as e:
        warn(f"RAG index build failed: {e} — will retry on first service start")
        report.warnings.append(f"RAG_INDEX: {e}")


# ─── Phase 11: Deploy service files ──────────────────────────────────────────

def deploy_services(report: InstallReport) -> None:
    banner("PHASE 11 — DEPLOYING SPECTER LIBRARY SERVICES")

    src_dir = Path(__file__).parent
    service_files = list(src_dir.glob("services/*.py"))

    for src in service_files:
        dest = BASE_DIR / "services" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        os.chmod(dest, 0o755)
        step(f"Deployed: {src.name}")
        report.cloned.append(str(dest))

    ok(f"Services deployed to {BASE_DIR / 'services'}")


# ─── Phase 12: Systemd units ─────────────────────────────────────────────────

def write_systemd_units(report: InstallReport) -> None:
    banner("PHASE 12 — SYSTEMD UNITS")

    python_bin = VENV_DIR / "bin" / "python"

    units: dict[str, str] = {}

    # kiwix-serve: serves all ZIM files on port 8080
    zim_glob = str(ZIM_DIR) + "/*/*.zim"
    units["specter-kiwix.service"] = textwrap.dedent(f"""\
        [Unit]
        Description=SPECTER Kiwix Offline Library Server
        After=network.target

        [Service]
        Type=simple
        User={SPECTER_USER}
        ExecStart=/usr/local/bin/kiwix-serve \\
            --port 8080 \\
            --threads 4 \\
            {zim_glob}
        Restart=always
        RestartSec=5
        StandardOutput=journal
        StandardError=journal
        SyslogIdentifier=specter-kiwix

        [Install]
        WantedBy=multi-user.target
    """)

    # Ollama (if not already managed by its own unit)
    units["specter-ollama.service"] = textwrap.dedent(f"""\
        [Unit]
        Description=SPECTER Ollama LLM Server
        After=network.target

        [Service]
        Type=simple
        User=ollama
        Group=ollama
        Environment=OLLAMA_HOST=0.0.0.0:11434
        Environment=OLLAMA_MODELS=/usr/share/ollama/.ollama/models
        ExecStart=/usr/local/bin/ollama serve
        Restart=always
        RestartSec=5
        StandardOutput=journal
        StandardError=journal
        SyslogIdentifier=specter-ollama

        [Install]
        WantedBy=multi-user.target
    """)

    # specter-library-api: Flask RAG API on port 5001
    units["specter-library-api.service"] = textwrap.dedent(f"""\
        [Unit]
        Description=SPECTER Library RAG API
        After=network.target specter-ollama.service specter-kiwix.service

        [Service]
        Type=simple
        User={SPECTER_USER}
        WorkingDirectory={BASE_DIR}
        ExecStart={python_bin} {BASE_DIR}/services/library_api.py
        Restart=always
        RestartSec=10
        Environment=OLLAMA_HOST=http://localhost:11434
        Environment=KIWIX_URL=http://localhost:8080
        Environment=PDF_DIR={PDF_DIR}
        Environment=VECTOR_INDEX_DIR={LIBRARY_DIR}/vector_index
        Environment=MQTT_BROKER={MQTT_BROKER}
        StandardOutput=journal
        StandardError=journal
        SyslogIdentifier=specter-library-api

        [Install]
        WantedBy=multi-user.target
    """)

    # specter-index-builder: rebuild RAG index nightly
    units["specter-index-builder.service"] = textwrap.dedent(f"""\
        [Unit]
        Description=SPECTER RAG Index Builder
        After=network.target

        [Service]
        Type=oneshot
        User={SPECTER_USER}
        ExecStart={python_bin} {BASE_DIR}/services/build_index.py \\
            --pdf-dir {PDF_DIR} \\
            --index-dir {LIBRARY_DIR}/vector_index
        StandardOutput=journal
        SyslogIdentifier=specter-index-builder
    """)

    units["specter-index-builder.timer"] = textwrap.dedent("""\
        [Unit]
        Description=Rebuild SPECTER RAG index nightly

        [Timer]
        OnCalendar=*-*-* 02:00:00
        Persistent=true

        [Install]
        WantedBy=timers.target
    """)

    systemd_dir = Path("/etc/systemd/system")
    for unit_name, content in units.items():
        path = systemd_dir / unit_name
        path.write_text(content)
        step(f"Wrote: {path}")

    run(["systemctl", "daemon-reload"])
    ok("systemd daemon reloaded")

    start_order = [
        "specter-kiwix.service",
        "specter-ollama.service",
        "specter-library-api.service",
        "specter-index-builder.timer",
    ]
    for unit in start_order:
        try:
            run(["systemctl", "enable", unit], check=False)
            run(["systemctl", "restart", unit], check=False)
            ok(f"Started: {unit}")
            report.services_started.append(unit)
        except Exception as e:
            warn(f"Could not start {unit}: {e}")
            report.warnings.append(f"SERVICE: {unit} — {e}")


# ─── Phase 13: Write config ───────────────────────────────────────────────────

def write_config(report: InstallReport) -> None:
    banner("PHASE 13 — CONFIG")
    config = {
        "version": VERSION,
        "node": "jetson-library",
        "ip": JETSON_IP,
        "mqtt_broker": MQTT_BROKER,
        "ollama": {
            "host": "http://localhost:11434",
            "default_model": "llama3.2:3b-instruct-q4_K_M",
            "embed_model": "nomic-embed-text",
            "context_window": 4096,
            "max_tokens": 512,
            "temperature": 0.2,
        },
        "kiwix": {
            "host": "http://localhost:8080",
            "zim_dir": str(ZIM_DIR),
            "search_results": 3,
        },
        "rag": {
            "pdf_dir": str(PDF_DIR),
            "vector_index_dir": str(LIBRARY_DIR / "vector_index"),
            "chunk_size": 512,
            "chunk_overlap": 64,
            "top_k": 5,
        },
        "library_dir": str(LIBRARY_DIR),
        "categories": {
            "01_MEDICAL_TRAUMA":     "Advanced Clinical, Trauma & Austere Medical",
            "02_RADIATION_CBRN":     "Blast, Fallout & CBRN Hazard Guidance",
            "03_FOOD_PRESERVATION":  "Safe Canning & Food Storage",
            "04_AGRICULTURE_CROPS":  "Crop Science, Agronomy & Seed Saving",
            "10_REPAIR_FABRICATION": "Hand-Tool Engineering, Carpentry & Repair",
            "11_CONSTRUCTION_SHELTER": "Expedient Shelter & Structural Rigging",
            "15_LOCAL_UTAH_LDS":     "Intermountain Regional & LDS Library",
            "17_MORALE_EDUCATION":   "Full Academic Tracks & Universal Reference",
        },
    }
    conf_path = CONFIG_DIR / "library.json"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(json.dumps(config, indent=2))
    ok(f"Config: {conf_path}")


# ─── Phase 14: Post-install report ───────────────────────────────────────────

def write_report(report: InstallReport) -> None:
    banner("SPECTER LIBRARY INSTALL REPORT")

    def gb(path_str: str) -> str:
        try:
            p = Path(path_str)
            if p.exists():
                return f" ({bytes_to_human(p.stat().st_size)})"
        except Exception:
            pass
        return ""

    lines = [
        f"SPECTER Offline AI Library — Install Report  v{VERSION}",
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"Duration:  {report.duration_sec / 3600:.1f} hours",
        "",
        "── OLLAMA MODELS ─────────────────────────────────────────────────────",
    ]
    for m in report.ollama_models:
        lines.append(f"  ✓ {m}")
    if not report.ollama_models:
        lines.append("  ✗ No models pulled — check Ollama logs")

    lines += ["", "── ZIM FILES DOWNLOADED ──────────────────────────────────────────────"]
    for f in report.downloaded_zim:
        lines.append(f"  ✓ {Path(f).name}{gb(f)}")
    for f in report.skipped:
        if f.endswith(".zim"):
            lines.append(f"  ↩ {Path(f).name}  (already existed)")

    lines += ["", "── PDF FILES DOWNLOADED ──────────────────────────────────────────────"]
    for f in report.downloaded_pdf:
        lines.append(f"  ✓ {Path(f).name}{gb(f)}")

    lines += ["", "── SCRAPED (ZIMIT) ───────────────────────────────────────────────────"]
    for f in report.scraped:
        lines.append(f"  ✓ {Path(f).name}{gb(f)}")

    lines += ["", "── STRUCTURED SOURCES ───────────────────────────────────────────────"]
    for f in report.cloned:
        lines.append(f"  ✓ {Path(f).name}")

    lines += ["", "── SERVICES ──────────────────────────────────────────────────────────"]
    for s in report.services_started:
        lines.append(f"  ✓ {s}")

    if report.failed:
        lines += ["", "── FAILED ─────────────────────────────────────────────────────────"]
        for f in report.failed:
            lines.append(f"  ✗ {f}")

    if report.warnings:
        lines += ["", "── WARNINGS ───────────────────────────────────────────────────────"]
        for w in report.warnings:
            lines.append(f"  ⚠ {w}")

    # Disk usage
    try:
        lib_size = run_shell(f"du -sh {LIBRARY_DIR} 2>/dev/null | cut -f1")
        lines += ["", f"── STORAGE ─────────────────── Library: {lib_size} total"]
    except Exception:
        pass

    lines += [
        "",
        "── ACCESS ─────────────────────────────────────────────────────────",
        f"  Kiwix Library:    http://{JETSON_IP}:8080",
        f"  Library RAG API:  http://{JETSON_IP}:5001",
        f"  Ollama API:       http://{JETSON_IP}:11434",
        "",
        "── USAGE ──────────────────────────────────────────────────────────",
        "  CLI query:    specter-ask 'How do I treat a tension pneumothorax?'",
        "  MQTT trigger: mosquitto_pub -h 192.168.1.1 -t shtf/library/ask",
        "               -m '{\"query\": \"fallout shelter construction\"}'",
        "  Dashboard:    AI LIBRARY panel on 192.168.1.1:5000",
        "",
        "═" * 70,
    ]

    report_text = "\n".join(lines)
    report_path = LIBRARY_DIR / "INSTALL_REPORT.txt"
    try:
        report_path.write_text(report_text)
    except Exception:
        pass
    print("\n" + report_text)
    print(f"\n  Report saved: {report_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print(textwrap.dedent(f"""
    ╔══════════════════════════════════════════════════════════════════╗
    ║     SPECTER OFFLINE AI LIBRARY — FIELD INSTALLER  v{VERSION}       ║
    ║     Jetson Orin Nano Super  ·  192.168.1.5                      ║
    ║                                                                  ║
    ║  Expected runtime: 4–12 hours (Wikipedia ~90 GB)                ║
    ║  Expected storage: 150–300 GB minimum                           ║
    ║  Internet required during install only — fully air-gapped after ║
    ╚══════════════════════════════════════════════════════════════════╝
    """))

    if os.geteuid() != 0:
        print("[ERROR] Must be run as root:  sudo python3 install_specter_library.py")
        return 1

    if not shutil.which("wget"):
        print("[ERROR] wget not found — run: apt-get install wget")
        return 1

    start = time.time()
    report = InstallReport()

    def _sigint(sig, frame):
        print("\n\n[INTERRUPTED] Install paused. Re-run to resume — wget resumes partial downloads.")
        report.duration_sec = time.time() - start
        write_report(report)
        sys.exit(130)

    signal.signal(signal.SIGINT, _sigint)

    install_system_packages(report)
    setup_venv(report)
    install_ollama(report)
    install_kiwix(report)
    create_directories(report)
    download_zim_files(report)
    download_pdf_files(report)
    run_zimit_scrapes(report)
    clone_structured_sources(report)
    deploy_services(report)
    write_config(report)
    build_rag_index(report)
    write_systemd_units(report)

    report.duration_sec = time.time() - start
    write_report(report)

    return 0 if not report.failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
