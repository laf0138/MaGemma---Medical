# SPECTER MONSTER — COMPUTATIONAL BOM (Complete)

**Session 5 (August 2026) — All locked decisions**

---

## PROCESSOR INVENTORY

### Compute Cluster (Geekworm X1500 — 4× CM5 remain)

| Device | Qty | Cores | Role | Unit price | Subtotal |
|---|---|---|---|---|---|
| Raspberry Pi Compute Module 5 8GB | 4 | 4 cores each (16 total) | Node 3 (TX/RX PlutoSDR), Node 4 (KrakenSDR DF), Node 5 (RAID/NFS), Node 6 (Voice/MMDVM) | ~$55 | ~$220 |
| Geekworm X1500-C2 metal cases | 4 | — | Housing for 4× CM5 | included | included |

### Separate Nodes (Outside X1500, on GbE switch)

| Device | Qty | Cores/TOPS | Role | Unit price | Subtotal |
|---|---|---|---|---|---|
| Raspberry Pi 5 8GB | 2 | 4 cores each (8 total) | Node 1 (Master/MQTT/dashboard), Node 2 (HF/VHF SDR) | ~$85 | ~$170 |
| **Raspberry Pi AI HAT+ 2 (Hailo-10H)** | 2 | 40 TOPS each (80 TOPS total) | One on Node 1 Pi 5, one on Node 2 Pi 5. 8GB dedicated LPDDR4X RAM per HAT. | ~$130 | ~$260 |

### AI Node (Standalone Jetson)

| Device | Qty | Cores/TOPS | Role | Unit price | Subtotal |
|---|---|---|---|---|---|
| **Jetson Orin NX 16GB (SUPER mode)** | 1 | 8 cores + 157 TOPS | Medical AI (MedGemma 7B/27B), Kiwix library server, ChromaDB RAG, LLaMA inference | ~$599 | ~$599 |
| Jetson carrier board (260-pin) | 1 | — | Included with SoM; same board as Nano Super (module swap only) | included | included |

### Medical Hub (Bluetooth aggregator)

| Device | Qty | Cores | Role | Unit price | Subtotal |
|---|---|---|---|---|---|
| Raspberry Pi Zero 2W 4GB | 1 | 4 cores | Bluetooth aggregator for all diagnostic devices (BP, oximeter, glucometer, ECG, stethoscope, urine dipstick) | ~$15 | ~$15 |

---

## NETWORKING

| Device | Qty | Role | Unit price | Subtotal |
|---|---|---|---|---|
| Netgear GS308EP Managed PoE+ switch | 1 | 8-port gigabit Ethernet, 62W PoE budget. Connects: 4× CM5 in X1500, 2× Pi 5, Jetson, Pi Zero at 192.168.1.x | ~$100 | ~$100 |
| Waveshare PoE HAT+ | 4 | Node 3, 4, 5, 6 (CM5 in X1500). Powers each CM5 directly from PoE switch. | ~$20 | ~$80 |

---

## STORAGE (All NVMe, no SATA)

| Device | Qty | Capacity | Role | Unit price | Subtotal |
|---|---|---|---|---|---|
| Samsung 990 Pro NVMe M.2 | 1 | 512GB | **Jetson storage:** Wikipedia ZIM (102GB), Gutenberg (60GB), Stack Overflow (90GB), Khan Academy (15GB), all PDFs (1GB), AI models (4.6GB), OS + ChromaDB + working space (31GB). Total ~326GB with 186GB free. | ~$75 | ~$75 |
| Samsung 970 EVO Plus NVMe M.2 | 2 | 1TB each | **Node 5 RAID-1 (mdadm):** RF captures (150GB), DF logs (30GB), system logs (10GB), archive headroom (300GB). Total 490GB usable on 2TB raw. | ~$75 | ~$150 |
| Samsung 970 EVO Plus NVMe M.2 | 6 | 256GB each | **CM5 + Pi 5 boot drives:** OS + local cache per node (~15–50GB used per node). One per Node 1–6. | ~$35 | ~$210 |
| Samsung 64GB Endurance microSD | 1 | 64GB | **Emergency recovery image only** (not primary boot). | ~$12 | ~$12 |

**Storage subtotal: ~$447**

---

## POWER DISTRIBUTION & SOLAR

