# SPECTER MONSTER — OPERATIONS MANUAL

**Version 1.2.0 · August 2026**
Field-deployable emergency communications and medical command center.

---

## HOW TO READ THIS MANUAL

| If you want to… | Go to |
|---|---|
| Understand what this is and how the pieces fit | Part 1 |
| Build it from bare hardware | Part 2 |
| Install software on each node | Part 3 |
| Know what talks to what | Part 4 |
| Actually use it on a patient | Part 5 |
| Fix it when it breaks | Part 6 |
| Know what does **not** work yet | Part 7 |

**Read Part 7 before you rely on this system for anything.** It is the honest register of what is built, what is specified but unbuilt, and what is known-broken. A manual that hides its gaps is worse than no manual.

---

# PART 1 — ARCHITECTURE

## 1.1 What SPECTER is

A hard case containing nine processors, five software-defined radios, a distributed offline knowledge library, a medical AI, and a Bluetooth vitals hub — designed to run air-gapped on solar power and be repaired in the field with off-the-shelf parts.

Two design commitments drive everything:

**Everything is local.** No cloud, no accounts, no internet after initial install. The AI models, the reference library, the medical guidance — all on disk in the case.

**Everything talks through MQTT.** Nodes never call each other directly. They publish to a broker and subscribe to topics. Any node can drop and return without breaking the others.

## 1.2 Node map

| Node | Hardware | IP | Role |
|---|---|---|---|
| **Node 1** | Raspberry Pi 5 8GB + AI HAT+ 2 | 192.168.1.1 | Master, MQTT broker, dashboard, trauma module, thermal watchdog |
| **Node 2** | Raspberry Pi 5 8GB + AI HAT+ 2 | 192.168.1.2 | HF/VHF SDR — HackRF One + AirSpy HF+, RX ring buffer |
| **Node 3** | CM5 8GB (X1500) | 192.168.1.3 | TX/RX — PlutoSDR, FT8/JS8, FCC ID automation |
| **Node 4** | CM5 8GB (X1500) | 192.168.1.4 | Passive intel — KrakenSDR 5-ch DF + RTL-SDR passive radar |
| **Node 5** | CM5 8GB (X1500) | 192.168.1.5 | Storage — RAID-1, NFS export, **Kiwix library server** |
| **Node 6** | CM5 8GB (X1500) | 192.168.1.6 | Voice — Pi-Star, MMDVM, DMR/YSF/D-STAR |
| **Jetson** | Orin NX 16GB | 192.168.1.10 | Medical AI (MedGemma 4B), RAG, Ollama |
| **Med Hub** | Pi Zero 2W | 192.168.1.20 | Bluetooth vitals aggregator |
| **Mesh radio** | Meshtastic-firmware LoRa device (model unspecified — see 7.2) | USB-attached to Node 1 | Internal comms + alarm relay, 915MHz US ISM band |

Total: 36 CPU cores, 237 TOPS, 76 GB RAM (all soldered — nothing upgradeable, order the right SKUs). The mesh radio is a Node 1 add-on, not a seventh node — it has no compute of its own and talks to Node 1 over USB serial.

**Why Node 1 and 2 are full Pi 5s and not CM5s:** the Hailo-10H AI HAT+ 2 requires the Pi 5's exposed PCIe interface. The X1500 carrier does not expose PCIe in a form the HAT can use. This is a hard compatibility constraint, not a preference.

## 1.3 Distributed library architecture

The Kiwix library (~287 GB of ZIM files) lives on **Node 5**, not the Jetson. This is deliberate.

If Kiwix ran on the Jetson, a heavy full-text search across 287 GB would compete with MedGemma inference for the same CPU and memory. Separating them means a library query and a medical query can run simultaneously without either stalling.

```
Jetson NVMe 512GB          Node 5 RAID-1 580GB usable
├── MedGemma 4B    1.9GB   ├── Kiwix ZIM library   287GB
├── nomic-embed    0.3GB   ├── RF triggered events  77GB (7-day)
├── ChromaDB index   5GB   └── headroom            216GB
├── JetPack + CUDA  20GB
├── working space    5GB
└── free           480GB
```

## 1.4 Repository layout

```
specter/
├── core/                      Node 1 master services
│   ├── mqtt_coordinator.py    Aggregates node state → shtf/system/state
│   ├── sdr_control.py         SDR device manager, MQTT control
│   ├── thermal_monitor.py     Temp watchdog + emergency halt
│   └── audio_recorder.py      Manual timed capture utility
├── services/
│   ├── specter_rx_ring_buffer.py   Pre-trigger RX capture (Node 2)
│   ├── library_api.py              Flask RAG API
│   └── build_index.py              ChromaDB index builder
├── medical/
│   ├── specter_medical_hub.py      BT vitals aggregator (Pi Zero 2W)
│   └── specter_medical_ai.py       MedGemma engine (Jetson)
├── trauma/
│   ├── specter_trauma.py           Casualty registry, triage, MARCH
│   └── specter_trauma_monitor.py   Terminal triage board
├── ward/
│   └── specter_ward.py             Care episodes, fluid balance, NEWS2, care tasks
├── mesh/
│   └── specter_mesh_relay.py       LoRa/Meshtastic alarm relay + internal comms (Node 1 add-on)
├── dashboard/
│   ├── dashboard_server.py         Flask + SocketIO backend
│   ├── dashboard.html              Main RF/system dashboard
│   ├── resus.html                  RESUS triage + MARCH screens (demo, not live)
│   ├── ward.html                   WARD sustained-care screen (live)
│   └── vendor/                     Vendored Socket.IO/D3 (no CDN dependency)
├── scripts/
│   ├── health_check.sh             Full system health report
│   ├── disk_report.sh              Storage → MQTT
│   ├── library_health.sh           Library service check
│   └── specter-ask                 CLI query to the library AI
├── config/specter.json             Master config
├── systemd/                        14 unit files
├── deploy/
│   ├── install_specter.py          Node installer (hardware probe + deploy)
│   ├── install_specter_library.py  Library installer (Jetson/Node 5)
│   ├── clone_deploy.py             SD card clone/deploy kit
│   └── build_payloads.sh           Payload builder
└── docs/                           This manual + specs
```

---

# PART 2 — HARDWARE BUILD

## 2.1 Bill of materials (compute)

| Item | Qty | ~Cost |
|---|---|---|
| Raspberry Pi 5 **8GB** | 2 | $170 |
| Raspberry Pi AI HAT+ 2 (Hailo-10H) | 2 | $260 |
| Compute Module 5 **8GB** | 4 | $220 |
| Geekworm X1500 carrier + C2 cases | 1 | included |
| Jetson Orin NX **16GB** + carrier | 1 | $599 |
| Raspberry Pi Zero 2W | 1 | $15 |
| Netgear GS308EP PoE+ switch | 1 | $100 |
| Waveshare PoE HAT+ (CM5 nodes) | 4 | $80 |
| **Compute subtotal** | | **~$1,444** |

**Order the exact RAM variants.** RAM is soldered to the SoM on all of these. There are no upgrades — a 4GB Pi 5 stays 4GB forever.

## 2.2 Storage

| Drive | Qty | Purpose |
|---|---|---|
| Samsung 990 Pro 512GB NVMe | 1 | Jetson — AI models, ChromaDB, OS |
| Samsung 970 EVO Plus 1TB | 1 | Node 5 RAID-1 member A |
| Samsung 970 EVO Plus 256GB | 1 | Node 5 RAID-1 member B |
| Samsung 970 EVO Plus 256GB | 6 | Boot drives, Nodes 1–6 |
| Samsung 64GB Endurance microSD | 1 | Emergency recovery image |

**~$407.** The 990 Pro on the Jetson is chosen for sustained read speed — ChromaDB retrieval latency is disk-bound.

## 2.3 Power

| Item | Spec |
|---|---|
| Meanwell LRS-75-5 | 15A @ 5V (75W) — primary rail |
| Meanwell LRS-60-12 | 5A @ 12V (60W) — coolers, PA, antenna amp |
| Copper bus bar + grounding bus | Per-node fusing, isolated grounding |
| EcoFlow 220W bifacial panel | 21.8V OCV |
| Bioenno SC-122430NE MPPT | 24V/30A, accepts 12–50V in, 12.8V out |
| LiFePO4 12.8V battery | 20–50Ah |

Measured peak draw is **13.7A @ 5V (69W)** with all systems live. The 15A PSU runs at ~92% of nameplate at peak — acceptable because peak is transient (a MedGemma query is 3–5 seconds). Sustained load sits near 8A.

Solar delivers ~15A into the battery (192W) in full sun, which offsets a 24-hour system load in roughly 4.6 hours.

**Fusing:** 15A main on the 5V side, 3A per Pi 5 tap, 2A per CM5 tap. Per-node fusing means one node failing short does not take down the bus.

## 2.4 Thermal

| State | Temp | Action |
|---|---|---|
| Idle | < 45 °C | Normal |
| RX load | < 70 °C | Normal |
| Warning | ≥ 75 °C | MQTT alarm on `shtf/system/alarm` |
| Emergency halt | ≥ 85 °C | `systemctl poweroff` issued by thermal_monitor |
| Hardware cutoff | ~95 °C | Pi 5 silicon protection |

Active cooling is **mandatory**, not optional. Passive heatsinks fail above 75 °C under sustained SDR load. Node 4 (KrakenSDR DF) runs hottest at 65–80% sustained CPU.

---

# PART 3 — INSTALLATION

