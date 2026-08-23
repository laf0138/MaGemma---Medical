# SPECTER MEDICAL DISPLAYS — UI DESIGN BRIEF v1.0

**Target:** 15" lid-mounted touchscreen, Node 1 (Pi 5), Flask + WebSocket + D3 + Canvas
**Audience:** WFR-trained layperson treating their own family, possibly at 3 AM, possibly with cold hands, possibly with no physician reachable
**Job of the screen:** Let the operator see the whole physiologic story in one look, and know within two seconds whether the situation is getting worse.

This brief is the specification. Build from it exactly; every color, type, and layout decision below is derived from it.

---

## PART 1 — COMPLETE DATA INVENTORY

Everything the medical displays can draw on. Nothing here is aspirational; each item maps to a real source in the locked build.

### 1.1 Raw — Bluetooth, automatic (medical hub, Pi Zero 2W)

> **This table is stale relative to `docs/MANUAL.md` Part 7.4 — treat it as design intent, not current status.** The `ecg_rhythm` row below describes the AliveCor KardiaMobile 6L parser, which was **removed** for fabricating a BLE characteristic that doesn't exist. Of the four rows above it, Omron and Masimo are shipped but **hard-blocked by default** — checked against Bluetooth SIG specs and found to very likely not match how these devices actually communicate, the same class of bug as the removed Kardia parser. **Contour Next One's parser has since been rewritten against the real, confirmed Bluetooth SIG Glucose Service protocol** (still gated pending hardware confirmation, not yet "fake"). **Braun ThermoScan 7 is worse than a wrong parser: the physical device this kit names has no Bluetooth radio at all** and cannot be fixed in software — see Part 7.4 before treating that row as buildable. The Polar H10 rows are new: a real ECG/HR integration via `bleakheart`, plus a NeuroKit2-based QRS/T-wave measurement layer (`medical/ecg_analysis.py`) on top of the raw waveform. All of it is still gated the same way pending real-hardware confirmation, and the derived `ecg_*` fields are **measurements with hedged advisory flags, not a diagnosis** — see Part 7.4 for the QRS-measurement caveat and why the T-wave flag is weaker than the QRS one. None of the Bluetooth rows will actually populate on a fresh install until someone verifies the relevant device against real hardware. The derived `ecg_*` scalar fields (not the raw waveform/RR arrays) are wired into MedGemma's prompt as of this build — see Part 7.4.

| Field | Unit | Source device | Cadence |
|---|---|---|---|
| `bp_systolic` | mmHg | Omron BP7450 | On measurement |
| `bp_diastolic` | mmHg | Omron BP7450 | On measurement |
| `pulse` | bpm | Omron / Masimo / Polar H10 | On measurement / 5–10 s |
| `spo2` | % | Masimo MightySat | 5–10 s |
| `temperature_c` | °C | Braun ThermoScan 7 | On measurement |
| `glucose_mg_dl` | mg/dL | Contour Next One | On measurement |
| `ecg_waveform_uv` | µV, 130Hz samples | Polar H10 | Per collection cycle (`polar_stream_seconds`, default 10s) |
| `rr_intervals_ms` | ms | Polar H10 | Per collection cycle |
| `ecg_q_s_peak_interval_ms` | ms; experimental, **not clinical QRS duration** | Polar H10 (derived, NeuroKit2) | Per collection cycle when ≥3 valid Q-R-S tuples |
| `ecg_r_wave_abs_amplitude_uv` | µV; experimental | Polar H10 (derived, NeuroKit2) | Per collection cycle when ≥3 valid R-T pairs |
| `ecg_t_wave_abs_amplitude_uv` | µV; experimental | Polar H10 (derived, NeuroKit2) | Per collection cycle when ≥3 valid R-T pairs |
| `ecg_t_r_abs_ratio` | ratio; experimental, no diagnostic threshold | Polar H10 (derived, NeuroKit2) | Per collection cycle when ≥3 valid R-T pairs |
| `ecg_morphology_beats_analyzed` / `ecg_amplitude_beats_analyzed` | beats | Polar H10 (derived, NeuroKit2) | Per collection cycle |
| `ecg_analysis_status` | `experimental_not_clinically_validated` | Polar H10 (derived, NeuroKit2) | Per collection cycle |
| `ecg_analysis_warnings` | text | Polar H10 (derived, NeuroKit2) | Per collection cycle, only when a measurement is withheld |
| `ecg_rhythm` | normal / afib / inconclusive / unreadable | ~~AliveCor KardiaMobile 6L~~ REMOVED, see Part 7.4 | On measurement |

