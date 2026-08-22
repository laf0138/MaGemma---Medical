# Polar H10 validation procedure

## What this can and cannot prove

`medical/polar_h10_validation.py` is an isolated collection harness. It does
not connect to MQTT, publish patient data, or enable `polar_h10` in the
production device gate.

A passing offline run proves that the SPECTER harness can reconstruct the
130Hz sample timeline, detect missing or disordered data, hand a waveform to
the experimental ECG analyzer, export evidence, and replay the capture. It is
always labelled `NOT_RUN_NO_HARDWARE`.

A passing live run proves only that the tested computer, Bluetooth stack,
bleak/bleakheart versions, and that physical H10 completed this integration
test. It is labelled `PASS_INTEGRATION_ONLY`. It does **not** establish ECG
diagnostic accuracy. Clinical accuracy remains
`NOT_EVALUATED_REQUIRES_REFERENCE_ECG` until a simultaneous diagnostic ECG is
reviewed by a qualified person.

The harness follows the upstream interfaces rather than packet-arrival
assumptions:

- [Polar documents H10 ECG as 130Hz in microvolts](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/products/PolarH10.md).
- [BleakHeart documents each ECG frame timestamp as the time of its last sample](https://github.com/fsmeraldi/bleakheart).
- [Polar requires applications to terminate H10 ECG streaming](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/KnownIssues.md); the harness treats an unconfirmed stop as a failed run.

## Offline verification — available now

From the repository root, using the same Python environment as SPECTER:

```bash
python specter/medical/polar_h10_validation.py \
  --self-test \
  --duration 15 \
  --output polar-h10-self-test
```

Expected terminal fields:

```json
{
  "passed": true,
  "hardware_integration_status": "NOT_RUN_NO_HARDWARE",
  "clinical_accuracy_status": "NOT_EVALUATED_REQUIRES_REFERENCE_ECG"
}
```

The new output directory contains:

| File | Purpose |
|---|---|
| `manifest.json` | Provenance, platform, Python and dependency versions, validation labels |
| `capture.json` | Lossless frame-level capture for replay |
| `ecg_samples.csv` | Per-sample timestamp, frame number and microvolt value |
| `heart_rate.csv` | Heart-rate and RR notification frames |
| `report.json` | Acceptance results, transport metrics and experimental analyzer output |
| `SHA256SUMS` | Integrity hashes for every other artifact |

Revalidate any saved capture without Bluetooth:

```bash
python specter/medical/polar_h10_validation.py \
  --replay polar-h10-self-test \
  --output polar-h10-replay
```

Output directories are intentionally never overwritten.
Root-level `polar-h10-*` artifact directories are ignored by Git. A live
capture contains physiologic data and a Bluetooth device identifier; treat it
as sensitive, store it securely, and do not attach or commit it without an
intentional de-identification review.

## Live integration run — when the H10 is available

Prepare the H10 sensor pod, chest strap, a fresh battery, and a Bluetooth-
capable computer. Wet the strap electrodes, wear the strap, and close Polar
Flow or any other program that could already be connected to the sensor.

Run a 60-second resting capture:

```bash
python specter/medical/polar_h10_validation.py \
  --live \
  --duration 60 \
  --output polar-h10-live-resting
```

If more than one H10 is visible, repeat with a unique name/address substring:

```bash
python specter/medical/polar_h10_validation.py \
  --live \
  --duration 60 \
  --device "TEST123" \
  --output polar-h10-live-resting
```

Do not remove the sensor from the strap until the command exits; the harness
must stop both PMD ECG streaming and heart-rate notifications in its cleanup
path.

## Integration acceptance criteria

The run passes only when all of these are true:

- ECG streaming started and was confirmed stopped.
- Heart-rate notification cleanup was confirmed for a live run.
- At least two ECG frames and at least 90% of the requested duration were
  captured, with an absolute minimum of ten seconds.
- All ECG samples are integers and all HR/RR values are positive integers.
- At least one heart-rate notification frame was captured during a live run.
- Reconstructed sample timestamps strictly increase.
- Sample coverage is at least 98% of the 130Hz device-timestamp span.
- No collection or cleanup error was recorded.

No BPM, amplitude, or morphology value is checked against a “normal” clinical
range. Transport integrity and physiologic interpretation are separate tests.

## Clinical comparison remains separate

Do not intentionally provoke an arrhythmia, electrolyte abnormality, or other
medical event. If clinical morphology validation is pursued, collect the H10
simultaneously with a diagnostic/reference ECG under appropriate supervision.
Preserve both original recordings and their clocks/markers. Compare the raw
waveforms and clinician-measured intervals; do not treat agreement with a
consumer watch or SPECTER's own derived value as an independent reference.

Only after the physical integration run passes and the evidence is reviewed
should `polar_h10` be considered for the production
`SPECTER_VERIFIED_BLE_DEVICES` setting. Clinical morphology features remain
experimental even after transport integration passes.