## 3.1 Prerequisites

- Debian/Raspberry Pi OS Bookworm (64-bit) on all Pi and CM5 nodes
- JetPack 6.2 on the Jetson (SUPER mode enabled)
- Internet access **during install only** — the system is air-gapped afterward
- Static IPs assigned per the node map in §1.2

## 3.2 Network first

Before any software, get the mesh talking. Every node needs a static IP and every node must reach 192.168.1.1.

```bash
# /etc/dhcpcd.conf on each node (adjust per node map)
interface eth0
static ip_address=192.168.1.1/24
static routers=192.168.1.254
static domain_name_servers=192.168.1.254
```

Verify from Node 1:
```bash
for i in 2 3 4 5 6 10 20; do ping -c1 -W1 192.168.1.$i >/dev/null && echo "OK  .$i" || echo "DEAD .$i"; done
```

Do not proceed until every node answers.

## 3.3 Node 1 — master

**The broker requires authentication, with a separate least-privilege
account per service** (trauma, medical AI, dashboard, etc.) instead of one
shared login — a leaked dashboard credential (broad read, many exposure
points) can't be used to forge a trauma command, because the broker's ACL
file only lets the `specter-dashboard` account read, never write. Pick one
master password and use it on *every* node - there is no central secret
store on an air-gapped network, so a mismatched password on any one node
just means every service on it silently can't authenticate:

```bash
export SPECTER_MQTT_USER=specter-operator    # broad-access role for manual/CLI use
export SPECTER_MQTT_PASSWORD='pick-a-real-password-here'
```

If you skip this, every node falls back to the documented default
(`specter-operator` / `specter-change-me`) — fine for a first bring-up on a
bench, but change it before this leaves the building. Each service's own
password is *derived* from this one master password (see
`_derive_service_password()` in `deploy/install_specter.py`), so every
node's independent install run agrees on all eleven accounts without
needing to distribute eleven separate secrets by hand.

```bash
sudo apt update && sudo apt install -y mosquitto mosquitto-clients python3-venv git
sudo mkdir -p /opt/specter /etc/specter /var/log/specter /var/lib/specter
sudo cp -r specter/* /opt/specter/
sudo python3 -m venv /opt/specter/venv
sudo /opt/specter/venv/bin/pip install paho-mqtt==2.1.0 flask==3.1.3 flask-socketio==5.6.1 requests==2.33.1 numpy==2.4.6
# Pinned to the versions this repo's test suite is actually run against
# (see requirements-dev.txt) - an unpinned install months from now can
# pull a materially different, untested version onto a field kit.

# Service user
sudo useradd -r -s /bin/false -G audio,dialout,plugdev specter 2>/dev/null || true
sudo chown -R specter:specter /opt/specter /var/log/specter /var/lib/specter

# Broker config, password file (12 accounts: 1 operator + 11 services), and
# the matching ACL file are all generated by the installer - hand-deriving
# eleven per-service passwords and an ACL file in bash isn't practical to
# keep in sync with deploy/install_specter.py's MQTT_SERVICES table, so
# this step is not shown as raw bash the way the others are:
sudo python3 /opt/specter/deploy/install_specter.py
```

This single installer run does everything the older manual bash sequence
did (broker config, service user, specter.json) *plus* generates
`/etc/mosquitto/specter_passwd` (all 12 accounts), `/etc/mosquitto/specter_acl`
(per-account topic rules), and `specter.json`'s `mqtt.services` block. See
Part 3.4 for what else it does.

```bash
# Units
sudo cp /opt/specter/systemd/specter-mqtt.service \
        /opt/specter/systemd/specter-coordinator.service \
        /opt/specter/systemd/specter-dashboard.service \
        /opt/specter/systemd/specter-thermal.service \
        /opt/specter/systemd/specter-trauma.service \
        /opt/specter/systemd/specter-ward.service \
        /opt/specter/systemd/specter-mesh.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now specter-mqtt specter-coordinator specter-dashboard specter-thermal specter-trauma specter-ward specter-mesh
```

**`specter-mesh.service` needs a Meshtastic-firmware LoRa radio physically
attached over USB before it will do anything useful** — see Part 7.2. If
none is attached, the service starts, logs that no hardware was found, and
retries every 30s; it does not block or delay any other service.

Verify:
```bash
export MQTT_USER="${SPECTER_MQTT_USER:-specter-operator}"
export MQTT_PASSWORD="${SPECTER_MQTT_PASSWORD:-specter-change-me}"
mosquitto_sub -h 127.0.0.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'shtf/#' -v    # should show heartbeats within 10s
curl --cacert /etc/specter/tls/dashboard.crt https://192.168.1.1/api/status
# Loopback-only backend diagnostic on Node 1:
curl -s http://127.0.0.1:5000/api/status
```

## 3.4 Nodes 2–6 — automated

The installer probes hardware, installs everything, and reports what it could not find:

```bash
sudo python3 /opt/specter/deploy/install_specter.py
```

It will:
- Probe CPU, RAM, disk, USB, I2C, network
- Detect HackRF (1d50:6089), PlutoSDR (0456:b673), KrakenSDR (5× 0bda:2838), RTL-SDR, GPS
- Install apt packages, create the venv, pip install dependencies
- Write udev rules and blacklist the DVB kernel modules that hijack RTL-SDR
- Deploy scripts, write configs, enable services
- Print a report

**Missing SDR hardware is reported but does not abort the install.** Connect hardware later and re-run.

Then enable the units that node needs:
```bash
# Node 2 (HF/VHF SDR)
sudo cp /opt/specter/systemd/specter-sdr-control.service \
        /opt/specter/systemd/specter-rx-buffer.service /etc/systemd/system/
sudo systemctl enable --now specter-sdr-control specter-rx-buffer
```

## 3.5 Node 5 — storage and library

**RAID-1 first:**
```bash
sudo mdadm --create /dev/md0 --level=1 --raid-devices=2 /dev/nvme0n1 /dev/nvme1n1
sudo mkfs.ext4 /dev/md0
sudo mkdir -p /mnt/specter && sudo mount /dev/md0 /mnt/specter
echo '/dev/md0 /mnt/specter ext4 defaults 0 2' | sudo tee -a /etc/fstab
sudo mdadm --detail --scan | sudo tee -a /etc/mdadm/mdadm.conf
sudo update-initramfs -u
```

**NFS export** so other nodes can reach RF captures:
```bash
sudo apt install -y nfs-kernel-server
echo '/mnt/specter 192.168.1.0/24(rw,sync,no_subtree_check)' | sudo tee -a /etc/exports
sudo exportfs -ra && sudo systemctl enable --now nfs-kernel-server
```

**Library download** — this is the long one. Budget 4–12 hours and ~300 GB:
```bash
sudo python3 /opt/specter/deploy/install_specter_library.py
```

It downloads Wikipedia (102 GB), Stack Overflow (90 GB), Gutenberg (60 GB), Khan Academy (15 GB), WikiMed, iFixit, Wikibooks, plus the PDF corpus (MSF, WHO, Army TMs, FEMA). `wget --continue` means an interrupted run resumes — safe to Ctrl-C and restart.

Then:
```bash
sudo cp /opt/specter/systemd/specter-kiwix.service /etc/systemd/system/
sudo systemctl enable --now specter-kiwix
curl -s localhost:8080 | head -20     # library responding
```

## 3.6 Jetson — medical AI

```bash
# Ollama
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
ollama pull medgemma:4b
ollama pull nomic-embed-text

# Python deps
sudo /opt/specter/venv/bin/pip install paho-mqtt==2.1.0 requests==2.33.1 chromadb
# paho-mqtt/requests pinned to the versions this repo's test suite runs
# against (see requirements-dev.txt). chromadb is left unpinned here
# deliberately rather than guessing a version - it isn't in
# requirements-dev.txt and hasn't been exercised by this project's test
# suite at all, so pin it only after that verification happens.

# Build the RAG index over the PDF corpus
sudo /opt/specter/venv/bin/python /opt/specter/services/build_index.py \
    --pdf-dir /mnt/specter/library/pdf \
    --index-dir /opt/specter/chroma

sudo cp /opt/specter/systemd/specter-ollama.service \
        /opt/specter/systemd/specter-medical-ai.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now specter-medical-ai
```

**Do not pull medgemma:27b.** It needs ~17 GB and the Jetson has 16 GB unified. It will swap and become unusable. The 4B model returns in 3–5 seconds; the 27B would take 30+ even if it fit.

Test:
```bash
sudo /opt/specter/venv/bin/python /opt/specter/medical/specter_medical_ai.py \
  --ask "fever, RLQ pain, guarding" --patient operator
```

## 3.7 Medical hub — Pi Zero 2W

**Read Part 7.4 before wiring up any actual device.** Every shipped BLE
device type (Omron, Masimo, Braun, Contour, Polar H10) is hard-blocked by
default - the hub will discover paired devices but publish zero vitals for
them until you've confirmed the parser against real captured traffic and
added the device to `SPECTER_VERIFIED_BLE_DEVICES`. This is not a
"finish the paperwork later" gap: Omron and Masimo's parsers were checked
against Bluetooth SIG specs and look like the same class of bug as the
removed KardiaMobile parser. Contour Next One's parser has since been
rewritten against the real confirmed protocol (still gated pending
hardware confirmation). Braun ThermoScan 7 as specified in this kit has no
Bluetooth radio at all and cannot be fixed - see Part 7.4 before buying or
wiring one up.

