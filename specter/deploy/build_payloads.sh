#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║       SPECTER — SNAPSHOT BUILDER  (build_payloads.sh)                   ║
# ║                                                                          ║
# ║  Run this on the MASTER NODE (internet-connected, fully installed)       ║
# ║  to create the bundled tarballs for offline cloning.                     ║
# ║                                                                          ║
# ║  Output:  ./payloads/specter_sdr.tar.gz     (~2-5 GB)                   ║
# ║           ./payloads/specter_library.tar.gz (~150-300 GB)               ║
# ║                                                                          ║
# ║  Usage:   sudo bash build_payloads.sh [sdr|library|both]               ║
# ║  Default: both                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_DIR="$SCRIPT_DIR/payloads"
TARGET="${1:-both}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "  ${RED}✗${NC}  $1"; }
step() { echo -e "  ${CYAN}▶${NC}  $1"; }
head() { echo -e "\n${CYAN}══ $1 ══════════════════════════════════════════════${NC}"; }

if [[ "$EUID" -ne 0 ]]; then
  echo "[ERROR] Run as root: sudo bash build_payloads.sh"
  exit 1
fi

mkdir -p "$PAYLOAD_DIR"
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   SPECTER Snapshot Builder — Payload Packer          ║"
echo "║   Output: $PAYLOAD_DIR"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Target: $TARGET"
echo "  This creates offline clone payloads from this machine."
echo ""

# ─── SDR Package ──────────────────────────────────────────────────────────────
build_sdr() {
  head "Building SDR Payload"
  OUT="$PAYLOAD_DIR/specter_sdr.tar.gz"
  TMP="$PAYLOAD_DIR/specter_sdr.tar.gz.part"

  step "Stopping services to get clean snapshot ..."
  systemctl stop specter-rx-buffer.service  2>/dev/null || true
  systemctl stop specter-dashboard.service  2>/dev/null || true

  step "Creating tarball: $OUT"
  step "  Sources: /opt/specter  /etc/specter  /etc/systemd/system/specter-*  /etc/udev/rules.d/99-specter-sdr.rules"

  tar --create \
      --gzip \
      --file="$TMP" \
      --exclude="*.pyc" \
      --exclude="__pycache__" \
      --exclude="*.log" \
      --exclude="*.part" \
      --warning=no-file-changed \
      /opt/specter \
      /etc/specter \
      $(ls /etc/systemd/system/specter-*.service 2>/dev/null || true) \
      $(ls /etc/systemd/system/specter-*.timer   2>/dev/null || true) \
      /etc/mosquitto/conf.d/specter.conf  2>/dev/null || true \
      /etc/udev/rules.d/99-specter-sdr.rules 2>/dev/null || true \
      /etc/modprobe.d/specter-rtlsdr.conf 2>/dev/null || true \
      /etc/cron.d/specter 2>/dev/null || true \
    && mv "$TMP" "$OUT" \
    || { fail "SDR tarball failed"; rm -f "$TMP"; return 1; }

  SIZE=$(du -sh "$OUT" | cut -f1)
  ok "SDR payload: $OUT  ($SIZE)"

  # Restart services
  systemctl start specter-rx-buffer.service 2>/dev/null || true
  systemctl start specter-dashboard.service 2>/dev/null || true

  # Generate SHA256
  sha256sum "$OUT" > "$OUT.sha256"
  ok "Checksum: $OUT.sha256"
}

