# MaGemma---Medical

The SPECTER project (field-deployable emergency comms + medical command
center) lives in `specter/` — see `specter/README.md` and
`specter/docs/MANUAL.md` for what it is and how to run it.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest --cov=specter --cov-report=term-missing --cov-fail-under=65
```

CI enforces the measured coverage floor. Direct dependencies are declared in
`requirements-dev.in`; `requirements-dev.txt` pins the complete resolved
development/test graph used by CI.

## Polar H10 validation harness

The ECG collection harness can be verified without hardware and later reused
unchanged for a live H10 integration capture:

```bash
python specter/medical/polar_h10_validation.py --self-test --output polar-h10-self-test
```

See [the validation procedure](specter/docs/POLAR_H10_VALIDATION.md). A passing
self-test is deliberately labelled as offline-only; it does not mark the H10
hardware or the experimental ECG measurements clinically validated.

## Twelve-lead ECG research pipeline

The Biocare/DeepECG-SL/AntonioR92/ECG-XPLAIM/PTB-XL/MedGemma/ONNX path is
integrated but ships safely disabled until exact hardware exports, model files,
hashes, task labels and datasets are provisioned:

```bash
python specter/medical/specter_ecg_ai.py --config /etc/specter/specter.json status
```

See [the implementation and commissioning status](specter/docs/ECG_AI_IMPLEMENTATION_STATUS.md).
Passing software tests is not hardware or clinical validation.