```bash
sudo apt install -y python3-venv bluez
sudo python3 -m venv /opt/specter/venv
sudo /opt/specter/venv/bin/pip install paho-mqtt==2.1.0 bleak==3.0.2 bleakheart==0.2.0 neurokit2==0.2.13
# Pinned to the versions this repo's test suite is actually run against
# (see requirements-dev.txt) - see the note in Part 3.3 on why.

# Only once you've verified a device's parser against real hardware:
# sudo systemctl edit specter-medical-hub
#   [Service]
#   Environment=SPECTER_VERIFIED_BLE_DEVICES=omron_bp7450,contour_next_one,polar_h10

sudo cp /opt/specter/systemd/specter-medical-hub.service /etc/systemd/system/
sudo systemctl enable --now specter-medical-hub
```

Pair each Bluetooth device once with `bluetoothctl` before the hub can read it:
```bash
bluetoothctl
> scan on
> pair <MAC>
> trust <MAC>
> quit
```

## 3.8 Verification — the whole system

From Node 1:
```bash
bash /opt/specter/scripts/health_check.sh
mosquitto_sub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'shtf/#' -v | head -40
```

Expected within 60 seconds: heartbeats from every node, thermal readings, SDR device inventory, medical AI status `online`, trauma scene `scene_active: false`.

---

# PART 4 — MQTT CONTRACT

Everything in SPECTER is this table. If you understand it, you can extend the system.

## 4.1 System

| Topic | Dir | Payload |
|---|---|---|
| `shtf/system/heartbeat` | pub | Coordinator alive |
| `shtf/system/state` | pub | Full state snapshot (retained) |
| `shtf/system/thermal` | pub | Per-node CPU/GPU temps, throttle flags |
| `shtf/system/alarm` | pub | System alarms |
| `shtf/system/disk` | pub | Storage usage |
| `shtf/pi/+/status` | pub | Per-node heartbeat + health |

## 4.2 RF

| Topic | Dir | Payload |
|---|---|---|
| `shtf/sdr/status` | pub | Device inventory |
| `shtf/sdr/cmd` | sub | Device control commands |
| `shtf/rx/status` | pub | Ring buffer health, capture count, VOX level |
| `shtf/rx/event` | pub | New capture: filename, reason, duration |
| `shtf/rx/recording` | pub | `0` / `1` |
| `shtf/rx/trigger` | sub | External trigger — any node can fire a capture |
| `shtf/df/bearing` | pub | KrakenSDR bearing + confidence |
| `shtf/radar/contacts/+` | pub | Passive radar contacts |
| `shtf/tx/status` | pub | TX state, FCC ID timer |

## 4.3 Medical

| Topic | Dir | Payload |
|---|---|---|
| `shtf/medical/vitals/<pid>` | pub | Full snapshot: all readings + timestamps |
| `shtf/medical/vitals/<pid>/<type>` | pub | Single reading (dashboard cards) |
| `shtf/medical/query/<pid>` | sub | `{"query": "...", "request_id": "..."}` |
| `shtf/medical/diagnosis/<pid>` | pub | AI output + `vitals_used` + sources (retained) |
| `shtf/medical/derived/<pid>` | pub | MAP, pulse pressure, shock index, NEWS2, qSOFA, fever burden, delta-from-baseline (retained) — see `medical/clinical_scores.py`; NEWS2/qSOFA always `partial` here, no RR/consciousness sensor |
| `shtf/medical/profile/<pid>` | sub | Update standing patient profile |
| `shtf/medical/ai/status` | pub | `online` / `thinking` / `offline` |

Reading types: `bp_systolic`, `bp_diastolic`, `pulse`, `spo2`, `temperature_c`, `glucose_mg_dl`, `respiratory_rate`.

## 4.4 Trauma

| Topic | Dir | Payload |
|---|---|---|
| `shtf/trauma/scene` | pub | Full scene (retained) — casualties, counts, alerts |
| `shtf/trauma/casualty/<id>` | pub | Single casualty (retained) |
| `shtf/trauma/alert` | pub | Active alerts across all casualties |
| `shtf/trauma/protocol` | pub | MARCH content (retained, static) |
| `shtf/trauma/command/open_scene` | sub | `{}` |
| `shtf/trauma/command/add_casualty` | sub | `{"mechanism":"gsw","notes":"..."}` |
| `shtf/trauma/command/triage` | sub | `{"casualty_id":"C-1","assessment":{...}}` |
| `shtf/trauma/command/intervention` | sub | `{"casualty_id":"C-1","type":"tourniquet","site":"L thigh"}` |
| `shtf/trauma/command/convert_tourniquet` | sub | `{"casualty_id":"C-1","tq_id":"C-1-TQ1"}` |
| `shtf/trauma/command/vitals` | sub | `{"casualty_id":"C-1","vitals":{...}}` |
| `shtf/trauma/command/assessed` | sub | `{"casualty_id":"C-1"}` — clears staleness |

## 4.5 Ward

| Topic | Dir | Payload |
|---|---|---|
| `shtf/ward/episode` | pub | All open episodes (retained) — fluid balance, care tasks, skin/nutrition/mobility, vitals, NEWS2, alerts |
| `shtf/ward/episode/<id>` | pub | Single episode (retained) |
| `shtf/ward/alert` | pub | Active alerts across all open episodes |
| `shtf/ward/command/open_episode` | sub | `{"patient_id":"p1","presenting_problem":"pneumonia"}` |
| `shtf/ward/command/close_episode` | sub | `{"episode_id":"W-1"}` |
| `shtf/ward/command/intake` | sub | `{"episode_id":"W-1","route":"oral","volume_ml":250,"description":"water"}` |
| `shtf/ward/command/output` | sub | `{"episode_id":"W-1","route":"urine","volume_ml":300}` — `route` also takes `emesis`\|`stool`\|`drain`\|`blood` |
| `shtf/ward/command/add_care_task` | sub | `{"episode_id":"W-1","task_type":"medication","interval_minutes":360,"label":"prednisone"}` — `interval_minutes` required for any type beyond `reposition`/`skin_check` (seeded automatically), see `specter_ward.py`'s `DEFAULT_TASK_INTERVAL_MINUTES` docstring for why |
| `shtf/ward/command/complete_task` | sub | `{"episode_id":"W-1","task_id":"W-1-T1"}` |
| `shtf/ward/command/skin_check` | sub | `{"episode_id":"W-1","sites":{"sacrum":"intact","heels_l":"blanching_erythema"}}` |
| `shtf/ward/command/nutrition` | sub | `{"episode_id":"W-1","description":"lunch","percent_consumed":60}` |
| `shtf/ward/command/mobility` | sub | `{"episode_id":"W-1","level":"sat_edge","duration_minutes":10}` |
| `shtf/ward/command/vitals` | sub | `{"episode_id":"W-1","values":{"rr":18,"spo2":97,"bp_systolic":120,"pulse":75,"avpu":"A","temperature_c":37.0}}` — NEWS2 computed server-side, missing parameters never scored as normal |

## 4.6 Mesh relay (LoRa/Meshtastic)

| Topic | Dir | Payload |
|---|---|---|
| `shtf/mesh/status` | pub | `{"state": ..., "timestamp_utc": ...}` (retained) — `connected`/`disconnected`/`not_found`/`ambiguous`/`connect_failed`/`offline` |
| `shtf/mesh/sent` | pub | Log of what was actually transmitted onto the mesh: `{"source","kind","text","timestamp_utc"}` |
| `shtf/mesh/inbound` | pub | Text received from another mesh node: `{"from","text","timestamp_utc"}` |
| `shtf/mesh/command/send` | sub | `{"text":"...", "priority":"alert"\|"text"}` — operator-composed outbound message; `priority:"alert"` uses Meshtastic's higher-priority `sendAlert()` API |

This service **subscribes** to `shtf/trauma/alert`, `shtf/ward/alert`, and `shtf/system/alarm` (not shown again here - see 4.3/4.4/4.5) and relays new or changed alarms onto the LoRa mesh automatically. See Part 7.2 for the rate-limiting/dedup rationale and hardware status.

**Retained messages matter.** Scene state, diagnoses, and system state are published retained so a dashboard connecting late immediately gets current state rather than waiting for the next publish cycle.

---

# PART 5 — FIELD OPERATIONS

SPECTER has **three clinical modes**. They are different problems with different data models. The mode is a deliberate operator choice — the system never guesses.

| | RESUS | WARD | CHRONIC |
|---|---|---|---|
| Timescale | Seconds–minutes | Hours–weeks | Months–years |
| Question | What kills this person first? | Better or worse? | Managed correctly? |
| Artifact | Checklist | Trend + care schedule | Differential + sourcing |
| AI role | Minimal | High | High |
| **Likelihood of use** | Low | **Highest** | Certain |

## 5.1 Deployment sequence

1. Open case, deploy antenna mast on tripod, plant ground rod, connect grounding strap
2. Connect solar panel → MPPT → battery (or shore power)
3. Power on — Node 1 first, wait 60s for broker, then remaining nodes
4. `bash /opt/specter/scripts/health_check.sh`
5. Open dashboard at `https://192.168.1.1` and sign in with the
   `SPECTER_DASHBOARD_USER` / `SPECTER_DASHBOARD_PASSWORD` values supplied to
   the installer (see Part 3.3 and Part 7.4)
6. Verify Kiwix at `http://192.168.1.5:8080` and medical AI status `online`

Cold start to operational: about 4 minutes.

