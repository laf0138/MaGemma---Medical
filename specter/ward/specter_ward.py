#!/usr/bin/env python3
"""
SPECTER Ward Module — sustained bed care (hours to weeks)
Runs on: Node 1 (Pi 5, 192.168.1.1) alongside the MQTT broker

Per docs/SPECTER_CLINICAL_MODES.md Part 1: this is the highest-probability
scenario in the entire build - someone sick or recovering in bed for days,
not actively dying (RESUS) and not on a standing chronic regimen (CHRONIC).
The question this mode answers is "is this person getting better or worse,"
and what actually harms someone in this window is rarely the presenting
illness itself - it's dehydration, pressure injury, and deterioration
nobody caught because nobody was watching a trend.

Responsibilities:
  1. Care episode registry - one episode per patient, spans the bed-care
     window, closed when they're discharged/evacuated/handed off
  2. Fluid balance - every intake/output logged by measurement, running
     24h net and cumulative since episode open
  3. Care task scheduling - reposition, skin checks, and anything else the
     operator schedules, surfaced as a countdown, not a checklist
  4. Skin checks by anatomical site, escalating on non-blanching erythema
  5. NEWS2 early-warning scoring from vitals, trend-aware (a rise of >=2
     matters more than the absolute number)
  6. Nutrition and mobility logging
  7. Alarm derivation per the thresholds in SPECTER_CLINICAL_MODES.md 1.4

Publishes to:  shtf/ward/episode
               shtf/ward/episode/<episode_id>
               shtf/ward/alert
Subscribes to: shtf/ward/command/#

TRAINING GATE
-------------
This module tracks and times care tasks. It does not teach them. NEWS2 is
a hospital-validated screening aid, not a diagnosis, and is known to
under-trigger in immunosuppressed patients whose fever/inflammatory
response is pharmacologically blunted - a low score is not reassurance.
See episode_summary()'s "news2_caveat" field, always present.

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
from dataclasses import dataclass, field, asdict, fields as dataclass_fields
from pathlib import Path
from typing import Optional, Dict, Any, List

import paho.mqtt.client as mqtt

from medical.clinical_scores import calculate_news2

# --- paho-mqtt 1.x / 2.x compatibility -------------------------------------
def _mqtt_client(client_id: str = ""):
    """Construct an MQTT client that works on paho-mqtt 1.x and 2.x."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x has no CallbackAPIVersion - fall back to the
        # old-style constructor (deprecated but functional on 2.x too),
        # NOT a recursive call to this same function, which would hit the
        # same AttributeError every time and blow the stack.
        return mqtt.Client(client_id=client_id)
# ---------------------------------------------------------------------------

# --- MQTT auth --------------------------------------------------------------
# See docs/MANUAL.md Part 3.3 - the broker requires auth, with a dedicated
# least-privilege ACL account per service. This is the "ward" account: it
# can only read shtf/ward/command/# and write the episode/alert topics, so
# a leaked credential from any other service can't forge ward commands.
# Fallback values below are used only when specter.json has no
# mqtt.services.ward entry (e.g. running outside a real install).
MQTT_SERVICE_KEY      = "ward"
MQTT_DEFAULT_USERNAME = "specter-ward"
MQTT_DEFAULT_PASSWORD = "specter-change-me"


