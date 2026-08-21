# MaGemma---Medical

The SPECTER project (field-deployable emergency comms + medical command
center) lives in `specter/` — see `specter/README.md` and
`specter/docs/MANUAL.md` for what it is and how to run it.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests currently cover the trauma/triage decision logic
(`specter/trauma/specter_trauma.py`) and the Bluetooth vitals-device
parsers (`specter/medical/specter_medical_hub.py`) — the two areas with the
highest patient-safety impact per `specter/docs/MANUAL.md` Part 7. See that
doc's "Known Gaps" section for what else still needs coverage.