| Device | Qty | Spec | Role | Unit price | Subtotal |
|---|---|---|---|---|---|
| Meanwell LRS-75-5 | 1 | 15A @ 5V (75W capacity) | Primary PSU. Accepts 90–264V AC or 12–48V DC input. Handles peak 13.7A system load (69W) with headroom. | ~$90 | ~$90 |
| Meanwell LRS-60-12 | 1 | 5A @ 12V (60W capacity) | 12V rail for coolers, external PA, antenna amp. | ~$70 | ~$70 |
| 5V bus bar + grounding bus bar | 1 | Copper stock, M5/M6 bolts | Main power distribution with individual fusing per node tap. Polycarbonate spacers for isolation. | ~$30 | ~$30 |
| EcoFlow 220W Bifacial Solar Panel | 1 | 21.8V OCV, 220W + 155W bifacial | Foldable portable solar. Feeds MPPT controller during field deployment. | ~$350 | ~$350 |
| Bioenno SC-122430NE MPPT Controller | 1 | 24V/30A, accepts 12–50V input | Charges 12.8V LiFePO4 at ~15A (192W). Hybrid mode with Meanwell via blocking diodes. | ~$90 | ~$90 |
| Anderson XT60 connectors + MC4 cables | 1 | High-capacity solar/battery connectors | Battery-to-PSU distribution, solar panel to MPPT. | ~$35 | ~$35 |

**Power subtotal: ~$665**

---

## COOLING (Mandatory for sustained load)

| Device | Qty | Role | Unit price | Subtotal |
|---|---|---|---|---|
| Active cooler + heatsink per Pi 5 | 2 | Keeps Pi 5 + AI HAT stack below 65°C under full load | ~$25 | ~$50 |
| Jetson heatsink + active cooler | 1 | Keeps Jetson below 75°C during 27B LLM inference | ~$40 | ~$40 |
| Raspberry Pi Zero 2W heatsink (passive) | 1 | Medical hub stays cool with minimal active load | ~$5 | ~$5 |

**Cooling subtotal: ~$95**

---

## COMPLETE COMPUTATIONAL BOM

| Category | Cost |
|---|---|
| Compute (9 processors + Hailo-10H ×2) | ~$1,264 |
| Storage (all NVMe) | ~$447 |
| Networking (switch + PoE HATs) | ~$180 |
| Power + PSU + solar + MPPT | ~$665 |
| Cooling | ~$95 |
| **TOTAL** | **~$2,651** |

---

## COMPUTE SUMMARY

| Metric | Value |
|---|---|
| **Total processors** | 9 (6 CM5 + 2 Pi 5 + 1 Jetson + 1 Pi Zero) |
| **Total CPU cores** | 36 (24 from X1500 cluster + 8 from Jetson + 4 from Pi Zero) |
| **Total AI TOPS** | 237 (80 from Hailo ×2 + 157 from Jetson) |
| **Unified RAM (Jetson)** | 16GB LPDDR5 (runs LLaMA 7B or MedGemma 27B with RAG) |
| **Dedicated accelerator RAM** | 16GB LPDDR4X (8GB per Hailo-10H, independent of system) |
| **Peak system load** | 13.7A @ 5V (69W) |
| **PSU headroom** | 15A @ 5V (75W capacity), 46% utilization at peak |
| **Solar charging rate** | ~15A into 12.8V LiFePO4 (192W), 2.7 hours to offset 24-hour load in full sun |

---

## PERFORMANCE TARGETS (Locked)

| Workload | Model / Config | Target Performance |
|---|---|---|
| Medical AI diagnosis | MedGemma 7B Q4_K_M | ~20 tok/s = 5–15 sec per query |
| Medical AI with image | MedGemma 27B Q4_K_M | ~8 tok/s = 10–30 sec per query |
| Offline library search | Kiwix 300GB ZIM | <500ms full-text |
| RAG (patient history + guidelines) | ChromaDB + embeddings | <200ms vector search |
| RF signal classification | RadioML 2018.01 on Hailo-10H | Real-time, <100ms per signal |
| HF/VHF/UHF continuous monitoring | GNU Radio on Node 2 Pi 5 | Continuous, 70–80% CPU |
| KrakenSDR DF + passive radar | 5-ch coherent DF on Node 4 CM5 | Continuous, 65–80% CPU |
| MQTT + dashboard | Flask + Mosquitto on Node 1 Pi 5 | <30ms MQTT latency, <250ms DF to screen |

---

## CANONICAL 12-LEAD ECG SOFTWARE / AI STACK (LOCKED)

The following seven-part combination is the selected SPECTER 12-lead ECG path. Selection
does not mean installed, clinically validated, or cleared for diagnostic use.

