#!/usr/bin/env bash
# SPECTER health check — prints status of all services and hardware
# Usage: bash health_check.sh

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "  ${RED}✗${NC}  $1"; }
head() { echo -e "\n${CYAN}── $1 ──────────────────────────────────────────────${NC}"; }

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║         SPECTER HEALTH CHECK                     ║"
echo "║  $(date -u '+%Y-%m-%dT%H:%M:%SZ')                         ║"
echo "╚══════════════════════════════════════════════════╝"

head "SYSTEM"
echo "  Host:    $(hostname)"
echo "  IP:      $(hostname -I | awk '{print $1}')"
echo "  Uptime:  $(uptime -p)"
echo "  Load:    $(uptime | awk -F'load average:' '{print $2}')"

TEMP=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
if [ -n "$TEMP" ]; then
  TC=$(echo "scale=1; $TEMP/1000" | bc)
  if (( $(echo "$TC > 75" | bc -l) )); then
    warn "CPU: ${TC}°C  (HIGH)"
  else
    ok "CPU: ${TC}°C"
  fi
fi

head "SERVICES"
SERVICES=(
  "specter-mqtt.service"
  "specter-dashboard.service"
  "specter-rx-buffer.service"
  "specter-mqtt-coordinator.service"
  "specter-sdr-control.service"
  "specter-thermal.service"
  "gpsd.service"
)
for svc in "${SERVICES[@]}"; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    ok "$svc"
  else
    fail "$svc  (INACTIVE)"
  fi
done

head "SDR HARDWARE (USB)"
lsusb_out=$(lsusb 2>/dev/null)

if echo "$lsusb_out" | grep -q "1d50:6089"; then
  ok "HackRF One"
else
  fail "HackRF One  — NOT DETECTED"
fi

if echo "$lsusb_out" | grep -q "0456:b673"; then
  ok "PlutoSDR (ADALM-Pluto)"
else
  fail "PlutoSDR  — NOT DETECTED"
fi

kraken_count=$(echo "$lsusb_out" | grep -c "0bda:2838" || true)
if [ "$kraken_count" -ge 5 ]; then
  ok "KrakenSDR  (${kraken_count} RTL devices)"
elif [ "$kraken_count" -ge 1 ]; then
  warn "RTL-SDR detected (${kraken_count}) — need 5 for KrakenSDR"
else
  fail "RTL-SDR / KrakenSDR  — NOT DETECTED"
fi

head "GPS"
if ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1 | grep -q dev; then
  GPS_DEV=$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1)
  ok "GPS device: $GPS_DEV"
  if systemctl is-active --quiet gpsd; then
    ok "gpsd running"
  else
    warn "gpsd not running"
  fi
else
  fail "GPS device not found"
fi

head "STORAGE"
for MNT in / /mnt/specter/live /mnt/specter/archive; do
  if mountpoint -q "$MNT" 2>/dev/null || [ "$MNT" = "/" ]; then
    PCT=$(df "$MNT" 2>/dev/null | awk 'NR==2{gsub(/%/,""); print $5}')
    AVAIL=$(df -h "$MNT" 2>/dev/null | awk 'NR==2{print $4}')
    if [ "${PCT:-0}" -gt 85 ]; then
      warn "$MNT — ${PCT}% used (${AVAIL} free)"
    else
      ok "$MNT — ${PCT}% used (${AVAIL} free)"
    fi
  else
    warn "$MNT — not mounted"
  fi
done

head "MQTT BROKER"
if mosquitto_sub -h 192.168.1.1 -t "shtf/system/heartbeat" -C 1 -W 3 >/dev/null 2>&1; then
  ok "MQTT broker reachable and publishing"
else
  warn "MQTT broker not responding within 3s"
fi

head "DASHBOARD"
if curl -sf http://localhost:5000/api/status >/dev/null 2>&1; then
  ok "Dashboard HTTP reachable: http://$(hostname -I | awk '{print $1}'):5000"
else
  fail "Dashboard not reachable"
fi

echo ""
echo "════════════════════════════════════════════════════"
echo "  Done. Logs: journalctl -u specter-* -f"
echo ""