Per-reading metadata, all displayable: `device_name`, `timestamp_utc`, `rssi` (dBm), `battery_pct` (BLE characteristic 0x2A19).

### 1.2 Raw — manual entry (Tier 1–3 kit, no Bluetooth)

These have no radio. The UI must make entering them fast enough that they actually get entered.

| Field | Unit / values | Instrument |
|---|---|---|
| `respiratory_rate` | breaths/min | Counted, 30 s × 2 |
| `consciousness` | A / C / V / P / U | ACVPU assessment |
| `pain_score` | 0–10 | Verbal scale |
| `cap_refill` | seconds | Nail bed press |
| `pupils_left` / `pupils_right` | mm + reactive/sluggish/fixed | Penlight + gauge card |
| `peak_flow` | L/min | Peak flow meter |
| `inr` | ratio | iHealth PT3 |
| `urinalysis` | 10 fields (see below) | Siemens Multistix 10SG |
| `ketones` | neg / trace / small / mod / large | Ketone strips |
| `wound` | length × width × depth mm, + photo | Ruler + camera |
| `urine_output` | mL / interval | Measured |

Urinalysis sub-fields: leukocytes, nitrite, urobilinogen, protein, pH, blood, specific gravity, ketone, bilirubin, glucose. **Nitrite + leukocytes together is the UTI screen and gets special prominence for both immunosuppressed patients.**

### 1.3 Derived — computed, never entered

**STATUS: Implemented (August 2026).** Computed by `medical/specter_medical_ai.py`'s `VitalsCache.derived()` (scoring functions shared with WARD mode via `medical/clinical_scores.py`), published retained to `shtf/medical/derived/<patient_id>` on every vitals update, and surfaced in MedGemma's prompt under a `DERIVED METRICS` block. Every derived value's inputs are in the same MQTT payload (`per_parameter`), so a score the operator can't audit is not possible.

