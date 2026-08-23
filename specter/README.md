# SPECTER MONSTER v1.2.0

Field-deployable emergency communications and medical command center.

**Start here: [`docs/MANUAL.md`](docs/MANUAL.md)** — architecture, install, operations, troubleshooting.

**Then read [`docs/MANUAL.md` Part 7](docs/MANUAL.md#part-7--known-gaps)** — the honest register of what is built, what is specified but unbuilt, and what was found broken and corrected. Read it before relying on any of this.

## Layout

| Dir | Contents |
|---|---|
| `core/` | Node 1 master services — MQTT coordinator, SDR control, thermal watchdog |
| `services/` | RX ring buffer, library RAG API, index builder |
| `medical/` | Bluetooth vitals hub (Pi Zero 2W), MedGemma AI engine (Jetson) |
| `trauma/` | Casualty registry, START triage, MARCH state machine, terminal monitor |
| `dashboard/` | Flask backend, main dashboard, RESUS triage + MARCH screens |
| `scripts/` | Health check, disk report, library health, CLI query tool |
| `config/` | Master configuration |
| `systemd/` | 12 unit files, all nodes |
| `deploy/` | Installers and clone/deploy kit |
| `docs/` | Manual, UI brief, clinical modes spec, computational BOM |

## Fast start

```bash
# Node 1 (master) - set a real shared password first, see docs/MANUAL.md
# Part 3.3; every other node's install must use the same one.
export SPECTER_MQTT_PASSWORD='pick-a-real-password-here'
sudo cp -r . /opt/specter/
sudo python3 /opt/specter/deploy/install_specter.py

# Verify
bash /opt/specter/scripts/health_check.sh
mosquitto_sub -h 192.168.1.1 -u specter-operator -P "$SPECTER_MQTT_PASSWORD" -t 'shtf/#' -v
```

## Try the trauma screens with no hardware

Open `dashboard/resus.html` in any browser. Fully interactive — live tourniquet clocks, state-driven MARCH directive, staleness alerts.

## Generate the paper MARCH card

```bash
python3 trauma/specter_trauma.py --print-protocol > march_card.txt
```

Print it. Laminate it. It is the only part of this system that works with no power.

---

Decision support for a trained responder. Not a clinician.
