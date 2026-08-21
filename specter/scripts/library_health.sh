#!/usr/bin/env bash
# SPECTER Library Health Check
# Verifies Ollama, kiwix-serve, library API, and content inventory

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "  ${RED}✗${NC}  $1"; }
head() { echo -e "\n${CYAN}── $1 ───────────────────────────────────────────${NC}"; }

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║    SPECTER LIBRARY HEALTH CHECK                  ║"
echo "║    $(date -u '+%Y-%m-%dT%H:%M:%SZ')             ║"
echo "╚══════════════════════════════════════════════════╝"

head "SERVICES"
SERVICES=("specter-kiwix.service" "specter-ollama.service" "specter-library-api.service" "specter-index-builder.timer")
for svc in "${SERVICES[@]}"; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    ok "$svc"
  else
    fail "$svc  ← NOT RUNNING"
  fi
done

head "OLLAMA"
if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
  ok "Ollama API reachable"
  MODELS=$(curl -sf http://localhost:11434/api/tags | python3 -c "
import json,sys
d=json.load(sys.stdin)
for m in d.get('models',[]): print(f\"  {m['name']}\")
" 2>/dev/null)
  if [[ -n "$MODELS" ]]; then
    echo "$MODELS" | while read -r line; do ok "Model: $line"; done
  else
    warn "No models loaded — run: ollama pull llama3.2:3b-instruct-q4_K_M"
  fi
else
  fail "Ollama API not reachable (port 11434)"
fi

head "KIWIX LIBRARY"
if curl -sf http://localhost:8080 >/dev/null 2>&1; then
  ok "kiwix-serve reachable at :8080"
else
  fail "kiwix-serve not reachable"
fi
ZIM_COUNT=$(find /mnt/specter/library/zim -name "*.zim" 2>/dev/null | wc -l)
ok "ZIM files: $ZIM_COUNT"
if [[ "$ZIM_COUNT" -gt 0 ]]; then
  find /mnt/specter/library/zim -name "*.zim" 2>/dev/null | while read -r f; do
    SIZE=$(du -h "$f" | cut -f1)
    ok "  $(basename "$f")  ($SIZE)"
  done
fi

head "PDF LIBRARY"
PDF_COUNT=$(find /mnt/specter/library/pdf -name "*.pdf" 2>/dev/null | wc -l)
ok "PDF files: $PDF_COUNT"
for CAT in 01_MEDICAL_TRAUMA 02_RADIATION_CBRN 03_FOOD_PRESERVATION 04_AGRICULTURE_CROPS \
           10_REPAIR_FABRICATION 11_CONSTRUCTION_SHELTER 15_LOCAL_UTAH_LDS 17_MORALE_EDUCATION; do
  COUNT=$(find "/mnt/specter/library/pdf/$CAT" -name "*.pdf" 2>/dev/null | wc -l)
  if [[ "$COUNT" -gt 0 ]]; then
    ok "$CAT: $COUNT files"
  else
    warn "$CAT: 0 files"
  fi
done

head "RAG INDEX"
INDEX_DIR="/mnt/specter/library/vector_index"
if [[ -d "$INDEX_DIR" ]]; then
  ok "Vector index directory exists: $INDEX_DIR"
  INDEX_SIZE=$(du -sh "$INDEX_DIR" 2>/dev/null | cut -f1)
  ok "Index size: $INDEX_SIZE"
else
  warn "Vector index not built yet — run: python3 /opt/specter/services/build_index.py"
fi

head "LIBRARY API"
if curl -sf http://localhost:5001/status >/dev/null 2>&1; then
  ok "Library API reachable at :5001"
  curl -sf http://localhost:5001/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"  ZIM files:      {d.get('zim_files',0)}\")
print(f\"  PDF files:      {d.get('pdf_files',0)}\")
print(f\"  Indexed chunks: {d.get('indexed_chunks',0)}\")
print(f\"  Uptime:         {d.get('uptime_sec',0)}s\")
" 2>/dev/null
else
  fail "Library API not reachable at :5001"
fi

head "STORAGE"
for MNT in / /mnt/specter/library; do
  if [[ -d "$MNT" ]]; then
    PCT=$(df "$MNT" 2>/dev/null | awk 'NR==2{gsub(/%/,""); print $5}')
    AVAIL=$(df -h "$MNT" 2>/dev/null | awk 'NR==2{print $4}')
    if [[ "${PCT:-0}" -gt 85 ]]; then
      warn "$MNT: ${PCT}% used (${AVAIL} free) ← LOW"
    else
      ok "$MNT: ${PCT}% used (${AVAIL} free)"
    fi
  fi
done

head "QUICK TEST"
echo "  Running test query: 'bleeding control tourniquet'"
RESULT=$(curl -sf -X POST http://localhost:5001/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"bleeding control tourniquet"}' 2>/dev/null)
if [[ -n "$RESULT" ]]; then
  ELAPSED=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('elapsed_sec','?'))" 2>/dev/null)
  ok "Query succeeded (${ELAPSED}s)"
else
  fail "Test query failed — check specter-library-api.service logs"
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Kiwix:       http://$(hostname -I | awk '{print $1}'):8080"
echo "  Library API: http://$(hostname -I | awk '{print $1}'):5001"
echo "  CLI:         specter-ask \"your question\""
echo "  Logs:        journalctl -u specter-library-api -f"
echo ""