def _mqtt_credentials() -> tuple:
    """Read this service's MQTT username/password from
    /etc/specter/specter.json (written by the installer) if available,
    else fall back to the documented default."""
    try:
        cfg = json.loads(Path("/etc/specter/specter.json").read_text())
        mqtt_cfg = cfg.get("mqtt", {})
        service_cfg = mqtt_cfg.get("services", {}).get(MQTT_SERVICE_KEY)
        if service_cfg:
            return (
                service_cfg.get("username", MQTT_DEFAULT_USERNAME),
                service_cfg.get("password", MQTT_DEFAULT_PASSWORD),
            )
        # No dedicated services.<key> entry - do NOT fall back to the
        # broad "operator" credential (mqtt.username/password): that
        # account has readwrite on shtf/# by design (see
        # deploy/install_specter.py's ACL for it), so a missing config
        # entry would silently hand this service far MORE privilege than
        # its own least-privilege ACL grants, not less. Fall to this
        # service's own documented default instead - on a real broker its
        # password won't match the real (derived) one for this account,
        # so the connection is rejected rather than silently succeeding
        # with elevated access. Re-run the installer to fix this properly.
        logger.error(
            "specter.json has no mqtt.services.%s entry - using this "
            "service's own default credential (which will fail to "
            "authenticate against a real broker) instead of the broad "
            "operator account. Re-run deploy/install_specter.py.",
            MQTT_SERVICE_KEY,
        )
        return MQTT_DEFAULT_USERNAME, MQTT_DEFAULT_PASSWORD
    except Exception:
        return MQTT_DEFAULT_USERNAME, MQTT_DEFAULT_PASSWORD
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.environ.get("SPECTER_LOG", "/var/log/specter/ward.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("specter.ward")


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
    sign = "-" if s < 0 else ""
    s = abs(s)
    return f"{sign}{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ===========================================================================
# NEWS2 (National Early Warning Score 2, Royal College of Physicians UK,
# 2017) - a standard, publicly documented hospital early-warning score.
# ===========================================================================
#
# Scoring logic lives in specter/medical/clinical_scores.py (imported
# above), shared with the medical/chronic-patient AI engine so both call
# sites score identically. This module charts manual bedside entries, where
# an operator enters every NEWS2 parameter (including respiration rate and
# consciousness) by hand - that path is usually complete. See
# clinical_scores.py's module docstring for why the automated-device path
# (specter_medical_ai.py) is always partial by contrast.


# ===========================================================================
# ENUMS / CONSTANTS
# ===========================================================================

class CareTaskType(Enum):
    REPOSITION = "reposition"
    SKIN_CHECK = "skin_check"
    ORAL_CARE = "oral_care"
    ANKLE_PUMPS = "ankle_pumps"
    MEDICATION = "medication"
    FLUID_OFFER = "fluid_offer"
    WOUND_CHECK = "wound_check"
    TEMPERATURE = "temperature"
    ORIENTATION_CHECK = "orientation_check"
    BOWEL_CHECK = "bowel_check"


# Only these two get a built-in default interval, because
# SPECTER_CLINICAL_MODES.md Part 1.1/1.4 explicitly gives numbers for them
# ("every 2 hours", "q4h skin check"). Every other task type's real-world
# interval is prescription- or care-plan-specific (medication dosing above
# all) - inventing a universal default for those would be presenting a
# fabricated clinical parameter as if it were sourced, which this project
# does not do. add_care_task() requires an explicit interval for anything
# not in this dict.
DEFAULT_TASK_INTERVAL_MINUTES = {
    CareTaskType.REPOSITION.value: 120,
    CareTaskType.SKIN_CHECK.value: 240,
}

SKIN_SITES = (
    "sacrum", "heels_l", "heels_r", "elbows_l", "elbows_r",
    "occiput", "hips_l", "hips_r", "other",
)
SKIN_FINDINGS = ("intact", "blanching_erythema", "non_blanching", "broken")

DEFAULT_OBSERVATION_INTERVAL_MINUTES = 240  # q4h
ESCALATED_OBSERVATION_INTERVAL_MINUTES = 60  # q1h once NEWS2 >= 5


# ===========================================================================
# DATA CLASSES
# ===========================================================================

@dataclass
class CareTask:
    task_id: str
    task_type: str
    interval_minutes: int
    label: str = ""
    last_done_utc: str = ""
    created_utc: str = ""

    @property
    def next_due_utc_dt(self) -> Optional[datetime]:
        base = self.last_done_utc or self.created_utc
        if not base:
            return None
        try:
            dt = datetime.fromisoformat(base)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        from datetime import timedelta
        return dt + timedelta(minutes=self.interval_minutes)

    @property
    def minutes_until_due(self) -> Optional[float]:
        """Negative once overdue, positive while still due in the future."""
        due = self.next_due_utc_dt
        if due is None:
            return None
        return (due - datetime.now(timezone.utc)).total_seconds() / 60.0

    @property
    def overdue_minutes(self) -> float:
        """Always >= 0 - the amount that alarm thresholds compare against."""
        remaining = self.minutes_until_due
        if remaining is None:
            return 0.0
        return max(0.0, -remaining)

    def to_dict(self) -> Dict[str, Any]:
        due = self.next_due_utc_dt
        remaining = self.minutes_until_due
        if remaining is None:
            due_display = "--"
        elif remaining < 0:
            due_display = f"OVERDUE {fmt_elapsed(-remaining * 60)}"
        else:
            due_display = f"due in {fmt_elapsed(remaining * 60)}"
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "label": self.label or self.task_type.replace("_", " ").title(),
            "interval_minutes": self.interval_minutes,
            "last_done_utc": self.last_done_utc,
            "next_due_utc": due.isoformat() if due else None,
            "overdue_minutes": round(self.overdue_minutes, 1),
            "due_display": due_display,
        }


