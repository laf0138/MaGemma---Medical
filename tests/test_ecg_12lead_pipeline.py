import hashlib
import json
import math
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from medical.ecg_12lead import (
    CANONICAL_LEADS,
    BiocareXMLImporter,
    CanonicalECG,
    ECGImportError,
    archive_record,
    prepare_model_input,
)
from medical.ecg_models import (
    ANTONIO_LABELS,
    IsolatedModelRunner,
    ModelConfigurationError,
    ModelRegistry,
    ModelSpec,
    agreement_report,
    evaluate_predictions,
    validate_dataset_manifest,
)
from medical.specter_ecg_ai import ECGPipeline
import medical.specter_ecg_ai as ecg_service_mod
import medical.ecg_model_runner as model_runner_mod
from medical.specter_medical_ai import ECGAnalysisCache, PromptBuilder, VitalsCache


def waveform(sample_rate=500, seconds=10, amplitude=1.0):
    axis = np.arange(sample_rate * seconds) / sample_rate
    return amplitude * (np.sin(2 * math.pi * 1.13 * axis) + 0.08 * np.sin(2 * math.pi * 17 * axis))


def xml_bytes(*, sample_rate=500, unit="uV", gain=1, seconds=10, omit=None, samples=None):
    signal = waveform(sample_rate, seconds) * (1000 if unit == "uV" else 1)
    if samples is not None:
        signal = np.asarray(samples)
    leads = []
    for index, lead in enumerate(CANONICAL_LEADS):
        if lead == omit:
            continue
        values = " ".join(f"{value + index * 0.01:.7f}" for value in signal)
        leads.append(f'<Lead name="{lead}"><Samples>{values}</Samples></Lead>')
    return (f"""<?xml version="1.0"?>
<BiocareECG>
  <PatientID>patient-1</PatientID><StudyID>study-1</StudyID>
  <AcquisitionDateTime>2026-08-22T10:30:00-06:00</AcquisitionDateTime>
  <SampleRate>{sample_rate}</SampleRate><AmplitudeUnit>{unit}</AmplitudeUnit><Gain>{gain}</Gain>
  <Measurements><HeartRate unit="bpm">61</HeartRate><QRS unit="ms">94</QRS></Measurements>
  <Interpretation>Sinus rhythm</Interpretation>
  <Waveforms>{''.join(leads)}</Waveforms>
</BiocareECG>""").encode()


def record():
    raw = xml_bytes()
    return BiocareXMLImporter().load(raw)[0]


def test_biocare_import_preserves_all_leads_metadata_and_machine_output():
    parsed, raw = BiocareXMLImporter().load(xml_bytes())
    assert raw == xml_bytes()
    assert parsed.signals_mv.shape == (12, 5000)
    assert parsed.patient_id == "patient-1"
    assert parsed.acquired_at_utc == "2026-08-22T16:30:00Z"
    assert parsed.machine_measurements == {
        "HeartRate": {"value": "61", "attributes": {"unit": "bpm"}},
        "QRS": {"value": "94", "attributes": {"unit": "ms"}},
    }
    assert parsed.machine_interpretation == ["Sinus rhythm"]
    assert parsed.quality_report()["status"] == "pass"
    assert parsed.metadata_document()["raw_data_usage"]["source_bytes"] == "archived_unchanged"
    np.testing.assert_allclose(parsed.signals_mv[0, :10], waveform()[:10], atol=1e-6)


@pytest.mark.parametrize(
    "raw,match",
    [
        (b"", "empty"),
        (b"<broken", "invalid XML"),
        (b'<!DOCTYPE x [<!ENTITY y "x">]><x/>', "DTD/entity"),
        (xml_bytes(omit="V6"), "missing canonical leads"),
        (xml_bytes(sample_rate=50), "sample rate"),
        (xml_bytes(unit="counts"), "unsupported amplitude unit"),
        (xml_bytes(seconds=5), "shorter than 8 seconds"),
        (xml_bytes(samples=np.zeros(5000)), "quality gate failed"),
    ],
    ids=["empty", "malformed", "entity", "missing-lead", "sample-rate", "unit", "short", "flatline"],
)
def test_biocare_import_fails_closed(raw, match):
    with pytest.raises(ECGImportError, match=match):
        BiocareXMLImporter().load(raw)


def test_biocare_import_rejects_nonfinite_and_ambiguous_fields():
    raw = xml_bytes().replace(b"0.0000000", b"nan", 1)
    with pytest.raises(ECGImportError, match="NaN"):
        BiocareXMLImporter().load(raw)
    raw = xml_bytes().replace(b"<SampleRate>500</SampleRate>", b"<SampleRate>500</SampleRate><SamplingRate>250</SamplingRate>")
    with pytest.raises(ECGImportError, match="ambiguous sample rate"):
        BiocareXMLImporter().load(raw)


