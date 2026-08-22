#!/usr/bin/env python3
"""
SPECTER Medical AI Engine
Runs on: Jetson Orin NX 16GB (192.168.1.10)

Responsibilities:
  1. Subscribe to medical hub vitals via MQTT (shtf/medical/vitals/#)
  2. Maintain a live, timestamped vitals cache per patient
  3. Build dynamic MedGemma prompts that include real vitals + patient profile
  4. Query ChromaDB RAG for relevant clinical guidance
  5. Run MedGemma 4B inference via Ollama
  6. Publish diagnosis back to MQTT (shtf/medical/diagnosis/<patient_id>)

Model: MedGemma 4B Q4_K_M via Ollama (~1.9GB, 40-50 tok/s, 3-5s response)

Author: SPECTER Build Team
Date: August 2026
Version: 1.2.0
"""

import os
import re
import json
import time
import logging
import argparse
import threading
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
import paho.mqtt.client as mqtt

from medical.clinical_scores import (
    calculate_map,
    calculate_news2,
    calculate_pulse_pressure,
    calculate_qsofa,
    calculate_shock_index,
)

# --- paho-mqtt 1.x / 2.x compatibility -------------------------------------
def _mqtt_client(client_id: str = ""):
    """Construct an MQTT client that works on paho-mqtt 1.x and 2.x."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x has no CallbackAPIVersion - fall back to the
        # old-style constructor (deprecated but functional on 2.x too),
        # NOT a recursive call to this same function, which would hit the
        # same AttributeError every time and blow the stack.
        return mqtt.Client(client_id=client_id)
# ---------------------------------------------------------------------------

# --- MQTT auth --------------------------------------------------------------
# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "medical_ai" account:
# it can only read the vitals/query/profile topics and write its own
# diagnosis/status topics. Runtime connections require the dedicated
# credential and reject the installer's placeholder password.
MQTT_SERVICE_KEY      = "medical_ai"
MQTT_DEFAULT_USERNAME = "specter-medical-ai"
MQTT_DEFAULT_PASSWORD = "specter-change-me"


def _mqtt_credentials() -> tuple[str, str]:
    """Return only this service's dedicated credential, or fail closed."""
    try:
        cfg = json.loads(Path("/etc/specter/specter.json").read_text())
    except Exception as exc:
        raise RuntimeError("MQTT configuration is unreadable") from exc
    if not isinstance(cfg, dict):
        raise RuntimeError("MQTT configuration must be a JSON object")
    service_cfg = cfg.get("mqtt", {}).get("services", {}).get(MQTT_SERVICE_KEY, {})
    username, password = service_cfg.get("username"), service_cfg.get("password")
    if not username or not password or password == MQTT_DEFAULT_PASSWORD:
        raise RuntimeError(f"dedicated MQTT credentials missing for {MQTT_SERVICE_KEY}")
    return username, password
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.environ.get("SPECTER_LOG", "/var/log/specter/medical_ai.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("specter.medical_ai")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    mqtt_host: str = "192.168.1.1"
    mqtt_port: int = 1883

    ollama_host: str = "http://127.0.0.1:11434"
    model: str = "medgemma:4b"

    # Kiwix runs on Node 5 (distributed storage architecture)
    kiwix_host: str = "http://192.168.1.5:8080"

    # ChromaDB lives on the Jetson NVMe alongside the model
    chroma_path: str = "/opt/specter/chroma"
    chroma_collection: str = "specter_medical"

    # A vitals reading older than this is flagged as stale in the prompt
    vitals_stale_seconds: int = 600

    # Inference guard rails
    max_tokens: int = 900
    temperature: float = 0.3  # low temp: clinical reasoning, not creativity

    request_timeout: int = 120


# ---------------------------------------------------------------------------
# Patient profile — the standing clinical context MedGemma always receives
# ---------------------------------------------------------------------------

@dataclass
class PatientProfile:
    """
    Standing medical context for a patient. This is injected into every
    prompt so MedGemma reasons with the immunosuppression profile in view
    rather than treating each query as a blank-slate encounter.
    """
    patient_id: str
    display_name: str = ""
    age: Optional[int] = None
    sex: str = ""
    conditions: List[str] = field(default_factory=list)
    medications: List[str] = field(default_factory=list)
    allergies: List[str] = field(default_factory=list)
    critical_flags: List[str] = field(default_factory=list)
    infusion_schedule: Dict[str, str] = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        lines = [f"PATIENT: {self.display_name or self.patient_id}"]
        if self.age is not None or self.sex:
            lines.append(f"  Age/Sex: {self.age or 'unknown'} / {self.sex or 'unknown'}")
        if self.conditions:
            lines.append("  Conditions: " + "; ".join(self.conditions))
        if self.medications:
            lines.append("  Medications: " + "; ".join(self.medications))
        if self.allergies:
            lines.append("  Allergies: " + "; ".join(self.allergies))
        if self.infusion_schedule:
            sched = "; ".join(f"{k}: {v}" for k, v in self.infusion_schedule.items())
            lines.append("  Infusion schedule: " + sched)
        if self.critical_flags:
            lines.append("  CRITICAL FLAGS:")
            for flag in self.critical_flags:
                lines.append(f"    - {flag}")
        return "\n".join(lines)


def default_profiles() -> Dict[str, PatientProfile]:
    """
    Built-in profiles for the household. Edit these to match reality, or
    override at runtime by publishing to shtf/medical/profile/<patient_id>.
    """
    return {
        "operator": PatientProfile(
            patient_id="operator",
            display_name="Operator",
            conditions=[
                "Kidney transplant recipient",
                "Chronic immunosuppression",
            ],
            medications=[
                "Belatacept (Nulojix) IV infusion every 28 days",
                "Mycophenolate sodium (Myfortic) oral",
                "Prednisone oral",
            ],
            critical_flags=[
                "IMMUNOSUPPRESSED: infection presents atypically and progresses fast. "
                "Fever may be blunted; absence of fever does not rule out serious infection.",
                "Belatacept requires 2-8C cold chain and silicone-free syringes (BD PlastiPak).",
                "Graft function is the priority organ concern: watch urine output, "
                "swelling, blood pressure, and graft-site tenderness.",
                "Avoid nephrotoxic drugs (NSAIDs, aminoglycosides) unless no alternative.",
                "Live vaccines are contraindicated.",
            ],
            infusion_schedule={"Belatacept": "every 28 days"},
        ),
        "spouse": PatientProfile(
            patient_id="spouse",
            display_name="Spouse",
            conditions=["Rheumatoid arthritis on biologic therapy"],
            medications=["RA biologic IV infusion every 28 days (agent TBD)"],
            critical_flags=[
                "IMMUNOSUPPRESSED via biologic therapy: elevated infection risk, "
                "atypical presentation possible.",
                "Biologic requires cold chain storage at 2-8C.",
            ],
            infusion_schedule={"RA biologic": "every 28 days"},
        ),
        "default": PatientProfile(
            patient_id="default",
            display_name="Unregistered patient",
        ),
    }


# ---------------------------------------------------------------------------
# Vitals cache
# ---------------------------------------------------------------------------

@dataclass
class VitalReading:
    value: Any
    unit: str
    timestamp_utc: str
    device_name: str = ""

    def age_seconds(self) -> float:
        try:
            ts = datetime.fromisoformat(self.timestamp_utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            return float("inf")


class VitalsCache:
    """
    Thread-safe store of the most recent reading per vital type per patient,
    plus a bounded trend history so MedGemma can see direction of change
    rather than a single isolated number.
    """

    HISTORY_LIMIT = 24

    # Reference ranges used only to annotate the prompt. These are coarse adult
    # ranges for orientation; they are not a substitute for clinical judgement.
    REFERENCE = {
        "bp_systolic": (90, 140, "mmHg"),
        "bp_diastolic": (60, 90, "mmHg"),
        "pulse": (60, 100, "bpm"),
        "spo2": (94, 100, "%"),
        "temperature_c": (36.1, 37.8, "C"),
        "glucose_mg_dl": (70, 140, "mg/dL"),
    }

    LABELS = {
        "bp_systolic": "Blood pressure (systolic)",
        "bp_diastolic": "Blood pressure (diastolic)",
        "pulse": "Heart rate",
        "spo2": "SpO2",
        "temperature_c": "Temperature",
        "temperature_f": "Temperature (F)",
        "glucose_mg_dl": "Blood glucose",
        "ecg_rhythm": "ECG rhythm",
        "respiratory_rate": "Respiratory rate",
        "ecg_q_s_peak_interval_ms": "ECG Q-to-S peak interval (experimental; not QRS duration)",
        "ecg_r_wave_abs_amplitude_uv": "ECG absolute R-wave amplitude (experimental)",
        "ecg_t_wave_abs_amplitude_uv": "ECG absolute T-wave amplitude (experimental)",
        "ecg_t_r_abs_ratio": "ECG absolute T/R amplitude ratio (experimental)",
        "ecg_morphology_beats_analyzed": "ECG valid Q-R-S beats analyzed",
        "ecg_amplitude_beats_analyzed": "ECG valid R-T beats analyzed",
        "ecg_analysis_status": "ECG analysis validation status",
        "ecg_analysis_warnings": "ECG analysis warnings",
    }

    # Raw sample arrays (a full ECG waveform, a list of RR intervals) are
    # not useful dumped into a text prompt - hundreds/thousands of bare
    # numbers waste context and tell MedGemma nothing a text model can act
    # on. See medical/ecg_analysis.py: the explicitly experimental derived
    # scalars and completeness metadata are what belong in the prompt: the
    # raw waveform is future multimodal-input material, not implemented
    # here (see docs/MANUAL.md Part 7.4).
    RAW_ARRAY_READING_TYPES = {"ecg_waveform_uv", "rr_intervals_ms"}

    def __init__(self, stale_seconds: int = 600):
        self._lock = threading.Lock()
        self._latest: Dict[str, Dict[str, VitalReading]] = {}
        self._history: Dict[str, Dict[str, List[VitalReading]]] = {}
        self.stale_seconds = stale_seconds

    def update(self, patient_id: str, reading_type: str, reading: VitalReading) -> None:
        with self._lock:
            self._latest.setdefault(patient_id, {})[reading_type] = reading
            hist = self._history.setdefault(patient_id, {}).setdefault(reading_type, [])
            hist.append(reading)
            if len(hist) > self.HISTORY_LIMIT:
                del hist[: len(hist) - self.HISTORY_LIMIT]

    def latest(self, patient_id: str) -> Dict[str, VitalReading]:
        with self._lock:
            return dict(self._latest.get(patient_id, {}))

    def history(self, patient_id: str, reading_type: str) -> List[VitalReading]:
        with self._lock:
            return list(self._history.get(patient_id, {}).get(reading_type, []))

    def _annotate(self, reading_type: str, value: Any) -> str:
        ref = self.REFERENCE.get(reading_type)
        if not ref or not isinstance(value, (int, float)):
            return ""
        low, high, _unit = ref
        if value < low:
            return "  [below reference range]"
        if value > high:
            return "  [above reference range]"
        return "  [within reference range]"

    def _trend(self, patient_id: str, reading_type: str) -> str:
        hist = self.history(patient_id, reading_type)
        numeric = [r.value for r in hist if isinstance(r.value, (int, float))]
        if len(numeric) < 3:
            return ""
        recent = numeric[-3:]
        if recent[-1] > recent[0]:
            return f"  (trend over last {len(recent)} readings: rising {recent[0]} -> {recent[-1]})"
        if recent[-1] < recent[0]:
            return f"  (trend over last {len(recent)} readings: falling {recent[0]} -> {recent[-1]})"
        return "  (trend: stable)"

    # -- Derived metrics ----------------------------------------------------
    # See docs/SPECTER_MEDICAL_UI_BRIEF.md Part 1.3 ("Derived - computed,
    # never entered"). NEWS2 and qSOFA both include respiration rate and/or
    # consciousness as inputs; no BLE device wired into this build measures
    # either, so those two scores are ALWAYS partial on this path - see
    # clinical_scores.py's module docstring. They are still worth computing:
    # a partial NEWS2/qSOFA that visibly lists what's missing is useful and
    # honest; silently omitting the score entirely would hide that SPECTER
    # *could* compute it if respiration rate/consciousness were ever wired
    # up (e.g. a future capnometer or manual entry), and a caller who wants
    # the number without checking `partial` deserves that to be impossible.

    def fever_burden_minutes(
        self,
        patient_id: str,
        threshold_c: float = 38.0,
        window_seconds: int = 86400,
    ) -> Optional[float]:
        """
        Minutes with temperature above threshold_c within the last
        window_seconds, integrated over the retained temperature history.

        Bounded by HISTORY_LIMIT (only the most recent readings are kept in
        memory, not persisted across restarts) - if temperature is sampled
        more often than HISTORY_LIMIT times per window, this undercounts
        the true 24h burden. That is a real limitation of the current
        in-memory cache, not a rounding quirk.
        """
        hist = self.history(patient_id, "temperature_c")
        if not hist:
            return None

        now = datetime.now(timezone.utc)
        points = []
        for r in hist:
            if not isinstance(r.value, (int, float)):
                continue
            try:
                ts = datetime.fromisoformat(r.timestamp_utc)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            age = (now - ts).total_seconds()
            if age < 0 or age > window_seconds:
                continue
            points.append((ts, float(r.value)))

        if not points:
            return None
        points.sort(key=lambda p: p[0])

        burden_seconds = 0.0
        for i, (ts, value) in enumerate(points):
            interval_end = points[i + 1][0] if i + 1 < len(points) else now
            interval_end = min(interval_end, now)
            span = max(0.0, (interval_end - ts).total_seconds())
            span = min(span, window_seconds)
            if value > threshold_c:
                burden_seconds += span

        return round(burden_seconds / 60, 1)

    def delta_from_baseline(
        self, patient_id: str, reading_type: str
    ) -> Optional[Dict[str, Any]]:
        """
        Current reading vs. the median of this reading type's retained
        history. NOT a persisted 30-day baseline (docs/
        SPECTER_MEDICAL_UI_BRIEF.md 1.3 specifies 30 days) - there is no
        long-term vitals store yet, only the in-memory HISTORY_LIMIT window,
        which resets on every service restart. Returns None until at least
        3 numeric readings are available, so a same-session baseline is
        never presented from a single or a pair of readings.
        """
        numeric = [
            r.value for r in self.history(patient_id, reading_type)
            if isinstance(r.value, (int, float))
        ]
        if len(numeric) < 3:
            return None

        current = numeric[-1]
        pool = sorted(numeric[:-1])
        n = len(pool)
        median = (
            pool[n // 2] if n % 2 == 1
            else (pool[n // 2 - 1] + pool[n // 2]) / 2
        )
        return {
            "current": current,
            "baseline_median": round(median, 2),
            "delta": round(current - median, 2),
            "baseline_sample_size": n,
        }

    def derived(self, patient_id: str) -> Dict[str, Any]:
        """All derived clinical metrics for one patient's current cache state."""
        latest = self.latest(patient_id)

        def val(key: str) -> Any:
            reading = latest.get(key)
            return reading.value if reading is not None else None

        sbp, dbp, hr = val("bp_systolic"), val("bp_diastolic"), val("pulse")

        # rr, supplemental_o2, avpu (NEWS2) and rr, altered_mentation
        # (qSOFA) are intentionally never included: no connected device
        # publishes them, and calculate_news2/calculate_qsofa treat an
        # absent key as missing, never as a normal/negative reading.
        news2_input = {
            k: v for k, v in {
                "spo2": val("spo2"),
                "bp_systolic": sbp,
                "pulse": hr,
                "temperature_c": val("temperature_c"),
            }.items() if v is not None
        }
        qsofa_input = {k: v for k, v in {"bp_systolic": sbp}.items() if v is not None}

        return {
            "map_mmhg": calculate_map(sbp, dbp),
            "pulse_pressure_mmhg": calculate_pulse_pressure(sbp, dbp),
            "shock_index": calculate_shock_index(hr, sbp),
            "news2": calculate_news2(news2_input),
            "qsofa": calculate_qsofa(qsofa_input),
            "fever_burden_minutes_24h": self.fever_burden_minutes(patient_id),
            "delta_from_baseline": {
                k: delta
                for k in ("bp_systolic", "bp_diastolic", "pulse", "spo2", "temperature_c")
                for delta in [self.delta_from_baseline(patient_id, k)]
                if delta is not None
            },
            "note": (
                "NEWS2/qSOFA use connected-device vitals only: this build has no "
                "respiration rate or consciousness sensor, so both scores are "
                "always partial (see each score's missing_parameters) and must "
                "never be read as reassuring on their own. Fever burden and "
                f"baseline delta are bounded by the last {self.HISTORY_LIMIT} "
                "readings held in memory, not a persisted 24h/30-day history."
            ),
        }

    def to_prompt_block(self, patient_id: str) -> str:
        latest = self.latest(patient_id)
        if not latest:
            return (
                "PATIENT VITALS: none received from the medical hub.\n"
                "  Treat all vitals as UNKNOWN. Do not assume normal values. "
                "State explicitly which vitals you would need to narrow the differential."
            )

        lines = ["PATIENT VITALS (live from medical hub):"]
        stale_any = False

        for reading_type, reading in sorted(latest.items()):
            if reading_type == "temperature_f":
                continue  # avoid duplicate temperature lines; C is canonical
            if reading_type in self.RAW_ARRAY_READING_TYPES:
                continue  # raw sample arrays - see RAW_ARRAY_READING_TYPES
            label = self.LABELS.get(reading_type, reading_type)
            age = reading.age_seconds()
            age_txt = f"{int(age)}s ago" if age < 3600 else f"{age / 3600:.1f}h ago"
            stale = age > self.stale_seconds
            stale_any = stale_any or stale
            stale_txt = "  [STALE - may not reflect current state]" if stale else ""

            if reading_type == "ecg_analysis_warnings" and isinstance(reading.value, list):
                if not reading.value:
                    continue
                lines.append(f"  - {label} ({age_txt}){stale_txt}:")
                for flag in reading.value:
                    lines.append(f"      - {flag}")
                continue

            annotation = self._annotate(reading_type, reading.value)
            trend = self._trend(patient_id, reading_type)
            lines.append(
                f"  - {label}: {reading.value} {reading.unit} "
                f"({age_txt}){annotation}{stale_txt}{trend}"
            )

        missing = [
            self.LABELS[k]
            for k in ("bp_systolic", "pulse", "spo2", "temperature_c")
            if k not in latest
        ]
        if missing:
            lines.append("  NOT MEASURED: " + ", ".join(missing))

        if stale_any:
            lines.append(
                "  NOTE: one or more readings are stale. Recommend re-measuring "
                "before acting on them."
            )

        derived = self.derived(patient_id)
        derived_lines = []
        if derived["map_mmhg"] is not None:
            derived_lines.append(f"  MAP: {derived['map_mmhg']} mmHg")
        if derived["pulse_pressure_mmhg"] is not None:
            derived_lines.append(f"  Pulse pressure: {derived['pulse_pressure_mmhg']} mmHg")
        if derived["shock_index"] is not None:
            derived_lines.append(f"  Shock index: {derived['shock_index']}")
        news2 = derived["news2"]
        if news2["per_parameter"]:
            partial_txt = " (PARTIAL - missing: " + ", ".join(news2["missing_parameters"]) + ")" if news2["partial"] else ""
            derived_lines.append(f"  NEWS2: {news2['total']} ({news2['risk']}){partial_txt}")
        qsofa = derived["qsofa"]
        if qsofa["per_parameter"]:
            partial_txt = " (PARTIAL - missing: " + ", ".join(qsofa["missing_parameters"]) + ", cannot rule out positive)" if qsofa["partial"] else ""
            derived_lines.append(f"  qSOFA: {qsofa['total']}{partial_txt}")
        if derived["fever_burden_minutes_24h"] is not None:
            derived_lines.append(f"  Fever burden (>38.0C, last 24h): {derived['fever_burden_minutes_24h']} min")
        if derived_lines:
            lines.append("DERIVED METRICS:")
            lines.extend(derived_lines)
            lines.append(
                "  " + derived["note"]
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Retrieval (ChromaDB + Kiwix)
# ---------------------------------------------------------------------------

class GuidelineRetriever:
    """
    Retrieval over the offline clinical corpus. ChromaDB holds embedded
    excerpts of the reference PDFs (MSF, WHO, Army TMs, FEMA). Kiwix on
    Node 5 serves the full ZIM library for follow-up reading by the operator.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.collection = None
        self._init_chroma()

    def _init_chroma(self) -> None:
        try:
            import chromadb
            from chromadb.config import Settings

            client = chromadb.PersistentClient(
                path=self.cfg.chroma_path,
                settings=Settings(anonymized_telemetry=False),
            )
            self.collection = client.get_or_create_collection(
                name=self.cfg.chroma_collection
            )
            logger.info(
                "ChromaDB ready at %s (collection=%s, documents=%s)",
                self.cfg.chroma_path,
                self.cfg.chroma_collection,
                self.collection.count(),
            )
        except Exception as exc:
            logger.warning("ChromaDB unavailable, retrieval disabled: %s", exc)
            self.collection = None

    def retrieve(self, query: str, n_results: int = 4) -> List[Dict[str, Any]]:
        if self.collection is None:
            return []
        try:
            res = self.collection.query(query_texts=[query], n_results=n_results)
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            out = []
            for doc, meta in zip(docs, metas):
                out.append(
                    {
                        "text": doc,
                        "source": (meta or {}).get("source", "unknown source"),
                        "page": (meta or {}).get("page"),
                    }
                )
            return out
        except Exception as exc:
            logger.error("Retrieval failed: %s", exc)
            return []

    def to_prompt_block(self, passages: List[Dict[str, Any]]) -> str:
        if not passages:
            return (
                "REFERENCE MATERIAL: no matching passages retrieved from the offline "
                "library. Rely on general clinical knowledge and say so plainly."
            )
        lines = ["REFERENCE MATERIAL (retrieved from offline clinical library):"]
        for i, p in enumerate(passages, 1):
            src = p["source"]
            if p.get("page"):
                src = f"{src}, p.{p['page']}"
            text = " ".join(p["text"].split())
            if len(text) > 900:
                text = text[:900] + "..."
            lines.append(f"  [{i}] {src}\n      {text}")
        return "\n".join(lines)

    def kiwix_search_url(self, query: str) -> str:
        from urllib.parse import quote_plus

        return f"{self.cfg.kiwix_host}/search?books.filter.lang=eng&pattern={quote_plus(query)}"


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PREAMBLE = """You are MedGemma running offline inside SPECTER, a field \
emergency medical and communications system. You are advising a Wilderness First \
Responder-trained layperson caring for their own family, potentially with no \
physician and no evacuation available.

How to answer:
1. Lead with the most time-critical concern. If something needs action in the next
   few minutes, say that first.
2. Give a differential diagnosis ordered by likelihood. For each item, state which
   specific vitals or findings support it and which argue against it.
3. Name the red flags that would change the assessment, and what each would mean.
4. Recommend the specific physical exams or measurements that would most narrow the
   differential, in priority order.
5. Give management guidance that is achievable with a field medical kit, and say
   plainly when something is beyond field capability.
6. State clearly when evacuation or physician contact is required, and how urgently.

Constraints you must respect:
- Reason from the vitals actually provided. Never invent a vital sign that was not
  measured. If a needed value is missing, say which one and why it matters.
- Account for immunosuppression when it is present in the patient profile. Blunted
  fever, atypical presentation, and rapid deterioration are expected.
- Check every drug you suggest against the patient's medication list and note
  interactions or contraindications.
- Distinguish clearly between what you are confident about and what is uncertain.
  Uncertainty stated plainly is more useful than false precision.
- You are decision support, not a clinician. Recommend physician contact whenever
  it is realistically available.
"""

MEDICATION_SOURCING_BLOCK = """MEDICATION SOURCING (use when cost or access is the obstacle):
Check in this order: 1) insurance formulary price, 2) Cost Plus Drugs,
3) Walmart low-cost generics program, 4) Amazon RxPass, 5) GoodRx local pricing,
6) Costco or local pharmacy cash price, 7) verified CIPA Canadian pharmacy for
expensive brand-name maintenance drugs. Mention this only when relevant to the
question actually asked."""


class PromptBuilder:
    def __init__(self, vitals: VitalsCache, profiles: Dict[str, PatientProfile]):
        self.vitals = vitals
        self.profiles = profiles

    def profile_for(self, patient_id: str) -> PatientProfile:
        return self.profiles.get(
            patient_id, PatientProfile(patient_id=patient_id, display_name=patient_id)
        )

    def build(
        self,
        patient_id: str,
        user_query: str,
        passages: List[Dict[str, Any]],
        retriever: GuidelineRetriever,
        include_sourcing: bool = False,
    ) -> str:
        blocks = [
            SYSTEM_PREAMBLE,
            "=" * 70,
            self.profile_for(patient_id).to_prompt_block(),
            "",
            self.vitals.to_prompt_block(patient_id),
            "",
            retriever.to_prompt_block(passages),
        ]
        if include_sourcing:
            blocks += ["", MEDICATION_SOURCING_BLOCK]
        blocks += [
            "=" * 70,
            f"CURRENT TIME (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "",
            "OPERATOR'S QUESTION:",
            user_query.strip(),
            "",
            "Answer now, following the structure above.",
        ]
        return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Ollama inference
# ---------------------------------------------------------------------------

class OllamaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def health(self) -> bool:
        try:
            r = requests.get(f"{self.cfg.ollama_host}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def model_present(self) -> bool:
        try:
            r = requests.get(f"{self.cfg.ollama_host}/api/tags", timeout=5)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            base = self.cfg.model.split(":")[0]
            return any(n == self.cfg.model or n.startswith(base) for n in names)
        except Exception:
            return False

    def generate(self, prompt: str) -> str:
        payload = {
            "model": self.cfg.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.cfg.temperature,
                "num_predict": self.cfg.max_tokens,
            },
        }
        started = time.time()
        r = requests.post(
            f"{self.cfg.ollama_host}/api/generate",
            json=payload,
            timeout=self.cfg.request_timeout,
        )
        r.raise_for_status()
        data = r.json()
        elapsed = time.time() - started
        tokens = data.get("eval_count", 0)
        rate = tokens / elapsed if elapsed > 0 else 0
        logger.info(
            "Inference complete: %s tokens in %.1fs (%.1f tok/s)", tokens, elapsed, rate
        )
        return data.get("response", "").strip()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class MedicalAIEngine:
    """
    Ties the pieces together: MQTT ingest of hub vitals, retrieval, prompt
    assembly, inference, and publication of the result.
    """

    TOPIC_VITALS = "shtf/medical/vitals/#"
    TOPIC_QUERY = "shtf/medical/query/#"
    TOPIC_PROFILE = "shtf/medical/profile/#"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.vitals = VitalsCache(stale_seconds=cfg.vitals_stale_seconds)
        self.profiles = default_profiles()
        self.retriever = GuidelineRetriever(cfg)
        self.prompts = PromptBuilder(self.vitals, self.profiles)
        self.ollama = OllamaClient(cfg)

        self.mqtt = _mqtt_client("specter-medical-ai")
        self.mqtt.username_pw_set(*_mqtt_credentials())
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_disconnect = self._on_disconnect
        self.mqtt.on_message = self._on_message
        self.connected = False

        self._busy = threading.Lock()

    # -- MQTT lifecycle ----------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected = True
            logger.info("MQTT connected to %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
            client.subscribe(self.TOPIC_VITALS, qos=1)
            client.subscribe(self.TOPIC_QUERY, qos=1)
            client.subscribe(self.TOPIC_PROFILE, qos=1)
            self._publish_status("online")
        else:
            self.connected = False
            logger.error("MQTT connection failed: %s", reason_code)

    def _on_disconnect(
        self, client, userdata, disconnect_flags_or_reason_code,
        reason_code=None, properties=None,
    ):
        reason_code = (
            disconnect_flags_or_reason_code if reason_code is None else reason_code
        )
        self.connected = False
        if reason_code != 0:
            logger.warning("MQTT disconnected unexpectedly: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            logger.warning("Non-JSON payload on %s, ignoring", topic)
            return

        if topic.startswith("shtf/medical/vitals/"):
            self._handle_vitals(topic, payload)
        elif topic.startswith("shtf/medical/query/"):
            self._handle_query(topic, payload)
        elif topic.startswith("shtf/medical/profile/"):
            self._handle_profile(topic, payload)

    # -- Handlers ----------------------------------------------------------

    def _handle_vitals(self, topic: str, payload: Dict[str, Any]) -> None:
        """
        Accepts both hub publication shapes:
          shtf/medical/vitals/<patient_id>              -> full snapshot
          shtf/medical/vitals/<patient_id>/<reading>    -> single reading
        """
        parts = topic.split("/")
        if len(parts) < 4:
            return
        patient_id = parts[3]

        if len(parts) == 4 and "readings" in payload:
            for r in payload.get("readings", []):
                self._store_reading(patient_id, r)
            logger.info(
                "Vitals snapshot for %s: %d readings",
                patient_id,
                len(payload.get("readings", [])),
            )
        elif len(parts) >= 5:
            self._store_reading(patient_id, payload, reading_type=parts[4])
        else:
            return
        self._publish_derived(patient_id)

    def _store_reading(
        self,
        patient_id: str,
        r: Dict[str, Any],
        reading_type: Optional[str] = None,
    ) -> None:
        rtype = reading_type or r.get("reading_type")
        if not rtype:
            return
        reading = VitalReading(
            value=r.get("value"),
            unit=r.get("unit", ""),
            timestamp_utc=r.get(
                "timestamp_utc", datetime.now(timezone.utc).isoformat()
            ),
            device_name=r.get("device_name", ""),
        )
        self.vitals.update(patient_id, rtype, reading)

    def _handle_profile(self, topic: str, payload: Dict[str, Any]) -> None:
        parts = topic.split("/")
        if len(parts) < 4:
            return
        patient_id = parts[3]
        prof = self.profiles.get(patient_id) or PatientProfile(patient_id=patient_id)
        for fieldname in (
            "display_name",
            "age",
            "sex",
            "conditions",
            "medications",
            "allergies",
            "critical_flags",
            "infusion_schedule",
        ):
            if fieldname in payload:
                setattr(prof, fieldname, payload[fieldname])
        self.profiles[patient_id] = prof
        self.prompts.profiles = self.profiles
        logger.info("Updated profile for %s", patient_id)

    def _handle_query(self, topic: str, payload: Dict[str, Any]) -> None:
        parts = topic.split("/")
        patient_id = parts[3] if len(parts) >= 4 else "default"
        question = payload.get("query") or payload.get("question") or ""
        if not question.strip():
            return
        request_id = payload.get("request_id", str(int(time.time() * 1000)))
        threading.Thread(
            target=self._run_query,
            args=(patient_id, question, request_id),
            daemon=True,
        ).start()

    # -- Inference pipeline ------------------------------------------------

    def _run_query(self, patient_id: str, question: str, request_id: str) -> None:
        if not self._busy.acquire(blocking=False):
            self._publish_diagnosis(
                patient_id,
                request_id,
                error="Inference already in progress. Retry in a few seconds.",
            )
            return
        try:
            self._publish_status("thinking", patient_id=patient_id, request_id=request_id)
            logger.info("Query [%s] patient=%s: %s", request_id, patient_id, question)

            retrieval_query = self._retrieval_query(patient_id, question)
            passages = self.retriever.retrieve(retrieval_query, n_results=4)
            logger.info("Retrieved %d reference passages", len(passages))

            include_sourcing = bool(
                re.search(
                    r"\b(cost|price|afford|pharmacy|refill|source|obtain|buy)\b",
                    question,
                    re.I,
                )
            )

            prompt = self.prompts.build(
                patient_id=patient_id,
                user_query=question,
                passages=passages,
                retriever=self.retriever,
                include_sourcing=include_sourcing,
            )

            answer = self.ollama.generate(prompt)

            self._publish_diagnosis(
                patient_id=patient_id,
                request_id=request_id,
                answer=answer,
                question=question,
                passages=passages,
                kiwix_url=self.retriever.kiwix_search_url(retrieval_query),
            )
        except requests.exceptions.Timeout:
            logger.error("Inference timed out")
            self._publish_diagnosis(
                patient_id, request_id, error="Inference timed out. Model may be loading."
            )
        except Exception as exc:
            logger.exception("Query failed")
            self._publish_diagnosis(patient_id, request_id, error=str(exc))
        finally:
            self._busy.release()
            self._publish_status("online")

    def _retrieval_query(self, patient_id: str, question: str) -> str:
        """Bias retrieval toward the patient's standing conditions."""
        prof = self.prompts.profile_for(patient_id)
        extras = " ".join(prof.conditions[:2]) if prof.conditions else ""
        return f"{question} {extras}".strip()

    # -- Publication -------------------------------------------------------

    def _publish_diagnosis(
        self,
        patient_id: str,
        request_id: str,
        answer: str = "",
        question: str = "",
        passages: Optional[List[Dict[str, Any]]] = None,
        kiwix_url: str = "",
        error: str = "",
    ) -> None:
        vitals_snapshot = {
            k: {"value": v.value, "unit": v.unit, "age_seconds": int(v.age_seconds())}
            for k, v in self.vitals.latest(patient_id).items()
        }
        payload = {
            "request_id": request_id,
            "patient_id": patient_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "question": question,
            "answer": answer,
            "error": error,
            "model": self.cfg.model,
            "vitals_used": vitals_snapshot,
            "sources": [
                {"source": p["source"], "page": p.get("page")} for p in (passages or [])
            ],
            "kiwix_search_url": kiwix_url,
            "disclaimer": (
                "Offline decision support for a trained responder. Not a substitute "
                "for a clinician. Seek physician contact whenever available."
            ),
        }
        topic = f"shtf/medical/diagnosis/{patient_id}"
        self.mqtt.publish(topic, json.dumps(payload), qos=1, retain=True)
        logger.info("Published diagnosis to %s (request_id=%s)", topic, request_id)

    def _publish_status(self, state: str, **extra) -> None:
        payload = {
            "state": state,
            "model": self.cfg.model,
            "model_loaded": self.ollama.model_present(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        payload.update(extra)
        self.mqtt.publish("shtf/medical/ai/status", json.dumps(payload), qos=1)

    def _publish_derived(self, patient_id: str) -> None:
        """Per docs/SPECTER_MEDICAL_UI_BRIEF.md 1.3: derived metrics get
        their own retained topic so a dashboard can render them without
        recomputing from raw vitals itself."""
        payload = dict(self.vitals.derived(patient_id))
        payload["patient_id"] = patient_id
        payload["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
        topic = f"shtf/medical/derived/{patient_id}"
        self.mqtt.publish(topic, json.dumps(payload), qos=1, retain=True)

    # -- Run ---------------------------------------------------------------

    def start(self) -> None:
        if not self.ollama.health():
            logger.warning(
                "Ollama not reachable at %s. Start it with: systemctl start ollama",
                self.cfg.ollama_host,
            )
        elif not self.ollama.model_present():
            logger.warning(
                "Model %s not found. Pull it with: ollama pull %s",
                self.cfg.model,
                self.cfg.model,
            )
        else:
            logger.info("Ollama ready, model %s present", self.cfg.model)

        self.mqtt.will_set(
            "shtf/medical/ai/status",
            json.dumps({"state": "offline"}),
            qos=1,
            retain=True,
        )
        self.mqtt.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        self.mqtt.loop_forever()

    def stop(self) -> None:
        self._publish_status("offline")
        self.mqtt.disconnect()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="SPECTER Medical AI Engine (Jetson)")
    ap.add_argument("--mqtt-host", default="192.168.1.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="medgemma:4b")
    ap.add_argument("--kiwix-host", default="http://192.168.1.5:8080")
    ap.add_argument("--chroma-path", default="/opt/specter/chroma")
    ap.add_argument(
        "--ask",
        metavar="QUESTION",
        help="Run a single query from the command line and exit (bypasses MQTT loop)",
    )
    ap.add_argument("--patient", default="operator", help="Patient ID for --ask")
    args = ap.parse_args()

    cfg = Config(
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        ollama_host=args.ollama_host,
        model=args.model,
        kiwix_host=args.kiwix_host,
        chroma_path=args.chroma_path,
    )

    engine = MedicalAIEngine(cfg)

    if args.ask:
        # One-shot mode: connect briefly to absorb retained vitals, then answer.
        engine.mqtt.connect(cfg.mqtt_host, cfg.mqtt_port, keepalive=60)
        engine.mqtt.loop_start()
        time.sleep(2)  # allow retained vitals messages to arrive
        passages = engine.retriever.retrieve(args.ask, n_results=4)
        prompt = engine.prompts.build(
            patient_id=args.patient,
            user_query=args.ask,
            passages=passages,
            retriever=engine.retriever,
        )
        print("\n" + "=" * 70)
        print(engine.ollama.generate(prompt))
        print("=" * 70 + "\n")
        engine.mqtt.loop_stop()
        engine.mqtt.disconnect()
        return

    logger.info("=" * 60)
    logger.info("SPECTER Medical AI Engine v1.2.0")
    logger.info("MQTT:   %s:%s", cfg.mqtt_host, cfg.mqtt_port)
    logger.info("Ollama: %s (model=%s)", cfg.ollama_host, cfg.model)
    logger.info("Kiwix:  %s", cfg.kiwix_host)
    logger.info("Chroma: %s", cfg.chroma_path)
    logger.info("=" * 60)

    try:
        engine.start()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        engine.stop()


if __name__ == "__main__":
    main()
