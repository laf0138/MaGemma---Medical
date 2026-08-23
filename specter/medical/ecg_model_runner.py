#!/usr/bin/env python3
"""Isolated runtime entry point for SPECTER's registered ECG models.

Run this script with the Python environment pinned for the selected model.  It
prints exactly one JSON result on stdout; diagnostic text belongs on stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np


def _load_request(path: str | Path) -> tuple[dict[str, Any], np.ndarray]:
    request = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise ValueError("request must use schema_version 1")
    model = request.get("model", {})
    artifact = Path(str(model.get("artifact_path", "")))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if digest != model.get("artifact_sha256"):
        raise ValueError("artifact SHA-256 mismatch inside isolated runner")
    input_info = request.get("input", {})
    loaded = np.load(str(input_info.get("path", "")), allow_pickle=False)
    signal = np.asarray(loaded[str(input_info.get("array", "signal"))], dtype=np.float32)
    if list(signal.shape) != input_info.get("shape") or not np.isfinite(signal).all():
        raise ValueError("input waveform shape/content does not match request")
    return request, signal


def _runtime_repository(model_id: str, model: dict[str, Any]) -> Path:
    repository = Path(str(model.get("runtime_source_path", "")))
    if not repository.is_dir():
        raise RuntimeError(f"{model_id} runtime_source_path must point to the pinned upstream source")
    expected = model.get("runtime_source_commit")
    if expected:
        completed = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if completed.returncode != 0 or completed.stdout.strip().lower() != expected:
            raise RuntimeError(f"{model_id} runtime source commit mismatch")
        dirty = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if dirty.returncode != 0 or dirty.stdout.strip():
            raise RuntimeError(f"{model_id} runtime source is not an exact clean checkout")
    return repository


def _deepecg(artifact: str, signal: np.ndarray, model_spec: dict[str, Any]) -> np.ndarray:
    import torch
    import pandas as pd

    repository = _runtime_repository("deepecg-sl", model_spec)
    sys.path.insert(0, str(repository))
    try:
        from utils.constants import PTBXL_POWER_RATIO
        from utils.ecg_signal_processor import ECGSignalProcessor
    finally:
        sys.path.pop(0)

    # SPECTER supplies lead-first.  The pinned upstream preprocessing code
    # operates on rows of (samples, 12), performs its PTB-XL power scaling and
    # noise cleaning, and the model wrapper then applies the MHI factor.
    lead_last = np.asarray(signal[0].T, dtype=np.float32)
    processor = ECGSignalProcessor(fs=int(model_spec["sample_rate_hz"]))
    frame = pd.DataFrame({"ecg_path": ["specter-input"], "ecg_signal": [lead_last]})
    scaled = processor.scale_ecg_signals(frame, power_ratio=PTBXL_POWER_RATIO)
    cleaned = processor.clean_and_process_ecg_leads(scaled, max_workers=1)
    model_input = np.asarray(cleaned.iloc[0]["ecg_signal"], dtype=np.float32).T[np.newaxis, ...]
    model_input *= np.float32(1 / 0.0048)

    model = torch.jit.load(artifact, map_location="cpu")
    model.eval()
    with torch.no_grad():
        logits = model(torch.from_numpy(model_input))
        output = torch.sigmoid(logits).detach().cpu().numpy()
    return output


def _antonio(artifact: str, signal: np.ndarray, model_spec: dict[str, Any]) -> np.ndarray:
    repository = _runtime_repository("antonior92", model_spec)
    source = repository / "model.py"
    if not source.is_file():
        raise RuntimeError("SPECTER_ANTONIO_REPO must point to the pinned upstream source")
    module_spec = importlib.util.spec_from_file_location("specter_antonio_model", source)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("unable to load AntonioR92 model definition")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    model = module.get_model(6)
    model.load_weights(artifact)
    return np.asarray(model.predict(signal, verbose=0))


def _xplaim(
    artifact: str, signal: np.ndarray, model_spec: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any]]:
    import tensorflow as tf

    if model_spec.get("runtime_source_commit"):
        _runtime_repository("ecg-xplaim", model_spec)
    model = tf.keras.models.load_model(artifact, compile=False)
    tensor = tf.convert_to_tensor(signal)
    with tf.GradientTape() as tape:
        tape.watch(tensor)
        output = model(tensor, training=False)
        target = tf.reduce_max(output[0])
    gradient = tape.gradient(target, tensor)
    if gradient is None:
        explanation = {"status": "withheld", "reason": "model gradient unavailable"}
    else:
        saliency = np.abs(gradient.numpy()[0])
        # ECG-XPLAIM is lead-last by contract.  Report bounded summaries and
        # leave the full explainability artifact to its isolated environment.
        lead_scores = saliency.mean(axis=0)
        order = np.argsort(lead_scores)[::-1][:3]
        lead_names = ["I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"]
        explanation = {
            "status": "available",
            "method": "SPECTER input-gradient summary (not the ECG-XPLAIM publication method)",
            "top_leads": [{"lead": lead_names[int(index)], "score": float(lead_scores[index])} for index in order],
        }
    return np.asarray(output), explanation


def _onnx(artifact: str, signal: np.ndarray, model: dict[str, Any]) -> np.ndarray:
    import onnxruntime as ort

    session = ort.InferenceSession(artifact, providers=["CPUExecutionProvider"])
    registered_input = model["input_name"]
    registered_output = model["output_name"]
    actual_inputs = {item.name for item in session.get_inputs()}
    actual_outputs = {item.name for item in session.get_outputs()}
    if registered_input not in actual_inputs or registered_output not in actual_outputs:
        raise ValueError("ONNX registered input/output name does not match artifact")
    return np.asarray(session.run([registered_output], {registered_input: signal})[0])


def run(request_path: str | Path) -> dict[str, Any]:
    request, signal = _load_request(request_path)
    model = request["model"]
    model_id = model["model_id"]
    artifact = model["artifact_path"]
    explanation = None
    if model.get("artifact_format") == "onnx":
        output = _onnx(artifact, signal, model)
    elif model_id == "deepecg-sl":
        output = _deepecg(artifact, signal, model)
    elif model_id == "antonior92":
        output = _antonio(artifact, signal, model)
    elif model_id == "ecg-xplaim":
        output, explanation = _xplaim(artifact, signal, model)
    else:
        raise ValueError(f"unsupported model ID: {model_id}")
    output = np.asarray(output, dtype=np.float64).reshape(-1)
    if model.get("output_transform") == "sigmoid":
        output = 1.0 / (1.0 + np.exp(-np.clip(output, -80, 80)))
    labels = model["labels"]
    if output.size != len(labels) or not np.isfinite(output).all() or np.any(output < 0) or np.any(output > 1):
        raise ValueError("model output does not match registered probability contract")
    result = {
        "schema_version": 1,
        "probabilities": {label: float(value) for label, value in zip(labels, output)},
    }
    if explanation is not None:
        result["explanation"] = explanation
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args.request), separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"ECG model runner failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