## 5.2 WARD mode — sustained care

**This is the most likely scenario in the entire build.** Someone in bed for five days with pneumonia or a bad GI illness is vastly more probable than a gunshot wound. **Implemented** as of this build — see Part 7.1.

What actually harms them is not the illness — it is dehydration, pressure injury, and deterioration nobody caught.

Open `https://192.168.1.1/ward` (operator login required, same credential as the main dashboard - Part 7.4). Unlike RESUS, this screen is genuinely live: every action (opening an episode, logging intake/output, completing a care task, recording vitals) publishes a real MQTT command to `specter-ward.service`, which persists it and republishes the updated episode back to every connected screen. Multiple episodes (1-2 known patients) are supported via the tab strip in the header.

**Flow:**
1. `+ New episode` — patient ID and presenting problem. Reposition (q2h) and skin-check (q4h) care tasks are seeded automatically; add anything else (medication, wound checks, etc.) with its own interval — there's no invented universal default for those, since real intervals are prescription/care-plan specific.
2. Log every intake and output by measurement as it happens, not at end of shift.
3. Record vitals at the interval the header shows — it escalates from 4h to 1h automatically once NEWS2 reaches 5, per the routine below.
4. Work the Care Due panel like RESUS's MARCH checklist — it's a countdown, not a list to revisit later.

**The numbers that matter:**

| Watch | Why |
|---|---|
| **Fluid balance** | Highest-value tracked number. For a transplant recipient it cuts both ways — running dry damages the graft, overloading strains the heart and floods lungs. Alarm at −1,000 mL **and** +1,500 mL / 24h. |
| **Repositioning** | Every 2 hours. Overdue by 30 minutes is a real alarm — that is how pressure injuries start. |
| **NEWS2 trend** | A rise of ≥2 between consecutive observations matters more than the absolute value. |
| **Skin at bony prominences** | Non-blanching erythema is already a stage 1 injury. Response is immediate offloading, not more frequent checking. |

**Routine:**
- Vitals every 4 hours (hourly if NEWS2 ≥ 5)
- Log every intake and output by measurement, not estimate
- Reposition q2h, skin check q4h
- Ask MedGemma trend questions, not snapshot questions: *"Day 3, NEWS2 went 5→3→5, temp trending up, intake dropping. Secondary infection?"*

## 5.3 RESUS mode — trauma

Open `https://192.168.1.1/resus`. Do not open `resus.html` directly: the
served route provides operator authentication and the application context.

**Trauma care is algorithmic, not diagnostic.** You do not need a differential for a gunshot wound; you need MARCH executed fast and in order. The AI is available but never on the critical path — a 3–5 second inference is an eternity when someone is bleeding.

**Flow:**
1. `+ ADD CASUALTY` — one tap. Classify afterward.
2. Assess bleeding → the screen locks or unlocks downstream steps based on your answer
3. Work MARCH. Each check timestamps and drops into the intervention log.
4. Watch the **DO THIS NOW** line — it recomputes from state on every action

**The three things the system will not let you get wrong:**

**Tourniquet time.** The clock starts on application and never stops until conversion is logged. Amber at 2 hours, critical at 4. Write the time on the windlass as well — the marker is ground truth when electronics fail.

**Burp before needle.** If a chest seal is placed and tension is suspected, the directive says **BURP THE SEAL FIRST** and the needle decompression step stays locked with the reason. A seal you placed is a likelier cause of rising pressure than a new tension. This is the guidance most often forgotten under stress.

**Reassessment intervals.** Immediate 5 min, Delayed 15, Minimal 30. Casualties deteriorate quietly while you work on someone else. Stale ones sort to the top of the board.

**The paper card.** Generate and laminate it:
```bash
python3 /opt/specter/trauma/specter_trauma.py --print-protocol > march_card.txt
```
133 lines. This is the only part of the trauma system that works with no power at all.

## 5.4 CHRONIC mode — standing management

Two immunosuppressed patients, both on 28-day IV infusion schedules.

**Cold chain is the hard constraint.** Belatacept and the RA biologic both require 2–8 °C. The BougeRV CR22 compressor unit holds this; thermoelectric coolers physically cannot in warm ambient. Any excursion is logged permanently — that history determines whether a vial is still usable.

**Belatacept specifics:** silicone-free syringes (BD PlastiPak) are required — standard syringes are incompatible. Reconstituted solution must be used within 24 hours; when a dose is mixed, that countdown takes over the entire display row. It is the highest-stakes timer in the system.

**Immunosuppression changes the alarm thresholds.** Fever may be blunted; absence of fever does not rule out serious infection. NEWS2 and qSOFA are hospital-validated and are known to **under-trigger** in immunosuppressed patients. The display carries this caveat and so should you — a low score is not reassurance.

## 5.5 RF operations

**Triggered event recording only**, 15 seconds per event, 7-day retention. All five SDRs monitor simultaneously; continuous IQ capture would consume 11 TB/day and is not the design.

```bash
touch /run/specter/sdr_trigger                    # manual capture
mosquitto_pub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t shtf/rx/trigger -m 'manual'   # remote trigger
```

**FCC Part 97:** all transmissions require station call sign ID every 10 minutes and at end of transmission. This is implemented in the Node 6 TX automation and is **not optional**. SPECTER TX is for licensed amateur operators only. Receive-only operation carries no such requirement.

## 5.6 Shutdown

```bash
sudo systemctl stop 'specter-*'    # on each node
sudo poweroff
```

Node 1 last — other nodes buffer to it. Let the RX ring buffer finalize any in-progress capture (it drains on SIGTERM).

---

# PART 6 — TROUBLESHOOTING

## 6.1 First moves

```bash
bash /opt/specter/scripts/health_check.sh          # full system report
systemctl status 'specter-*'                        # what is running
journalctl -u specter-<name> -n 50 --no-pager       # recent errors
mosquitto_sub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'shtf/#' -v         # is traffic flowing
```

**Dashboard prompts for a login and rejects it.** Every dashboard/RESUS/WARD
route except `/api/status` requires the operator credential (Part 7.4). The
configuration stores only a PBKDF2 password hash, so the password cannot be
recovered from `specter.json`. Set `SPECTER_DASHBOARD_USER` and
`SPECTER_DASHBOARD_PASSWORD` and re-run the installer to reset it.

## 6.2 MQTT

**Nothing is publishing.**
```bash
systemctl status specter-mqtt
sudo ss -tlnp | grep 1883                                              # broker listening?
mosquitto_pub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t test -m hi   # broker accepting?
mosquitto_pub -h 192.168.1.1 -t test -m hi                             # should be REJECTED - confirms auth is enforced
```
If the broker is up but nodes are silent, the problem is network or per-node service. Check `ping` from each node to .1 first. Two distinct auth failure modes, both logged by the affected service:

- **`MQTT connect refused (rc=5)` (`Not authorised`)** — the username/password itself is wrong. That service's credential in `/etc/specter/specter.json` (`mqtt.services.<name>` or the flat `mqtt.username`/`mqtt.password`) doesn't match `/etc/mosquitto/specter_passwd` on Node 1 - re-run the installer (or the password step) with the same `SPECTER_MQTT_PASSWORD` on both.
- **Connects fine, but publishes/subscribes silently do nothing** — the credential is valid but the ACL file (`/etc/mosquitto/specter_acl`) doesn't grant that account the topic it's trying to use. Check `mosquitto.log` for a `Denied` line naming the topic, then compare against that account's `MQTT_SERVICES` entry in `deploy/install_specter.py`. This is the tradeoff of least-privilege accounts: a genuine code bug that makes a service touch the wrong topic now fails *silently* at the broker instead of being caught anywhere else - watch `mosquitto.log`, not just the service's own log, when a topic seems to be missing data after a code change.

**Reconnect storm — repeated "MQTT connected" every few seconds.** Two processes are sharing a client ID and kicking each other off.
```bash
pkill -f specter_trauma     # or whichever service
systemctl restart specter-trauma
```

**paho-mqtt version error on startup.** The bundled shim handles both 1.x and 2.x. If you see `Client() takes ...`, you are running an older copy of the file. Re-deploy from `/opt/specter/`.

## 6.3 SDR

**Device not detected.**
```bash
lsusb                                        # is it on the bus at all?
sudo udevadm control --reload-rules && sudo udevadm trigger
lsmod | grep dvb                             # must be empty
```
If DVB modules are loaded they have hijacked the RTL-SDR:
```bash
sudo modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2830
cat /etc/modprobe.d/specter-rtlsdr.conf      # blacklist should exist
```

**KrakenSDR shows fewer than 5 devices.** All five tuners must enumerate for coherent DF. Usually a power issue — the Kraken needs its own supply, not bus power. Check the USB hub current rating.

**Ring buffer not capturing.**
```bash
journalctl -u specter-rx-buffer -f
python3 /opt/specter/services/specter_rx_ring_buffer.py list-devices
touch /run/specter/sdr_trigger               # test trigger
```
Queue overflow warnings in the log mean `--queue-depth` needs raising.

## 6.4 Medical AI

**Status stuck `offline`.**
```bash
systemctl status ollama
curl -s localhost:11434/api/tags             # is Ollama answering?
ollama list                                  # is medgemma:4b present?
```

**Inference times out.** First query after boot loads the model into memory and takes longer. If it persists:
```bash
free -h                                      # under memory pressure?
tegrastats                                   # Jetson GPU state
```
If you pulled the 27B model, remove it — it does not fit in 16 GB:
```bash
ollama rm medgemma:27b
```