def test_archive_retains_exact_source_and_canonical_waveform(tmp_path):
    raw = xml_bytes()
    parsed, _ = BiocareXMLImporter().load(raw)
    paths = archive_record(parsed, raw, tmp_path)
    assert Path(paths["source"]).read_bytes() == raw
    assert hashlib.sha256(Path(paths["source"]).read_bytes()).hexdigest() == parsed.source_sha256
    stored = np.load(paths["waveform"])
    assert stored["signals_mv"].shape == (12, 5000)
    metadata = json.loads(Path(paths["metadata"]).read_text())
    assert metadata["source"]["sha256"] == parsed.source_sha256
    sums = Path(paths["checksums"]).read_text()
    assert "source.xml" in sums and "waveform.npz" in sums and "record.json" in sums
    assert archive_record(parsed, raw, tmp_path) == paths


def test_prepare_model_input_records_resampling_padding_crop_and_axes():
    parsed = record()
    antonio, provenance = prepare_model_input(parsed, sample_rate_hz=400, sample_count=4096)
    assert antonio.shape == (12, 4096)
    assert provenance["zero_pad_left"] == 48
    assert provenance["zero_pad_right"] == 48
    deep, provenance = prepare_model_input(parsed, sample_rate_hz=250, sample_count=2000)
    assert deep.shape == (12, 2000)
    assert provenance["crop_left"] == 250
    assert provenance["crop_right"] == 250


def artifact(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "model.bin"
    path.write_bytes(b"exact test model")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def spec(tmp_path, model_id="antonior92", *, command=None, labels=None, enabled=True, digest=None):
    path, actual = artifact(tmp_path)
    if labels is None:
        labels = list(ANTONIO_LABELS) if model_id == "antonior92" else ["AF"]
    return ModelSpec.from_dict({
        "model_id": model_id,
        "display_name": model_id,
        "version": "test-1",
        "artifact_path": str(path.resolve()),
        "artifact_sha256": digest or actual,
        "command": command or [sys.executable, "runner.py", "{request}"],
        "sample_rate_hz": 400,
        "sample_count": 4096,
        "lead_axis": "lead_last",
        "labels": labels,
        "thresholds": {label: 0.5 for label in labels},
        "enabled": enabled,
        "role": "test",
    })


def test_model_registry_hash_status_and_invalid_contracts(tmp_path):
    good = spec(tmp_path)
    registry = ModelRegistry([good])
    assert registry.verify_artifact(good) == good.artifact_sha256
    assert registry.status()["models"][0]["ready"] is True
    bad = spec(tmp_path / "bad", digest="f" * 64)
    assert ModelRegistry([bad]).status()["models"][0]["ready"] is False
    with pytest.raises(ModelConfigurationError, match="labels/order"):
        spec(tmp_path / "labels", labels=["AF"])


def test_atomic_registry_replacement_and_onnx_lineage_requirements(tmp_path):
    original = spec(tmp_path / "original", enabled=False)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"schema_version": 1, "models": [original.public_document()]}))
    replacement = spec(tmp_path / "replacement", enabled=True)
    loaded = ModelRegistry.register_atomic(registry_path, replacement)
    assert loaded.specs["antonior92"].artifact_sha256 == replacement.artifact_sha256
    assert loaded.status()["models"][0]["ready"] is True
    candidate = replacement.public_document()
    candidate.update(artifact_format="onnx", onnx_opset=17, input_name="", output_name="probabilities")
    with pytest.raises(ModelConfigurationError, match="input/output names"):
        ModelSpec.from_dict(candidate)


