#!/usr/bin/env python3
"""Versioned research-model adapters for canonical 12-lead ECG records.

Research runtimes are intentionally isolated from SPECTER's long-running
medical services.  Each registered model is invoked without a shell, receives
an immutable NPZ input plus JSON request, and must return a bounded JSON result
on stdout.  This accommodates DeepECG-SL, the legacy AntonioR92 TensorFlow
baseline, and ECG-XPLAIM without forcing mutually incompatible ML stacks into
the medical-hub environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from medical.ecg_12lead import CANONICAL_LEADS, CanonicalECG, prepare_model_input


MODEL_IDS = ("deepecg-sl", "antonior92", "ecg-xplaim")
ANTONIO_LABELS = ("1dAVb", "RBBB", "LBBB", "SB", "AF", "ST")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESULT_BYTES = 2 * 1024 * 1024


class ModelConfigurationError(ValueError):
    pass


class ModelExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    display_name: str
    version: str
    artifact_path: str
    artifact_sha256: str
    command: tuple[str, ...]
    sample_rate_hz: int
    sample_count: int
    lead_axis: str
    labels: tuple[str, ...]
    thresholds: Mapping[str, float] = field(default_factory=dict)
    enabled: bool = False
    role: str = "secondary"
    license: str = "unverified"
    source_url: str = ""
    runtime_notes: str = ""
    input_scale: float = 1.0
    artifact_format: str = "external"
    source_artifact_sha256: str = ""
    onnx_opset: int | None = None
    input_name: str = ""
    output_name: str = ""
    output_transform: str = "none"
    runtime_source_commit: str = ""
    runtime_source_path: str = ""
    lead_order: tuple[str, ...] = CANONICAL_LEADS

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelSpec":
        command = data.get("command", [])
        labels = data.get("labels", [])
        spec = cls(
            model_id=str(data.get("model_id", "")),
            display_name=str(data.get("display_name", "")),
            version=str(data.get("version", "")),
            artifact_path=str(data.get("artifact_path", "")),
            artifact_sha256=str(data.get("artifact_sha256", "")).lower(),
            command=tuple(str(item) for item in command) if isinstance(command, list) else (),
            sample_rate_hz=int(data.get("sample_rate_hz", 0)),
            sample_count=int(data.get("sample_count", 0)),
            lead_axis=str(data.get("lead_axis", "")),
            labels=tuple(str(item) for item in labels) if isinstance(labels, list) else (),
            thresholds={str(key): float(value) for key, value in dict(data.get("thresholds", {})).items()},
            enabled=bool(data.get("enabled", False)),
            role=str(data.get("role", "secondary")),
            license=str(data.get("license", "unverified")),
            source_url=str(data.get("source_url", "")),
            runtime_notes=str(data.get("runtime_notes", "")),
            input_scale=float(data.get("input_scale", 1.0)),
            artifact_format=str(data.get("artifact_format", "external")),
            source_artifact_sha256=str(data.get("source_artifact_sha256", "")).lower(),
            onnx_opset=(int(data["onnx_opset"]) if data.get("onnx_opset") is not None else None),
            input_name=str(data.get("input_name", "")),
            output_name=str(data.get("output_name", "")),
            output_transform=str(data.get("output_transform", "none")),
            runtime_source_commit=str(data.get("runtime_source_commit", "")).lower(),
            runtime_source_path=str(data.get("runtime_source_path", "")),
            lead_order=tuple(str(item) for item in data.get("lead_order", CANONICAL_LEADS)),
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        if self.model_id not in MODEL_IDS:
            raise ModelConfigurationError(f"unsupported ECG model ID: {self.model_id!r}")
        if not self.display_name or not self.version:
            raise ModelConfigurationError(f"{self.model_id}: display name and version are required")
        if not _SHA256.fullmatch(self.artifact_sha256):
            raise ModelConfigurationError(f"{self.model_id}: exact artifact SHA-256 is required")
        if not self.artifact_path or not Path(self.artifact_path).is_absolute():
            raise ModelConfigurationError(f"{self.model_id}: artifact path must be absolute")
        if not self.command or not all(self.command):
            raise ModelConfigurationError(f"{self.model_id}: isolated runner command is required")
        if not Path(self.command[0]).is_absolute():
            raise ModelConfigurationError(f"{self.model_id}: runner executable must be an absolute path")
        if not 100 <= self.sample_rate_hz <= 2000 or not 1000 <= self.sample_count <= 1_000_000:
            raise ModelConfigurationError(f"{self.model_id}: invalid input dimensions")
        if self.lead_axis not in {"lead_first", "lead_last"}:
            raise ModelConfigurationError(f"{self.model_id}: lead_axis must be lead_first or lead_last")
        if not self.labels or len(set(self.labels)) != len(self.labels):
            raise ModelConfigurationError(f"{self.model_id}: non-empty unique labels are required")
        if len(self.lead_order) != 12 or set(self.lead_order) != set(CANONICAL_LEADS):
            raise ModelConfigurationError(f"{self.model_id}: lead_order must be a permutation of all 12 canonical leads")
        if self.model_id == "antonior92" and tuple(self.labels) != ANTONIO_LABELS:
            raise ModelConfigurationError("antonior92 labels/order must match the published six-output model")
        if not np.isfinite(self.input_scale) or self.input_scale == 0:
            raise ModelConfigurationError(f"{self.model_id}: input_scale must be finite and non-zero")
        for label, threshold in self.thresholds.items():
            if label not in self.labels or not 0 <= threshold <= 1:
                raise ModelConfigurationError(f"{self.model_id}: invalid threshold for {label}")
        if self.artifact_format not in {"external", "torchscript", "keras_weights", "keras", "onnx"}:
            raise ModelConfigurationError(f"{self.model_id}: unsupported artifact format")
        if self.source_artifact_sha256 and not _SHA256.fullmatch(self.source_artifact_sha256):
            raise ModelConfigurationError(f"{self.model_id}: invalid source artifact SHA-256")
        if self.artifact_format == "onnx":
            if self.onnx_opset is None or not 9 <= self.onnx_opset <= 30:
                raise ModelConfigurationError(f"{self.model_id}: ONNX opset must be registered")
            if not self.input_name or not self.output_name or not self.source_artifact_sha256:
                raise ModelConfigurationError(
                    f"{self.model_id}: ONNX input/output names and source-artifact lineage are required"
                )
        if self.output_transform not in {"none", "sigmoid"}:
            raise ModelConfigurationError(f"{self.model_id}: output_transform must be none or sigmoid")
        if self.runtime_source_commit and not re.fullmatch(r"[0-9a-f]{40}", self.runtime_source_commit):
            raise ModelConfigurationError(f"{self.model_id}: runtime source commit must be a full Git SHA")
        if self.runtime_source_commit and (
            not self.runtime_source_path or not Path(self.runtime_source_path).is_absolute()
        ):
            raise ModelConfigurationError(f"{self.model_id}: absolute runtime source path is required with a commit")

    def public_document(self) -> dict[str, Any]:
        result = asdict(self)
        result["command"] = list(self.command)
        result["labels"] = list(self.labels)
        result["lead_order"] = list(self.lead_order)
        return result


@dataclass
class ModelResult:
    model_id: str
    display_name: str
    version: str
    artifact_sha256: str
    status: str
    probabilities: dict[str, float] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    explanation: dict[str, Any] | None = None
    preprocessing: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    thresholds_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModelRegistry:
    """Read-only runtime view of an ExChanGeAI-style local registry."""

    def __init__(self, specs: Sequence[ModelSpec], registry_path: str | Path = "") -> None:
        if len({spec.model_id for spec in specs}) != len(specs):
            raise ModelConfigurationError("duplicate ECG model ID")
        self.specs = {spec.model_id: spec for spec in specs}
        self.registry_path = str(registry_path)

    @classmethod
    def load(cls, path: str | Path) -> "ModelRegistry":
        registry_path = Path(path)
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelConfigurationError(f"unable to read model registry: {registry_path}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("models"), list):
            raise ModelConfigurationError("ECG model registry must use schema_version 1 and a models list")
        return cls([ModelSpec.from_dict(item) for item in data["models"]], registry_path)

    def verify_artifact(self, spec: ModelSpec) -> str:
        path = Path(spec.artifact_path)
        if not path.is_file():
            raise ModelConfigurationError(f"{spec.model_id}: model artifact is missing")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if actual != spec.artifact_sha256:
            raise ModelConfigurationError(f"{spec.model_id}: artifact SHA-256 mismatch")
        return actual

    def status(self) -> dict[str, Any]:
        models: list[dict[str, Any]] = []
        for spec in self.specs.values():
            entry = {
                "model_id": spec.model_id,
                "display_name": spec.display_name,
                "version": spec.version,
                "role": spec.role,
                "enabled": spec.enabled,
                "ready": False,
                "reason": "disabled",
            }
            if spec.enabled:
                try:
                    self.verify_artifact(spec)
                    entry.update(ready=True, reason="verified")
                except ModelConfigurationError as exc:
                    entry["reason"] = str(exc)
            models.append(entry)
        return {"schema_version": 1, "registry_path": self.registry_path, "models": models}

    @classmethod
    def register_atomic(cls, registry_path: str | Path, replacement: ModelSpec) -> "ModelRegistry":
        """Verify an offline artifact and atomically replace its registry entry.

        This is model lifecycle management, not download or conversion.  ONNX
        conversions must name and hash their source artifact in ``ModelSpec``.
        """
        path = Path(registry_path)
        current = cls.load(path)
        current.verify_artifact(replacement)
        specs = dict(current.specs)
        specs[replacement.model_id] = replacement
        document = {
            "schema_version": 1,
            "models": [spec.public_document() for spec in specs.values()],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        return cls.load(path)


class IsolatedModelRunner:
    def __init__(self, registry: ModelRegistry, timeout_seconds: int = 120) -> None:
        self.registry = registry
        self.timeout_seconds = timeout_seconds

    def run(self, spec: ModelSpec, record: CanonicalECG) -> ModelResult:
        if not spec.enabled:
            return self._unavailable(spec, "model disabled")
        try:
            self.registry.verify_artifact(spec)
            waveform, preprocessing = prepare_model_input(
                record, sample_rate_hz=spec.sample_rate_hz, sample_count=spec.sample_count
            )
            indices = [CANONICAL_LEADS.index(lead) for lead in spec.lead_order]
            waveform = waveform[indices]
            preprocessing["target_lead_order"] = list(spec.lead_order)
            waveform *= np.float32(spec.input_scale)
            if spec.lead_axis == "lead_last":
                waveform = waveform.T
            with tempfile.TemporaryDirectory(prefix="specter-ecg-model-") as directory:
                working = Path(directory)
                input_path = working / "input.npz"
                request_path = working / "request.json"
                np.savez_compressed(
                    input_path,
                    signal=waveform[np.newaxis, ...],
                    sample_rate_hz=np.asarray(spec.sample_rate_hz),
                    record_id=np.asarray(record.record_id),
                )
                request = {
                    "schema_version": 1,
                    "model": spec.public_document(),
                    "input": {
                        "path": str(input_path),
                        "array": "signal",
                        "shape": list(waveform[np.newaxis, ...].shape),
                        "dtype": "float32",
                        "unit_before_input_scale": "mV",
                    },
                    "record_id": record.record_id,
                }
                request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
                command = [
                    item.replace("{artifact}", spec.artifact_path)
                    .replace("{input}", str(input_path))
                    .replace("{request}", str(request_path))
                    for item in spec.command
                ]
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    check=False,
                    env={**os.environ, "PYTHONNOUSERSITE": "1"},
                )
            if completed.returncode != 0:
                stderr = completed.stderr.decode("utf-8", "replace")[-1000:]
                raise ModelExecutionError(f"runner exited {completed.returncode}: {stderr}")
            if len(completed.stdout) > _MAX_RESULT_BYTES:
                raise ModelExecutionError("runner result exceeds 2 MiB")
            try:
                payload = json.loads(completed.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ModelExecutionError("runner did not return valid JSON") from exc
            probabilities = self._validate_result(spec, payload)
            findings = [
                label for label, probability in probabilities.items()
                if label in spec.thresholds and probability >= spec.thresholds[label]
            ]
            explanation = payload.get("explanation")
            if explanation is not None and not isinstance(explanation, dict):
                raise ModelExecutionError("explanation must be an object when present")
            return ModelResult(
                model_id=spec.model_id,
                display_name=spec.display_name,
                version=spec.version,
                artifact_sha256=spec.artifact_sha256,
                status="complete",
                probabilities=probabilities,
                findings=findings,
                explanation=explanation,
                preprocessing=preprocessing,
                thresholds_applied=bool(spec.thresholds),
            )
        except (ModelConfigurationError, ModelExecutionError, OSError, subprocess.SubprocessError) as exc:
            return self._unavailable(spec, str(exc))

    @staticmethod
    def _validate_result(spec: ModelSpec, payload: Any) -> dict[str, float]:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ModelExecutionError("runner result must be a schema_version 1 object")
        raw = payload.get("probabilities")
        if not isinstance(raw, dict) or set(raw) != set(spec.labels):
            raise ModelExecutionError("runner must return one probability for every registered label")
        result: dict[str, float] = {}
        for label in spec.labels:
            try:
                probability = float(raw[label])
            except (TypeError, ValueError) as exc:
                raise ModelExecutionError(f"invalid probability for {label}") from exc
            if not np.isfinite(probability) or not 0 <= probability <= 1:
                raise ModelExecutionError(f"probability for {label} is outside [0, 1]")
            result[label] = probability
        return result

    @staticmethod
    def _unavailable(spec: ModelSpec, error: str) -> ModelResult:
        return ModelResult(
            model_id=spec.model_id,
            display_name=spec.display_name,
            version=spec.version,
            artifact_sha256=spec.artifact_sha256,
            status="unavailable",
            error=error[:2000],
        )


def agreement_report(results: Sequence[ModelResult]) -> dict[str, Any]:
    """Report overlapping labels without pretending unlike taxonomies agree."""
    aliases = {
        "1st degree AV block": "first_degree_av_block",
        "1dAVb": "first_degree_av_block",
        "Right bundle branch block": "right_bundle_branch_block",
        "RBBB": "right_bundle_branch_block",
        "Left bundle branch block": "left_bundle_branch_block",
        "LBBB": "left_bundle_branch_block",
        "Bradycardia": "bradycardia",
        "SB": "bradycardia",
        "Afib": "atrial_fibrillation",
        "AF": "atrial_fibrillation",
    }
    votes: dict[str, list[tuple[str, str, bool | None, float]]] = {}
    for result in results:
        if result.status != "complete":
            continue
        findings = set(result.findings)
        for label, probability in result.probabilities.items():
            canonical = aliases.get(label, label)
            positive = (label in findings) if result.thresholds_applied else None
            votes.setdefault(canonical, []).append((result.model_id, label, positive, probability))
    compared = {label: entries for label, entries in votes.items() if len(entries) > 1}
    disagreements = [
        label for label, entries in compared.items()
        if len([positive for _, _, positive, _ in entries if positive is not None]) > 1
        and len({positive for _, _, positive, _ in entries if positive is not None}) > 1
    ]
    return {
        "comparable_labels": {
            label: [{"model_id": model, "registered_label": original_label,
                     "positive": positive, "probability": probability}
                    for model, original_label, positive, probability in entries]
            for label, entries in compared.items()
        },
        "disagreements": disagreements,
        "note": (
            "Only identical labels and the explicit published DeepECG/Antonio alias map are compared. "
            "positive is null when a model has no registered threshold; null is never treated as negative."
        ),
    }


def validate_dataset_manifest(path: str | Path) -> dict[str, Any]:
    """Validate PTB-XL/external evaluation manifests and patient split isolation."""
    manifest_path = Path(path)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelConfigurationError("evaluation manifest is unreadable") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ModelConfigurationError("evaluation manifest must use schema_version 1")
    datasets = data.get("datasets")
    records = data.get("records")
    if not isinstance(datasets, list) or not datasets or not isinstance(records, list) or not records:
        raise ModelConfigurationError("evaluation manifest requires datasets and records")
    dataset_ids: set[str] = set()
    dataset_roots: dict[str, Path] = {}
    for dataset in datasets:
        if not isinstance(dataset, dict):
            raise ModelConfigurationError("dataset entries must be objects")
        dataset_id = str(dataset.get("dataset_id", ""))
        if not dataset_id or dataset_id in dataset_ids:
            raise ModelConfigurationError("dataset IDs must be non-empty and unique")
        dataset_ids.add(dataset_id)
        if not dataset.get("version") or not dataset.get("license") or not dataset.get("source_url"):
            raise ModelConfigurationError(f"{dataset_id}: version, license, and source URL are required")
        root = Path(str(dataset.get("root_dir", "")))
        if not root.is_absolute() or not root.is_dir():
            raise ModelConfigurationError(f"{dataset_id}: existing absolute root_dir is required")
        dataset_roots[dataset_id] = root.resolve()
    patient_splits: dict[tuple[str, str], set[str]] = {}
    record_ids: set[str] = set()
    counts = {"train": 0, "validation": 0, "test": 0}
    for record in records:
        if not isinstance(record, dict):
            raise ModelConfigurationError("record entries must be objects")
        record_id = str(record.get("record_id", ""))
        patient_id = str(record.get("patient_id", ""))
        dataset_id = str(record.get("dataset_id", ""))
        split = str(record.get("split", ""))
        digest = str(record.get("waveform_sha256", "")).lower()
        relative_waveform = Path(str(record.get("waveform_path", "")))
        true_labels = record.get("labels", [])
        if not record_id or record_id in record_ids:
            raise ModelConfigurationError("record IDs must be non-empty and unique")
        if not patient_id or dataset_id not in dataset_ids or split not in counts or not _SHA256.fullmatch(digest):
            raise ModelConfigurationError(f"invalid evaluation record: {record_id}")
        if not isinstance(true_labels, list) or any(not isinstance(label, str) or not label for label in true_labels):
            raise ModelConfigurationError(f"{record_id}: labels must be a list of non-empty strings")
        if relative_waveform.is_absolute() or not str(relative_waveform):
            raise ModelConfigurationError(f"{record_id}: waveform_path must be relative to dataset root")
        waveform = (dataset_roots[dataset_id] / relative_waveform).resolve()
        try:
            waveform.relative_to(dataset_roots[dataset_id])
        except ValueError as exc:
            raise ModelConfigurationError(f"{record_id}: waveform path escapes dataset root") from exc
        if not waveform.is_file():
            raise ModelConfigurationError(f"{record_id}: waveform file is missing")
        actual_digest = hashlib.sha256()
        with waveform.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                actual_digest.update(block)
        if actual_digest.hexdigest() != digest:
            raise ModelConfigurationError(f"{record_id}: waveform SHA-256 mismatch")
        record_ids.add(record_id)
        counts[split] += 1
        patient_splits.setdefault((dataset_id, patient_id), set()).add(split)
    leaks = [f"{dataset}/{patient}" for (dataset, patient), splits in patient_splits.items() if len(splits) > 1]
    if leaks:
        raise ModelConfigurationError("patient leakage across evaluation splits: " + ", ".join(sorted(leaks)))
    return {
        "status": "valid",
        "dataset_count": len(dataset_ids),
        "record_count": len(record_ids),
        "split_counts": counts,
        "patient_split_leakage": False,
    }


def _binary_auc(truth: np.ndarray, scores: np.ndarray) -> float | None:
    positives = int(truth.sum())
    negatives = int(truth.size - positives)
    if not positives or not negatives:
        return None
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(scores.size, dtype=float)
    start = 0
    while start < scores.size:
        end = start + 1
        while end < scores.size and scores[order[end]] == scores[order[start]]:
            end += 1
        ranks[order[start:end]] = (start + 1 + end) / 2.0
        start = end
    return float((ranks[truth.astype(bool)].sum() - positives * (positives + 1) / 2) / (positives * negatives))


def evaluate_predictions(
    manifest_path: str | Path,
    predictions_path: str | Path,
    *,
    split: str = "test",
) -> dict[str, Any]:
    """Evaluate one frozen prediction file without training or threshold tuning."""
    if split not in {"validation", "test"}:
        raise ModelConfigurationError("only frozen validation or test splits may be evaluated")
    validate_dataset_manifest(manifest_path)
    manifest_bytes = Path(manifest_path).read_bytes()
    manifest = json.loads(manifest_bytes)
    selected = {record["record_id"]: record for record in manifest["records"] if record["split"] == split}
    if not selected:
        raise ModelConfigurationError(f"dataset manifest has no {split} records")
    prediction_bytes = Path(predictions_path).read_bytes()
    try:
        payload = json.loads(prediction_bytes)
    except json.JSONDecodeError as exc:
        raise ModelConfigurationError("prediction file is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ModelConfigurationError("prediction file must use schema_version 1")
    if (
        not payload.get("model_id") or not payload.get("model_version")
        or not _SHA256.fullmatch(str(payload.get("artifact_sha256", "")).lower())
    ):
        raise ModelConfigurationError("prediction model ID, version, and artifact SHA-256 are required")
    labels = payload.get("labels")
    thresholds = payload.get("thresholds")
    rows = payload.get("predictions")
    if (
        not isinstance(labels, list) or not labels or len(set(labels)) != len(labels)
        or not isinstance(thresholds, dict) or set(thresholds) != set(labels)
        or not isinstance(rows, list)
    ):
        raise ModelConfigurationError("prediction labels, thresholds, or rows are invalid")
    try:
        threshold_values = {label: float(thresholds[label]) for label in labels}
    except (TypeError, ValueError) as exc:
        raise ModelConfigurationError("prediction thresholds must be numeric") from exc
    if any(not 0 <= value <= 1 for value in threshold_values.values()):
        raise ModelConfigurationError("prediction thresholds must be within [0, 1]")
    by_id: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("record_id") or not isinstance(row.get("probabilities"), dict):
            raise ModelConfigurationError("invalid prediction row")
        record_id = str(row["record_id"])
        if record_id in by_id or set(row["probabilities"]) != set(labels):
            raise ModelConfigurationError(f"duplicate/incomplete prediction: {record_id}")
        try:
            probabilities = {label: float(row["probabilities"][label]) for label in labels}
        except (TypeError, ValueError) as exc:
            raise ModelConfigurationError(f"non-numeric probability: {record_id}") from exc
        if any(not np.isfinite(value) or not 0 <= value <= 1 for value in probabilities.values()):
            raise ModelConfigurationError(f"invalid probability: {record_id}")
        by_id[record_id] = probabilities
    if set(by_id) != set(selected):
        missing = sorted(set(selected) - set(by_id))
        extra = sorted(set(by_id) - set(selected))
        raise ModelConfigurationError(f"prediction records do not exactly match {split} split; missing={missing}, extra={extra}")
    ordered_ids = sorted(selected)
    per_label: dict[str, Any] = {}
    for label in labels:
        truth = np.asarray([label in selected[record_id].get("labels", []) for record_id in ordered_ids], dtype=int)
        scores = np.asarray([by_id[record_id][label] for record_id in ordered_ids], dtype=float)
        predicted = scores >= threshold_values[label]
        tp = int(np.sum((truth == 1) & predicted))
        tn = int(np.sum((truth == 0) & ~predicted))
        fp = int(np.sum((truth == 0) & predicted))
        fn = int(np.sum((truth == 1) & ~predicted))
        per_label[label] = {
            "threshold": threshold_values[label],
            "support_positive": int(truth.sum()),
            "support_negative": int(truth.size - truth.sum()),
            "tp": tp, "tn": tn, "fp": fp, "fn": fn,
            "sensitivity": tp / (tp + fn) if tp + fn else None,
            "specificity": tn / (tn + fp) if tn + fp else None,
            "precision": tp / (tp + fp) if tp + fp else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "brier_score": float(np.mean((scores - truth) ** 2)),
            "auroc": _binary_auc(truth, scores),
        }
    return {
        "schema_version": 1,
        "status": "complete",
        "split": split,
        "record_count": len(ordered_ids),
        "model_id": payload.get("model_id"),
        "model_version": payload.get("model_version"),
        "artifact_sha256": payload.get("artifact_sha256"),
        "dataset_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "predictions_sha256": hashlib.sha256(prediction_bytes).hexdigest(),
        "per_label": per_label,
        "note": "Metrics are descriptive research evaluation only; thresholds were supplied, not tuned by this command.",
    }