@dataclass
class FluidEntry:
    entry_id: str
    utc: str
    route: str          # oral|iv (intake); urine|emesis|stool|drain|blood (output)
    volume_ml: float
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SkinCheckEntry:
    utc: str
    sites: Dict[str, str] = field(default_factory=dict)  # site -> finding
    notes: str = ""

    @property
    def worst_finding(self) -> str:
        order = {"intact": 0, "blanching_erythema": 1, "broken": 2, "non_blanching": 3}
        worst = "intact"
        for finding in self.sites.values():
            if order.get(finding, 0) > order.get(worst, 0):
                worst = finding
        return worst

    def to_dict(self) -> Dict[str, Any]:
        return {"utc": self.utc, "sites": self.sites, "notes": self.notes,
                "worst_finding": self.worst_finding}


@dataclass
class NutritionEntry:
    utc: str
    description: str = ""
    estimated_kcal: Optional[float] = None
    estimated_protein_g: Optional[float] = None
    percent_consumed: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MobilityEntry:
    utc: str
    level: str = "bedbound"  # bedbound|sat_edge|stood|walked_assisted|walked_alone
    duration_minutes: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VitalsEntry:
    utc: str
    values: Dict[str, Any] = field(default_factory=dict)
    news2: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"utc": self.utc, "values": self.values, "news2": self.news2}


