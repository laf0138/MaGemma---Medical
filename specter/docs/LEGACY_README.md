# SHTF SPECTER — Clone & Deploy Kit  v1.0.0
## Complete Field Deployment Guide

---

## WHAT THIS IS

This kit clones a fully-built, internet-installed SPECTER system onto new hardware
with zero internet required. One script, one menu, walks away.

---

## FOLDER STRUCTURE

```
SHTF_AI_SDR_Install/
├── clone_deploy.py        ← RUN THIS on the target machine
├── build_payloads.sh      ← Run this on the MASTER to pack tarballs
├── payloads/              ← Auto-populated by build_payloads.sh
│   ├── specter_sdr.tar.gz
│   ├── specter_library.tar.gz
│   ├── *.sha256           ← Integrity checksums
│   └── MANIFEST.json      ← Build manifest
└── README.md              ← This file
```

---

## TWO-PHASE WORKFLOW

### PHASE 1 — On the master node (internet-connected, one time)

```bash
# After install_specter.py and install_specter_library.py are complete:
sudo bash build_payloads.sh both
```

This creates:
- `payloads/specter_sdr.tar.gz`     — SDR package (~2–5 GB)
- `payloads/specter_library.tar.gz` — AI library (~150–300 GB)

Copy the entire `SHTF_AI_SDR_Install/` folder to a USB drive or SSD.

### PHASE 2 — On each target machine (no internet required)

```bash
sudo python3 clone_deploy.py
```

The menu appears:

```
╔══════════════════════════════════════════════════════════════════╗
║          SHTF SPECTER — CLONE & DEPLOY KIT  v1.0.0               ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  What would you like to install on this machine?                 ║
║                                                                  ║
║   [1]  SPECTER SDR Package                                       ║
║   [2]  SPECTER AI Library                                        ║
║   [3]  BOTH  (full SPECTER Monster deployment)                   ║
║   [Q]  Quit                                                      ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

Then source selection:

```
║   [A]  Bundled tarballs  (./payloads/ — fastest, fully offline)  ║
║   [B]  Network rsync     (from master node — LAN, no internet)   ║
║   [C]  Auto-detect       (tries bundled first, then network)     ║
```

Confirm → walks away → post-install report.

---

## CLONE METHODS

### Method A — Bundled Tarballs (recommended for field)
- Requires: `payloads/` folder populated by `build_payloads.sh`
- No network needed at all
- Speed limited by USB/SSD read speed
- SDR package: ~5–15 minutes
- Library package: 30 min – 2 hours (large files)

### Method B — Network rsync (LAN only)
- Requires: target machine on same PoE mesh as master
- Requires: SSH access from target to master (192.168.1.1 or 192.168.1.5)
- No internet needed — purely LAN rsync
- Useful when tarballs are not pre-built
- Speed limited by PoE switch throughput (~125 MB/s on GbE)

### Method C — Auto-detect
- Tries Method A first, falls back to Method B
- Best choice when you're not sure what's available

---

## TARGET HARDWARE

| Package | Target | Min RAM | Min Disk |
|---|---|---|---|
| SDR Package | Raspberry Pi 5 (8 GB) | 4 GB | 16 GB |
| AI Library | Jetson Orin Nano Super | 6 GB | 300 GB |
| Both | Either (install separately) | — | — |

---

## WHAT GETS CLONED

### SDR Package
```
/opt/specter/            ← All Python services + venv
/etc/specter/            ← Config files
/etc/systemd/system/specter-*.service
/etc/mosquitto/conf.d/specter.conf
/etc/udev/rules.d/99-specter-sdr.rules
/etc/modprobe.d/specter-rtlsdr.conf
/etc/cron.d/specter
```
Services started:
- specter-mqtt
- specter-dashboard
- specter-rx-buffer
- specter-mqtt-coordinator
- specter-sdr-control
- specter-thermal

### AI Library
```
/opt/specter/            ← Library API + venv
/etc/specter/library.json
/etc/systemd/system/specter-kiwix.service
/etc/systemd/system/specter-ollama.service
/etc/systemd/system/specter-library-api.service
/mnt/specter/library/    ← ALL ZIM files, PDFs, vector index
~/.ollama/models/        ← LLaMA 3.2 3B + nomic-embed-text models
```
Services started:
- specter-kiwix (port 8080)
- specter-ollama (port 11434)
- specter-library-api (port 5001)
- specter-index-builder.timer

---

## VERIFICATION AFTER CLONE

```bash
# SDR system
bash /opt/specter/scripts/health_check.sh

# AI Library
bash /opt/specter/scripts/library_health.sh

# Quick AI test
specter-ask "What is the treatment for tension pneumothorax?"

# Service status (all)
systemctl status specter-*

# Logs
journalctl -u specter-* -f
```

---

## NETWORK MAP (after full deployment)

| IP | Node | Role |
|---|---|---|
| 192.168.1.1 | Pi 1 Master | MQTT broker · Dashboard :5000 |
| 192.168.1.2 | Pi 2 TX/RX | HackRF · Pluto |
| 192.168.1.3 | Pi 3 DF | KrakenSDR |
| 192.168.1.4 | Pi 4 Radar | Passive radar |
| 192.168.1.5 | Jetson Library | Kiwix :8080 · AI API :5001 |

All on Netgear GS308EP PoE switch. All powered from LiFePO4 12.8V.

---

## TROUBLESHOOTING

**Clone script says "tarball not found":**
```
Run build_payloads.sh on the master first, copy payloads/ to USB.
```

**rsync fails "no master reachable":**
```
Verify target is on 192.168.1.x subnet.
Verify master is powered on.
Try: ping 192.168.1.1
```

**Service fails to start after clone:**
```
journalctl -u <service-name> -n 50
# Usually: venv missing → clone_deploy.py rebuilds it automatically
# Or: config file missing → check /etc/specter/
```

**Ollama models missing after library clone:**
```
# If rsync missed the model directory:
ollama pull llama3.2:3b-instruct-q4_K_M
ollama pull nomic-embed-text
```

**Library API returning "no context":**
```
# RAG index may need rebuild on new machine:
python3 /opt/specter/services/build_index.py
```

---

*SPECTER Monster — Built for the field. Clone it anywhere.*