def test_isolated_runner_consumes_full_waveform_and_preserves_all_probabilities(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text("""
import argparse, json, numpy as np
p=argparse.ArgumentParser(); p.add_argument('--request'); a=p.parse_args()
r=json.load(open(a.request)); x=np.load(r['input']['path'])['signal']
assert x.shape == (1,4096,12)
labels=r['model']['labels']
print(json.dumps({'schema_version':1,'probabilities':{k:(i+1)/10 for i,k in enumerate(labels)}}))
""")
    model = spec(tmp_path / "model", command=[sys.executable, str(runner), "--request", "{request}"])
    result = IsolatedModelRunner(ModelRegistry([model]), timeout_seconds=5).run(model, record())
    assert result.status == "complete"
    assert list(result.probabilities) == list(ANTONIO_LABELS)
    assert result.probabilities["ST"] == pytest.approx(0.6)
    assert result.findings == ["AF", "ST"]
    assert result.preprocessing["zero_pad_left"] == 48


def test_isolated_runner_fails_closed_on_hash_and_incomplete_outputs(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text("print('{\"schema_version\":1,\"probabilities\":{\"AF\":0.9}}')")
    bad_hash = spec(tmp_path / "hash", digest="f" * 64, command=[sys.executable, str(runner)])
    result = IsolatedModelRunner(ModelRegistry([bad_hash])).run(bad_hash, record())
    assert result.status == "unavailable" and "SHA-256 mismatch" in result.error
    incomplete = spec(tmp_path / "output", command=[sys.executable, str(runner)])
    result = IsolatedModelRunner(ModelRegistry([incomplete])).run(incomplete, record())
    assert result.status == "unavailable" and "every registered label" in result.error


def test_isolated_runtime_request_rechecks_artifact_hash_and_shape(tmp_path):
    model = tmp_path / "model.bin"; model.write_bytes(b"model")
    input_path = tmp_path / "input.npz"; np.savez_compressed(input_path, signal=np.ones((1, 12, 2500), dtype=np.float32))
    request = tmp_path / "request.json"
    document = {
        "schema_version": 1,
        "model": {"artifact_path": str(model), "artifact_sha256": hashlib.sha256(b"model").hexdigest()},
        "input": {"path": str(input_path), "array": "signal", "shape": [1, 12, 2500]},
    }
    request.write_text(json.dumps(document))
    loaded, signal = model_runner_mod._load_request(request)
    assert loaded["model"]["artifact_path"] == str(model)
    assert signal.shape == (1, 12, 2500)
    document["model"]["artifact_sha256"] = "f" * 64; request.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        model_runner_mod._load_request(request)


def test_runtime_source_must_match_clean_registered_git_commit(tmp_path):
    repository = tmp_path / "source"; repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    (repository / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(repository), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "pin"], check=True)
    commit = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    spec = {"runtime_source_path": str(repository), "runtime_source_commit": commit}
    assert model_runner_mod._runtime_repository("deepecg-sl", spec) == repository
    (repository / "source.py").write_text("value = 2\n")
    with pytest.raises(RuntimeError, match="not an exact clean checkout"):
        model_runner_mod._runtime_repository("deepecg-sl", spec)


def test_agreement_compares_only_shared_labels(tmp_path):
    # Exercise through simple result-shaped objects returned by two fake models.
    from medical.ecg_models import ModelResult
    one = ModelResult("one", "one", "1", "a" * 64, "complete", {"AF": 0.8}, ["AF"], thresholds_applied=True)
    two = ModelResult("two", "two", "1", "b" * 64, "complete", {"Afib": 0.2, "PVC": 0.9}, [], thresholds_applied=True)
    report = agreement_report([one, two])
    assert report["disagreements"] == ["atrial_fibrillation"]
    assert "PVC" not in report["comparable_labels"]


def disabled_registry(tmp_path):
    path, digest = artifact(tmp_path)
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "models": [{
        "model_id": "antonior92", "display_name": "baseline", "version": "1",
        "artifact_path": str(path.resolve()), "artifact_sha256": digest,
        "command": [sys.executable, "unused.py"], "sample_rate_hz": 400,
        "sample_count": 4096, "lead_axis": "lead_last", "labels": list(ANTONIO_LABELS),
        "thresholds": {}, "enabled": False,
    }]}))
    return registry


def pipeline_config(tmp_path):
    inbox = tmp_path / "inbox"; inbox.mkdir()
    return {
        "mqtt": {"broker": "localhost", "port": 1883},
        "ecg_ai": {
            "inbox_dir": str(inbox), "archive_dir": str(tmp_path / "archive"),
            "rejected_dir": str(tmp_path / "rejected"),
            "model_registry": str(disabled_registry(tmp_path / "models")),
            "max_file_age_seconds": 86400,
        },
    }


def test_pipeline_archives_publishes_and_tracks_every_raw_data_consumer(tmp_path):
    config = pipeline_config(tmp_path)
    source = Path(config["ecg_ai"]["inbox_dir"]) / "exam.xml"
    source.write_bytes(xml_bytes())
    result = ECGPipeline(config).process(source)
    assert result["status"] == "models_unavailable"
    assert Path(result["source"]["archive"]["source"]).read_bytes() == source.read_bytes()
    assert result["raw_data_usage"]["antonior92"] == {"status": "not_used", "reason": "model disabled"}
    assert json.loads((Path(result["source"]["archive"]["root"]) / "analysis.json").read_text())["record_id"] == result["record_id"]