**"Inference already in progress."** By design — one query at a time on a single Jetson. Wait 5 seconds and retry.

**No vitals in the prompt.** The AI reports what it actually received. If it says vitals are unknown, the hub is not publishing:
```bash
mosquitto_sub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'shtf/medical/vitals/#' -v
systemctl status specter-medical-hub          # on the Pi Zero
```

## 6.5 Medical hub

**No devices found.**
```bash
bluetoothctl devices                          # paired?
sudo systemctl restart bluetooth
journalctl -u specter-medical-hub -f
```
Devices must be paired and trusted once via `bluetoothctl` before the hub can read them. Bluetooth range on the Pi Zero 2W is short — keep instruments within about 3 meters.

**Device is discovered but no vitals ever publish.** This is very likely the verification gate (Part 7.4), not a Bluetooth problem — check the startup log for `UNVERIFIED DEVICE PARSERS BLOCKED`. Every shipped parser is blocked until its `dev_type` is added to `SPECTER_VERIFIED_BLE_DEVICES`. This is the expected, safe default, not a bug to work around.

**Readings look wrong** (only possible once a device has been deliberately verified and unblocked). Check the RSSI in the payload first — below −80 dBm the connection is marginal and parse errors follow. If RSSI is fine and the numbers are still implausible, the parser itself is suspect: re-capture real characteristic data from the device and re-check it against the parser before trusting it further.

## 6.6 Library

**Kiwix not responding.**
```bash
systemctl status specter-kiwix
ls -la /mnt/specter/library/zim/*/*.zim       # files present?
mount | grep md0                              # RAID mounted?
cat /proc/mdstat                              # RAID healthy?
```

**RAID degraded.**
```bash
sudo mdadm --detail /dev/md0
sudo mdadm --manage /dev/md0 --add /dev/nvme1n1    # re-add replacement
```
RAID-1 survives one drive. Replace immediately — a second failure loses the library and the RF archive together.

## 6.7 Thermal

**Emergency shutdown fired.** thermal_monitor issues poweroff at 85 °C. Before restarting, find out why:
- Fan failure on a compute board
- Blocked case ventilation
- Thermal pad lost contact
- Ambient too high for the duty cycle

```bash
vcgencmd measure_temp                         # Pi
tegrastats                                    # Jetson
journalctl -u specter-thermal -n 100
```

## 6.8 Power

**Nodes browning out under load.** Peak is 13.7A @ 5V. If you added hardware beyond the locked BOM you have exceeded the 15A PSU. Symptoms are random reboots on the highest-draw node, usually the Jetson during inference.

**Solar not charging.** Check MPPT display for input voltage. The EcoFlow panel is 21.8V open-circuit; below ~14V the MPPT cannot buck to 12.8V. Shade on one cell string collapses output disproportionately.

## 6.9 Mesh relay

**`shtf/mesh/status` stays at `not_found` / journal says "No Meshtastic LoRa hardware detected."** Expected if no radio is plugged into Node 1 - the service idles and retries every 30s rather than crashing. Check `lsusb` and that the radio is actually in Meshtastic firmware (not another mode); check `/dev/ttyUSB*`/`/dev/ttyACM*` exists.

**Status is `ambiguous`.** More than one USB-serial device is attached to Node 1 (a GPS puck, another radio) and auto-detection can't tell them apart - this is by design (see Part 7.2 for why it doesn't just guess). Add `--serial-port /dev/ttyUSBx` to `specter-mesh.service`'s `ExecStart` line (the unit file has a commented-out example) after identifying the right device with `ls -l /dev/serial/by-id/`.

**Alerts aren't reaching the mesh.** Confirm `shtf/mesh/status` is `connected`, then check `journalctl -u specter-mesh` for "Mesh TX" lines - if an alert fired on the dashboard but nothing shows there, check `_last_sent` suppression logic isn't the cause (an unchanged alert set only re-announces every 15 minutes by design, see Part 7.2) versus a real delivery failure.

**Channel/encryption setup.** This service does not manage the LoRa channel or its pre-shared key - provision that once on the radio itself with Meshtastic's own CLI (`meshtastic --ch-set psk <key> --ch-index 0`) or app before deploying. `specter-mesh.service` just uses whatever channel the radio already has configured.

---

# PART 7 — KNOWN GAPS

**Read this before relying on the system.** Marked honestly.

## 7.1 Built and tested

- ✅ Trauma module — casualty registry, START triage, tourniquet clocks, staleness alerts, MARCH state machine, scene state that survives a service restart (see Part 7.4). Exercised end-to-end over MQTT.
- ✅ RX ring buffer — pre-trigger capture with the wrap-detection bug fixed.
- ✅ Installers — hardware probe, package install, service deployment.
- ✅ MARCH paper card generation.
- ✅ Dashboard server (`dashboard_server.py`) — MQTT-to-WebSocket relay, `/`, `/resus`, `/ward`, `/api/state`, `/api/status` routes, broker-connection status tracking, operator login. See Part 7.4 for the security/XSS/offline fixes this had needed.
- ✅ **Ward module (`ward/specter_ward.py`) and the live `/ward` screen** — the highest-probability scenario in the whole build (Part 5.2), previously fully specified with no code. `CareEpisodeRegistry` (fluid balance, scheduled care tasks, skin checks, nutrition, mobility, vitals) persists with the same atomic-write/restore pattern as the trauma module; NEWS2 is computed server-side (standard RCP NEWS2 Scale 1 - see the module docstring for why Scale 2 isn't implemented, and why a missing vitals parameter is never silently scored as 0/normal); alarms match every threshold in `SPECTER_CLINICAL_MODES.md` 1.4. Unlike RESUS, `ward.html` is genuinely live, not a local demo: every action round-trips through real MQTT (`shtf/ward/command/#` → `specter-ward.service` → persisted + republished → every connected screen), confirmed end-to-end with a real local broker and headless browser, not just unit tests. This needed one deliberate ACL change - see the write-back note in Part 7.2.
- ✅ **Derived clinical metrics (MAP, pulse pressure, shock index, NEWS2, qSOFA, fever burden, Δ-from-baseline)** — previously documented in `SPECTER_MEDICAL_UI_BRIEF.md` Part 1.3 as design intent only, not computed anywhere. Now implemented: scoring functions live in the new `medical/clinical_scores.py`, shared between WARD's manual bedside NEWS2 and the automated-device path in `medical/specter_medical_ai.py`'s `VitalsCache.derived()`. Published retained to `shtf/medical/derived/<patient_id>` on every vitals update and surfaced in MedGemma's prompt. **NEWS2 and qSOFA are always partial on the automated-device path** — no BLE device in this build measures respiration rate or level of consciousness, so both scores are computed with those parameters explicitly listed as missing (never scored as 0/normal), and `qSOFA.positive` is `null`, not `false`, whenever an input is absent, so a partial score can never read as "ruled out." Fever burden and Δ-from-baseline are bounded by the AI engine's in-memory reading history (not persisted across restarts, and not yet a true 24h/30-day window) — see `SPECTER_MEDICAL_UI_BRIEF.md` 1.3 for the full accounting of what's real vs. still a gap.

## 7.2 Built, not yet tested against real hardware

- ⚠️ **RESUS UI (`resus.html`)** — the triage board and MARCH screens (state-driven directive, step gating, obligation timers, append-only correction log) are real, tested logic, but run entirely against **locally-generated sample casualties in browser memory**, not the live trauma service. It does not consume `shtf/trauma/#`, so nothing an operator does on this screen reaches the trauma service, MQTT, or any other operator's screen, and refreshing the page discards all of it. A `⚠ DEMO MODE` banner now says this explicitly on the screen itself (August 2026 fix - see Part 7.4) rather than presenting as connected, which it previously did not. Wiring this to the real trauma service (consume `shtf/trauma/scene` for state, publish `shtf/trauma/command/#` for actions) is real, substantial work that has not been done - **WARD mode's `/ward` screen is what that work looks like once done** (Part 7.1), including the one ACL change it needed: the dashboard's MQTT credential is deliberately broad-READ/zero-WRITE across `shtf/#` (so a leaked credential can't forge a command), but WARD's care-task completion/intake logging genuinely needs a write-back path, so it now carries one narrow exception - `write shtf/ward/command/#` and nothing else (`deploy/install_specter.py`'s `MQTT_SERVICES["dashboard"]`). RESUS staying read-only/demo was a deliberate choice this pass, not an oversight; wiring it live would need the equivalent `shtf/trauma/command/#` write exception plus the actual state-consumption work above.
- ⚠️ **Medical hub** — see Part 7.4. All five device types are hard-blocked by default pending real-hardware verification, not merely untested. Contour Next One's parser has a confirmed-correct rewrite awaiting hardware confirmation; Braun ThermoScan 7 as specified has no Bluetooth radio and cannot be fixed at all (see Part 7.4).
- ⚠️ **Mesh relay (`mesh/specter_mesh_relay.py`)** — internal comms and alarm notification over a LoRa mesh (Meshtastic firmware, 915MHz US ISM band), added August 2026 in response to an operator request for alarm notification independent of the LAN/dashboard. Built against the real `meshtastic` PyPI package (v2.7.11) and its documented API (`SerialInterface`, `sendAlert`/`sendText`, pypubsub receive topics) - not guessed, and one real hardware-detection bug was found and worked around by reading that library's source rather than assuming: `SerialInterface(devPath=None)` calls a bare `sys.exit(1)`, not a catchable exception, when more than one candidate USB-serial port is found, which would have silently killed the whole systemd service on any Node 1 with a second USB-serial device attached (a GPS puck, another radio). `MeshtasticHardware.resolve_port()` always resolves the port itself first and never lets that code path execute - covered by a test against the real library, not a mock, so it stays protected against future `meshtastic` releases changing that behavior again. It subscribes to `shtf/trauma/alert`, `shtf/ward/alert`, and `shtf/system/alarm` and relays new-or-changed alarms onto the mesh, using Meshtastic's higher-priority `sendAlert()` API for anything containing a `critical`-level alert and plain `sendText()` otherwise; an unchanged alert set is only re-announced every 15 minutes (`REALERT_INTERVAL_SECONDS`) rather than every time its source republishes (the ward module alone republishes every 30s regardless of change) - LoRa airtime is scarce and shared across the whole mesh, and flooding it would be worse than not relaying at all. Inbound mesh text relays back onto `shtf/mesh/inbound`, and `shtf/mesh/command/send` accepts operator-composed outbound messages, so this is genuinely bidirectional, not alarm-only. **NOT run against a real Meshtastic radio** - same hardware-gating discipline as the medical BLE devices: if nothing is attached (true today, true in CI), the service idles cleanly and retries every 30s rather than crashing or blocking anything else. What "encrypted" means here: Meshtastic's own per-channel AES-256-CTR pre-shared key, configured on the physical radio with Meshtastic's own CLI/app during hardware setup - this service sends/receives on whatever channel the radio already has configured and does not implement or manage encryption itself, deliberately, for the same reason SPECTER doesn't reimplement NEWS2 scoring or MQTT auth from scratch when a well-tested version already exists. Delivery is best-effort (`wantAck=False`, no retry/confirmation tracking) - "no reply on the mesh" means exactly that, not "message not sent" or "everyone is fine."
- ⚠️ **Medical AI engine** — logic is sound, but MedGemma output quality on your specific patient profiles is unverified. Run practice queries with known cases before you need it.
- ⚠️ **Library RAG** — index builder exists; retrieval quality across the PDF corpus is untested.
- ⚠️ **MQTT broker authentication and per-service ACLs** — the broker previously ran with `allow_anonymous true` and no password: anyone on the wired LAN could read every patient's vitals/diagnosis in cleartext or publish a forged trauma command with nothing to reject it. It now requires auth, with a **separate least-privilege account per service** (see `MQTT_SERVICES` in `deploy/install_specter.py`) rather than one shared login, so a leaked dashboard credential can't be used to forge a trauma command - the ACL file only lets it read. All of this is wired through every service and generated automatically by the installer, but has been verified with unit tests and code review only — **not yet exercised against a real multi-node mesh**. Before relying on it: confirm every node actually connects post-install (`journalctl -u specter-<name>` should show no `rc=5 Not authorised` errors), confirm each service can actually publish/subscribe its own topics (a valid credential with the wrong ACL entry fails *silently* - check `mosquitto.log` for `Denied` lines, Part 6.2), and confirm an unauthenticated `mosquitto_pub` is rejected outright. Traffic is still unencrypted (no TLS) — this blocks casual/opportunistic access and limits blast radius from one leaked credential, it does not defend against a device already trusted enough to hold a valid one.
- ✅ **Deprecated Eventlet runtime removed** — the dashboard now uses
  Flask-SocketIO's maintained threading/simple-websocket backend. Eventlet is
  absent from production and test dependency manifests.
