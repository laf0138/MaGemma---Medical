#!/usr/bin/env bash
# SPECTER disk usage reporter — publishes to MQTT
# Called by cron every 10 minutes

BROKER="192.168.1.1"
TOPIC="shtf/system/disk"

# Broker requires auth (docs/MANUAL.md Part 3.3) - read the credential the
# installer wrote to specter.json, falling back to the documented default.
MQTT_USER=$(python3 -c "import json;print(json.load(open('/etc/specter/specter.json')).get('mqtt',{}).get('username','specter-operator'))" 2>/dev/null || echo "specter-operator")
MQTT_PASS=$(python3 -c "import json;print(json.load(open('/etc/specter/specter.json')).get('mqtt',{}).get('password','specter-change-me'))" 2>/dev/null || echo "specter-change-me")

LIVE_USED=$(df -h /mnt/specter/live  2>/dev/null | awk 'NR==2{print $3}')
LIVE_AVAIL=$(df -h /mnt/specter/live 2>/dev/null | awk 'NR==2{print $4}')
LIVE_PCT=$(df /mnt/specter/live      2>/dev/null | awk 'NR==2{print $5}' | tr -d '%')

ROOT_USED=$(df -h /  2>/dev/null | awk 'NR==2{print $3}')
ROOT_AVAIL=$(df -h / 2>/dev/null | awk 'NR==2{print $4}')
ROOT_PCT=$(df /      2>/dev/null | awk 'NR==2{print $5}' | tr -d '%')

REC_COUNT=$(find /mnt/specter/live/recordings -name "*.wav" 2>/dev/null | wc -l)
ARCHIVE_COUNT=$(find /mnt/specter/archive -name "*.wav"    2>/dev/null | wc -l)

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

PAYLOAD=$(cat <<EOF
{
  "timestamp_utc": "${TS}",
  "live": {
    "used": "${LIVE_USED}",
    "available": "${LIVE_AVAIL}",
    "pct": ${LIVE_PCT:-0}
  },
  "root": {
    "used": "${ROOT_USED}",
    "available": "${ROOT_AVAIL}",
    "pct": ${ROOT_PCT:-0}
  },
  "recording_count": ${REC_COUNT},
  "archive_count":   ${ARCHIVE_COUNT}
}
EOF
)

mosquitto_pub -h "${BROKER}" -u "${MQTT_USER}" -P "${MQTT_PASS}" -t "${TOPIC}" -m "${PAYLOAD}" 2>/dev/null || true

# Alarm if live storage > 85%
if [ "${LIVE_PCT:-0}" -gt 85 ]; then
  ALARM="{\"source\":\"disk_report\",\"level\":\"warning\",\"msg\":\"Live storage ${LIVE_PCT}% full — archive or clear recordings\"}"
  mosquitto_pub -h "${BROKER}" -u "${MQTT_USER}" -P "${MQTT_PASS}" -t "shtf/system/alarm" -m "${ALARM}" 2>/dev/null || true
fi