def test_pipeline_rejects_outside_path_and_archives_bad_source(tmp_path):
    config = pipeline_config(tmp_path)
    outside = tmp_path / "outside.xml"; outside.write_bytes(xml_bytes())
    with pytest.raises(ECGImportError, match="inside"):
        ECGPipeline(config).process(outside)
    bad = Path(config["ecg_ai"]["inbox_dir"]) / "bad.xml"; bad.write_bytes(b"not xml")
    result = ECGPipeline(config).process(bad)
    assert result["status"] == "rejected"
    archived = Path(result["rejected_archive"]) / "source.xml"
    assert archived.read_bytes() == b"not xml"


class FakeMQTT:
    def __init__(self):
        self.published = []
        self.subscribed = []
        self.credentials = None

    def username_pw_set(self, *credentials):
        self.credentials = credentials

    def publish(self, topic, payload, **kwargs):
        self.published.append((topic, json.loads(payload), kwargs))

    def subscribe(self, topic, **kwargs):
        self.subscribed.append((topic, kwargs))


def test_pipeline_publishes_retained_result_and_flags_future_clock_skew(tmp_path):
    config = pipeline_config(tmp_path)
    mqtt = FakeMQTT()
    source = Path(config["ecg_ai"]["inbox_dir"]) / "future.xml"
    source.write_bytes(xml_bytes().replace(b"2026-08-22T10:30:00-06:00", b"2099-08-22T10:30:00-06:00"))
    result = ECGPipeline(config, mqtt_client=mqtt).process(source)
    assert result["freshness"]["current_for_live_decisions"] is False
    assert result["freshness"]["future_clock_skew_seconds"] > 0
    topic, published, options = mqtt.published[-1]
    assert topic == "shtf/medical/ecg_analysis/patient-1"
    assert published["record_id"] == result["record_id"]
    assert options == {"qos": 1, "retain": True}


def test_scan_marks_permanent_rejection_and_does_not_repeat(tmp_path, monkeypatch):
    config = pipeline_config(tmp_path)
    source = Path(config["ecg_ai"]["inbox_dir"]) / "oversize.xml"
    source.write_bytes(b"small")
    pipeline = ECGPipeline(config)
    monkeypatch.setattr(pipeline, "process", lambda path: (_ for _ in ()).throw(ECGImportError("permanent")))
    result = pipeline.scan_once()
    assert result[0]["status"] == "rejected_unarchived"
    assert source.with_suffix(".xml.processed").read_text().strip() == "rejected_unarchived"
    assert pipeline.scan_once() == []


def test_ecg_service_requires_explicit_single_node_enable_and_dedicated_auth(tmp_path, monkeypatch):
    config = pipeline_config(tmp_path)
    config["mqtt"]["services"] = {"ecg_ai": {"username": "specter-ecg-ai", "password": "secret"}}
    with pytest.raises(RuntimeError, match="disabled"):
        ecg_service_mod.ECGService(config)
    config["ecg_ai"]["service_enabled"] = True
    mqtt = FakeMQTT()
    monkeypatch.setattr(ecg_service_mod, "_mqtt_client", lambda *args, **kwargs: mqtt)
    service = ecg_service_mod.ECGService(config)
    assert mqtt.credentials == ("specter-ecg-ai", "secret")
    service._on_connect(mqtt, None, None, 0)
    assert mqtt.subscribed == [(ecg_service_mod.TOPIC_COMMAND, {"qos": 1})]