- ⚠️ **Test coverage is real but uneven** — CI publishes a per-file report and
  enforces a 65% project-wide floor. Focused tests now cover the coordinator,
  SDR-control, thermal, dashboard-auth/TLS, MQTT-auth, persistence-corruption,
  and RX timeout paths, but blocking service loops and real hardware I/O remain
  less covered than the pure trauma/WARD/clinical logic.

## 7.3 Specified but not built

- ❌ **PATIENT screen** — the flowsheet dashboard exists as a design spec and a rendered mockup, not as working code wired to MQTT. The data it needs (raw vitals plus MAP/pulse pressure/shock index/NEWS2/qSOFA/fever burden/Δ-from-baseline) is now real and published to `shtf/medical/derived/<patient_id>` (Part 7.1) - what's missing is the screen itself, not the numbers behind it.
- ⚠️ **Canonical seven-part 12-lead ECG pipeline — software integrated, artifacts/hardware not commissioned.** The locked combination is: **(1) Biocare iE300** acquisition, **(2) DeepECG-SL** primary research classifier, **(3) AntonioR92** six-label regression baseline, **(4) ECG-XPLAIM** later explainable secondary model, **(5) PTB-XL plus other external datasets** as the test corpus, **(6) MedGemma** as the constrained explanation/context layer, and **(7) ExChanGeAI concepts** for evaluation and ONNX model lifecycle management. SPECTER now has separate strict generic XML, HL7 aECG, and public WFDB ingestion paths; source-rate preservation through 8,000 Hz; evidence archiving; unit/lead/sample-rate/quality gates; explicitly non-vendor synthetic fixtures; model-specific preprocessing; hash-pinned isolated runners; an atomic model/ONNX registry; patient-split dataset validation; explicit disagreement records; retained MQTT results; source-aware MedGemma prompt integration; and an authenticated `/ecg` attribution screen. It remains disabled by default because no real Biocare XML, model artifacts or datasets were supplied. The generic/synthetic XML paths are not represented as Biocare-compatible. DeepECG's inspected EfficientNet release also lacks published thresholds, and the ECG-XPLAIM task is not yet chosen, so the software refuses to manufacture those details. See [ECG_AI_IMPLEMENTATION_STATUS.md](ECG_AI_IMPLEMENTATION_STATUS.md) for the exact commissioning gates. Until hardware-specific and clinician-reviewed validation passes, every output remains **RESEARCH ONLY / NOT FOR DIAGNOSIS** and may not drive treatment or autonomous alerts.
- ❌ **Node 3/4/6 workloads** — GNU Radio flowgraphs, KrakenSDR DF calibration, passive radar DSP, Pi-Star/MMDVM config, FCC ID automation. Node roles are assigned; the software is not written.
- ❌ **Hailo signal classification** — AI HAT+ 2 hardware is specified, the AMC model on RadioML is not implemented.
- ❌ **Cold chain telemetry** — the BougeRV has no data output. Temperature logging is manual unless you add a separate BLE thermometer.
- ❌ **Dual 28-day infusion alerting** — specified, not implemented.

## 7.4 Known-broken / corrected

