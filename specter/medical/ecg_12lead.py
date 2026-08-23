#!/usr/bin/env python3
"""Canonical, loss-aware 12-lead ECG ingestion for SPECTER.

The Biocare iE300 can export XML and DICOM, but its public documentation does
not define the XML element schema.  This module therefore accepts only an
explicit, inspectable subset of XML structures and fails closed on ambiguity.
The untouched source file is always retained by :func:`archive_record`; model
input is a derived artifact with a complete transformation log.

Nothing in this module diagnoses a patient.  It validates and normalizes
waveform data for separately versioned research models.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
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
    source_format: str = "biocare-xml"
    source_device: str = "Biocare iE300"
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
        if not 100 <= self.sample_rate_hz <= 2000:
            issues.append("sample rate outside supported 100-2000 Hz range")
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
                "source_bytes": "archived_unchanged",
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


class BiocareXMLImporter:
    """Conservative importer for exported Biocare XML.

    A real iE300 XML sample must still be captured during hardware validation.
    Until then, unsupported or ambiguous vendor structures are rejected instead
    of being guessed from tag position.
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
        sample_rate = _parse_float(sample_rate_text, "sample rate")
        if not 100 <= sample_rate <= 2000:
            raise ECGImportError("sample rate must be between 100 and 2000 Hz")
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