@dataclass
class CareEpisode:
    episode_id: str
    patient_id: str
    opened_utc: str
    presenting_problem: str = ""
    closed_utc: str = ""
    observation_interval_minutes: int = DEFAULT_OBSERVATION_INTERVAL_MINUTES

    fluid_intake: List[FluidEntry] = field(default_factory=list)
    fluid_output: List[FluidEntry] = field(default_factory=list)
    care_tasks: List[CareTask] = field(default_factory=list)
    skin_checks: List[SkinCheckEntry] = field(default_factory=list)
    nutrition: List[NutritionEntry] = field(default_factory=list)
    mobility: List[MobilityEntry] = field(default_factory=list)
    vitals: List[VitalsEntry] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return not self.closed_utc

    # ---- derived: fluid balance ------------------------------------------

    def _volume_since(self, entries: List[FluidEntry], seconds: float) -> float:
        total = 0.0
        for e in entries:
            if elapsed_seconds(e.utc) <= seconds:
                total += e.volume_ml
        return total

    @property
    def intake_24h_ml(self) -> float:
        return self._volume_since(self.fluid_intake, 86400)

    @property
    def output_24h_ml(self) -> float:
        return self._volume_since(self.fluid_output, 86400)

    @property
    def net_24h_ml(self) -> float:
        return self.intake_24h_ml - self.output_24h_ml

    @property
    def cumulative_net_ml(self) -> float:
        total_in = sum(e.volume_ml for e in self.fluid_intake)
        total_out = sum(e.volume_ml for e in self.fluid_output)
        return total_in - total_out

    @property
    def hours_since_open(self) -> float:
        return elapsed_seconds(self.opened_utc) / 3600.0

    # ---- derived: NEWS2 ----------------------------------------------------

    @property
    def latest_news2(self) -> Optional[Dict[str, Any]]:
        return self.vitals[-1].news2 if self.vitals else None

    @property
    def news2_rising(self) -> bool:
        """True when the two most recent NEWS2 totals rose by >= 2 -
        SPECTER_CLINICAL_MODES.md 1.4: the trend matters more than the
        absolute value."""
        if len(self.vitals) < 2:
            return False
        prev, cur = self.vitals[-2].news2, self.vitals[-1].news2
        if not prev or not cur:
            return False
        return (cur.get("total", 0) - prev.get("total", 0)) >= 2

    # ---- derived: alerts ----------------------------------------------------

    @property
    def active_alerts(self) -> List[Dict[str, str]]:
        alerts: List[Dict[str, str]] = []

        for t in self.care_tasks:
            if t.task_type == CareTaskType.REPOSITION.value and t.overdue_minutes > 30:
                alerts.append({
                    "level": "critical",
                    "text": f"Reposition overdue by {int(t.overdue_minutes)}m "
                            f"- this is how pressure injuries start",
                })

        if self.skin_checks:
            worst = self.skin_checks[-1].worst_finding
            if worst == "non_blanching":
                alerts.append({
                    "level": "critical",
                    "text": "Non-blanching erythema found - already a stage 1 "
                            "injury, offload immediately",
                })

        net24 = self.net_24h_ml
        if net24 < -1000:
            alerts.append({
                "level": "critical",
                "text": f"Net fluid balance {net24:+.0f} mL/24h - dehydration risk",
            })
        elif net24 > 1500:
            alerts.append({
                "level": "critical",
                "text": f"Net fluid balance {net24:+.0f} mL/24h - overload risk "
                        f"(graft and lungs)",
            })

        last_urine = next(
            (e for e in reversed(self.fluid_output) if e.route == "urine"), None
        )
        urine_hours = (elapsed_seconds(last_urine.utc) / 3600.0) if last_urine \
            else self.hours_since_open
        if urine_hours >= 8:
            alerts.append({
                "level": "critical",
                "text": f"No urine output logged in {urine_hours:.1f}h",
            })

        if self.news2_rising:
            prev_total = self.vitals[-2].news2.get("total")
            cur_total = self.vitals[-1].news2.get("total")
            alerts.append({
                "level": "critical",
                "text": f"NEWS2 rising {prev_total} -> {cur_total} between "
                        f"consecutive observations",
            })

        if self.vitals:
            since_last_obs_min = elapsed_seconds(self.vitals[-1].utc) / 60.0
        else:
            since_last_obs_min = elapsed_seconds(self.opened_utc) / 60.0
        obs_overdue = since_last_obs_min - self.observation_interval_minutes
        if obs_overdue > 60:
            alerts.append({
                "level": "caution",
                "text": f"Observation overdue by {int(obs_overdue)}m",
            })

        if len(self.nutrition) >= 3:
            last3 = self.nutrition[-3:]
            if all((n.percent_consumed or 0) < 50 for n in last3):
                alerts.append({
                    "level": "caution",
                    "text": "Oral intake < 50% for 3 consecutive meals",
                })

        mobility_hours = (elapsed_seconds(self.mobility[-1].utc) / 3600.0) \
            if self.mobility else self.hours_since_open
        if mobility_hours >= 24:
            alerts.append({
                "level": "caution",
                "text": f"No mobility logged in {mobility_hours:.1f}h",
            })

        last_stool = next(
            (e for e in reversed(self.fluid_output) if e.route == "stool"), None
        )
        bowel_hours = (elapsed_seconds(last_stool.utc) / 3600.0) if last_stool \
            else self.hours_since_open
        if bowel_hours >= 72:
            alerts.append({
                "level": "caution",
                "text": f"Bowels not opened in {bowel_hours:.1f}h",
            })

        return alerts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "patient_id": self.patient_id,
            "opened_utc": self.opened_utc,
            "closed_utc": self.closed_utc,
            "active": self.active,
            "presenting_problem": self.presenting_problem,
            "elapsed_display": fmt_elapsed(elapsed_seconds(self.opened_utc)),
            "day_number": int(self.hours_since_open // 24) + 1,
            "observation_interval_minutes": self.observation_interval_minutes,
            "fluid_balance": {
                "intake_24h_ml": round(self.intake_24h_ml, 1),
                "output_24h_ml": round(self.output_24h_ml, 1),
                "net_24h_ml": round(self.net_24h_ml, 1),
                "cumulative_net_ml": round(self.cumulative_net_ml, 1),
            },
            # Ascending by minutes-until-due: most overdue (most negative)
            # first, then soonest-due next - the countdown list should lead
            # with whatever needs attention first.
            "care_tasks": [t.to_dict() for t in sorted(
                self.care_tasks,
                key=lambda t: t.minutes_until_due if t.minutes_until_due is not None else float("inf"),
            )],
            "skin_checks": [s.to_dict() for s in self.skin_checks[-5:]],
            "latest_skin_finding": self.skin_checks[-1].worst_finding if self.skin_checks else None,
            "nutrition": [n.to_dict() for n in self.nutrition[-10:]],
            "mobility": [m.to_dict() for m in self.mobility[-10:]],
            "vitals": [v.to_dict() for v in self.vitals[-20:]],
            "latest_news2": self.latest_news2,
            "news2_rising": self.news2_rising,
            "news2_caveat": (
                "NEWS2 is a hospital-validated screening aid, not a diagnosis. "
                "It is known to under-trigger in immunosuppressed patients "
                "whose fever/inflammatory response is pharmacologically "
                "blunted - a low score is not reassurance."
            ),
            "active_alerts": self.active_alerts,
        }