- 🔧 **AliveCor KardiaMobile parser — REMOVED in v1.1.0.** The original implementation fabricated a BLE characteristic that does not exist. The 6L uses a proprietary protocol and does rhythm determination in the Kardia app. **If you have an older copy of `specter_medical_hub.py`, delete it.**

  Two workable paths instead:
  - **Polar H10** (~$90) — documented open BLE, continuous HR, RR intervals, and a real ECG waveform. Best fit for continuous monitoring and it feeds NEWS2 directly. **Implemented** as of this build via [`bleakheart`](https://github.com/fsmeraldi/bleakheart) (MPL-2.0), a maintained library for Polar's PMD interface — see `dev_type='polar_h10'` and `_collect_polar_h10_stream()` in `specter_medical_hub.py`. Streams 130Hz single-lead ECG (microvolts) plus heart rate and RR intervals for `polar_stream_seconds` (default 10s) per collection cycle, published to `shtf/medical/vitals/<patient>/ecg_waveform_uv` and the usual `pulse`/`rr_intervals_ms` topics. Delegating the protocol to a real source-checked library is a meaningfully lower-risk starting point than the hand-parsed devices above, but it is **still gated behind `SPECTER_VERIFIED_BLE_DEVICES`** like every other device — nothing here has been run against a real H10 yet. The isolated [Polar H10 validation harness and operator procedure](POLAR_H10_VALIDATION.md) are complete and verified offline; its live mode preserves raw evidence and never enables the production gate automatically.
  - **Original single-lead KardiaMobile** (~$79) — transmits over FM audio, which has been publicly demodulated ([`seemoo-lab/kardia-demod`](https://github.com/seemoo-lab/kardia-demod), GPLv3, needs GNU Radio). Gives a real waveform you can render and hand to MedGemma's multimodal input. Fully air-gapped. You own the accuracy. **Not yet implemented** — a real project for whoever picks up a KardiaMobile.

  For a transplant recipient the high-value ECG use case is **hyperkalemia** (peaked T waves, widening QRS), which needs a waveform. A "normal/AFib" classification byte would not have told you anything useful anyway. The signal-processing layer in [`medical/ecg_analysis.py`](../medical/ecg_analysis.py) uses [NeuroKit2](https://github.com/neuropsychology/NeuroKit) (MIT) for delineation, but its current morphology output is deliberately restricted to **experimental measurements**, not clinical findings:
  - NeuroKit2 returns Q and S peak locations separately from R-onset/R-offset markers. The code therefore publishes `ecg_q_s_peak_interval_ms`, not `ecg_qrs_duration_ms`. A Q-to-S peak interval is not the clinical onset-to-offset QRS duration, and the code, UI, or MedGemma must not apply the clinical 120ms widened-QRS threshold to it.
  - R/T amplitudes are published as absolute amplitudes and their per-beat median ratio (`ecg_r_wave_abs_amplitude_uv`, `ecg_t_wave_abs_amplitude_uv`, `ecg_t_r_abs_ratio`). The ratio is not a validated peaked-T-wave criterion. The former automatic hyperkalemia-style advisory flags have been removed.
  - Each window reports `ecg_analysis_status=experimental_not_clinically_validated` plus the number of valid Q-R-S and R-T beat tuples. A morphology scalar is withheld unless at least three finite, correctly ordered beat tuples survive validation. Incomplete arrays, NaNs, invalid peak order, bad sampling rates, and NeuroKit2 failures degrade to an error/warning instead of escaping into the collector.

  These measurements have only been exercised against synthetic waveforms. They have **not** been validated against a physical Polar H10 and a simultaneous diagnostic ECG with clinician-measured reference intervals. The status and precise measurement names reach `specter_medical_ai.py`'s prompt alongside the scalars so MedGemma cannot silently receive the old diagnostic-looking labels. The raw waveform and RR-interval arrays (`ecg_waveform_uv`, `rr_intervals_ms`) remain excluded from the text prompt (`RAW_ARRAY_READING_TYPES`) — hundreds of bare numbers would waste context and are future multimodal-input material, not implemented here.

- 🔧 **Omron / Masimo BLE parsers — HARD-BLOCKED by default (August 2026).** These weren't just untested — checked against Bluetooth SIG specifications and public reverse-engineering research, they show the same pattern as the removed Kardia parser: plausible-looking code that does not match how these devices actually communicate.

  - **Omron BP7450**: `BluetoothDeviceConfig` points it at service UUID `180a`, the generic Device Information Service (manufacturer/model/serial strings) — not a data service. Its characteristic UUIDs (`2a6e`, `2a6f`, `2a3c`) are the real Bluetooth SIG assignments for **Temperature**, **Humidity**, and **Alert Category ID** — nothing to do with blood pressure or pulse. Independent reverse-engineering ([userx14/omblepy](https://github.com/userx14/omblepy), [evnleong/open-BPM](https://github.com/evnleong/open-BPM)) shows Omron devices actually speak a proprietary EEPROM read/write command protocol, not a single flags+value notification.
  - **Masimo MightySat**: `parse_masimo_oximeter`'s own docstring claims to read the "Standard BLE Heart Rate Measurement" characteristic (0x2A37) and pulls an SpO2 byte out of it — but [that characteristic has no SpO2 field under the Bluetooth spec](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html). SpO2 lives in a separate standard characteristic, PLX Continuous Measurement (0x2A5F, [Pulse Oximeter Service spec](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/PLXS_v1.0.1/out/en/index-en.html)), with a different structure entirely.
  - `collect_from_device()` also only reads the *first* characteristic listed per device ("Simplified: use first reading") and feeds its raw bytes to a parser expecting several characteristics' combined data — an independent bug on top of the protocol mismatch, affecting these two (each lists multiple characteristics).

  A plausible-looking wrong vital sign is worse than no reading — nothing about a parsed number by itself reveals it's fabricated. Rather than delete this code outright (unlike Kardia, there's no confirmed-correct replacement path to point to yet, and the UUID/parsing scaffolding is a starting point for whoever does the real capture), `collect_from_device()` hard-blocks every unverified device type by default: `VERIFIED_DEVICE_TYPES` is empty unless `SPECTER_VERIFIED_BLE_DEVICES` names it. **Verifying one is not a code change** — capture real GATT traffic from the device (a BLE sniffer or `bleak`'s own characteristic dump), confirm or fix the parser against it, then add that `dev_type` to the env var. Until then the hub will scan and discover these devices but publish zero vitals for them, with a startup banner listing what's blocked.

- 🔧 **Contour Next One glucose parser — FIXED (August 2026), still gated.** Unlike Omron, this meter genuinely implements the standard Bluetooth SIG **Glucose Service (0x1808) / Glucose Measurement characteristic (0x2A18)** — confirmed via three independent sources ([weliem/blessed-android](https://github.com/weliem/blessed-android), [NightscoutFoundation/xDrip](https://github.com/NightscoutFoundation/xDrip), [Chakib-Temal/Android_BLE_Usb_Sensors](https://github.com/Chakib-Temal/Android_BLE_Usb_Sensors)). `BluetoothDeviceConfig`'s `service_uuid` is now `1808` (was `180a`, the same Device Information mistake as Omron). `parse_contour_glucometer` now decodes the real record: a flags byte (time-offset/glucose-present/units/sensor-status bits), sequence number, 7-byte base time, and an IEEE 11073-20601 **SFLOAT** concentration — ported from xDrip's `GlucoseReadingRx.java`/`BluetoothCHelper.java` (GPLv3) and cross-checked against the Bluetooth SIG GATT Specification Supplement. The SFLOAT decoder (`_decode_sfloat`) returns `None` — dropping the reading rather than fabricating a number — for the spec's reserved sentinel values (NaN/NRes/±INFINITY), so a device-reported sensor error can't silently turn into a plausible-looking glucose value. Unit conversion (device reports either kg/L or mol/L, selected by a flag bit, never mg/dL directly) uses a molar-mass-derived constant (`GLUCOSE_MOLAR_MASS_G_PER_MOL = 180.156`, IUPAC 2021 standard atomic weights) rather than borrowing either of the two slightly different mmol/L↔mg/dL display constants found in xDrip's own codebase, which convert a different unit and don't apply here. **Still in `VERIFIED_DEVICE_TYPES`'s blocked-by-default set** — a correct decoder against a confirmed-standard protocol is lower-risk than the hand-parsed devices above, but "should be right" isn't "hardware confirmed," the same bar `polar_h10` is held to.

- 🔧 **Braun ThermoScan 7 — cannot be fixed as specified; not a parser bug.** Investigated alongside Contour Next One using the same methodology, with a different outcome: the physical device this kit's docs actually name, the plain **"ThermoScan 7" (IRT6520)**, has **no Bluetooth radio at all**. It's a basic ear thermometer — 9-reading on-device memory button, no app, no wireless sync of any kind — confirmed against Braun's own US/UK product pages and multiple independent reviews (August 2026). Braun sells a visually similar but distinct SKU, **"ThermoScan 7+ Connect"** (BLE 5.0, syncs to the Braun Family Care app), which is a different product requiring its own from-scratch protocol verification if the kit's hardware were swapped to it. `braun_thermoscan` stays hard-blocked permanently under the current kit — there is no real GATT traffic to capture from a device that has no radio, so unlike Contour there is no fix to make here in software. If the field kit is meant to include a Bluetooth-connected thermometer, replace the physical unit with the Connect model and treat it as a new, unverified device.

- 🔧 **MQTT callback API migration and 1.x fallback — FIXED (August 2026).**
  Every Paho client, including the new WARD service, requests callback API
  VERSION2 and uses its connect/disconnect signatures. The helpers retain a
  non-recursive old-style constructor fallback for paho-mqtt 1.x; regression
  tests cover both paths.

- 🔧 **Trauma scene "persistence" only ever wrote, never restored — FIXED (August 2026).** `SceneRegistry._persist()` wrote `scene.json` on every mutation, but nothing ever read it back — a service restart (crash, power loss, upgrade) silently discarded the active scene even though the surrounding comments said persistence existed specifically to survive that. It also wrote the file in place, so a crash mid-write could leave a truncated/corrupt `scene.json`. Fixed: `SceneRegistry.__init__` now calls `_restore()`, which reconstructs every casualty (including full vitals history and tourniquet records — the old persisted shape was `scene_summary()`, which only kept the *latest* vitals reading, not the history) from disk. Writes go through a sibling `.tmp` file with `fsync()` then `os.replace()` (atomic on POSIX), plus a best-effort directory-entry `fsync`, so a crash mid-write can never leave `scene.json` corrupted — the file on disk is always either the complete old state or the complete new state. A corrupt or unrecognized-format file logs loudly and starts an empty scene rather than crashing the service or silently guessing at a malformed structure. See `tests/test_trauma.py::TestSceneRegistryPersistence`.

- 🔧 **Dashboard XSS via alarm text, hardcoded secret, wide-open CORS, `/resus` unrouted — FIXED (August 2026).** An external code review of the dashboard/RESUS surface found several real issues, all now fixed:
  - **innerHTML XSS**: `addAlarm()` in `dashboard.html` built each alarm row with `innerHTML`, interpolating the alarm's `msg` field directly. Any MQTT publisher (see the ACL note in Part 7.2) could put `<img src=x onerror=...>` or similar into an alarm and run arbitrary JavaScript in every connected operator's browser. Now builds the row from DOM nodes with `textContent` — verified with a headless-browser test injecting exactly that payload and confirming no `<img>` tag lands in the DOM and no script runs.
  - **Hardcoded Flask `SECRET_KEY`**: was a literal string in source, so identical on every install (it's in the git repo) — not a secret. Now read from `specter.json`'s `dashboard.secret_key` if the installer sets one, else a random key generated per process start.
  - **`cors_allowed_origins="*"`**: let any origin's page drive the dashboard's WebSocket, including `trigger_rx`. Now defaults to flask-socketio's same-origin-only behavior (`None`) unless `specter.json`'s `dashboard.cors_allowed_origins` explicitly configures a trusted list.
  - **`/resus` route missing**: the medical UI brief documented `/resus`, but `dashboard_server.py` only routed `/`, `/api/state`, and `/api/status`. Added at `https://192.168.1.1/resus`.
  - **Offline dashboard depended on the internet**: `dashboard.html` loaded Socket.IO and D3 from `cdnjs.cloudflare.com` — on a genuinely offline network both `io` and `d3` came back `undefined` and the dashboard's core script failed outright, which is the opposite of what an offline-first emergency dashboard needs. Both are now vendored locally under `dashboard/vendor/` (same exact versions, MIT/ISC licensed) and served by Flask's static route.
  - **RF waterfall permanently simulated with no indication**: the spectrum panel renders random noise unconditionally — there is no real waterfall MQTT topic, server handler, or SDR pipeline anywhere in this codebase (Node 3/4/6 workloads remain unimplemented, Part 7.3). It now carries a permanent `SIMULATED DATA` badge and watermark rather than looking like live RF telemetry.
  - **Node health could stay green forever**: the server stamps a wrapper-level `last_seen` on every Pi status update, but it never reached the client (`applyFullState` discarded it; the live push never sent it), and nothing re-evaluated a node's dot color once painted — a Pi that reported once and then went dark stayed "healthy" indefinitely. `last_seen` now travels with both the live push and the full-state snapshot, and a client-side timer re-derives every dot's color every 5s from age, not just on new traffic. The "Pi count" badge was also counting every green dot on the page, including SDR hardware indicators sharing the same CSS class — scoped to the node-status panel only.
  - **`/api/status` "uptime" was the Unix epoch**: `int(time.time())` reported billions of seconds of uptime. Now `time.time() - START_TIME` (process start).
  - **Alarm timestamps came from the browser, not the source event; reconnects could duplicate alarms; the initial "No alarms" placeholder never cleared**: the live `alarm` push sent only the raw MQTT payload, dropping the server-side receive timestamp entirely. Alarms now carry `{time, data}` consistently (live push and reconnect replay both), the client de-dupes by that timestamp instead of guessing, and the placeholder text is cleared on first real alarm rather than sitting above the real list forever.
  - **Version strings frozen at 1.0.0**: `dashboard_server.py`'s `VERSION` is bumped to reflect the fixes in this pass, and the dashboard footer now fetches it from `/api/status` instead of a hardcoded string in the HTML, so it can't drift again silently.
  - **Unpinned dependencies**: `requirements-dev.txt`, `deploy/install_specter.py`'s `PIP_PACKAGES`, and the `pip install` commands in this manual now pin exact versions for every package this repo's test suite actually exercises, so a field-kit rebuild months from now can't silently pull a materially different, untested version. A few Pi-hardware-only packages (`scipy`, `soundfile`, `pyaudio`, `pyserial`, `gps3`, `matplotlib`) remain unpinned — this project's test suite doesn't exercise them, so guessing a version to pin would be no more trustworthy than leaving them open; pin those once they get their own verification pass.

  **Not fixed in this pass** — flagged, not silently left implied as done: RESUS is still demo-only and not wired to live trauma MQTT state (see the Part 7.2 entry above); the tourniquet-toggle-off inconsistency in RESUS is fixed (un-checking a MARCH step now logs an explicit correction event and marks the open tourniquet record `removed` rather than either leaving it silently ticking or deleting it — append-only, per the review's own recommendation), but RESUS's cards/checklist rows are now keyboard-operable (added `tabindex`/`role="button"`/Enter-Space handling, with focus preserved across the once-a-second redraw) while the "Ask MedGemma"/"Ward"/"Chronic" controls are now explicitly labeled `NOT INSTALLED` rather than looking clickable and doing nothing.

- 🔧 **Dashboard authentication and browser transport — FIXED (August 2026).**
  Dashboard, RESUS, WARD, state API, vendor assets, and Socket.IO control
  events require an installer-generated operator login. Passwords are stored
  as PBKDF2 hashes; login is CSRF-protected; sessions use HttpOnly,
  SameSite=Strict, Secure cookies and expire after eight hours. `/api/status`
  remains public as a minimal health probe.

  The installer binds Flask to `127.0.0.1:5000`, generates a stable 3072-bit
  RSA certificate with the node IP in its subject-alt-name, and exposes only
  an Nginx TLS 1.2/1.3 endpoint. Verify the certificate fingerprint out of
  band from `/etc/specter/tls/dashboard.sha256`, then import/trust
  `/etc/specter/tls/dashboard.crt` on operator devices. Accepting an
  unverified self-signed certificate does not protect the first connection
  against impersonation.

  **Still not built**: per-operator accounts/audit attribution and separate
  read-only/full-control roles. MQTT ACLs isolate services, but MQTT command
  payloads do not contain a per-human operator identity.

## 7.5 The binding constraint

**Training, not hardware.**

Needle decompression performed wrong kills people. An i-gel in a patient with a gag reflex causes aspiration. Buying the kit without the training buys a false sense of readiness, which is worse than owning nothing.

| Course | Time | Cost | Priority |
|---|---|---|---|
| **Stop the Bleed** | 2 h | Free | **Do this first.** Tourniquet, packing, pressure — the intervention you are most likely to need. |
| **WFR** | 70–80 h | ~$700 | Assessment, ward care, evacuation decisions. Highest-value non-hardware investment. |
| **TCCC-MP** | 16–40 h | $300–800 | Chest seals, decompression, airway adjuncts |
| **IV/IO or phlebotomy** | Varies | $200–600 | Self-cannulation for Belatacept **and** trauma access — one course serves both |

---

# APPENDIX A — QUICK REFERENCE

```bash
# Health
bash /opt/specter/scripts/health_check.sh
systemctl status 'specter-*'
journalctl -u 'specter-*' -f

# MQTT (requires -u/-P per Part 3.3)
mosquitto_sub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'shtf/#' -v
mosquitto_sub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'shtf/medical/#' -v
mosquitto_sub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t 'shtf/trauma/#' -v

# RF
touch /run/specter/sdr_trigger
python3 /opt/specter/services/specter_rx_ring_buffer.py list-devices

# Medical
python3 /opt/specter/medical/specter_medical_ai.py --ask "QUESTION" --patient operator
/opt/specter/scripts/specter-ask "How do I treat a tension pneumothorax?"
python3 /opt/specter/medical/specter_ecg_ai.py --config /etc/specter/specter.json status
python3 /opt/specter/medical/specter_ecg_ai.py --config /etc/specter/specter.json import EXAM.xml

# Import a licensed public 12-lead WFDB record without calling it Biocare data
python3 /opt/specter/medical/specter_ecg_ai.py --config /etc/specter/specter.json import-wfdb /data/ptb-xl/records100/00000/00001_lr \
  --patient-id DATASET-PATIENT-ID --study-id 00001_lr \
  --acquired-at 2000-01-01T00:00:00Z --device "PTB-XL source recorder" \
  --dataset-id ptb-xl --dataset-version 1.0.3 --dataset-license "ODbL 1.0"

# Generate a plumbing fixture; output declares vendorCompatibility=none
python3 /opt/specter/medical/specter_ecg_ai.py --config /etc/specter/specter.json make-synthetic-xml /data/ptb-xl/records100/00000/00001_lr \
  --patient-id SYNTHETIC-PTB-1 --study-id 00001_lr \
  --acquired-at 2000-01-01T00:00:00Z --device "PTB-XL source recorder" \
  --dataset-id ptb-xl --dataset-version 1.0.3 --dataset-license "ODbL 1.0" \
  --output /mnt/specter/live/ecg/inbox/ptb-fixture.xml

# Trauma
python3 /opt/specter/trauma/specter_trauma.py --print-protocol > march_card.txt
python3 /opt/specter/trauma/specter_trauma_monitor.py --mqtt-host 192.168.1.1

# Web
https://192.168.1.1           Dashboard (operator login required)
https://192.168.1.1/resus     RESUS screens (demo, not live - Part 7.2)
https://192.168.1.1/ward      WARD screen (live)
https://192.168.1.1/ecg       12-lead ECG research review (structured results; no waveform arrays)
http://192.168.1.5:8080       Kiwix library
http://192.168.1.10:11434     Ollama API
```

## Paths

| Path | Contents |
|---|---|
| `/opt/specter/` | Application + venv |
| `/etc/specter/specter.json` | Master config |
| `/var/log/specter/` | Service logs |
| `/var/lib/specter/scene.json` | Trauma scene state |
| `/mnt/specter/library/` | Kiwix ZIM + PDF corpus (Node 5) |
| `/mnt/specter/live/recordings/` | RF captures + sidecar JSON |
| `/mnt/specter/live/ecg/inbox/` | Biocare XML import inbox |
| `/mnt/specter/archive/ecg/` | Immutable ECG sources, canonical waveforms, metadata and analyses |
| `/run/specter/sdr_trigger` | RX trigger file |

## Alarm thresholds

| Condition | Level |
|---|---|
| MAP < 65 | Critical — graft perfusion floor |
| qSOFA ≥ 2 | Critical — sepsis in immunosuppressed |
| Shock index > 1.0 | Critical — decompensating |
| SpO2 < 90% | Critical |
| NEWS2 ≥ 7, or any single parameter = 3 | Critical |
| Tourniquet > 2h / > 4h | Caution / Critical |
| Cold chain outside 2–8 °C | Critical |
| Reconstitution < 4h remaining | Critical |
| Reposition overdue > 30 min | Critical |
| Fluid balance < −1,000 or > +1,500 mL/24h | Critical |
| Temp ≥ 38.0 °C | Critical (immunosuppressed) |
| CPU ≥ 75 °C / ≥ 85 °C | Warning / Emergency halt |

---

**SPECTER is decision support for a trained responder. It is not a clinician. Contact a physician whenever that is possible.**
