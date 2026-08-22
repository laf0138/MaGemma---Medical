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