# ===========================================================================
# CARE EPISODE REGISTRY
# ===========================================================================

class CareEpisodeRegistry:
    """
    Holds every care episode. Persisted to disk on every mutation, restored
    on startup - see _persist()/_restore(), which follow the exact pattern
    established for the trauma module's SceneRegistry (raw dataclass state
    via dataclasses.asdict, not a derived summary; atomic write-then-replace
    plus fsync; loud handling of a missing/corrupt/unrecognized file).
    """

    def __init__(self, persist_path: str = "/var/lib/specter/ward.json"):
        self._lock = threading.Lock()
        self.episodes: Dict[str, CareEpisode] = {}
        self.persist_path = persist_path
        self._counter = 0
        self._restore()

    # ---- lifecycle --------------------------------------------------------

    def open_episode(self, patient_id: str, presenting_problem: str = "") -> CareEpisode:
        with self._lock:
            self._counter += 1
            eid = f"W-{self._counter}"
            now = utcnow()
            ep = CareEpisode(
                episode_id=eid,
                patient_id=patient_id,
                opened_utc=now,
                presenting_problem=presenting_problem,
            )
            # Reposition and skin-check are the two tasks the mode's own
            # spec calls routine for essentially every bedbound patient -
            # seeded automatically so the first Care Due countdown is never
            # empty. Everything else is added deliberately via
            # add_care_task() with an explicit interval (see
            # DEFAULT_TASK_INTERVAL_MINUTES's docstring for why).
            for task_type, interval in DEFAULT_TASK_INTERVAL_MINUTES.items():
                ep.care_tasks.append(CareTask(
                    task_id=f"{eid}-T{len(ep.care_tasks) + 1}",
                    task_type=task_type,
                    interval_minutes=interval,
                    created_utc=now,
                ))
            self.episodes[eid] = ep
        logger.info("Episode %s opened for patient %s", eid, patient_id)
        self._persist()
        return ep

    def close_episode(self, episode_id: str) -> Optional[CareEpisode]:
        ep = self.get(episode_id)
        if not ep:
            return None
        with self._lock:
            ep.closed_utc = utcnow()
        logger.info("Episode %s closed", episode_id)
        self._persist()
        return ep

    def get(self, episode_id: str) -> Optional[CareEpisode]:
        with self._lock:
            return self.episodes.get(episode_id)

    def open_episodes(self) -> List[CareEpisode]:
        with self._lock:
            return [e for e in self.episodes.values() if e.active]

    # ---- fluid balance ------------------------------------------------------

    def log_intake(self, episode_id: str, route: str, volume_ml: float,
                    description: str = "") -> Optional[CareEpisode]:
        ep = self.get(episode_id)
        if not ep:
            return None
        with self._lock:
            ep.fluid_intake.append(FluidEntry(
                entry_id=f"{episode_id}-IN{len(ep.fluid_intake) + 1}",
                utc=utcnow(), route=route, volume_ml=float(volume_ml),
                description=description,
            ))
        self._persist()
        return ep

    def log_output(self, episode_id: str, route: str, volume_ml: float,
                    description: str = "") -> Optional[CareEpisode]:
        ep = self.get(episode_id)
        if not ep:
            return None
        with self._lock:
            ep.fluid_output.append(FluidEntry(
                entry_id=f"{episode_id}-OUT{len(ep.fluid_output) + 1}",
                utc=utcnow(), route=route, volume_ml=float(volume_ml),
                description=description,
            ))
        self._persist()
        return ep

    # ---- care tasks -----------------------------------------------------

    def add_care_task(self, episode_id: str, task_type: str,
                       interval_minutes: Optional[int] = None,
                       label: str = "") -> Optional[CareTask]:
        ep = self.get(episode_id)
        if not ep:
            return None
        if interval_minutes is None:
            interval_minutes = DEFAULT_TASK_INTERVAL_MINUTES.get(task_type)
        if interval_minutes is None:
            logger.error(
                "add_care_task(%s, %s): no interval given and none defaulted "
                "for this task type - refusing to guess a clinical interval",
                episode_id, task_type,
            )
            return None
        with self._lock:
            task = CareTask(
                task_id=f"{episode_id}-T{len(ep.care_tasks) + 1}",
                task_type=task_type,
                interval_minutes=int(interval_minutes),
                label=label,
                created_utc=utcnow(),
            )
            ep.care_tasks.append(task)
        self._persist()
        return task

    def complete_care_task(self, episode_id: str, task_id: str) -> Optional[CareEpisode]:
        ep = self.get(episode_id)
        if not ep:
            return None
        with self._lock:
            for t in ep.care_tasks:
                if t.task_id == task_id:
                    t.last_done_utc = utcnow()
        self._persist()
        return ep

    # ---- skin / nutrition / mobility -------------------------------------

    def record_skin_check(self, episode_id: str, sites: Dict[str, str],
                           notes: str = "") -> Optional[CareEpisode]:
        ep = self.get(episode_id)
        if not ep:
            return None
        clean_sites = {k: v for k, v in sites.items()
                        if k in SKIN_SITES and v in SKIN_FINDINGS}
        with self._lock:
            ep.skin_checks.append(SkinCheckEntry(utc=utcnow(), sites=clean_sites, notes=notes))
            for t in ep.care_tasks:
                if t.task_type == CareTaskType.SKIN_CHECK.value:
                    t.last_done_utc = utcnow()
        self._persist()
        return ep

    def record_nutrition(self, episode_id: str, description: str = "",
                          estimated_kcal: Optional[float] = None,
                          estimated_protein_g: Optional[float] = None,
                          percent_consumed: Optional[float] = None) -> Optional[CareEpisode]:
        ep = self.get(episode_id)
        if not ep:
            return None
        with self._lock:
            ep.nutrition.append(NutritionEntry(
                utc=utcnow(), description=description,
                estimated_kcal=estimated_kcal,
                estimated_protein_g=estimated_protein_g,
                percent_consumed=percent_consumed,
            ))
        self._persist()
        return ep

    def record_mobility(self, episode_id: str, level: str,
                         duration_minutes: float = 0.0) -> Optional[CareEpisode]:
        ep = self.get(episode_id)
        if not ep:
            return None
        with self._lock:
            ep.mobility.append(MobilityEntry(
                utc=utcnow(), level=level, duration_minutes=float(duration_minutes),
            ))
        self._persist()
        return ep

    # ---- vitals / NEWS2 ---------------------------------------------------

    def record_vitals(self, episode_id: str, values: Dict[str, Any]) -> Optional[CareEpisode]:
        ep = self.get(episode_id)
        if not ep:
            return None
        news2 = calculate_news2(values)
        with self._lock:
            ep.vitals.append(VitalsEntry(utc=utcnow(), values=values, news2=news2))
            # Escalate observation cadence once NEWS2 >= 5, per
            # SPECTER_CLINICAL_MODES.md 1.1: "hourly if NEWS2 >= 5".
            if news2.get("total", 0) >= 5:
                ep.observation_interval_minutes = ESCALATED_OBSERVATION_INTERVAL_MINUTES
            else:
                ep.observation_interval_minutes = DEFAULT_OBSERVATION_INTERVAL_MINUTES
            for t in ep.care_tasks:
                # "vitals" isn't seeded as a CareTask (see open_episode) since
                # its interval is dynamic, but if the operator added one
                # explicitly, keep it in sync too.
                if t.task_type == "vitals":
                    t.last_done_utc = utcnow()
        self._persist()
        return ep

    # ---- persistence ------------------------------------------------------
    #
    # Same reasoning as SceneRegistry in specter_trauma.py: persist the raw
    # dataclass state (every fluid entry, every skin check, every vitals/
    # NEWS2 reading) via dataclasses.asdict, not a collapsed summary that
    # would silently lose history on restart. Atomic write-then-replace
    # plus fsync so a crash mid-write can't corrupt the file.

    def _persist(self) -> None:
        try:
            raw = {
                "format": 1,
                "counter": self._counter,
                "episodes": {eid: asdict(ep) for eid, ep in self.episodes.items()},
            }
            directory = os.path.dirname(self.persist_path) or "."
            os.makedirs(directory, exist_ok=True)

            tmp_path = f"{self.persist_path}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(raw, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.persist_path)

            try:
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception as exc:
            logger.error("Could not persist ward state: %s", exc)

    def _restore(self) -> None:
        try:
            raw_text = Path(self.persist_path).read_text()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.error("Could not read persisted ward state %s: %s", self.persist_path, exc)
            return

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.error(
                "Persisted ward state %s is corrupt (%s) - starting with NO "
                "episodes. The previous state may be unrecoverable.",
                self.persist_path, exc,
            )
            return

        # json.loads() can hand back ANY JSON value ([], null, a bare
        # number...), not just an object - validate the shape before
        # calling .get()/.items() on anything, or a merely-valid-but-wrong
        # persisted file crashes the service at startup instead of
        # degrading to an empty registry as documented.
        if (
            not isinstance(raw, dict)
            or raw.get("format") != 1
            or not isinstance(raw.get("episodes"), dict)
        ):
            logger.error(
                "Persisted ward state %s is not in the expected format - "
                "starting with NO episodes rather than guessing at its "
                "structure.", self.persist_path,
            )
            return

        episode_fields = {f.name for f in dataclass_fields(CareEpisode)}
        fluid_fields = {f.name for f in dataclass_fields(FluidEntry)}
        task_fields = {f.name for f in dataclass_fields(CareTask)}
        skin_fields = {f.name for f in dataclass_fields(SkinCheckEntry)}
        nutrition_fields = {f.name for f in dataclass_fields(NutritionEntry)}
        mobility_fields = {f.name for f in dataclass_fields(MobilityEntry)}
        vitals_fields = {f.name for f in dataclass_fields(VitalsEntry)}

        restored: Dict[str, CareEpisode] = {}
        for eid, edict in raw.get("episodes", {}).items():
            if not isinstance(edict, dict):
                logger.error(
                    "Skipping unrecoverable episode record %s in %s: "
                    "expected an object, got %s",
                    eid, self.persist_path, type(edict).__name__,
                )
                continue
            try:
                fields_only = {k: v for k, v in edict.items() if k in episode_fields}
                fields_only["fluid_intake"] = [
                    FluidEntry(**{k: v for k, v in e.items() if k in fluid_fields})
                    for e in edict.get("fluid_intake", []) or []
                    if isinstance(e, dict)
                ]
                fields_only["fluid_output"] = [
                    FluidEntry(**{k: v for k, v in e.items() if k in fluid_fields})
                    for e in edict.get("fluid_output", []) or []
                    if isinstance(e, dict)
                ]
                fields_only["care_tasks"] = [
                    CareTask(**{k: v for k, v in t.items() if k in task_fields})
                    for t in edict.get("care_tasks", []) or []
                    if isinstance(t, dict)
                ]
                fields_only["skin_checks"] = [
                    SkinCheckEntry(**{k: v for k, v in s.items() if k in skin_fields})
                    for s in edict.get("skin_checks", []) or []
                    if isinstance(s, dict)
                ]
                fields_only["nutrition"] = [
                    NutritionEntry(**{k: v for k, v in n.items() if k in nutrition_fields})
                    for n in edict.get("nutrition", []) or []
                    if isinstance(n, dict)
                ]
                fields_only["mobility"] = [
                    MobilityEntry(**{k: v for k, v in m.items() if k in mobility_fields})
                    for m in edict.get("mobility", []) or []
                    if isinstance(m, dict)
                ]
                fields_only["vitals"] = [
                    VitalsEntry(**{k: v for k, v in vv.items() if k in vitals_fields})
                    for vv in edict.get("vitals", []) or []
                    if isinstance(vv, dict)
                ]
                restored[eid] = CareEpisode(**fields_only)
            except (TypeError, KeyError, AttributeError) as exc:
                logger.error(
                    "Skipping unrecoverable episode record %s in %s: %s",
                    eid, self.persist_path, exc,
                )

        self.episodes = restored
        try:
            self._counter = int(raw.get("counter", len(restored)))
        except (TypeError, ValueError):
            logger.error(
                "Persisted ward state %s has a non-numeric counter (%r) - "
                "falling back to the restored episode count. New episode "
                "IDs may collide with old ones if this understates the "
                "real counter.",
                self.persist_path, raw.get("counter"),
            )
            self._counter = len(restored)
        logger.info(
            "Restored %d care episode%s from %s",
            len(restored), "" if len(restored) == 1 else "s", self.persist_path,
        )


