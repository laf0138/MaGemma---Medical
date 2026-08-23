#!/usr/bin/env python3
"""Canonical, loss-aware 12-lead ECG ingestion for SPECTER.

The Biocare iE300 can export XML and DICOM, but its public documentation does
not define the XML element schema.  This module therefore keeps the vendor
profile disabled and labels its inspectable fallback as strict generic XML.
It also provides separate HL7 aECG and public WFDB adapters. All fail closed on
ambiguity.
The untouched source file is always retained by :func:`archive_record`; model
input is a derived artifact with a complete transformation log.

Nothing in this module diagnoses a patient.  It validates and normalizes
waveform data for separately versioned research models.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

import numpy as np


CANONICAL_LEADS = ("I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6")
_LEAD_ALIASES = {
    "I": "I", "DI": "I", "LEADI": "I",
    "II": "II", "DII": "II", "LEADII": "II",
    "III": "III", "DIII": "III", "LEADIII": "III",
    "AVR": "aVR", "AVL": "aVL", "AVF": "aVF",
    **{f"V{i}": f"V{i}" for i in range(1, 7)},
}
_SAMPLE_RATE_NAMES = {"samplerate", "samplingrate", "samplefrequency", "samplingfrequency", "sampleratehz"}
_UNIT_NAMES = {"unit", "units", "amplitudeunit", "waveformunit"}
_GAIN_NAMES = {"gain", "amplitudegain", "sensitivity"}
_TIME_NAMES = {"acquisitiontime", "acquisitiondatetime", "recordingtime", "recordingdatetime", "datetime"}
_PATIENT_NAMES = {"patientid", "patientidentifier", "recordno", "recordnumber"}
_STUDY_NAMES = {"studyid", "examid", "accessionnumber", "ecgid"}
_DATA_TAGS = {"samples", "sampledata", "digits", "waveformdata", "waveform", "data"}
_LEAD_TAGS = {"lead", "channel", "waveformchannel", "leadwaveform"}
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_XML_ELEMENTS = 50_000
MIN_SOURCE_SAMPLE_RATE_HZ = 100
MAX_SOURCE_SAMPLE_RATE_HZ = 8_000
HL7_V3_NAMESPACE = "urn:hl7-org:v3"


class ECGImportError(ValueError):
    """The source cannot be converted without guessing."""


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].split(":")[-1]


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _local_name(value).lower())


def _lead_name(value: str | None) -> str | None:
    if not value:
        return None
    return _LEAD_ALIASES.get(re.sub(r"[^A-Z0-9]", "", value.upper()))


def _parse_float(value: Any, label: str) -> float:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ECGImportError(f"invalid {label}: {value!r}") from exc
    if not math.isfinite(result):
        raise ECGImportError(f"non-finite {label}")
    return result


def _validate_source_sample_rate(value: Any) -> float:
    sample_rate = _parse_float(value, "sample rate")
    if not MIN_SOURCE_SAMPLE_RATE_HZ <= sample_rate <= MAX_SOURCE_SAMPLE_RATE_HZ:
        raise ECGImportError(
            f"sample rate must be between {MIN_SOURCE_SAMPLE_RATE_HZ} and "
            f"{MAX_SOURCE_SAMPLE_RATE_HZ} Hz"
        )
    return sample_rate


def _parse_timestamp(value: str | None) -> str:
    if not value:
        raise ECGImportError("acquisition timestamp is required")
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ECGImportError("acquisition timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ECGImportError("acquisition timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _first_unique(values: Iterable[str], label: str, *, required: bool = True) -> str | None:
    cleaned = {str(value).strip() for value in values if str(value).strip()}
    if not cleaned:
        if required:
            raise ECGImportError(f"{label} is required")
        return None
    if len(cleaned) != 1:
        raise ECGImportError(f"ambiguous {label}: {sorted(cleaned)!r}")
    return cleaned.pop()


def _numeric_samples(text: str, lead: str) -> np.ndarray:
    tokens = [item for item in re.split(r"[\s,;|]+", text.strip()) if item]
    if not tokens:
        raise ECGImportError(f"lead {lead} contains no samples")
    if len(tokens) > 2_000_000:
        raise ECGImportError(f"lead {lead} has too many samples")
    try:
        samples = np.asarray([float(item) for item in tokens], dtype=np.float64)
    except ValueError as exc:
        raise ECGImportError(f"lead {lead} contains a non-numeric sample") from exc
    if not np.isfinite(samples).all():
        raise ECGImportError(f"lead {lead} contains NaN or infinite samples")
    return samples


def _unit_multiplier_to_mv(unit: str) -> float:
    normalized = unit.strip().replace("µ", "u").replace("μ", "u").lower()
    mapping = {
        "mv": 1.0, "millivolt": 1.0, "millivolts": 1.0,
        "uv": 0.001, "microvolt": 0.001, "microvolts": 0.001,
        "v": 1000.0, "volt": 1000.0, "volts": 1000.0,
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise ECGImportError(f"unsupported amplitude unit: {unit!r}") from exc


@dataclass
class CanonicalECG:
    patient_id: str
    study_id: str
    acquired_at_utc: str
    sample_rate_hz: float
    signals_mv: np.ndarray
    source_name: str
    source_sha256: str
    source_format: str = "strict-generic-xml-v1"
    source_device: str = "unspecified ECG source"
    metadata: dict[str, Any] = field(default_factory=dict)
    machine_measurements: dict[str, Any] = field(default_factory=dict)
    machine_interpretation: list[str] = field(default_factory=list)
    transformations: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        data = np.asarray(self.signals_mv, dtype=np.float64)
        if data.ndim != 2 or data.shape[0] != len(CANONICAL_LEADS):
            raise ECGImportError("signals must have shape (12, samples) in canonical lead order")
        if data.shape[1] < 1 or not np.isfinite(data).all():
            raise ECGImportError("signals must contain finite samples")
        self.signals_mv = data

    @property
    def duration_seconds(self) -> float:
        return self.signals_mv.shape[1] / self.sample_rate_hz

    @property
    def record_id(self) -> str:
        return self.source_sha256[:24]

    def quality_report(self) -> dict[str, Any]:
        issues: list[str] = []
        per_lead: dict[str, dict[str, float | bool]] = {}
        for index, lead in enumerate(CANONICAL_LEADS):
            signal = self.signals_mv[index]
            peak_to_peak = float(np.ptp(signal))
            stddev = float(np.std(signal))
            flatline = peak_to_peak < 0.02 or stddev < 0.005
            extreme = float(np.max(np.abs(signal))) > 20.0
            clipped = bool(signal.size > 10 and np.unique(signal).size / signal.size < 0.02)
            per_lead[lead] = {
                "peak_to_peak_mv": round(peak_to_peak, 6),
                "stddev_mv": round(stddev, 6),
                "flatline": flatline,
                "extreme_amplitude": extreme,
                "possible_clipping": clipped,
            }
            if flatline:
                issues.append(f"{lead}: flatline or inadequate signal")
            if extreme:
                issues.append(f"{lead}: amplitude exceeds 20 mV")
            if clipped:
                issues.append(f"{lead}: possible clipping/quantization")
        if not MIN_SOURCE_SAMPLE_RATE_HZ <= self.sample_rate_hz <= MAX_SOURCE_SAMPLE_RATE_HZ:
            issues.append(
                "sample rate outside supported "
                f"{MIN_SOURCE_SAMPLE_RATE_HZ}-{MAX_SOURCE_SAMPLE_RATE_HZ} Hz source range"
            )
        if self.duration_seconds < 8:
            issues.append("recording shorter than 8 seconds")
        if self.duration_seconds > 300:
            issues.append("recording longer than 300 seconds")
        return {
            "status": "pass" if not issues else "fail",
            "issues": issues,
            "sample_rate_hz": self.sample_rate_hz,
            "sample_count_per_lead": int(self.signals_mv.shape[1]),
            "duration_seconds": round(self.duration_seconds, 6),
            "per_lead": per_lead,
        }

    def metadata_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "record_id": self.record_id,
            "patient_id": self.patient_id,
            "study_id": self.study_id,
            "acquired_at_utc": self.acquired_at_utc,
            "source": {
                "name": self.source_name,
                "sha256": self.source_sha256,
                "format": self.source_format,
                "device": self.source_device,
            },
            "waveform": {
                "lead_order": list(CANONICAL_LEADS),
                "sample_rate_hz": self.sample_rate_hz,
                "sample_count_per_lead": int(self.signals_mv.shape[1]),
                "duration_seconds": self.duration_seconds,
                "unit": "mV",
            },
            "machine_measurements": self.machine_measurements,
            "machine_interpretation": self.machine_interpretation,
            "metadata": self.metadata,
            "transformations": self.transformations,
            "quality": self.quality_report(),
            "raw_data_usage": {
                "source_bytes": (
                    "exact_companion_files_archived_losslessly_in_deterministic_zip"
                    if self.source_format == "wfdb-bundle"
                    else "archived_unchanged"
                ),
                "all_12_waveform_leads": (
                    "preserved in full in waveform.npz; each model receives all leads "
                    "with its registered crop/pad/resample transformation"
                ),
                "machine_measurements": "preserved_and_forwarded_as_separate_machine_output",
                "machine_interpretation": "preserved_and_forwarded_as_unverified_machine_output",
                "unrecognized_xml_metadata": (
                    "bounded searchable metadata is extracted; the complete XML remains archived unchanged"
                ),
            },
        }


class StrictGenericXMLImporter:
    """Conservative fallback for an explicitly inspectable XML subset.

    A real iE300 XML sample must still be captured during hardware validation.
    This adapter is not represented as vendor-compatible. Unsupported or
    ambiguous structures are rejected instead of being guessed from position.
    """

    def load(self, source: str | Path | bytes) -> tuple[CanonicalECG, bytes]:
        if isinstance(source, bytes):
            raw = source
            source_name = "memory.xml"
        else:
            path = Path(source)
            raw = path.read_bytes()
            source_name = path.name
        if not raw or len(raw) > _MAX_SOURCE_BYTES:
            raise ECGImportError("XML source is empty or exceeds 64 MiB")
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ECGImportError("DTD/entity declarations are not accepted")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ECGImportError("invalid XML source") from exc
        elements = list(root.iter())
        if len(elements) > _MAX_XML_ELEMENTS:
            raise ECGImportError("XML contains too many elements")

        values: dict[str, list[str]] = {}
        metadata: dict[str, Any] = {}
        metadata_bytes = 0
        metadata_truncated = False

        def retain_metadata(name: str, value: str) -> None:
            nonlocal metadata_bytes, metadata_truncated
            size = len(name.encode("utf-8")) + len(value.encode("utf-8"))
            if metadata_bytes + size > 512 * 1024:
                metadata_truncated = True
                return
            metadata.setdefault(name, []).append(value)
            metadata_bytes += size

        for element in elements:
            key = _key(element.tag)
            text = (element.text or "").strip()
            if text and len(text) <= 4096 and key not in _DATA_TAGS:
                values.setdefault(key, []).append(text)
                retain_metadata(_local_name(element.tag), text)
            for attribute, attr_value in element.attrib.items():
                attr_key = _key(attribute)
                if str(attr_value).strip():
                    retain_metadata(
                        f"{_local_name(element.tag)}@{_local_name(attribute)}",
                        str(attr_value).strip(),
                    )
                    # A measurement's ``unit="bpm"`` is metadata for that
                    # measurement, not the waveform amplitude unit.
                    if attr_key in _UNIT_NAMES and key not in (
                        _DATA_TAGS | _LEAD_TAGS | {"waveforms", "ecg", "biocareecg"}
                    ):
                        continue
                    values.setdefault(attr_key, []).append(str(attr_value).strip())

        sample_rate_text = _first_unique(
            (value for name in _SAMPLE_RATE_NAMES for value in values.get(name, [])),
            "sample rate",
        )
        sample_rate = _validate_source_sample_rate(sample_rate_text)
        unit = _first_unique(
            (value for name in _UNIT_NAMES for value in values.get(name, [])),
            "amplitude unit",
        )
        multiplier = _unit_multiplier_to_mv(unit or "")
        gain_text = _first_unique(
            (value for name in _GAIN_NAMES for value in values.get(name, [])), "amplitude gain", required=False
        )
        gain = _parse_float(gain_text, "amplitude gain") if gain_text else 1.0
        if gain <= 0:
            raise ECGImportError("amplitude gain must be positive")

        leads: dict[str, np.ndarray] = {}
        for element in elements:
            if _key(element.tag) not in _LEAD_TAGS:
                continue
            name_candidates = []
            for attribute, value in element.attrib.items():
                if _key(attribute) in {"name", "code", "id", "lead", "leadname", "leadid"}:
                    name_candidates.append(str(value))
            for child in list(element):
                if _key(child.tag) in {"name", "code", "id", "lead", "leadname", "leadid"} and child.text:
                    name_candidates.append(child.text)
            canonical_names = {_lead_name(item) for item in name_candidates} - {None}
            if not canonical_names:
                continue
            if len(canonical_names) != 1:
                raise ECGImportError("ambiguous lead identity")
            lead = canonical_names.pop()
            data_candidates: list[str] = []
            if (element.text or "").strip() and not list(element):
                data_candidates.append(element.text or "")
            for child in element.iter():
                if child is not element and _key(child.tag) in _DATA_TAGS and (child.text or "").strip():
                    data_candidates.append(child.text or "")
            distinct = {candidate.strip() for candidate in data_candidates if candidate.strip()}
            if len(distinct) != 1:
                raise ECGImportError(f"lead {lead} must contain exactly one sample sequence")
            if lead in leads:
                raise ECGImportError(f"duplicate lead {lead}")
            leads[lead] = _numeric_samples(distinct.pop(), lead) * multiplier / gain

        missing = [lead for lead in CANONICAL_LEADS if lead not in leads]
        if missing:
            raise ECGImportError(f"missing canonical leads: {', '.join(missing)}")
        lengths = {samples.size for samples in leads.values()}
        if len(lengths) != 1:
            raise ECGImportError("all leads must contain the same sample count")
        signals = np.stack([leads[lead] for lead in CANONICAL_LEADS])

        patient_id = _first_unique(
            (value for name in _PATIENT_NAMES for value in values.get(name, [])), "patient ID"
        )
        study_id = _first_unique(
            (value for name in _STUDY_NAMES for value in values.get(name, [])), "study ID"
        )
        acquired = _parse_timestamp(_first_unique(
            (value for name in _TIME_NAMES for value in values.get(name, [])),
            "acquisition timestamp",
        ))
        measurements = self._collect_prefixed(elements, {"measurement", "measurements", "globalmeasurement"})
        interpretations = self._collect_interpretations(elements)
        compact_metadata = {key: vals[0] if len(vals) == 1 else vals for key, vals in metadata.items()}
        if metadata_truncated:
            compact_metadata["SPECTERMetadataNotice"] = (
                "searchable metadata exceeded 512 KiB; consult the unchanged source XML for the complete record"
            )
        record = CanonicalECG(
            patient_id=patient_id or "",
            study_id=study_id or "",
            acquired_at_utc=acquired,
            sample_rate_hz=sample_rate,
            signals_mv=signals,
            source_name=source_name,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            metadata=compact_metadata,
            machine_measurements=measurements,
            machine_interpretation=interpretations,
            transformations=[{
                "operation": "unit_normalization",
                "source_unit": unit,
                "source_gain": gain,
                "target_unit": "mV",
                "formula": "source_samples * unit_multiplier_to_mV / gain",
            }],
        )
        quality = record.quality_report()
        if quality["status"] != "pass":
            raise ECGImportError("waveform quality gate failed: " + "; ".join(quality["issues"]))
        return record, raw

    @staticmethod
    def _collect_prefixed(elements: Iterable[ET.Element], container_names: set[str]) -> dict[str, Any]:
        collected: dict[str, Any] = {}
        for container in elements:
            if _key(container.tag) not in container_names:
                continue
            for item in container.iter():
                if item is container or list(item) or not (item.text or "").strip():
                    continue
                name = _local_name(item.tag)
                value = (item.text or "").strip()
                entry: dict[str, Any] = {"value": value}
                if item.attrib:
                    entry["attributes"] = {
                        _local_name(key): str(attribute_value)
                        for key, attribute_value in item.attrib.items()
                    }
                collected[name] = entry if name not in collected else [collected[name], entry]
        return collected

    @staticmethod
    def _collect_interpretations(elements: Iterable[ET.Element]) -> list[str]:
        result: list[str] = []
        for element in elements:
            if _key(element.tag) in {"interpretation", "diagnosis", "diagnosticstatement", "statement"}:
                text = (element.text or "").strip()
                if text and text not in result:
                    result.append(text)
        return result


# Compatibility import for earlier SPECTER callers. The implementation is and
# remains a strict generic adapter; the alias does not assert Biocare schema
# compatibility.
BiocareXMLImporter = StrictGenericXMLImporter


def _read_bounded_xml(source: str | Path | bytes) -> tuple[bytes, str, ET.Element, list[ET.Element]]:
    if isinstance(source, bytes):
        raw = source
        source_name = "memory.xml"
    else:
        path = Path(source)
        raw = path.read_bytes()
        source_name = path.name
    if not raw or len(raw) > _MAX_SOURCE_BYTES:
        raise ECGImportError("XML source is empty or exceeds 64 MiB")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ECGImportError("DTD/entity declarations are not accepted")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ECGImportError("invalid XML source") from exc
    elements = list(root.iter())
    if len(elements) > _MAX_XML_ELEMENTS:
        raise ECGImportError("XML contains too many elements")
    return raw, source_name, root, elements


def _parse_hl7_timestamp(value: str) -> str:
    """Parse an HL7 TS while refusing to invent a missing time zone."""
    match = re.fullmatch(
        r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
        r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
        r"(?P<fraction>\.\d+)?(?P<zone>Z|[+-]\d{4})?",
        value.strip(),
    )
    if not match or not match.group("zone"):
        raise ECGImportError("HL7 acquisition timestamp must include a time zone")
    fraction = match.group("fraction") or ""
    microsecond = int((fraction[1:] + "000000")[:6]) if fraction else 0
    zone = match.group("zone")
    zone_text = "+00:00" if zone == "Z" else f"{zone[:3]}:{zone[3:]}"
    try:
        parsed = datetime.fromisoformat(
            f"{match.group('year')}-{match.group('month')}-{match.group('day')}T"
            f"{match.group('hour')}:{match.group('minute')}:{match.group('second')}"
            f".{microsecond:06d}{zone_text}"
        )
    except ValueError as exc:
        raise ECGImportError("invalid HL7 acquisition timestamp") from exc
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _descendants(element: ET.Element, local_name: str) -> list[ET.Element]:
    return [item for item in element.iter() if _local_name(item.tag) == local_name]


class HL7AECGImporter:
    """Strict waveform importer for the published HL7 v3 annotated-ECG shape.

    This is deliberately a separate profile.  Supporting HL7 aECG does not
    imply that a Biocare export uses HL7 aECG.
    """

    _TIME_CODES = {"TIME_ABSOLUTE", "TIME_RELATIVE"}

    def load(self, source: str | Path | bytes) -> tuple[CanonicalECG, bytes]:
        raw, source_name, root, elements = _read_bounded_xml(source)
        if _local_name(root.tag) != "AnnotatedECG" or not root.tag.startswith(
            "{" + HL7_V3_NAMESPACE + "}"
        ):
            raise ECGImportError("source is not an HL7 v3 AnnotatedECG document")

        sample_rates: list[float] = []
        sequences = _descendants(root, "sequence")
        for sequence in sequences:
            codes = [item.get("code", "") for item in list(sequence) if _local_name(item.tag) == "code"]
            if not codes or codes[0] not in self._TIME_CODES:
                continue
            increments = _descendants(sequence, "increment")
            if len(increments) != 1:
                raise ECGImportError("HL7 aECG time sequence must contain one increment")
            increment = _parse_float(increments[0].get("value"), "HL7 time increment")
            unit = str(increments[0].get("unit", "")).strip().replace("µ", "u").lower()
            seconds_per_unit = {"s": 1.0, "ms": 0.001, "us": 0.000001}.get(unit)
            if seconds_per_unit is None or increment <= 0:
                raise ECGImportError("HL7 time increment requires a positive s, ms, or us unit")
            sample_rates.append(1.0 / (increment * seconds_per_unit))
        sample_rate_text = _first_unique(
            (f"{value:.12g}" for value in sample_rates), "HL7 sample rate"
        )
        sample_rate = _validate_source_sample_rate(sample_rate_text)

        leads: dict[str, np.ndarray] = {}
        scale_provenance: dict[str, dict[str, Any]] = {}
        for sequence in sequences:
            codes = [item.get("code", "") for item in list(sequence) if _local_name(item.tag) == "code"]
            if not codes or not codes[0].startswith("MDC_ECG_LEAD_"):
                continue
            lead = _lead_name(codes[0].removeprefix("MDC_ECG_LEAD_"))
            if lead is None:
                continue
            if lead in leads:
                raise ECGImportError(f"duplicate HL7 aECG lead {lead}")
            origins = _descendants(sequence, "origin")
            scales = _descendants(sequence, "scale")
            digits_nodes = _descendants(sequence, "digits")
            if len(origins) != 1 or len(scales) != 1 or len(digits_nodes) != 1:
                raise ECGImportError(
                    f"HL7 aECG lead {lead} requires one origin, scale, and digits sequence"
                )
            origin = _parse_float(origins[0].get("value"), f"{lead} origin")
            scale = _parse_float(scales[0].get("value"), f"{lead} scale")
            if scale == 0:
                raise ECGImportError(f"HL7 aECG lead {lead} scale must be non-zero")
            origin_unit = str(origins[0].get("unit", ""))
            scale_unit = str(scales[0].get("unit", ""))
            origin_mv = origin * _unit_multiplier_to_mv(origin_unit)
            scale_mv = scale * _unit_multiplier_to_mv(scale_unit)
            digits = _numeric_samples(digits_nodes[0].text or "", lead)
            leads[lead] = origin_mv + digits * scale_mv
            scale_provenance[lead] = {
                "origin": origin,
                "origin_unit": origin_unit,
                "scale": scale,
                "scale_unit": scale_unit,
                "formula": "origin_mV + digit * scale_mV",
            }

        missing = [lead for lead in CANONICAL_LEADS if lead not in leads]
        if missing:
            raise ECGImportError(f"missing canonical leads: {', '.join(missing)}")
        lengths = {samples.size for samples in leads.values()}
        if len(lengths) != 1:
            raise ECGImportError("all leads must contain the same sample count")

        series_nodes = _descendants(root, "series")
        timestamps: list[str] = []
        for series in series_nodes:
            for child in list(series):
                if _local_name(child.tag) != "effectiveTime":
                    continue
                lows = [item for item in list(child) if _local_name(item.tag) == "low"]
                timestamps.extend(item.get("value", "") for item in lows if item.get("value"))
        acquired = _parse_hl7_timestamp(_first_unique(timestamps, "HL7 acquisition timestamp") or "")

        patient_ids: list[str] = []
        for subject in _descendants(root, "trialSubject"):
            ids = [item for item in list(subject) if _local_name(item.tag) == "id"]
            patient_ids.extend(item.get("extension") or item.get("root") or "" for item in ids)
        patient_id = _first_unique(patient_ids, "HL7 patient ID")
        root_ids = [item for item in list(root) if _local_name(item.tag) == "id"]
        study_id = _first_unique(
            (item.get("extension") or item.get("root") or "" for item in root_ids),
            "HL7 ECG ID",
        )
        device_names = [
            (item.text or "").strip()
            for item in _descendants(root, "manufacturerModelName")
            if (item.text or "").strip()
        ]
        source_device = _first_unique(device_names, "HL7 device model", required=False) or "unspecified HL7 aECG device"
        signals = np.stack([leads[lead] for lead in CANONICAL_LEADS])
        record = CanonicalECG(
            patient_id=patient_id or "",
            study_id=study_id or "",
            acquired_at_utc=acquired,
            sample_rate_hz=sample_rate,
            signals_mv=signals,
            source_name=source_name,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_format="hl7-aecg-r1",
            source_device=source_device,
            metadata={
                "standard": "HL7 v3 Regulated Studies Annotated ECG Release 1",
                "namespace": HL7_V3_NAMESPACE,
                "schema_location": next(
                    (
                        value
                        for key, value in root.attrib.items()
                        if _local_name(key) == "schemaLocation"
                    ),
                    None,
                ),
            },
            transformations=[{
                "operation": "hl7_slist_physical_value_conversion",
                "per_lead": scale_provenance,
                "target_unit": "mV",
            }],
        )
        quality = record.quality_report()
        if quality["status"] != "pass":
            raise ECGImportError("waveform quality gate failed: " + "; ".join(quality["issues"]))
        return record, raw


@dataclass
class WFDBSignalRecord:
    """Loss-aware physical signals from any public WFDB record.

    A two-channel MIT-BIH record is valid here but cannot be promoted to a
    diagnostic 12-lead :class:`CanonicalECG`.
    """

    record_name: str
    sample_rate_hz: float
    signal_names: tuple[str, ...]
    signals_mv: np.ndarray
    source_bundle: bytes
    source_sha256: str
    metadata: dict[str, Any]

    def as_canonical_12lead(
        self,
        *,
        patient_id: str,
        study_id: str,
        acquired_at_utc: str,
        source_device: str,
    ) -> CanonicalECG:
        mapped: dict[str, np.ndarray] = {}
        for index, name in enumerate(self.signal_names):
            lead = _lead_name(name)
            if lead is None:
                continue
            if lead in mapped:
                raise ECGImportError(f"duplicate WFDB lead {lead}")
            mapped[lead] = self.signals_mv[index]
        missing = [lead for lead in CANONICAL_LEADS if lead not in mapped]
        if missing:
            raise ECGImportError(
                "WFDB record is not a complete diagnostic 12-lead record; missing: "
                + ", ".join(missing)
            )
        record = CanonicalECG(
            patient_id=patient_id,
            study_id=study_id,
            acquired_at_utc=_parse_timestamp(acquired_at_utc),
            sample_rate_hz=self.sample_rate_hz,
            signals_mv=np.stack([mapped[lead] for lead in CANONICAL_LEADS]),
            source_name=f"{self.record_name}.wfdb.zip",
            source_sha256=self.source_sha256,
            source_format="wfdb-bundle",
            source_device=source_device,
            metadata=self.metadata,
            transformations=[{
                "operation": "wfdb_physical_signal_conversion",
                "source_units": self.metadata.get("source_units"),
                "target_unit": "mV",
                "calibration": "applied by pinned wfdb reader from header gain/baseline",
            }],
        )
        quality = record.quality_report()
        if quality["status"] != "pass":
            raise ECGImportError("waveform quality gate failed: " + "; ".join(quality["issues"]))
        return record


class WFDBImporter:
    """Read public PhysioNet/WFDB records without treating them as device XML."""

    MAX_RECORD_FILES = 32

    @staticmethod
    def _bundle(record_base: Path) -> tuple[bytes, list[dict[str, Any]]]:
        files = sorted(path for path in record_base.parent.glob(record_base.name + ".*") if path.is_file())
        if not files or not record_base.with_suffix(".hea").is_file():
            raise ECGImportError("WFDB header and signal files are required")
        if len(files) > WFDBImporter.MAX_RECORD_FILES:
            raise ECGImportError("WFDB record contains too many companion files")
        total = sum(path.stat().st_size for path in files)
        if total > _MAX_SOURCE_BYTES:
            raise ECGImportError("WFDB source bundle exceeds 64 MiB")
        manifest: list[dict[str, Any]] = []
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                data = path.read_bytes()
                manifest.append({
                    "name": path.name,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                })
                info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        return output.getvalue(), manifest

    def load_signal_record(
        self,
        source: str | Path,
        *,
        dataset_id: str,
        dataset_version: str,
        dataset_license: str,
    ) -> WFDBSignalRecord:
        try:
            import wfdb
        except ImportError as exc:
            raise ECGImportError("wfdb dependency is required for public ECG records") from exc
        path = Path(source).resolve()
        record_base = path.with_suffix("") if path.suffix == ".hea" else path
        bundle, files = self._bundle(record_base)
        try:
            loaded = wfdb.rdrecord(str(record_base), physical=True, return_res=64)
        except Exception as exc:
            raise ECGImportError(f"WFDB record cannot be decoded: {exc}") from exc
        signals = np.asarray(loaded.p_signal, dtype=np.float64)
        if signals.ndim != 2 or signals.shape[0] < 1 or signals.shape[1] < 1:
            raise ECGImportError("WFDB record contains no physical signals")
        if not np.isfinite(signals).all():
            raise ECGImportError("WFDB record contains NaN or infinite physical samples")
        names = tuple(str(value) for value in loaded.sig_name)
        units = tuple(str(value) for value in loaded.units)
        if len(names) != signals.shape[1] or len(units) != signals.shape[1]:
            raise ECGImportError("WFDB signal metadata does not match channel count")
        normalized = []
        for index, unit in enumerate(units):
            normalized.append(signals[:, index] * _unit_multiplier_to_mv(unit))
        sample_rate = _validate_source_sample_rate(loaded.fs)
        metadata = {
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "dataset_license": dataset_license,
            "wfdb_record_name": str(getattr(loaded, "record_name", record_base.name)),
            "source_files": files,
            "source_units": list(units),
            "signal_names": list(names),
            "comments": [str(value) for value in getattr(loaded, "comments", [])],
            "adc_gain": [
                None if value is None else float(value)
                for value in (getattr(loaded, "adc_gain", None) or [])
            ],
            "baseline": [
                None if value is None else int(value)
                for value in (getattr(loaded, "baseline", None) or [])
            ],
        }
        return WFDBSignalRecord(
            record_name=record_base.name,
            sample_rate_hz=sample_rate,
            signal_names=names,
            signals_mv=np.stack(normalized),
            source_bundle=bundle,
            source_sha256=hashlib.sha256(bundle).hexdigest(),
            metadata=metadata,
        )

    def load(
        self,
        source: str | Path,
        *,
        patient_id: str,
        study_id: str,
        acquired_at_utc: str,
        source_device: str,
        dataset_id: str,
        dataset_version: str,
        dataset_license: str,
    ) -> tuple[CanonicalECG, bytes]:
        public = self.load_signal_record(
            source,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_license=dataset_license,
        )
        return (
            public.as_canonical_12lead(
                patient_id=patient_id,
                study_id=study_id,
                acquired_at_utc=acquired_at_utc,
                source_device=source_device,
            ),
            public.source_bundle,
        )


def export_specter_synthetic_xml(
    record: CanonicalECG,
    *,
    labels: Iterable[str] = (),
) -> bytes:
    """Create a plumbing fixture that is explicitly *not* vendor XML."""
    root = ET.Element(
        "SPECTERSyntheticECG",
        {"schemaVersion": "1", "vendorCompatibility": "none"},
    )
    ET.SubElement(root, "SyntheticNotice").text = (
        "Generated from a canonical public/test waveform; not a Biocare export"
    )
    ET.SubElement(root, "PatientID").text = record.patient_id
    ET.SubElement(root, "StudyID").text = record.study_id
    ET.SubElement(root, "AcquisitionDateTime").text = record.acquired_at_utc
    ET.SubElement(root, "SampleRate").text = f"{record.sample_rate_hz:.12g}"
    ET.SubElement(root, "AmplitudeUnit").text = "mV"
    ET.SubElement(root, "Gain").text = "1"
    provenance = ET.SubElement(root, "SourceProvenance")
    ET.SubElement(provenance, "SourceFormat").text = record.source_format
    ET.SubElement(provenance, "SourceSHA256").text = record.source_sha256
    ET.SubElement(provenance, "DatasetID").text = str(record.metadata.get("dataset_id", "unspecified"))
    label_node = ET.SubElement(root, "ReferenceLabels")
    for label in labels:
        ET.SubElement(label_node, "Label").text = str(label)
    waveforms = ET.SubElement(root, "Waveforms")
    for index, lead in enumerate(CANONICAL_LEADS):
        lead_node = ET.SubElement(waveforms, "Lead", {"name": lead})
        ET.SubElement(lead_node, "Samples").text = " ".join(
            f"{value:.9g}" for value in record.signals_mv[index]
        )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class XML12LeadImporter:
    """Dispatch strict XML inputs without conflating standards and vendors."""

    def load(self, source: str | Path | bytes) -> tuple[CanonicalECG, bytes]:
        _raw, _source_name, root, _elements = _read_bounded_xml(source)
        if _local_name(root.tag) == "AnnotatedECG":
            return HL7AECGImporter().load(source)
        record, imported = StrictGenericXMLImporter().load(source)
        if _local_name(root.tag) == "SPECTERSyntheticECG":
            if root.get("vendorCompatibility") != "none":
                raise ECGImportError("synthetic XML must deny vendor compatibility")
            record.source_format = "specter-synthetic-xml-v1"
            record.source_device = "SPECTER public-data fixture"
            record.transformations.insert(0, {
                "operation": "synthetic_fixture_import",
                "clinical_use": "software plumbing and regression tests only",
                "vendor_compatibility": "none",
            })
        return record, imported


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def archive_record(record: CanonicalECG, raw_source: bytes, archive_root: str | Path) -> dict[str, str]:
    """Archive source, canonical waveform, and metadata without overwriting."""
    if hashlib.sha256(raw_source).hexdigest() != record.source_sha256:
        raise ECGImportError("source bytes no longer match the imported hash")
    root = Path(archive_root) / record.acquired_at_utc[:10] / record.record_id
    source_suffix = Path(record.source_name).suffix.lower() or ".xml"
    source_path = root / f"source{source_suffix}"
    waveform_path = root / "waveform.npz"
    metadata_path = root / "record.json"
    checksums_path = root / "SHA256SUMS"
    if source_path.exists() and source_path.read_bytes() != raw_source:
        raise ECGImportError("archive record ID collision")
    if metadata_path.exists():
        try:
            existing_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ECGImportError("existing ECG archive metadata is corrupt") from exc
        if existing_metadata.get("source", {}).get("sha256") != record.source_sha256:
            raise ECGImportError("existing ECG archive metadata does not match source")
    if waveform_path.exists():
        try:
            with np.load(waveform_path, allow_pickle=False) as existing_waveform:
                if existing_waveform["signals_mv"].shape != record.signals_mv.shape:
                    raise ECGImportError("existing ECG archive waveform has the wrong shape")
        except (OSError, KeyError, ValueError) as exc:
            if isinstance(exc, ECGImportError):
                raise
            raise ECGImportError("existing ECG archive waveform is corrupt") from exc
    if not source_path.exists():
        _atomic_write(source_path, raw_source)
    root.mkdir(parents=True, exist_ok=True)
    if not waveform_path.exists():
        with tempfile.NamedTemporaryFile(prefix=".waveform.", suffix=".npz", dir=root, delete=False) as handle:
            temp_npz = Path(handle.name)
        try:
            np.savez_compressed(
                temp_npz,
                signals_mv=record.signals_mv.astype(np.float64),
                lead_names=np.asarray(CANONICAL_LEADS),
                sample_rate_hz=np.asarray(record.sample_rate_hz),
            )
            os.replace(temp_npz, waveform_path)
        finally:
            temp_npz.unlink(missing_ok=True)
    if not metadata_path.exists():
        _atomic_write(metadata_path, (json.dumps(record.metadata_document(), indent=2, sort_keys=True) + "\n").encode())
    checksum_lines = []
    for artifact in (source_path, waveform_path, metadata_path):
        checksum_lines.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
    checksum_bytes = ("\n".join(checksum_lines) + "\n").encode()
    if checksums_path.exists():
        if checksums_path.read_bytes() != checksum_bytes:
            raise ECGImportError("existing ECG archive checksum manifest does not match artifacts")
    else:
        _atomic_write(checksums_path, checksum_bytes)
    return {
        "root": str(root),
        "source": str(source_path),
        "waveform": str(waveform_path),
        "metadata": str(metadata_path),
        "checksums": str(checksums_path),
    }


def prepare_model_input(record: CanonicalECG, *, sample_rate_hz: int, sample_count: int) -> tuple[np.ndarray, dict[str, Any]]:
    """Resample and symmetrically zero-pad/crop to an explicit model contract."""
    from scipy.signal import resample_poly

    if sample_rate_hz <= 0 or sample_count <= 0:
        raise ValueError("model sample rate and count must be positive")
    duration = record.duration_seconds
    target_length = max(1, int(round(duration * sample_rate_hz)))
    ratio = Fraction(str(sample_rate_hz)) / Fraction(str(record.sample_rate_hz))
    ratio = ratio.limit_denominator(10_000)
    resampled = resample_poly(record.signals_mv, ratio.numerator, ratio.denominator, axis=1)
    # Floating sample rates can produce a one-sample rounding difference.
    if resampled.shape[1] > target_length:
        resampled = resampled[:, :target_length]
    elif resampled.shape[1] < target_length:
        resampled = np.pad(resampled, ((0, 0), (0, target_length - resampled.shape[1])))
    crop_left = crop_right = pad_left = pad_right = 0
    if target_length > sample_count:
        crop_left = (target_length - sample_count) // 2
        crop_right = target_length - sample_count - crop_left
        resampled = resampled[:, crop_left:target_length - crop_right]
    elif target_length < sample_count:
        pad_left = (sample_count - target_length) // 2
        pad_right = sample_count - target_length - pad_left
        resampled = np.pad(resampled, ((0, 0), (pad_left, pad_right)))
    provenance = {
        "operation": "model_input_preparation",
        "source_sample_rate_hz": record.sample_rate_hz,
        "target_sample_rate_hz": sample_rate_hz,
        "resampling": "polyphase_FIR_with_antialiasing",
        "resample_ratio": [ratio.numerator, ratio.denominator],
        "source_sample_count": int(record.signals_mv.shape[1]),
        "target_sample_count": sample_count,
        "crop_left": crop_left,
        "crop_right": crop_right,
        "zero_pad_left": pad_left,
        "zero_pad_right": pad_right,
        "lead_order": list(CANONICAL_LEADS),
        "unit": "mV",
    }
    return resampled.astype(np.float32), provenance


def experimental_lead_ii_measurements(record: CanonicalECG) -> dict[str, Any]:
    """Reuse the conservative delineator on canonical lead II.

    This is deliberately separate from device measurements and research-model
    predictions.  Its Q-to-S value is not clinical QRS duration.
    """
    from medical.ecg_analysis import analyze_ecg_waveform

    lead_ii_uv = record.signals_mv[CANONICAL_LEADS.index("II")] * 1000.0
    result = analyze_ecg_waveform(lead_ii_uv, record.sample_rate_hz)
    return {
        "source_lead": "II",
        "source_unit": "uV",
        "method": "SPECTER conservative NeuroKit2 single-lead delineation",
        **result,
    }