| # | Component | Locked role | Deployment status | Safety / validation status |
|---:|---|---|---|---|
| 1 | **Biocare iE300** | Physical 12-lead waveform acquisition and source report | Strict XML acquisition boundary, immutable raw archive and canonical 12-lead record are implemented; **real vendor XML/firmware validation is pending** | The importer fails closed when lead order, units, gain, sample rate, duration or timestamp is missing/ambiguous. A PDF or photograph is display evidence, not model input |
| 2 | **[DeepECG-SL](https://github.com/HeartWise-AI/DeepECG_Docker)** | **Primary research 12-lead classifier**; produces multi-label probabilities | Exact 250 Hz/2,500-sample/77-label/scaling contract and isolated TorchScript adapter are integrated; model remains disabled until exact weights/hash are provisioned | The inspected EfficientNet release has no published thresholds, so SPECTER preserves probabilities and refuses to invent positive flags |
| 3 | **[AntonioR92 automatic ECG diagnosis](https://github.com/antonior92/automatic-ecg-diagnosis)** | Initial six-label regression baseline for 1st-degree AV block, RBBB, LBBB, sinus bradycardia, AF and sinus tachycardia | Exact 400 Hz/4,096-sample contract, symmetric padding, labels/thresholds and isolated legacy TensorFlow adapter are integrated; weights/runtime remain unprovisioned | Baseline/comparator only, never a tie-breaker or clinical authority. Overlapping labels generate an explicit agreement/disagreement record |
| 4 | **[ECG-XPLAIM](https://github.com/gerard-raffy/ECG-XPLAIM)** | Later explainable secondary classifier | Isolated adapter and bounded explanation contract are integrated but disabled; the exact task, labels and weights must replace the obvious placeholder before enablement | Research aid only. SPECTER labels its generic gradient summary separately and does not misrepresent it as the publication method |
| 5 | **[PTB-XL](https://physionet.org/content/ptb-xl/1.0.3/) plus other external datasets** | Versioned integration, regression, calibration, subgroup and out-of-distribution corpus | Dataset manifest/version/license/hash validation and patient-level split-leak detection are implemented; datasets are not redistributed or downloaded | Public retrospective data cannot validate the Biocare chain or field population. Acquisition/licensing and frozen evaluation runs remain commissioning work |
| 6 | **MedGemma 4B** | Constrained explanation, retrieval-grounded context and operator summary | Retained ECG-analysis subscription, cache, complete structured-probability/quality/provenance/disagreement prompt block and diagnosis audit snapshot are integrated | It never receives a textual raw-sample dump, changes probabilities, hides disagreement or treats an unavailable model as a negative result |
| 7 | **[ExChanGeAI](https://github.com/VargheseLab/exchangeai) concepts** | Evaluation workflow and ONNX model registry/version/exchange patterns | Offline hash-pinned atomic registry is implemented. ONNX entries require opset, input/output names and source-artifact lineage; generic ONNX Runtime execution is isolated | No automatic download/conversion/fine-tuning. Field data can never trigger training, and a registry entry is not clinical validation |

Locked order of operations is: Biocare acquisition -> acquisition-file validation ->
signal-quality gate -> deterministic measurements -> DeepECG-SL primary inference ->
AntonioR92 regression comparison -> optional/later ECG-XPLAIM secondary inference ->
explicit agreement/disagreement record -> constrained MedGemma summary. PTB-XL and the
other external corpora validate the pipeline; ExChanGeAI concepts govern reproducible model
evaluation and ONNX lifecycle management. No component may convert missing, corrupt,
mis-scaled, or wrong-lead data into a clinical-looking result. Until hardware-specific and
clinician-reviewed validation is complete, every model output is **RESEARCH ONLY / NOT FOR
DIAGNOSIS**.

---

## KEY DESIGN DECISIONS (LOCKED)

✅ **AI accelerators:** Two Hailo-10H (40 TOPS each) on Node 1 & 2 Pi 5s for parallel signal classification + medical AI  
✅ **Jetson:** Single Orin NX 16GB (157 TOPS) for medical AI + full offline library (Kiwix + RAG)  
✅ **No second Jetson:** One Jetson is sufficient for SHTF medical queries (5–30 sec latency acceptable)  
✅ **Hybrid cluster:** 4× CM5 in X1500 (RF/storage) + 2× Pi 5 standalone (compute-intensive + accelerators)  
✅ **All NVMe storage:** No SATA/ASM1166 (field reliability concern); mdadm RAID-1 instead  
✅ **Solar hybrid:** EcoFlow 220W + Bioenno MPPT + Meanwell PSU in parallel (grid + solar charging)  
✅ **15A @ 5V PSU:** Right-sized for 69W peak load with 31% headroom  
✅ **Bus bar distribution:** Clean power topology, no daisy-chain cables  

---

## REMAINING WORK (Session 6+)

- Medical hub code (Pi Zero 2W Bluetooth → MQTT bridge)
- MPPT + bus bar wiring diagram
- Master image render (prompt v5 final)
- MedGemma system prompt (7-tier medication sourcing + immunosuppression profile)
- Cold chain monitoring (dual 28-day infusion alerts)
- Full family medication protocol document
- WFR training coordination

---

**BOM locked for production build. Ready for case integration and assembly guide revision.**