def test_ecg_cli_status_reports_disabled_registry_ready(tmp_path, capsys):
    config = pipeline_config(tmp_path)
    config_path = tmp_path / "specter.json"; config_path.write_text(json.dumps(config))
    assert ecg_service_mod.main(["--config", str(config_path), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["models"][0]["enabled"] is False


def test_dataset_manifest_accepts_patient_isolation_and_rejects_leakage(tmp_path):
    manifest = tmp_path / "evaluation.json"
    dataset_root = tmp_path / "ptb-xl"; dataset_root.mkdir()
    (dataset_root / "r1.dat").write_bytes(b"record one")
    (dataset_root / "r2.dat").write_bytes(b"record two")
    (dataset_root / "r3.dat").write_bytes(b"record three")
    base = {
        "schema_version": 1,
        "datasets": [{"dataset_id": "ptb-xl", "version": "1.0.3", "license": "ODbL 1.0", "source_url": "https://physionet.org/content/ptb-xl/", "root_dir": str(dataset_root)}],
        "records": [
            {"record_id": "r1", "patient_id": "p1", "dataset_id": "ptb-xl", "split": "train", "waveform_path": "r1.dat", "waveform_sha256": hashlib.sha256(b"record one").hexdigest(), "labels": ["AF"]},
            {"record_id": "r2", "patient_id": "p2", "dataset_id": "ptb-xl", "split": "test", "waveform_path": "r2.dat", "waveform_sha256": hashlib.sha256(b"record two").hexdigest(), "labels": []},
        ],
    }
    manifest.write_text(json.dumps(base))
    assert validate_dataset_manifest(manifest)["patient_split_leakage"] is False
    base["records"].append({"record_id": "r3", "patient_id": "p1", "dataset_id": "ptb-xl", "split": "test", "waveform_path": "r3.dat", "waveform_sha256": hashlib.sha256(b"record three").hexdigest(), "labels": ["AF"]})
    manifest.write_text(json.dumps(base))
    with pytest.raises(ModelConfigurationError, match="patient leakage"):
        validate_dataset_manifest(manifest)


def test_frozen_prediction_evaluation_requires_exact_split_and_reports_metrics(tmp_path):
    root = tmp_path / "dataset"; root.mkdir()
    records = []
    for index, (patient, truth) in enumerate((("p1", True), ("p2", False)), 1):
        file = root / f"r{index}.dat"; file.write_bytes(f"record-{index}".encode())
        records.append({
            "record_id": f"r{index}", "patient_id": patient, "dataset_id": "external",
            "split": "test", "waveform_path": file.name,
            "waveform_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            "labels": ["AF"] if truth else [],
        })
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "datasets": [{"dataset_id": "external", "version": "1", "license": "test", "source_url": "https://example.test", "root_dir": str(root)}],
        "records": records,
    }))
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps({
        "schema_version": 1, "model_id": "test", "model_version": "1", "artifact_sha256": "a" * 64,
        "labels": ["AF"], "thresholds": {"AF": 0.5},
        "predictions": [
            {"record_id": "r1", "probabilities": {"AF": 0.9}},
            {"record_id": "r2", "probabilities": {"AF": 0.1}},
        ],
    }))
    report = evaluate_predictions(manifest, predictions)
    assert report["per_label"]["AF"]["sensitivity"] == 1
    assert report["per_label"]["AF"]["specificity"] == 1
    assert report["per_label"]["AF"]["auroc"] == 1
    payload = json.loads(predictions.read_text()); payload["predictions"].pop(); predictions.write_text(json.dumps(payload))
    with pytest.raises(ModelConfigurationError, match="exactly match"):
        evaluate_predictions(manifest, predictions)


def analysis_payload():
    return {
        "schema_version": 1, "status": "complete", "record_id": "record-1", "patient_id": "p1",
        "acquired_at_utc": datetime.now(timezone.utc).isoformat(), "quality": {"status": "pass", "issues": []},
        "machine_output": {"measurements": {"HR": 61}, "interpretation": ["Sinus rhythm"]},
        "models": [{
            "model_id": "antonior92", "display_name": "AntonioR92", "version": "1", "status": "complete",
            "artifact_sha256": "a" * 64, "probabilities": {"AF": 0.1, "ST": 0.7}, "findings": ["ST"],
            "explanation": None,
        }],
        "agreement": {"disagreements": []},
    }


def test_medgemma_cache_rejects_mismatch_and_uses_complete_structured_output():
    cache = ECGAnalysisCache()
    assert not cache.update("other", analysis_payload())
    assert cache.update("p1", analysis_payload())
    block = cache.to_prompt_block("p1")
    assert "AntonioR92" in block and "AF: probability 0.1000" in block
    assert "ST: probability 0.7000" in block and "Biocare machine measurements" in block
    payload = cache.latest("p1"); payload["models"][0]["probabilities"]["AF"] = 1
    assert cache.latest("p1")["models"][0]["probabilities"]["AF"] == 0.1
    future = analysis_payload(); future["acquired_at_utc"] = "2099-01-01T00:00:00Z"
    assert cache.update("p1", future)
    assert "STALE - do not treat as current" in cache.to_prompt_block("p1")


class FakeRetriever:
    def to_prompt_block(self, passages):
        return "REFERENCE MATERIAL: none"


def test_prompt_builder_includes_ecg_and_does_not_dump_raw_waveform():
    cache = ECGAnalysisCache(); cache.update("p1", analysis_payload())
    prompt = PromptBuilder(VitalsCache(), {}, cache).build("p1", "review ECG", [], FakeRetriever())
    assert "12-LEAD ECG RESEARCH ANALYSIS" in prompt
    assert "probability 0.7000" in prompt
    assert "ecg_waveform_uv" not in prompt