**Known gap vs. this spec, stated plainly rather than glossed over:** no BLE device wired into this build measures respiration rate or level of consciousness. NEWS2 and qSOFA are therefore **always partial** on this path — both are still computed and published (with `partial: true` and `missing_parameters` naming exactly what's absent) rather than withheld, because a partial score that visibly says what it's missing is more useful than no score at all, and hiding it would make it harder to notice the gap should a future sensor (e.g. a capnometer) ever close it. `qSOFA.positive` is `null`, never `false`, whenever an input is missing — a partial qSOFA is never allowed to read as "ruled out." Fever burden and Δ-from-baseline are bounded by the AI engine's in-memory reading history (`VitalsCache.HISTORY_LIMIT`, currently 24 readings per vital, not persisted across restarts) rather than a true persisted 24h/30-day window — Δ-from-baseline is a same-session median, not a 30-day one, until a persisted vitals history exists.

| Metric | Formula | Why it's on this screen |
|---|---|---|
| **MAP** | `DBP + (SBP − DBP) / 3` | Organ perfusion. **< 65 is graft-threatening for a transplant recipient** — this is the single most important derived number on the display for the operator. |
| **Pulse pressure** | `SBP − DBP` | Narrow (< 25) suggests falling stroke volume before SBP drops |
| **Shock index** | `HR / SBP` | > 0.9 concerning, > 1.0 suggests shock. Rises before BP falls. |
| **NEWS2** | Aggregate 0–20 from RR, SpO2, supplemental O2, temp, SBP, HR, ACVPU | Early warning. Any single parameter scoring 3 escalates regardless of total. **Always partial on the automated-device path — see above.** |
| **qSOFA** | 1 pt each: RR ≥ 22, SBP ≤ 100, altered mentation | Sepsis screen. **≥ 2 in an immunosuppressed patient is an emergency.** **Always partial on the automated-device path — see above.** |
| **Trend slope** | Least-squares over last 3–6 readings | Direction beats snapshot |
| **Staleness** | `now − timestamp_utc` | A 40-minute-old SpO2 is not a current SpO2 |
| **Fever burden** | Minutes above 38.0 °C in last 24 h | Immunosuppressed fevers get blunted; cumulative burden is more honest than peak. **Bounded by in-memory history depth — see above.** |
| **Δ from baseline** | Current vs. this patient's 30-day median | "Normal for them" beats "normal for a textbook". **Currently a same-session median, not 30 days — see above.** |

**Display caveat, shown in the score's detail panel:** NEWS2 and qSOFA are screening aids validated in hospital populations. They flag concern; they do not diagnose. Both are known to under-trigger in immunosuppressed patients whose fever and inflammatory response are pharmacologically suppressed. The display must never let a low score read as reassurance.

### 1.4 From the AI engine (Jetson, MedGemma 4B)

Topic `shtf/medical/diagnosis/<patient_id>`:

`differential[]` (ordered, each with supporting/contradicting findings) · `red_flags[]` · `recommended_exams[]` (priority ordered) · `management[]` · `evac_threshold` · `sources[]` (title + page) · `kiwix_search_url` · `vitals_used{}` (snapshot of exactly what the model saw) · `model` · `request_id` · `timestamp_utc`

Topic `shtf/medical/ai/status`: `state` (online / thinking / offline) · `model_loaded` · tokens/sec from the last run.

### 1.5 Cold chain (BougeRV CR22)

`interior_temp_c` · `setpoint` · `compressor_duty_pct` · `door_state` · `power_source` (battery / shore / solar) · `runtime_remaining_min` · `excursion_log[]` (start, end, peak, duration outside 2–8 °C)

### 1.6 Medication schedule

Belatacept and the RA biologic, each: `last_infusion_date` · `next_due_date` · `days_remaining` · `doses_on_hand` · `reconstitution_deadline` (24 h countdown, only when active).
Oral maintenance (myfortic, prednisone): `daily_schedule` · `last_taken` · `days_of_supply`.

### 1.7 System health

Hub heartbeat + per-device BLE state/RSSI/battery · MQTT broker state · Jetson RAM, temp, inference queue · Kiwix reachable on Node 5 · battery SoC, solar input W, system load W.

---

## PART 2 — VISUAL SYSTEM

### 2.1 The organizing idea

**The screen is a strip-chart recorder, not a card grid.**

Card grids of circular gauges are what every dashboard generator produces, and they are actively bad here: they show seven isolated numbers and hide the one thing that matters, which is whether the numbers are moving together. Clinicians don't read vitals that way. An anesthesia record and an ICU flowsheet plot every parameter against one shared time axis, stacked, so the whole physiologic story is a single visual shape.

So: a dark field ruled with an **ECG-paper grid** — rust hairlines at small-square and heavy-square intervals — with vitals traces plotted over it in phosphor colors, all sharing one horizontal time axis. BP renders as the classic up-caret/down-caret pair with the shaded MAP band between them. HR as connected dots. Temperature as a continuous line. Events (infusion given, dose taken, wound photographed, AI query run) drop as vertical ticks along the bottom rail.

This is the signature element. It is the one place boldness is spent. Everything else on every screen stays quiet and disciplined.

### 2.2 Palette

Six values. No gradients anywhere except the MAP perfusion band.

| Token | Hex | Use |
|---|---|---|
| `--ground` | `#0B1013` | Page field. Warm-shifted near-black; pure black reads as a dead panel and destroys the sense of a lit surface. |
| `--panel` | `#16202A` | Raised surfaces, entry sheets, detail drawers |
| `--grid` | `#7C2D1E` | Chart hairlines. 12% opacity for small squares, 26% for heavy. Never used for text. |
| `--bone` | `#E8DCC8` | Primary text and numerals. Paper-warm, not pure white — reduces glare on a lid screen at night. |
| `--phosphor` | `#4FD1A0` | In-range values, stable trends, connected traces |
| `--caution` | `#F0A830` | Out-of-range but not immediately dangerous, stale data |
| `--critical` | `#E5484D` | Alarm state, MAP < 65, qSOFA ≥ 2, cold chain excursion |
| `--inert` | `#6BA8D6` | Cold chain, device telemetry, non-physiologic data |

Color never carries meaning alone. Every critical state also gets a shape change (filled vs. hollow marker), a text label, and position in the alarm strip.

### 2.3 Typography

**IBM Plex family throughout** — an engineering typeface with genuine instrument-documentation lineage and true tabular figures, which matter when a 3-digit systolic has to align above a 2-digit diastolic across six columns.

| Role | Face | Spec |
|---|---|---|
| Vitals numerals | IBM Plex Sans Condensed | 600 weight, `font-variant-numeric: tabular-nums`, 72px hero / 44px secondary |
| Labels, eyebrows | IBM Plex Sans | 500, 11px, `letter-spacing: 0.14em`, uppercase |
| Body, AI output | IBM Plex Sans | 400, 15px, 1.55 line-height |
| Timestamps, device IDs, raw payload | IBM Plex Mono | 400, 12px |

Units are set at 0.45× the numeral size in `--bone` at 60% opacity, baseline-aligned to the numeral's baseline, never superscripted.

### 2.4 Structure

Structural devices encode something true. Specifically:

- **Time is always horizontal, always left-to-right, always the same scale across every stacked trace.** This is the whole point of the flowsheet; breaking it anywhere breaks the metaphor and the readability.
- **Vertical position within the chart stack is fixed and never reorders.** BP top, HR, SpO2, temp, glucose, events. Muscle memory is a safety feature. An operator reaching for the SpO2 lane at 3 AM should find it where it was last time.
- **No numbered markers.** Nothing here is a sequence.

---

## PART 3 — SCREEN ARCHITECTURE

Three screens, reached by a persistent segmented control. Plus one element that is always visible regardless of screen.

### 3.0 Always visible — the alarm rail

A 56px strip pinned to the top, below the SPECTER tab bar.

**Quiet state:** patient name, a single line reading `ALL PARAMETERS IN RANGE · LAST FULL SET 4m AGO`, and the composite NEWS2 figure at the right.

**Alarm state:** the rail fills `--critical`, states the specific finding in plain language and the number that produced it, and offers exactly one action. Not "ALERT: PARAMETER OUT OF RANGE" — instead: `MAP 58 mmHg — below graft perfusion threshold · 2m ago` with a `Show` button that jumps to the relevant lane.

Multiple concurrent alarms stack the rail to a maximum of three rows, ordered by severity, with a `+2 more` affordance. Never more than three; a wall of red is a wall of noise.

Alarms are acknowledgeable but never dismissible. An acknowledged alarm dims to a 3px left border in `--critical` and stays until the underlying value returns to range.

### 3.1 Screen A — PATIENT

The flowsheet. Default screen. Everything the operator needs during an active assessment.

```
┌──────────────────────────────────────────────────────────────┐
│ [ALARM RAIL]                                                 │
├──────────────────────────────────────────────────────────────┤
│ ┌─ CURRENT ─────────────┐ ┌─ DERIVED ────────────────────┐   │
│ │  138/87   92   98%    │ │ MAP    92  ·  PP  51         │   │
│ │  mmHg    bpm  SpO2    │ │ SI   0.67  ·  NEWS2  2       │   │
│ │  37.8°C  115 mg/dL    │ │ qSOFA   0  ·  FEVER 84m/24h  │   │
│ │  ECG normal   RR 18   │ │ [tap any figure for inputs]  │   │
│ └───────────────────────┘ └──────────────────────────────┘   │
├──────────────────────────────────────────────────────────────┤
│  FLOWSHEET                        [6h] [12h] [24h] [72h]     │
│                                                              │
│  BP    ∧       ∧      ∧       ∧          ← carets + MAP band │
│        ∨       ∨      ∨       ∨                              │
│  HR    ·───·───·───·───·───·──·                              │
│  SpO2  ────────────────────────                              │
│  TEMP  ────────╱────────────────                             │
│  GLUC  ·       ·          ·                                  │
│  ────────────────────────────────────────────────────────    │
│  EVENTS  ▲infusion  ▲dose   ▲photo    ▲AI query              │
│  08:00   10:00   12:00   14:00   16:00   18:00               │
├──────────────────────────────────────────────────────────────┤
│ [+ Record vitals]  [+ Manual entry]  [Ask MedGemma]          │
└──────────────────────────────────────────────────────────────┘
```

**Current block:** the seven live numbers at hero scale. Each carries an age stamp beneath in Plex Mono. A value older than 10 minutes shifts to `--caution` and its age stamp reads `STALE`. A value never measured this session renders as `——` in `--bone` at 30%, with the label `NOT MEASURED` — never a zero, never a blank, never a plausible-looking placeholder.

**Derived block:** six computed figures. Tapping any one slides a drawer showing the formula, the exact input values with their timestamps, the reference band, and the caveat text from §1.3.

**Flowsheet:** the signature. Canvas-rendered for the traces, SVG overlay for the grid and axis. Pinch or the range buttons change the window. Dragging a vertical scrub line reports every parameter's value at that instant in a floating readout — this is how the operator answers "what was her pressure when the fever spiked."

**Entry actions:** `Record vitals` triggers a hub collection cycle immediately rather than waiting for the 60-second timer. `Manual entry` opens the sheet in §3.4.

### 3.2 Screen B — ASSESS

MedGemma output, and the exam guidance that follows from it.

Top: the question that was asked, in the operator's own words, with the timestamp and a `vitals the model used` chip. Tapping the chip expands the exact `vitals_used` snapshot — critical for trust. If the model reasoned from a 40-minute-old SpO2, the operator must be able to see that.

Then, in order:

1. **Most time-critical concern** — set at 20px, `--bone`, in a bordered block. This is the model's lead, rendered first because it is the thing that might need action in the next few minutes.
2. **Differential** — an ordered list. Each item shows the condition, and beneath it two short rows: `Supports:` with the specific findings in `--phosphor`, `Argues against:` in `--caution`. This structure is non-negotiable; a differential without its evidence is a guess with a ranking.
3. **Red flags** — what would change the assessment, each with what it would mean.
4. **Recommended exams** — priority ordered, each with a checkbox. Checking one opens the manual-entry field for that finding, so assessment flows directly into data.
5. **Management** — field-achievable steps, with anything beyond field capability explicitly marked.
6. **Evacuation threshold** — always present, always last, in a bordered block.
7. **Sources** — cited passages with title and page, each linking to the Kiwix search URL on Node 5.

**While inference runs:** the panel does not spin. It shows the assembled prompt's context summary — which vitals were pulled, which passages were retrieved — so the wait is informative. Expected duration `3–5s` from the locked MedGemma 4B spec.

**Standing disclaimer,** persistent at the panel foot, not a modal: *Offline decision support for a trained responder. Not a clinician. Contact a physician whenever that is possible.*

### 3.3 Screen C — SUSTAIN

Cold chain, medications, inventory, device health. The things that must not fail quietly.

**Cold chain,** given the most space because a silent excursion destroys drugs that cannot be replaced:

A 24-hour temperature trace with the 2–8 °C band drawn as the only shaded region on the screen. Current temperature at hero scale in `--inert`, or `--critical` during an excursion. Below: power source, compressor duty, and runtime remaining on battery. Any past excursion renders as a marked span on the trace with its duration and peak — permanent, not clearable, because that history determines whether a vial is still usable.

**Infusion countdowns,** one row each for Belatacept and the RA biologic: days remaining rendered at 44px, next-due date, doses on hand. Inside 7 days the row shifts to `--caution`; inside 2 days, `--critical`. When a dose is reconstituted, a 24-hour countdown takes over the row entirely — this is the highest-stakes timer in the system and it gets the whole row.

**Oral maintenance:** compact rows, last-taken and days-of-supply. Under 14 days of supply goes `--caution`.

**Device health:** one row per Bluetooth instrument — name, connection state, RSSI as a four-bar glyph, battery percentage. Disconnected devices sort to the top. Under 20% battery goes `--caution`.

**System:** hub heartbeat, MQTT, Jetson (RAM, temp, model loaded), Kiwix reachability, battery SoC, solar input.

### 3.4 Manual entry sheet

Slides from the bottom, covers 70% of the screen, one parameter group at a time.

Numeric fields use a large custom keypad, not the OS keyboard — 64px keys, because the operator may be wearing gloves. Categorical fields (ACVPU, pupil reactivity, urinalysis) use large tap targets, never dropdowns. Urinalysis renders as a visual strip matching the physical Multistix color chart, tapped block by block.

Every entry stamps automatically. Nothing requires typing a time.

---

## PART 4 — ALARM LOGIC

Thresholds that trigger the rail. Patient-profile aware: the immunosuppressed thresholds below apply to both profiles in the locked build.

| Condition | Level | Rationale |
|---|---|---|
| MAP < 65 | Critical | Graft perfusion floor |
| qSOFA ≥ 2 | Critical | Sepsis screen in an immunosuppressed patient |
| SpO2 < 90% | Critical | |
| SBP < 90 or > 180 | Critical | |
| Shock index > 1.0 | Critical | |
| NEWS2 ≥ 7, or any single parameter = 3 | Critical | |
| Temp ≥ 38.0 °C | Caution → Critical if immunosuppressed | A fever in an immunosuppressed patient is a different event than a fever in a healthy one |
| Temp < 36.0 °C | Caution | Hypothermia can indicate sepsis with blunted response |
| Cold chain outside 2–8 °C | Critical | |
| Reconstitution < 4 h remaining | Critical | |
| Infusion due < 2 days | Critical | |
| Glucose < 70 or > 250 | Caution | |
| Nitrite + leukocytes both positive | Caution | UTI screen |
| Any vital stale > 30 min during active assessment | Caution | |
| Hub offline > 3 min | Caution | |

---

## PART 5 — DEGRADED STATES

Failure is the normal operating condition for a SHTF system. Each of these is a designed state, not an error dialog.

**Hub offline:** the Current block freezes with every value marked `LAST KNOWN` and its age. The flowsheet stops advancing and draws a vertical break line at the last reading. `Record vitals` is replaced by `Manual entry` promoted to primary. The system does not pretend to have live data.

**Jetson offline / model not loaded:** Screen B replaces the differential with the offline WFR protocol cards — the laminated-card content, on screen, indexed by presenting complaint. Stating plainly: `MedGemma unavailable. Showing offline protocols.` The screen never goes blank.

**Kiwix unreachable (Node 5 down):** citations still render with title and page so the operator can find the physical reference; the links are marked unavailable rather than broken.

**MQTT broker down:** every screen shows last-known with a persistent banner. Manual entry writes to local storage on Node 1 and syncs when the broker returns.

**Single device dropped:** that lane in the flowsheet greys after its last point; the parameter shows `NOT MEASURED` rather than a stale number promoted to current.

---

## PART 6 — QUALITY FLOOR

- Touch targets ≥ 44px; primary actions ≥ 64px. Assume gloves.
- Every critical action reachable one-handed from the screen's lower third.
- Full contrast compliance against `--ground` for all text; `--bone` on `--ground` is 13.6:1.
- Colorblind-safe: every state carries shape and text, never hue alone.
- `prefers-reduced-motion` respected — trace updates become instant redraws, no easing.
- Keyboard focus visible throughout for the attached keyboard.
- **Daylight mode:** a single toggle inverting to `--bone` ground with `--ground` text and heavier trace weights, for reading the lid screen in direct sun. Not a separate theme system — one inversion, tested.
- No scrolling required for anything in the Current block, Derived block, or alarm rail.
- Update latency: vitals to screen < 250 ms, matching the locked DF target.

---

## PART 7 — WHAT NOT TO BUILD

- No circular gauges. They cost space and communicate less than a numeral plus a trend.
- No animated pulse rings, no glowing heartbeats, no decorative ECG squiggles that aren't real data.
- No confidence percentages on the AI differential unless the model actually emits calibrated ones — a fabricated 87% is worse than no number.
- No modal dialogs for anything time-critical.
- No sound design in v1. A false alarm that wakes a household is a cost; earn the speaker later.
- No dark-mode/light-mode toggle beyond the daylight inversion. Two themes is two things to test.

---

**Build order:** alarm rail → Current + Derived blocks → flowsheet canvas → manual entry sheet → Screen B → Screen C → degraded states → daylight mode.
