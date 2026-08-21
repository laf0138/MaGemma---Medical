# SPECTER Offline AI Library
## Field Installer & Operations Guide  v1.0.0

---

## WHAT YOU DO (ONE TIME)

1. Buy a **Jetson Orin Nano Super** ($249) — connects to your PoE switch at 192.168.1.5
2. Flash **JetPack 6.x** (NVIDIA's Linux OS for Jetson) via SDK Manager or SD card
3. Plug into the Netgear GS308EP on your SPECTER PoE mesh
4. Run the installer — **then walk away**

```bash
sudo python3 install_specter_library.py
```

Expected runtime: **4–12 hours** depending on your pre-deployment download speed.  
Expected storage: **150–300 GB** (Wikipedia full-text ~90 GB alone).

---

## WHAT THE INSTALLER DOES AUTOMATICALLY

```
Phase  1  System packages (apt)
Phase  2  Python venv + pip dependencies
Phase  3  Ollama install + LLaMA 3.2 3B Q4_K_M model pull
Phase  4  kiwix-serve install (ARM64 binary)
Phase  5  Directory structure creation
Phase  6  ZIM file downloads (Wikipedia, WikiMed, Khan Academy, etc.)
Phase  7  PDF downloads (MSF, ORNL, USDA, FEMA, Army TMs, etc.)
Phase  8  Zimit scrapes (Merck Vet, USU Extension)
Phase  9  LDS scriptures structured JSON from GitHub + Church CDN
Phase 10  RAG vector index build (ChromaDB + local embeddings)
Phase 11  Service file deployment
Phase 12  Systemd units install + service start
Phase 13  Config file write
Phase 14  Post-install report
```

**Resume-safe.** If power is lost mid-download, re-run the installer.  
`wget --continue` resumes all partial downloads automatically.

---

## LIBRARY MANIFEST

### ZIM Files (Kiwix — full-text searchable)

| Category | File | Size |
|---|---|---|
| Medical | WikiMed (75,000+ articles) | ~2.5 GB |
| Education | Wikipedia EN full + images | ~90 GB |
| Education | Khan Academy complete | ~15 GB |
| Education | Wikibooks | ~4 GB |
| Education | Wikiversity | ~2 GB |
| Repair | iFixit repair guides | ~3.5 GB |
| Education | Project Gutenberg (70k books) | ~60 GB |
| Technical | Stack Overflow | ~90 GB |
| Navigation | Wikivoyage | ~0.8 GB |

### PDFs (RAG-indexed, AI-searchable)

| Category | Document |
|---|---|
| 01_MEDICAL | MSF Clinical Guidelines 2025 |
| 01_MEDICAL | MSF Paediatric Care 2024 |
| 01_MEDICAL | WHO Basic Emergency Care |
| 01_MEDICAL | US Army SF Medical Handbook |
| 01_MEDICAL | Where There Is No Doctor |
| 01_MEDICAL | Where There Is No Dentist |
| 02_CBRN | Nuclear War Survival Skills (ORNL/Kearny) |
| 02_CBRN | FEMA TR-87 Fallout Shelter Manual |
| 02_CBRN | Army FM 3-11 CBRN Operations |
| 03_FOOD | USDA Complete Guide to Home Canning |
| 04_AGRICULTURE | FAO Seeds in Emergencies |
| 04_AGRICULTURE | FAO Crop Production in Disaster Areas |
| 10_REPAIR | Army TM 5-551A Carpenter Tools |
| 10_REPAIR | Army FM 5-34 Engineer Field Data |
| 11_SHELTER | UNHCR Handbook for Emergencies |
| 11_SHELTER | FEMA P-348 Flood Utilities |
| 11_SHELTER | Army FM 3-34.343 Bridging & Rigging |
| 11_SHELTER | Army FM 5-125 Rigging Techniques |
| 15_LDS | LDS Standard Works (KJV + BoM + D&C + PoGP) |

### Scraped (Zimit)

| Category | Source |
|---|---|
| 01_MEDICAL | Merck MSD Veterinary Manual |
| 15_LDS | USU Extension (Intermountain West) |
| 04_AGRICULTURE | USDA National Agricultural Library |

### Structured Text

| Source | Format |
|---|---|
| LDS Scriptures (all standard works) | JSON / SQLite / CSV / HTML / TXT |
| LDS individual volumes (BoM, D&C, PoGP) | PDF from ChurchofJesusChrist.org |

---

## SERVICES

| Service | Port | Role |
|---|---|---|
| specter-kiwix.service | 8080 | Kiwix full-text search across all ZIMs |
| specter-ollama.service | 11434 | LLaMA 3.2 3B Q4 inference |
| specter-library-api.service | 5001 | Flask RAG API (search + AI answer) |
| specter-index-builder.timer | — | Nightly ChromaDB rebuild at 02:00 |

---

## ACCESS

### Browser (any device on the mesh)
```
http://192.168.1.5:8080    ← Kiwix full library browser
http://192.168.1.5:5001    ← Library RAG API
```

### CLI (from any Pi on the mesh)
```bash
specter-ask "How do I treat a tension pneumothorax?"
specter-ask "Nuclear fallout shelter construction"
specter-ask "LDS food storage guidelines"
specter-ask --search "dosimeter radiation"
specter-ask --status
specter-ask --categories
```

### MQTT (from any SPECTER service)
The broker requires auth - see `docs/MANUAL.md` Part 3.3.
```bash
# Ask a question
mosquitto_pub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t shtf/library/ask \
  -m '{"query": "How do I build an expedient fallout shelter?"}'

# Subscribe to answers
mosquitto_sub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t shtf/library/response -v

# Library heartbeat
mosquitto_sub -h 192.168.1.1 -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t shtf/library/status -v
```

### Dashboard integration
The SPECTER dashboard at 192.168.1.1:5000 includes an **AI LIBRARY** panel that sends queries to `shtf/library/ask` and displays answers via `shtf/library/response`.

---

## MAINTENANCE

```bash
# Health check
bash /opt/specter/scripts/library_health.sh

# Rebuild RAG index manually
python3 /opt/specter/services/build_index.py

# Check logs
journalctl -u specter-library-api -f
journalctl -u specter-kiwix -f
journalctl -u specter-ollama -f

# Restart services
systemctl restart specter-library-api.service
systemctl restart specter-kiwix.service

# Add a new ZIM file (drop into category dir, restart kiwix)
cp new_file.zim /mnt/specter/library/zim/17_MORALE_EDUCATION/
systemctl restart specter-kiwix.service

# Add a new PDF (drop in, rebuild index)
cp new_doc.pdf /mnt/specter/library/pdf/01_MEDICAL_TRAUMA/
python3 /opt/specter/services/build_index.py
```

---

## AI MODEL DETAILS

**Primary model:** `llama3.2:3b-instruct-q4_K_M`
- Size: ~2 GB on disk
- RAM: ~3.5 GB during inference
- Speed on Jetson: ~25–40 tokens/second (GPU inference)
- Context window: 4096 tokens
- Quantization: Q4_K_M (good balance of quality vs speed)

**Embedding model:** `nomic-embed-text`
- Used for PDF semantic search (RAG)
- Size: ~274 MB
- Runs locally — zero internet required

---

## HOW THE RAG PIPELINE WORKS

```
Operator query
      ↓
  Kiwix full-text search (ZIM files) → top 3 snippets
  ChromaDB vector search (PDFs)      → top 3 chunks
      ↓
  Build context prompt with sources
      ↓
  LLaMA 3.2 3B on Jetson GPU
      ↓
  Grounded answer with source citations
      ↓
  Return to operator (CLI / API / dashboard / MQTT)
```

The LLM is explicitly instructed to cite sources and flag when
context is insufficient rather than hallucinate. Temperature is
set to 0.2 for factual/reference queries.

---

*SPECTER Monster — Built for the field. Zero internet required after install.*