# ===========================================================================
# MQTT SERVICE
# ===========================================================================

class WardService:
    TOPIC_COMMAND = "shtf/ward/command/#"
    TOPIC_EPISODE = "shtf/ward/episode"
    TOPIC_ALERT = "shtf/ward/alert"

    def __init__(self, mqtt_host: str, mqtt_port: int, persist_path: str):
        self.registry = CareEpisodeRegistry(persist_path=persist_path)
        self.mqtt = _mqtt_client("specter-ward")
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
            self._publish_all()
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
            if cmd == "open_episode":
                r.open_episode(
                    payload["patient_id"],
                    presenting_problem=payload.get("presenting_problem", ""),
                )
            elif cmd == "close_episode":
                r.close_episode(payload["episode_id"])
            elif cmd == "intake":
                r.log_intake(
                    payload["episode_id"], payload["route"], payload["volume_ml"],
                    description=payload.get("description", ""),
                )
            elif cmd == "output":
                r.log_output(
                    payload["episode_id"], payload["route"], payload["volume_ml"],
                    description=payload.get("description", ""),
                )
            elif cmd == "add_care_task":
                r.add_care_task(
                    payload["episode_id"], payload["task_type"],
                    interval_minutes=payload.get("interval_minutes"),
                    label=payload.get("label", ""),
                )
            elif cmd == "complete_task":
                r.complete_care_task(payload["episode_id"], payload["task_id"])
            elif cmd == "skin_check":
                r.record_skin_check(
                    payload["episode_id"], payload.get("sites", {}),
                    notes=payload.get("notes", ""),
                )
            elif cmd == "nutrition":
                r.record_nutrition(
                    payload["episode_id"],
                    description=payload.get("description", ""),
                    estimated_kcal=payload.get("estimated_kcal"),
                    estimated_protein_g=payload.get("estimated_protein_g"),
                    percent_consumed=payload.get("percent_consumed"),
                )
            elif cmd == "mobility":
                r.record_mobility(
                    payload["episode_id"], payload["level"],
                    duration_minutes=payload.get("duration_minutes", 0.0),
                )
            elif cmd == "vitals":
                r.record_vitals(payload["episode_id"], payload.get("values", {}))
            else:
                logger.warning("Unknown command: %s", cmd)
                return

            self._publish_all()

        except KeyError as exc:
            logger.error("Command %s missing field %s", cmd, exc)
        except Exception:
            logger.exception("Command %s failed", cmd)

    # ---- publishing -----------------------------------------------------

    def _publish_all(self) -> None:
        episodes = self.registry.open_episodes()
        summary = {
            "timestamp_utc": utcnow(),
            "episodes": [e.to_dict() for e in episodes],
        }
        self.mqtt.publish(self.TOPIC_EPISODE, json.dumps(summary), qos=1, retain=True)

        alerts = []
        for e in episodes:
            for a in e.active_alerts:
                alerts.append({**a, "episode_id": e.episode_id, "patient_id": e.patient_id})
        if alerts:
            self.mqtt.publish(self.TOPIC_ALERT, json.dumps(alerts), qos=1)

        for e in episodes:
            self.mqtt.publish(
                f"{self.TOPIC_EPISODE}/{e.episode_id}",
                json.dumps(e.to_dict()), qos=1, retain=True,
            )

    # ---- clock ------------------------------------------------------------

    def _clock_loop(self) -> None:
        """
        Care-task due countdowns and fluid/obs alarms are time-derived, so
        state must be republished on a timer even when nothing is being
        entered - same pattern as the trauma module's tourniquet clocks.
        """
        while not self._stop.wait(30):
            if self.registry.episodes:
                self._publish_all()

    def start(self) -> None:
        self.mqtt.will_set(
            self.TOPIC_ALERT,
            json.dumps([{"level": "critical", "text": "Ward service offline"}]),
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

def main() -> None:
    ap = argparse.ArgumentParser(description="SPECTER Ward Module")
    ap.add_argument("--mqtt-host", default="192.168.1.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--persist", default="/var/lib/specter/ward.json")
    args = ap.parse_args()

    logger.info("=" * 60)
    logger.info("SPECTER Ward Module v1.0.0")
    logger.info("MQTT: %s:%s", args.mqtt_host, args.mqtt_port)
    logger.info("Episode persistence: %s", args.persist)
    logger.info("=" * 60)

    svc = WardService(args.mqtt_host, args.mqtt_port, args.persist)
    try:
        svc.start()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        svc.stop()


if __name__ == "__main__":
    main()
