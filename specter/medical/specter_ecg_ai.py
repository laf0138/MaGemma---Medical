#!/usr/bin/env python3
"""SPECTER offline 12-lead ECG research-analysis service.

This service owns the boundary between acquisition files and research models.
It never changes the source export, never treats a model output as a diagnosis,
and never asks MedGemma to infer directly from a textual dump of samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import paho.mqtt.client as mqtt

from medical.ecg_12lead import (
    BiocareXMLImporter,
    ECGImportError,
    archive_record,
    experimental_lead_ii_measurements,
)
from medical.ecg_models import (
    IsolatedModelRunner,
    ModelConfigurationError,
    ModelRegistry,
    agreement_report,
    evaluate_predictions,
    validate_dataset_manifest,
)


logger = logging.getLogger("specter.ecg_ai")
CONFIG_PATH = Path(os.environ.get("SPECTER_CONFIG", "/etc/specter/specter.json"))
MQTT_SERVICE_KEY = "ecg_ai"
MQTT_DEFAULT_USERNAME = "specter-ecg-ai"
MQTT_DEFAULT_PASSWORD = "specter-change-me"
TOPIC_COMMAND = "shtf/medical/ecg/command"
TOPIC_STATUS = "shtf/medical/ecg/status"
TOPIC_ANALYSIS_PREFIX = "shtf/medical/ecg_analysis/"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _mqtt_client(client_id: str = "specter-ecg-ai"):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)


def load_config(path: str | Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("SPECTER configuration is unreadable") from exc
    if not isinstance(data, dict):
        raise RuntimeError("SPECTER configuration must be an object")
    return data


def _mqtt_credentials(config: Mapping[str, Any]) -> tuple[str, str]:
    service = config.get("mqtt", {}).get("services", {}).get(MQTT_SERVICE_KEY, {})
    username, password = service.get("username"), service.get("password")
    if not username or not password or password == MQTT_DEFAULT_PASSWORD:
        raise RuntimeError("dedicated MQTT credentials missing for ecg_ai")
    return str(username), str(password)


class ECGPipeline:
    def __init__(self, config: Mapping[str, Any], *, mqtt_client: Any | None = None) -> None:
        ecg = config.get("ecg_ai", {})
        if not isinstance(ecg, Mapping):
            raise RuntimeError("ecg_ai configuration must be an object")
        self.config = config
        self.inbox = Path(str(ecg.get("inbox_dir", "/mnt/specter/live/ecg/inbox"))).resolve()
        self.archive = Path(str(ecg.get("archive_dir", "/mnt/specter/archive/ecg"))).resolve()
        self.rejected = Path(str(ecg.get("rejected_dir", "/mnt/specter/archive/ecg-rejected"))).resolve()
        self.registry_path = Path(str(ecg.get("model_registry", "/etc/specter/ecg-models.json")))
        self.poll_seconds = max(1, int(ecg.get("poll_seconds", 5)))
        self.max_file_age_seconds = max(60, int(ecg.get("max_file_age_seconds", 86_400)))
        self.registry = ModelRegistry.load(self.registry_path)
        self.runner = IsolatedModelRunner(self.registry, int(ecg.get("model_timeout_seconds", 120)))
        self.importer = BiocareXMLImporter()
        self.mqtt = mqtt_client
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def resolve_inbox_file(self, requested: str | Path) -> Path:
        path = Path(requested)
        if not path.is_absolute():
            path = self.inbox / path
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(self.inbox)
        except ValueError as exc:
            raise ECGImportError("ECG import path must be inside the configured inbox") from exc
        if not resolved.is_file() or resolved.suffix.lower() != ".xml":
            raise ECGImportError("only XML files in the ECG inbox are accepted")
        if resolved.stat().st_size > 64 * 1024 * 1024:
            raise ECGImportError("ECG XML exceeds the 64 MiB import limit")
        return resolved

    def process(self, path: str | Path) -> dict[str, Any]:
        source_path = self.resolve_inbox_file(path)
        raw = source_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            record, imported_raw = self.importer.load(source_path)
            archived = archive_record(record, imported_raw, self.archive)
        except ECGImportError as exc:
            rejected = self._archive_rejected(source_path.name, raw, str(exc))
            result = {
                "schema_version": 1,
                "status": "rejected",
                "source_sha256": digest,
                "source_name": source_path.name,
                "rejected_archive": rejected,
                "error": str(exc),
                "clinical_use": "not analyzed; reacquire or validate the vendor export schema",
            }
            self.publish_status(result)
            return result

        results = [self.runner.run(spec, record) for spec in self.registry.specs.values()]
        deterministic_measurements = experimental_lead_ii_measurements(record)
        acquired = datetime.fromisoformat(record.acquired_at_utc.replace("Z", "+00:00"))
        signed_age_seconds = (datetime.now(timezone.utc) - acquired).total_seconds()
        age_seconds = max(0.0, signed_age_seconds)
        future_skew_seconds = max(0.0, -signed_age_seconds)
        payload = {
            "schema_version": 1,
            "status": "complete" if any(item.status == "complete" for item in results) else "models_unavailable",
            "record_id": record.record_id,
            "patient_id": record.patient_id,
            "study_id": record.study_id,
            "acquired_at_utc": record.acquired_at_utc,
            "processed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": {
                "device": record.source_device,
                "format": record.source_format,
                "sha256": record.source_sha256,
                "archive": archived,
            },
            "freshness": {
                "age_seconds_at_processing": round(age_seconds, 3),
                "future_clock_skew_seconds": round(future_skew_seconds, 3),
                "current_for_live_decisions": (
                    0 <= signed_age_seconds <= self.max_file_age_seconds
                ),
                "max_file_age_seconds": self.max_file_age_seconds,
            },
            "quality": record.quality_report(),
            "acquisition_metadata": record.metadata,
            "machine_output": {
                "measurements": record.machine_measurements,
                "interpretation": record.machine_interpretation,
                "status": "unverified_device_output",
            },
            "deterministic_measurements": deterministic_measurements,
            "models": [item.to_dict() for item in results],
            "agreement": agreement_report(results),
            "raw_data_usage": {
                **record.metadata_document()["raw_data_usage"],
                **{
                    item.model_id: ({
                        "status": "used",
                        "leads": "all_12",
                        "source_sample_count_per_lead": int(record.signals_mv.shape[1]),
                        "model_input_transformation": item.preprocessing,
                    } if item.status == "complete" else {
                        "status": "not_used",
                        "reason": item.error,
                    }) for item in results
                },
                "deterministic_measurements": "canonical lead II analyzed with conservative measurement-withholding rules",
                "medgemma": "structured model, quality, measurement, provenance, and disagreement data only",
            },
            "limitations": [
                "Research outputs are suggestions, not diagnoses or treatment orders.",
                "A clinician must review the original 12-lead tracing and patient context.",
                "A model-unavailable result must never be interpreted as a negative finding.",
                "Biocare iE300 XML schema remains hardware-sample validation pending.",
            ],
        }
        self._write_analysis(archived["root"], payload)
        self.publish_analysis(record.patient_id, payload)
        return payload

    def _archive_rejected(self, source_name: str, raw: bytes, error: str) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        root = self.rejected / digest[:24]
        root.mkdir(parents=True, exist_ok=True)
        source = root / ("source" + (Path(source_name).suffix.lower() or ".xml"))
        if source.exists() and source.read_bytes() != raw:
            raise ECGImportError("rejected archive collision")
        if not source.exists():
            temporary = root / ".source.tmp"
            temporary.write_bytes(raw)
            os.replace(temporary, source)
        report = root / "rejection.json"
        temporary_report = root / ".rejection.tmp"
        temporary_report.write_text(json.dumps({
            "schema_version": 1,
            "source_name": source_name,
            "source_sha256": digest,
            "error": error,
            "rejected_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary_report, report)
        return str(root)

    @staticmethod
    def _write_analysis(root: str, payload: Mapping[str, Any]) -> None:
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        history_dir = Path(root) / "analyses"
        history_dir.mkdir(parents=True, exist_ok=True)
        timestamp = re.sub(r"[^0-9]", "", str(payload.get("processed_at_utc", "")))[:20]
        digest = hashlib.sha256(encoded).hexdigest()
        history = history_dir / f"{timestamp or 'unknown'}-{digest[:12]}.json"
        if history.exists() and history.read_bytes() != encoded:
            raise RuntimeError("ECG analysis archive collision")
        if not history.exists():
            temporary_history = history.with_name("." + history.name + ".tmp")
            temporary_history.write_bytes(encoded)
            os.replace(temporary_history, history)
        history_checksum = history.with_suffix(".sha256")
        if not history_checksum.exists():
            temporary_history_checksum = history_checksum.with_name("." + history_checksum.name + ".tmp")
            temporary_history_checksum.write_text(f"{digest}  {history.name}\n", encoding="ascii")
            os.replace(temporary_history_checksum, history_checksum)

        # A stable latest path is convenient for operators, while every prior
        # run remains immutable in analyses/ for audit and comparison.
        destination = Path(root) / "analysis.json"
        temporary = destination.with_name(".analysis.tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
        checksum = destination.with_name("analysis.sha256")
        temporary_checksum = checksum.with_name(".analysis.sha256.tmp")
        temporary_checksum.write_text(f"{digest}  analysis.json\n", encoding="ascii")
        os.replace(temporary_checksum, checksum)

    def publish_analysis(self, patient_id: str, payload: Mapping[str, Any]) -> None:
        if self.mqtt is None:
            return
        if not _SAFE_ID.fullmatch(patient_id):
            raise ECGImportError("patient ID is unsafe for an MQTT topic")
        self.mqtt.publish(
            TOPIC_ANALYSIS_PREFIX + patient_id,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=True,
        )

    def publish_status(self, payload: Mapping[str, Any]) -> None:
        if self.mqtt is not None:
            self.mqtt.publish(TOPIC_STATUS, json.dumps(payload, separators=(",", ":")), qos=1, retain=True)

    def scan_once(self) -> list[dict[str, Any]]:
        self.inbox.mkdir(parents=True, exist_ok=True)
        processed: list[dict[str, Any]] = []
        for path in sorted(self.inbox.glob("*.xml")):
            marker = path.with_suffix(path.suffix + ".processed")
            if marker.exists():
                continue
            try:
                result = self.process(path)
            except ECGImportError as exc:
                result = {
                    "schema_version": 1,
                    "status": "rejected_unarchived",
                    "source_name": path.name,
                    "error": str(exc),
                    "clinical_use": "not analyzed",
                }
                self.publish_status(result)
            except Exception:
                # A transient I/O/runtime failure must not prevent later files
                # in the inbox from being processed, and is retried next scan.
                logger.exception("ECG import failed transiently for %s", path.name)
                continue
            marker.write_text(
                str(result.get("record_id") or result.get("source_sha256") or result["status"]) + "\n",
                encoding="utf-8",
            )
            processed.append(result)
        return processed

    def serve(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.scan_once()
            except Exception:
                logger.exception("ECG inbox scan failed")


class ECGService:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = config
        if not bool(config.get("ecg_ai", {}).get("service_enabled", False)):
            raise RuntimeError("ECG AI service is disabled; commission exactly one analysis node before enabling")
        self.mqtt = _mqtt_client()
        username, password = _mqtt_credentials(config)
        self.mqtt.username_pw_set(username, password)
        self.pipeline = ECGPipeline(config, mqtt_client=self.mqtt)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if int(reason_code) != 0:
            logger.error("MQTT connection rejected: %s", reason_code)
            return
        client.subscribe(TOPIC_COMMAND, qos=1)
        self.pipeline.publish_status({"schema_version": 1, "status": "ready", **self.pipeline.registry.status()})

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("action") != "import" or not payload.get("path"):
                raise ValueError("command must request import with an inbox path")
            self.pipeline.process(str(payload["path"]))
        except Exception as exc:
            logger.warning("Rejected ECG command: %s", exc)
            self.pipeline.publish_status({"schema_version": 1, "status": "command_rejected", "error": str(exc)})

    def run(self) -> None:
        mqtt_config = self.config.get("mqtt", {})
        self.mqtt.connect(str(mqtt_config.get("broker", "192.168.1.1")), int(mqtt_config.get("port", 1883)), 60)
        self.mqtt.loop_start()
        try:
            self.pipeline.scan_once()
            self.pipeline.serve()
        finally:
            self.mqtt.loop_stop()
            self.mqtt.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SPECTER 12-lead ECG research-analysis pipeline")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    subparsers = parser.add_subparsers(dest="mode", required=True)
    import_parser = subparsers.add_parser("import", help="import and analyze one XML file from the inbox")
    import_parser.add_argument("path")
    subparsers.add_parser("status", help="verify model registry and artifacts")
    validation = subparsers.add_parser("validate-dataset", help="validate an offline evaluation manifest")
    validation.add_argument("manifest")
    evaluation = subparsers.add_parser("evaluate", help="evaluate frozen predictions without training")
    evaluation.add_argument("manifest")
    evaluation.add_argument("predictions")
    evaluation.add_argument("--split", choices=("validation", "test"), default="test")
    register = subparsers.add_parser("register-model", help="verify and atomically register one offline model spec")
    register.add_argument("spec", help="JSON file containing one complete model specification")
    subparsers.add_parser("service", help="run MQTT and inbox service")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.mode == "service":
            service = ECGService(config)
            signal.signal(signal.SIGTERM, lambda *_: service.pipeline.stop())
            signal.signal(signal.SIGINT, lambda *_: service.pipeline.stop())
            service.run()
            return 0
        pipeline = ECGPipeline(config)
        if args.mode == "import":
            result = pipeline.process(args.path)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] in {"complete", "models_unavailable"} else 2
        if args.mode == "status":
            status = pipeline.registry.status()
            print(json.dumps(status, indent=2, sort_keys=True))
            return 0 if all(not item["enabled"] or item["ready"] for item in status["models"]) else 2
        if args.mode == "validate-dataset":
            print(json.dumps(validate_dataset_manifest(args.manifest), indent=2, sort_keys=True))
            return 0
        if args.mode == "evaluate":
            print(json.dumps(evaluate_predictions(args.manifest, args.predictions, split=args.split), indent=2, sort_keys=True))
            return 0
        if args.mode == "register-model":
            from medical.ecg_models import ModelSpec
            candidate = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            registered = ModelRegistry.register_atomic(pipeline.registry_path, ModelSpec.from_dict(candidate))
            print(json.dumps(registered.status(), indent=2, sort_keys=True))
            return 0
    except (OSError, json.JSONDecodeError, RuntimeError, ECGImportError, ModelConfigurationError) as exc:
        logger.error("%s", exc)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
