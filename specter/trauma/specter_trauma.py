#!/usr/bin/env python3
"""
SPECTER Trauma / Mass Casualty Module
Runs on: Node 1 (Pi 5, 192.168.1.1) alongside the MQTT broker

Responsibilities:
  1. Casualty registry - ephemeral, numbered records (C-1, C-2, ...)
  2. START triage with retriage history
  3. Intervention logging with automatic timestamps
  4. Tourniquet clocks - the most important timestamp in trauma
  5. Hypothermia timers - starts on casualty creation, runs until warmed
  6. MARCH protocol state per casualty
  7. Reassessment discipline - surfaces casualties going stale
  8. Scene clock and casualty count

Publishes to:  shtf/trauma/scene
               shtf/trauma/casualty/<casualty_id>
               shtf/trauma/alert
Subscribes to: shtf/trauma/command/#

TRAINING GATE
-------------
This module logs and times interventions. It does not teach them. Several
procedures represented here - needle decompression, supraglottic airway
placement, intraosseous access - cause harm when performed without training.
The protocol content below follows the published MARCH / TCCC structure and
START triage, but it is a memory aid for a trained responder, not instruction.

Author: SPECTER Build Team
Date: August 2026
Version: 1.0.0
"""

import os
import json
import time
import logging
import argparse
import threading
from enum import Enum
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, Any, List

import paho.mqtt.client as mqtt

