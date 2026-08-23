# SPECTER 12-Lead ECG AI Implementation Status

Status date: 2026-08-22
Safety classification: **research decision support; not a diagnosis or treatment authority**

## What is implemented

The seven selected parts now have one integrated software path:

1. **Biocare iE300 acquisition boundary** — `medical/ecg_12lead.py` accepts a strict waveform-bearing XML subset, requires all 12 canonical leads, explicit units/gain/sample rate/time, rejects ambiguity and poor signals, preserves all source bytes, all machine measurements/interpretation, and unrecognized metadata, and records every normalization.
2. **DeepECG-SL** — registered as the primary research model with its 250 Hz × 2,500-sample, lead-first contract, published 77-label order, upstream scaling factor, immutable artifact hash, and isolated TorchScript runtime.
3. **AntonioR92** — registered as the six-label baseline with its published 400 Hz × 4,096-sample, lead-last contract, symmetric zero padding, published thresholds, immutable hash, and isolated legacy TensorFlow runtime.
4. **ECG-XPLAIM** — registered but disabled as the later secondary model. The adapter accepts exact task labels/weights and may return a bounded explainability object. The shipped label is intentionally a placeholder and cannot be enabled responsibly until a task is selected.
5. **PTB-XL/external evaluation corpus** — the manifest validator requires dataset version, license, source URL, waveform hashes, unique records and patient-level split isolation. It rejects patient leakage between train, validation and test.
6. **MedGemma** — subscribes to retained `shtf/medical/ecg_analysis/<patient_id>` results. Its prompt receives complete research probabilities, thresholds/flags, quality, device measurements, provenance and disagreements. It does not receive a text dump of waveform samples and cannot treat a missing model as a negative result.
7. **ExChanGeAI concepts / ONNX lifecycle** — the offline registry pins versions, licenses, sources, input/output contracts and artifact hashes. ONNX entries must additionally record opset, input/output names and the source-artifact hash. Registration verifies the artifact and atomically replaces the manifest; the system never downloads or fine-tunes a model automatically.

`medical/specter_ecg_ai.py` watches a bounded inbox, archives accepted and rejected sources unchanged, runs every enabled model independently, retains all probabilities, records comparable-label disagreements, publishes the structured result, and exposes import/status/dataset-validation/model-registration CLI modes. `specter-ecg-ai.service`, dedicated MQTT credentials/ACLs, installer paths and dashboard state routing are included.

## Deliberately not claimed complete

- **Biocare hardware/schema validation is pending.** Public iE300 material confirms XML/DICOM export but does not publish the XML schema. No real export was supplied. The importer therefore supports the documented SPECTER subset and rejects any unknown/ambiguous vendor layout. A real iE300 XML file and firmware/version details are required to finish the vendor profile.
- **No model artifact was supplied.** The shipped registry entries are disabled and contain unmistakable `UNPROVISIONED` versions/zero hashes. Downloading weights, accepting licenses, pinning the exact files and setting validated thresholds are deployment decisions, not safe assumptions for this merge.
- **DeepECG-SL EfficientNet thresholds are not published in the inspected upstream deployment repository.** All 77 probabilities are preserved; no positive/negative threshold flags are manufactured.
- **AntonioR92's upstream README files disagree on augmented-limb-lead order.** The main model README says aVR/aVL/aVF while `data/README.md` says aVL/aVF/aVR. SPECTER registers the main model README order, records the actual per-model order in preprocessing provenance, and keeps the model disabled until the exact pretrained artifact is checked against known vectors.
- **ECG-XPLAIM task/weights are not selected.** Its placeholder output contract must be replaced before enablement. A generic input-gradient summary in the runner is explicitly not represented as the publication's explanation method.
- **No PTB-XL or other patient dataset is committed.** Dataset licenses, size and patient information require a separate controlled acquisition. The validation tooling and split-integrity gate are present.
- **Clinical and hardware accuracy remain unvalidated.** Synthetic/unit tests establish software behavior, not sensitivity, specificity, calibration, lead placement correctness, clinical measurement accuracy or performance on the exact iE300.

## Required commissioning sequence

1. Export at least three de-identified, waveform-bearing XML exams from the exact iE300, including one normal recording and controlled lead-off/noise cases.
2. Add a fixture-derived vendor profile without deleting or weakening the strict generic gate; verify units and waveforms against the machine's printed grid/calibration pulse.
3. Provision each model in a separate locked environment, record upstream commit/model version/license, calculate its SHA-256, and register it offline with `specter_ecg_ai.py register-model`.
4. Acquire PTB-XL and approved external corpora under their licenses, generate the manifest, and run `validate-dataset` before any evaluation.
5. Establish label-specific thresholds/calibration on validation data, then freeze the held-out test split. Never tune against the test set or field patient data.
6. Compare simultaneous iE300 recordings with clinician-reviewed references. Document failure modes, subgroup results and acceptance criteria before changing the research-only status.