# ─── Library Package ──────────────────────────────────────────────────────────
build_library() {
  head "Building AI Library Payload"
  OUT="$PAYLOAD_DIR/specter_library.tar.gz"
  TMP="$PAYLOAD_DIR/specter_library.tar.gz.part"

  step "Stopping library services ..."
  systemctl stop specter-library-api.service 2>/dev/null || true

  step "Estimating size ..."
  LIBRARY_SIZE=$(du -sh /mnt/specter/library 2>/dev/null | cut -f1 || echo "?")
  step "Library directory: $LIBRARY_SIZE"
  warn "This may take 1-4 hours for a full library."

  step "Creating tarball: $OUT"
  step "  Sources: /opt/specter  /etc/specter/library.json  /mnt/specter/library"
  step "  Includes: ZIM files, PDFs, vector index, Ollama models"

  # Include Ollama models directory
  OLLAMA_MODEL_DIR=""
  for CANDIDATE in \
    "/usr/share/ollama/.ollama/models" \
    "/root/.ollama/models" \
    "/home/ollama/.ollama/models"; do
    if [[ -d "$CANDIDATE" ]]; then
      OLLAMA_MODEL_DIR="$CANDIDATE"
      break
    fi
  done

  if [[ -n "$OLLAMA_MODEL_DIR" ]]; then
    step "  Ollama models: $OLLAMA_MODEL_DIR"
    MODEL_SIZE=$(du -sh "$OLLAMA_MODEL_DIR" 2>/dev/null | cut -f1 || echo "?")
    step "  Model size: $MODEL_SIZE"
  else
    warn "  Ollama model directory not found — models will need re-pulling on target"
  fi

  tar --create \
      --gzip \
      --file="$TMP" \
      --exclude="*.pyc" \
      --exclude="__pycache__" \
      --exclude="*.log" \
      --exclude="*.part" \
      --warning=no-file-changed \
      /opt/specter \
      /etc/specter \
      $(ls /etc/systemd/system/specter-kiwix.service   2>/dev/null || true) \
      $(ls /etc/systemd/system/specter-ollama.service  2>/dev/null || true) \
      $(ls /etc/systemd/system/specter-library-api.service 2>/dev/null || true) \
      $(ls /etc/systemd/system/specter-index-builder.* 2>/dev/null || true) \
      /mnt/specter/library \
      ${OLLAMA_MODEL_DIR:-} \
    && mv "$TMP" "$OUT" \
    || { fail "Library tarball failed"; rm -f "$TMP"; return 1; }

  SIZE=$(du -sh "$OUT" | cut -f1)
  ok "Library payload: $OUT  ($SIZE)"

  # Restart
  systemctl start specter-library-api.service 2>/dev/null || true

  # Checksum
  sha256sum "$OUT" > "$OUT.sha256"
  ok "Checksum: $OUT.sha256"
}

# ─── Manifest ─────────────────────────────────────────────────────────────────
write_manifest() {
  head "Writing Payload Manifest"
  MANIFEST="$PAYLOAD_DIR/MANIFEST.json"
  TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  HOSTNAME=$(hostname)

  python3 -c "
import json, os, hashlib
from pathlib import Path

payload_dir = Path('$PAYLOAD_DIR')
files = {}
for f in sorted(payload_dir.glob('*.tar.gz')):
    sha_file = f.with_suffix('.gz.sha256')
    sha = sha_file.read_text().split()[0] if sha_file.exists() else 'unknown'
    files[f.name] = {
        'size_bytes': f.stat().st_size,
        'sha256': sha,
    }

manifest = {
    'version': '1.0.0',
    'built_on': '$TIMESTAMP',
    'built_by': '$HOSTNAME',
    'files': files,
}
Path('$MANIFEST').write_text(json.dumps(manifest, indent=2))
print('  Manifest: $MANIFEST')
"
  ok "Manifest written: $MANIFEST"
}

# ─── Execute ──────────────────────────────────────────────────────────────────
case "$TARGET" in
  sdr)     build_sdr ;;
  library) build_library ;;
  both)    build_sdr; build_library ;;
  *)       echo "Usage: $0 [sdr|library|both]"; exit 1 ;;
esac

write_manifest

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Payloads ready in: $PAYLOAD_DIR"
echo "  Copy the entire SHTF_AI_SDR_Install/ folder to USB drive"
echo "  and run: sudo python3 clone_deploy.py  on the target"
echo "═══════════════════════════════════════════════════════════════"
echo ""
ls -lh "$PAYLOAD_DIR/"
echo ""