# --- paho-mqtt 1.x / 2.x compatibility -------------------------------------
def _mqtt_client(client_id: str = ""):
    """Construct an MQTT client that works on paho-mqtt 1.x and 2.x."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except (AttributeError, TypeError):
        return _mqtt_client(client_id)
# ---------------------------------------------------------------------------

# --- MQTT auth --------------------------------------------------------------
# See docs/MANUAL.md Part 3.3 - the broker requires auth. These are the
# fallback credentials used only when specter.json has no mqtt.username/
# mqtt.password (e.g. running outside a real install).
MQTT_DEFAULT_USERNAME = "specter"
MQTT_DEFAULT_PASSWORD = "specter-change-me"


def _mqtt_credentials() -> tuple:
    """Read MQTT username/password from /etc/specter/specter.json (written
    by the installer) if available, else fall back to the documented default."""
    try:
        cfg = json.loads(Path("/etc/specter/specter.json").read_text())
        mqtt_cfg = cfg.get("mqtt", {})
        return (
            mqtt_cfg.get("username", MQTT_DEFAULT_USERNAME),
            mqtt_cfg.get("password", MQTT_DEFAULT_PASSWORD),
        )
    except Exception:
        return MQTT_DEFAULT_USERNAME, MQTT_DEFAULT_PASSWORD
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.environ.get("SPECTER_LOG", "/var/log/specter/trauma.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("specter.trauma")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_seconds(iso_ts: str) -> float:
    try:
        ts = datetime.fromisoformat(iso_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 0.0


def fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ===========================================================================
# ENUMS
# ===========================================================================

class TriageCategory(Enum):
    """
    START triage categories. Conventional color mapping is preserved for
    compatibility with physical triage tags, but the UI must never rely on
    color alone to convey category.
    """
    IMMEDIATE = "IMMEDIATE"   # red    - life threat, treatable now
    DELAYED = "DELAYED"       # yellow - serious, can wait
    MINIMAL = "MINIMAL"       # green  - walking wounded
    EXPECTANT = "EXPECTANT"   # grey   - unlikely to survive given resources
    DECEASED = "DECEASED"     # black

    @property
    def sort_rank(self) -> int:
        return {
            "IMMEDIATE": 0,
            "DELAYED": 1,
            "MINIMAL": 2,
            "EXPECTANT": 3,
            "DECEASED": 4,
        }[self.value]


class Mechanism(Enum):
    GSW = "gsw"
    BLAST = "blast"
    FALL = "fall"
    MVC = "mvc"
    CRUSH = "crush"
    BURN = "burn"
    STAB = "stab"
    ANIMAL = "animal"
    UNKNOWN = "unknown"

    @property
    def implications(self) -> List[str]:
        """
        Mechanism drives what you go looking for. These are prompts to assess,
        not diagnoses.
        """
        return {
            "gsw": [
                "Look for exit wounds - count holes, they come in even numbers "
                "more often than not",
                "Expose fully; a second wound is easy to miss under clothing",
                "Chest or abdomen involvement means rapid deterioration is possible",
            ],
            "blast": [
                "Primary blast injury: lungs and hollow organs, may present late",
                "Check tympanic membranes - rupture suggests significant overpressure",
                "Assume multiple injury mechanisms simultaneously",
                "Secondary fragmentation wounds may be small and numerous",
            ],
            "fall": [
                "Assume spinal injury until cleared",
                "Calcaneal, pelvic, and spinal fractures cluster in axial-load falls",
                "Height and landing surface both matter",
            ],
            "mvc": [
                "Assume spinal injury until cleared",
                "Look for seatbelt sign - marks the path of deceleration injury",
                "Steering wheel or dash contact suggests chest and abdominal trauma",
            ],
            "crush": [
                "CRUSH SYNDROME RISK - rhabdomyolysis and hyperkalemia after release",
                "Deterioration classically follows extrication, not precedes it",
                "This is the strongest ECG indication in the entire kit - "
                "watch for peaked T waves and widening QRS",
                "Hydration before release if extrication is prolonged and you are trained",
            ],
            "burn": [
                "Airway: singed nasal hair, soot, hoarseness, facial burns - "
                "swelling progresses and the window to act closes",
                "Estimate burn extent by rule of nines",
                "Fluid needs are large; hypothermia risk is high despite the mechanism",
            ],
            "stab": [
                "Do not remove impaled objects - stabilize in place",
                "Track the likely path; small entry does not mean small injury",
            ],
            "animal": [
                "High infection risk - both household patients are immunosuppressed",
                "Consider rabies exposure and tetanus status",
            ],
            "unknown": [
                "Expose and examine systematically front and back",
            ],
        }[self.value]


class InterventionType(Enum):
    TOURNIQUET = "tourniquet"
    WOUND_PACKING = "wound_packing"
    PRESSURE_DRESSING = "pressure_dressing"
    PELVIC_BINDER = "pelvic_binder"
    NPA = "npa"
    IGEL = "igel"
    RECOVERY_POSITION = "recovery_position"
    CHEST_SEAL = "chest_seal"
    NEEDLE_DECOMPRESSION = "needle_decompression"
    IO_ACCESS = "io_access"
    IV_ACCESS = "iv_access"
    TXA = "txa"
    FLUID = "fluid"
    HYPOTHERMIA_WRAP = "hypothermia_wrap"
    SPLINT = "splint"
    EYE_SHIELD = "eye_shield"
    ANALGESIA = "analgesia"
    ANTIBIOTIC = "antibiotic"


# ===========================================================================
# MARCH PROTOCOL CONTENT
# ===========================================================================

@dataclass
class ProtocolStep:
    step_id: str
    label: str
    detail: str = ""
    intervention: Optional[str] = None
    is_reassessment: bool = False
    training_gated: bool = False
    warning: str = ""


@dataclass
class ProtocolPhase:
    key: str
    letter: str
    title: str
    premise: str
    steps: List[ProtocolStep]


def march_protocol() -> List[ProtocolPhase]:
    """
    MARCH, in order. The order is the point: it sequences interventions by how
    fast the untreated problem kills. Sections may be worked in any order by
    the operator - the system does not know what it is looking at - but the
    display order never changes.
    """
    return [
        ProtocolPhase(
            key="massive_hemorrhage",
            letter="M",
            title="MASSIVE HEMORRHAGE",
            premise=(
                "Exsanguination is the fastest preventable death in penetrating "
                "trauma. Control bleeding before anything else, including airway."
            ),
            steps=[
                ProtocolStep(
                    "m1", "Identify the bleeding",
                    "Expose the area. Blood tracks and pools away from its source - "
                    "find the hole, do not treat the puddle.",
                ),
                ProtocolStep(
                    "m2", "Tourniquet - extremity bleeding",
                    "High and tight over clothing for the initial application. "
                    "Tighten until bleeding stops AND the distal pulse is gone. "
                    "A tourniquet that lets bleeding continue is worse than none - "
                    "it obstructs venous return while arterial flow persists.",
                    intervention="tourniquet",
                    warning="Log the time. Write it on the windlass as well.",
                ),
                ProtocolStep(
                    "m3", "Wound packing - junctional bleeding",
                    "Groin, axilla, neck - a tourniquet cannot reach these. Pack "
                    "hemostatic gauze directly onto the bleeding vessel, deep into "
                    "the wound, then hold firm pressure for 3 full minutes by the "
                    "clock. Do not peek early.",
                    intervention="wound_packing",
                ),
                ProtocolStep(
                    "m4", "Pressure dressing",
                    "Over packed wounds and for bleeding not requiring a tourniquet.",
                    intervention="pressure_dressing",
                ),
                ProtocolStep(
                    "m5", "Pelvic binder",
                    "Suspected pelvic fracture in blunt trauma. Position at the "
                    "greater trochanters, not the iliac crests.",
                    intervention="pelvic_binder",
                ),
                ProtocolStep(
                    "m6", "Reassess bleeding control",
                    "Dressings soak through and tourniquets loosen as limbs change "
                    "shape. Look again.",
                    is_reassessment=True,
                ),
            ],
        ),
        ProtocolPhase(
            key="airway",
            letter="A",
            title="AIRWAY",
            premise=(
                "An unconscious patient's airway closes from the tongue and from "
                "swelling. Positioning solves most of it."
            ),
            steps=[
                ProtocolStep(
                    "a1", "Assess - can they speak?",
                    "A patient talking in full sentences has a patent airway. "
                    "Snoring, gurgling, or stridor means it is compromised now.",
                ),
                ProtocolStep(
                    "a2", "Open the airway",
                    "Chin lift or jaw thrust. Use jaw thrust where spinal injury "
                    "is possible.",
                ),
                ProtocolStep(
                    "a3", "Nasopharyngeal airway",
                    "Tolerated by semi-conscious patients where an oral airway is "
                    "not. Lubricate, insert along the floor of the nostril.",
                    intervention="npa",
                    warning="Avoid with suspected mid-face or base-of-skull fracture.",
                ),
                ProtocolStep(
                    "a4", "Recovery position",
                    "Unconscious, breathing, no spinal concern - this alone "
                    "protects the airway better than most equipment.",
                    intervention="recovery_position",
                ),
                ProtocolStep(
                    "a5", "Supraglottic airway",
                    "Deeply unconscious with no gag reflex.",
                    intervention="igel",
                    training_gated=True,
                    warning="Causes aspiration if placed in a patient with a gag reflex.",
                ),
                ProtocolStep(
                    "a6", "Reassess airway",
                    "Swelling progresses. Burns and neck trauma especially - the "
                    "airway that was fine ten minutes ago may not be.",
                    is_reassessment=True,
                ),
            ],
        ),
        ProtocolPhase(
            key="respiration",
            letter="R",
            title="RESPIRATION",
            premise=(
                "Tension pneumothorax is the most survivable preventable death in "
                "penetrating chest trauma. It develops over minutes and it can "
                "develop under a chest seal you already placed."
            ),
            steps=[
                ProtocolStep(
                    "r1", "Expose and examine the chest",
                    "Front and back, neck to waist. Feel as well as look. Count "
                    "the holes.",
                ),
                ProtocolStep(
                    "r2", "Vented chest seal - every open chest wound",
                    "Vented allows trapped air out and prevents tension developing "
                    "underneath. Seal entry and exit both.",
                    intervention="chest_seal",
                ),
                ProtocolStep(
                    "r3", "Assess for tension pneumothorax",
                    "Increasing respiratory distress, decreasing breath sounds on "
                    "one side, rising heart rate, falling blood pressure. Tracheal "
                    "deviation and distended neck veins are late and unreliable - "
                    "do not wait for them.",
                ),
                ProtocolStep(
                    "r4", "Needle decompression",
                    "14g x 3.25 inch catheter, 5th intercostal space anterior "
                    "axillary line, over the top of the rib. Standard IV catheters "
                    "are too short to reach the pleural space in most adults.",
                    intervention="needle_decompression",
                    training_gated=True,
                    warning="Causes harm when the diagnosis is wrong. Training required.",
                ),
                ProtocolStep(
                    "r5", "Burp the seal if distress increases",
                    "Lift one corner of the seal to release trapped air. Simpler "
                    "and safer than a needle where the seal is the likely cause.",
                    is_reassessment=True,
                ),
                ProtocolStep(
                    "r6", "Reassess after every intervention",
                    "Tension can develop, and can recur after decompression.",
                    is_reassessment=True,
                ),
            ],
        ),
        ProtocolPhase(
            key="circulation",
            letter="C",
            title="CIRCULATION",
            premise=(
                "Blood pressure is the last thing to fall. Heart rate rising and "
                "pulse pressure narrowing tell you first - watch the shock index."
            ),
            steps=[
                ProtocolStep(
                    "c1", "Assess perfusion",
                    "Radial pulse present suggests a systolic roughly above 90. "
                    "Capillary refill, skin colour, mental status. Mental status "
                    "change in a bleeding patient is shock until proven otherwise.",
                ),
                ProtocolStep(
                    "c2", "Intraosseous access",
                    "Proximal tibia. Far more achievable than a peripheral IV in a "
                    "shocked patient with collapsed veins.",
                    intervention="io_access",
                    training_gated=True,
                ),
                ProtocolStep(
                    "c3", "IV access",
                    "Where veins are accessible and time permits.",
                    intervention="iv_access",
                ),
                ProtocolStep(
                    "c4", "Tranexamic acid",
                    "Give as early as possible in significant hemorrhage. Benefit "
                    "falls with delay and is absent or harmful beyond 3 hours.",
                    intervention="txa",
                    training_gated=True,
                    warning="Prescription medication. Dosing per your physician's direction.",
                ),
                ProtocolStep(
                    "c5", "Fluid - cautiously",
                    "Titrate to a palpable radial pulse and improved mental status, "
                    "not to a normal blood pressure. Over-resuscitation dislodges "
                    "clot and worsens bleeding.",
                    intervention="fluid",
                ),
                ProtocolStep(
                    "c6", "Reassess distal to every tourniquet",
                    "Confirm bleeding is still controlled and log elapsed time.",
                    is_reassessment=True,
                ),
            ],
        ),
        ProtocolPhase(
            key="hypothermia_head",
            letter="H",
            title="HYPOTHERMIA / HEAD",
            premise=(
                "A cold trauma patient stops clotting regardless of how well the "
                "wound was packed. Hypothermia, acidosis and coagulopathy reinforce "
                "each other. This is the cheapest intervention in the kit and the "
                "one most often skipped."
            ),
            steps=[
                ProtocolStep(
                    "h1", "Insulate from the ground",
                    "Conduction into cold ground is the main heat loss path. A foam "
                    "pad underneath matters more than a blanket on top.",
                ),
                ProtocolStep(
                    "h2", "Remove wet clothing",
                    "Cut it off rather than manipulating the patient. Wet clothing "
                    "removes heat many times faster than dry air.",
                ),
                ProtocolStep(
                    "h3", "Active warming and wrap",
                    "Chemical warming blanket inside an insulating shell. Cover the "
                    "head - significant loss occurs there.",
                    intervention="hypothermia_wrap",
                ),
                ProtocolStep(
                    "h4", "Head injury assessment",
                    "AVPU or GCS. Record a baseline so change is detectable. "
                    "Deterioration in level of consciousness is the finding that "
                    "matters, not the absolute number.",
                ),
                ProtocolStep(
                    "h5", "Elevate head 30 degrees if head injury suspected",
                    "Only once spine is protected and shock is addressed.",
                ),
                ProtocolStep(
                    "h6", "Reassess temperature and mental status",
                    "Both drift. Both are early warnings.",
                    is_reassessment=True,
                ),
            ],
        ),
    ]


# ===========================================================================
# START TRIAGE
# ===========================================================================

@dataclass
class TriageAssessment:
    """
    START triage decision support. The operator makes the call; this records
    the inputs and shows what the algorithm suggests from them.
    """
    walking: Optional[bool] = None
    breathing: Optional[bool] = None
    breathing_after_airway_opened: Optional[bool] = None
    respiratory_rate: Optional[int] = None
    radial_pulse_present: Optional[bool] = None
    cap_refill_seconds: Optional[float] = None
    follows_commands: Optional[bool] = None

    def suggest(self) -> tuple:
        """Returns (TriageCategory, reason). Suggestion only."""
        if self.walking is True:
            return TriageCategory.MINIMAL, "Ambulatory"

        if self.breathing is False:
            if self.breathing_after_airway_opened is False:
                return TriageCategory.DECEASED, "No respiration after airway opened"
            if self.breathing_after_airway_opened is True:
                return (
                    TriageCategory.IMMEDIATE,
                    "Respiration only after airway repositioning",
                )
            return (
                TriageCategory.IMMEDIATE,
                "Not breathing - open the airway and reassess",
            )

        if self.respiratory_rate is not None and self.respiratory_rate > 30:
            return TriageCategory.IMMEDIATE, f"Respiratory rate {self.respiratory_rate} > 30"

        if self.radial_pulse_present is False:
            return TriageCategory.IMMEDIATE, "No radial pulse"
        if self.cap_refill_seconds is not None and self.cap_refill_seconds > 2:
            return (
                TriageCategory.IMMEDIATE,
                f"Capillary refill {self.cap_refill_seconds}s > 2s",
            )

        if self.follows_commands is False:
            return TriageCategory.IMMEDIATE, "Does not follow commands"

        if all(
            v is not None
            for v in (self.respiratory_rate, self.radial_pulse_present, self.follows_commands)
        ):
            return TriageCategory.DELAYED, "Respiration, perfusion and mentation intact"

        return TriageCategory.DELAYED, "Assessment incomplete - default pending reassessment"


# ===========================================================================
# CASUALTY RECORD
# ===========================================================================

@dataclass
class Intervention:
    intervention_id: str
    type: str
    utc: str
    site: str = ""
    notes: str = ""
    protocol_step: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TourniquetRecord:
    """
    Tracked separately from other interventions because it carries a clock that
    drives decisions. Concern rises around 2 hours; limb viability falls sharply
    beyond 6. Conversion to a pressure dressing, where bleeding allows and the
    responder is trained, is a decision made against this elapsed time.
    """
    tq_id: str
    limb: str
    site: str
    applied_utc: str
    converted_utc: Optional[str] = None
    notes: str = ""

    @property
    def elapsed(self) -> float:
        end = self.converted_utc or utcnow()
        try:
            a = datetime.fromisoformat(self.applied_utc)
            b = datetime.fromisoformat(end)
            if a.tzinfo is None:
                a = a.replace(tzinfo=timezone.utc)
            if b.tzinfo is None:
                b = b.replace(tzinfo=timezone.utc)
            return (b - a).total_seconds()
        except Exception:
            return 0.0

    @property
    def alert_level(self) -> str:
        if self.converted_utc:
            return "converted"
        h = self.elapsed / 3600
        if h >= 4:
            return "critical"
        if h >= 2:
            return "caution"
        return "normal"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["elapsed_seconds"] = self.elapsed
        d["elapsed_display"] = fmt_elapsed(self.elapsed)
        d["alert_level"] = self.alert_level
        return d


@dataclass
class Casualty:
    casualty_id: str
    found_utc: str
    mechanism: str = "unknown"
    triage_category: str = "DELAYED"
    triage_history: List[Dict[str, Any]] = field(default_factory=list)
    triage_assessment: Dict[str, Any] = field(default_factory=dict)
    age_estimate: str = ""
    sex_estimate: str = ""
    notes: str = ""

    interventions: List[Intervention] = field(default_factory=list)
    tourniquets: List[TourniquetRecord] = field(default_factory=list)
    vitals: List[Dict[str, Any]] = field(default_factory=list)

    protocol_state: Dict[str, bool] = field(default_factory=dict)
    last_assessed_utc: str = ""
    hypothermia_managed: bool = False
    disposition: str = "on_scene"

    # ---- derived --------------------------------------------------------

    @property
    def seconds_since_assessed(self) -> float:
        return elapsed_seconds(self.last_assessed_utc or self.found_utc)

    @property
    def stale(self) -> bool:
        """
        Casualties deteriorate quietly while you work on someone else. Immediate
        patients need eyes on far more often than delayed ones.
        """
        limit = {
            "IMMEDIATE": 300,   # 5 min
            "DELAYED": 900,     # 15 min
            "MINIMAL": 1800,    # 30 min
            "EXPECTANT": 900,
            "DECEASED": 86400,
        }.get(self.triage_category, 900)
        return self.seconds_since_assessed > limit

    @property
    def latest_vitals(self) -> Dict[str, Any]:
        return self.vitals[-1] if self.vitals else {}

    @property
    def shock_index(self) -> Optional[float]:
        v = self.latest_vitals
        hr, sbp = v.get("pulse"), v.get("bp_systolic")
        if isinstance(hr, (int, float)) and isinstance(sbp, (int, float)) and sbp > 0:
            return round(hr / sbp, 2)
        return None

    @property
    def active_alerts(self) -> List[Dict[str, str]]:
        alerts = []

        for tq in self.tourniquets:
            if tq.alert_level == "critical":
                alerts.append({
                    "level": "critical",
                    "text": f"Tourniquet {tq.limb} at {fmt_elapsed(tq.elapsed)} "
                            f"- limb viability concern",
                })
            elif tq.alert_level == "caution":
                alerts.append({
                    "level": "caution",
                    "text": f"Tourniquet {tq.limb} at {fmt_elapsed(tq.elapsed)}",
                })

        si = self.shock_index
        if si is not None and si > 1.0:
            alerts.append({
                "level": "critical",
                "text": f"Shock index {si} - decompensating",
            })
        elif si is not None and si > 0.9:
            alerts.append({"level": "caution", "text": f"Shock index {si}"})

        if self.stale and self.triage_category in ("IMMEDIATE", "DELAYED", "EXPECTANT"):
            alerts.append({
                "level": "caution",
                "text": f"Not reassessed in {fmt_elapsed(self.seconds_since_assessed)}",
            })

        hypo_elapsed = elapsed_seconds(self.found_utc)
        if not self.hypothermia_managed and hypo_elapsed > 900:
            alerts.append({
                "level": "caution",
                "text": "Hypothermia prevention not logged - cold patients stop clotting",
            })

        return alerts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "casualty_id": self.casualty_id,
            "found_utc": self.found_utc,
            "elapsed_display": fmt_elapsed(elapsed_seconds(self.found_utc)),
            "mechanism": self.mechanism,
            "mechanism_implications": Mechanism(self.mechanism).implications,
            "triage_category": self.triage_category,
            "triage_sort_rank": TriageCategory(self.triage_category).sort_rank,
            "triage_history": self.triage_history,
            "triage_assessment": self.triage_assessment,
            "age_estimate": self.age_estimate,
            "sex_estimate": self.sex_estimate,
            "notes": self.notes,
            "interventions": [i.to_dict() for i in self.interventions],
            "tourniquets": [t.to_dict() for t in self.tourniquets],
            "latest_vitals": self.latest_vitals,
            "vitals_count": len(self.vitals),
            "shock_index": self.shock_index,
            "protocol_state": self.protocol_state,
            "last_assessed_utc": self.last_assessed_utc,
            "seconds_since_assessed": int(self.seconds_since_assessed),
            "stale": self.stale,
            "hypothermia_managed": self.hypothermia_managed,
            "disposition": self.disposition,
            "active_alerts": self.active_alerts,
        }


# ===========================================================================
# SCENE REGISTRY
# ===========================================================================

class SceneRegistry:
    """
    Holds all casualties for one incident. Records are ephemeral by design -
    they belong to a scene, not to a permanent patient file. Persisted to disk
    on every mutation so a power loss mid-incident does not erase the log.
    """

    def __init__(self, persist_path: str = "/var/lib/specter/scene.json"):
        self._lock = threading.Lock()
        self.casualties: Dict[str, Casualty] = {}
        self.scene_opened_utc = utcnow()
        self.scene_active = False
        self.persist_path = persist_path
        self._counter = 0

    # ---- lifecycle ------------------------------------------------------

    def open_scene(self) -> None:
        with self._lock:
            self.casualties.clear()
            self._counter = 0
            self.scene_opened_utc = utcnow()
            self.scene_active = True
        logger.info("Scene opened at %s", self.scene_opened_utc)
        self._persist()

    def close_scene(self) -> None:
        with self._lock:
            self.scene_active = False
        logger.info("Scene closed - %d casualties recorded", len(self.casualties))
        self._persist()

    # ---- casualties -----------------------------------------------------

    def add_casualty(self, mechanism: str = "unknown", notes: str = "") -> Casualty:
        """One tap creates a record. Classification comes after."""
        with self._lock:
            if not self.scene_active:
                self.scene_active = True
                self.scene_opened_utc = utcnow()
            self._counter += 1
            cid = f"C-{self._counter}"
            now = utcnow()
            try:
                mech = Mechanism(mechanism).value
            except ValueError:
                mech = Mechanism.UNKNOWN.value
            c = Casualty(
                casualty_id=cid,
                found_utc=now,
                last_assessed_utc=now,
                mechanism=mech,
                notes=notes,
            )
            self.casualties[cid] = c
        logger.info("Casualty %s added (mechanism=%s)", cid, mech)
        self._persist()
        return c

    def get(self, casualty_id: str) -> Optional[Casualty]:
        with self._lock:
            return self.casualties.get(casualty_id)

    def triage(
        self,
        casualty_id: str,
        category: Optional[str] = None,
        assessment: Optional[Dict[str, Any]] = None,
        by: str = "operator",
    ) -> Optional[Casualty]:
        c = self.get(casualty_id)
        if not c:
            return None

        suggested, reason = None, ""
        if assessment:
            ta = TriageAssessment(**{
                k: v for k, v in assessment.items()
                if k in TriageAssessment.__dataclass_fields__
            })
            suggested, reason = ta.suggest()
            c.triage_assessment = {
                **assessment,
                "suggested": suggested.value,
                "reason": reason,
            }

        final = category or (suggested.value if suggested else c.triage_category)
        try:
            final = TriageCategory(final).value
        except ValueError:
            final = TriageCategory.DELAYED.value

        with self._lock:
            c.triage_category = final
            c.triage_history.append({
                "utc": utcnow(),
                "category": final,
                "by": by,
                "suggested": suggested.value if suggested else None,
                "reason": reason,
            })
            c.last_assessed_utc = utcnow()

        logger.info("Casualty %s triaged %s (%s)", casualty_id, final, reason or "manual")
        self._persist()
        return c

    def log_intervention(
        self,
        casualty_id: str,
        itype: str,
        site: str = "",
        notes: str = "",
        protocol_step: str = "",
    ) -> Optional[Casualty]:
        c = self.get(casualty_id)
        if not c:
            return None

        now = utcnow()
        with self._lock:
            iid = f"{casualty_id}-I{len(c.interventions) + 1}"
            c.interventions.append(Intervention(
                intervention_id=iid, type=itype, utc=now,
                site=site, notes=notes, protocol_step=protocol_step,
            ))

            if itype == InterventionType.TOURNIQUET.value:
                c.tourniquets.append(TourniquetRecord(
                    tq_id=f"{casualty_id}-TQ{len(c.tourniquets) + 1}",
                    limb=site or "unspecified",
                    site=site,
                    applied_utc=now,
                    notes=notes,
                ))
                logger.warning(
                    "TOURNIQUET APPLIED %s %s at %s - write this time on the windlass",
                    casualty_id, site, now,
                )

            if itype == InterventionType.HYPOTHERMIA_WRAP.value:
                c.hypothermia_managed = True

            if protocol_step:
                c.protocol_state[protocol_step] = True

            c.last_assessed_utc = now

        logger.info("Casualty %s intervention: %s %s", casualty_id, itype, site)
        self._persist()
        return c

    def convert_tourniquet(self, casualty_id: str, tq_id: str) -> Optional[Casualty]:
        c = self.get(casualty_id)
        if not c:
            return None
        with self._lock:
            for tq in c.tourniquets:
                if tq.tq_id == tq_id and not tq.converted_utc:
                    tq.converted_utc = utcnow()
                    logger.info(
                        "Tourniquet %s converted after %s", tq_id, fmt_elapsed(tq.elapsed)
                    )
            c.last_assessed_utc = utcnow()
        self._persist()
        return c

    def record_vitals(self, casualty_id: str, vitals: Dict[str, Any]) -> Optional[Casualty]:
        c = self.get(casualty_id)
        if not c:
            return None
        with self._lock:
            c.vitals.append({**vitals, "utc": utcnow()})
            c.last_assessed_utc = utcnow()
        self._persist()
        return c

    def mark_assessed(self, casualty_id: str) -> Optional[Casualty]:
        c = self.get(casualty_id)
        if c:
            with self._lock:
                c.last_assessed_utc = utcnow()
            self._persist()
        return c

    def set_protocol_step(
        self, casualty_id: str, step_id: str, done: bool = True
    ) -> Optional[Casualty]:
        c = self.get(casualty_id)
        if not c:
            return None
        with self._lock:
            c.protocol_state[step_id] = done
            c.last_assessed_utc = utcnow()
        self._persist()
        return c

    # ---- views ----------------------------------------------------------

    def sorted_casualties(self) -> List[Casualty]:
        """Category first, then stale ones up, then longest since assessment."""
        with self._lock:
            items = list(self.casualties.values())
        return sorted(
            items,
            key=lambda c: (
                TriageCategory(c.triage_category).sort_rank,
                not c.stale,
                -c.seconds_since_assessed,
            ),
        )

    def scene_summary(self) -> Dict[str, Any]:
        cas = self.sorted_casualties()
        counts = {t.value: 0 for t in TriageCategory}
        for c in cas:
            counts[c.triage_category] += 1

        alerts = []
        for c in cas:
            for a in c.active_alerts:
                alerts.append({**a, "casualty_id": c.casualty_id})
        alerts.sort(key=lambda a: 0 if a["level"] == "critical" else 1)

        return {
            "scene_active": self.scene_active,
            "scene_opened_utc": self.scene_opened_utc,
            "scene_elapsed_display": fmt_elapsed(elapsed_seconds(self.scene_opened_utc)),
            "casualty_count": len(cas),
            "counts_by_category": counts,
            "casualties": [c.to_dict() for c in cas],
            "alerts": alerts,
            "timestamp_utc": utcnow(),
        }

    # ---- persistence ----------------------------------------------------

    def _persist(self) -> None:
        try:
            import os
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            with open(self.persist_path, "w") as f:
                json.dump(self.scene_summary(), f, indent=2)
        except Exception as exc:
            logger.error("Could not persist scene: %s", exc)


# ===========================================================================
# MQTT SERVICE
# ===========================================================================

class TraumaService:
    TOPIC_COMMAND = "shtf/trauma/command/#"
    TOPIC_SCENE = "shtf/trauma/scene"
    TOPIC_ALERT = "shtf/trauma/alert"
    TOPIC_PROTOCOL = "shtf/trauma/protocol"

    def __init__(self, mqtt_host: str, mqtt_port: int, persist_path: str):
        self.registry = SceneRegistry(persist_path=persist_path)
        self.protocol = march_protocol()
        self.mqtt = _mqtt_client("specter-trauma")
        self.mqtt.username_pw_set(*_mqtt_credentials())
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self._stop = threading.Event()

    # ---- mqtt -----------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connected to %s:%s", self.mqtt_host, self.mqtt_port)
            client.subscribe(self.TOPIC_COMMAND, qos=1)
            self._publish_protocol()
            self._publish_scene()
        else:
            logger.error("MQTT connect failed rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode()) if msg.payload else {}
        except Exception:
            logger.warning("Non-JSON command on %s", msg.topic)
            return

        cmd = msg.topic.split("/")[-1]
        r = self.registry

        try:
            if cmd == "open_scene":
                r.open_scene()
            elif cmd == "close_scene":
                r.close_scene()
            elif cmd == "add_casualty":
                r.add_casualty(
                    mechanism=payload.get("mechanism", "unknown"),
                    notes=payload.get("notes", ""),
                )
            elif cmd == "triage":
                r.triage(
                    payload["casualty_id"],
                    category=payload.get("category"),
                    assessment=payload.get("assessment"),
                )
            elif cmd == "intervention":
                r.log_intervention(
                    payload["casualty_id"],
                    payload["type"],
                    site=payload.get("site", ""),
                    notes=payload.get("notes", ""),
                    protocol_step=payload.get("protocol_step", ""),
                )
            elif cmd == "convert_tourniquet":
                r.convert_tourniquet(payload["casualty_id"], payload["tq_id"])
            elif cmd == "vitals":
                r.record_vitals(payload["casualty_id"], payload.get("vitals", {}))
            elif cmd == "assessed":
                r.mark_assessed(payload["casualty_id"])
            elif cmd == "protocol_step":
                r.set_protocol_step(
                    payload["casualty_id"],
                    payload["step_id"],
                    payload.get("done", True),
                )
            else:
                logger.warning("Unknown command: %s", cmd)
                return

            self._publish_scene()

        except KeyError as exc:
            logger.error("Command %s missing field %s", cmd, exc)
        except Exception:
            logger.exception("Command %s failed", cmd)

    # ---- publishing -----------------------------------------------------

    def _publish_scene(self) -> None:
        summary = self.registry.scene_summary()
        self.mqtt.publish(self.TOPIC_SCENE, json.dumps(summary), qos=1, retain=True)

        for c in summary["casualties"]:
            self.mqtt.publish(
                f"shtf/trauma/casualty/{c['casualty_id']}",
                json.dumps(c), qos=1, retain=True,
            )

        if summary["alerts"]:
            self.mqtt.publish(
                self.TOPIC_ALERT, json.dumps(summary["alerts"]), qos=1
            )

    def _publish_protocol(self) -> None:
        """Protocol content is static; publish retained so any client has it."""
        payload = [
            {
                "key": p.key,
                "letter": p.letter,
                "title": p.title,
                "premise": p.premise,
                "steps": [asdict(s) for s in p.steps],
            }
            for p in self.protocol
        ]
        self.mqtt.publish(
            self.TOPIC_PROTOCOL, json.dumps(payload), qos=1, retain=True
        )

    # ---- clock ----------------------------------------------------------

    def _clock_loop(self) -> None:
        """
        Tourniquet clocks and staleness are time-derived, so the scene must be
        republished on a timer even when nothing is being entered.
        """
        while not self._stop.wait(10):
            if self.registry.casualties:
                self._publish_scene()

    def start(self) -> None:
        self.mqtt.will_set(
            self.TOPIC_ALERT,
            json.dumps([{"level": "critical", "text": "Trauma service offline"}]),
            qos=1, retain=False,
        )
        self.mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        threading.Thread(target=self._clock_loop, daemon=True).start()
        self.mqtt.loop_forever()

    def stop(self) -> None:
        self._stop.set()
        self.mqtt.disconnect()


# ===========================================================================
# CLI
# ===========================================================================

def print_protocol() -> None:
    for phase in march_protocol():
        print(f"\n{'=' * 70}")
        print(f"  {phase.letter}   {phase.title}")
        print(f"{'=' * 70}")
        print(f"  {phase.premise}\n")
        for s in phase.steps:
            mark = "[R]" if s.is_reassessment else "[ ]"
            gate = "  *TRAINING REQUIRED*" if s.training_gated else ""
            print(f"  {mark} {s.label}{gate}")
            if s.detail:
                for line in _wrap(s.detail, 64):
                    print(f"        {line}")
            if s.warning:
                print(f"        ! {s.warning}")
        print()


def _wrap(text: str, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="SPECTER Trauma / Mass Casualty Module")
    ap.add_argument("--mqtt-host", default="192.168.1.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--persist", default="/var/lib/specter/scene.json")
    ap.add_argument(
        "--print-protocol",
        action="store_true",
        help="Print the MARCH protocol to stdout and exit - use this to generate "
             "the laminated card that lives in the case",
    )
    args = ap.parse_args()

    if args.print_protocol:
        print_protocol()
        return

    logger.info("=" * 60)
    logger.info("SPECTER Trauma Module v1.0.0")
    logger.info("MQTT: %s:%s", args.mqtt_host, args.mqtt_port)
    logger.info("Scene persistence: %s", args.persist)
    logger.info("=" * 60)

    svc = TraumaService(args.mqtt_host, args.mqtt_port, args.persist)
    try:
        svc.start()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        svc.stop()


if __name__ == "__main__":
    main()
