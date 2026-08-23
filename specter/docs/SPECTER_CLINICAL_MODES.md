# SPECTER — CLINICAL OPERATING MODES v1.0

**Supersedes:** the single-mode assumption in `SPECTER_MEDICAL_UI_BRIEF.md`
**Reason for revision:** the medical system was designed around two known patients with standing profiles. The actual mission includes trauma, mass casualty, and sustained convalescent care. These are three different problems with three different data models, three different UIs, and three different alarm logics.

---

## PART 0 — THE THREE MODES

| | RESUS | WARD | CHRONIC |
|---|---|---|---|
| **Timescale** | Seconds to minutes | Hours to weeks | Months to years |
| **Patients** | Unknown, possibly many | 1–2, known | 2, known |
| **Question being asked** | What kills this person first? | Is this person getting better or worse? | Is this managed correctly? |
| **Primary artifact** | Checklist | Trend + care schedule | Differential + sourcing |
| **Data records** | Ephemeral, numbered | Episode-scoped | Permanent profile |
| **AI role** | Minimal | High | High |
| **Probability of use** | Low | **Highest** | Certain |

**The mode-switch is a deliberate operator action, not an inference.** A three-position control in the alarm rail. The system never guesses which mode you're in, because guessing wrong costs either speed or safety.

**Note the probability row.** The most likely thing this system will ever do is monitor one person in bed with pneumonia or a bad GI illness for five days. That case was the least designed and is now specified first.

---

## PART 1 — WARD MODE (5-day bed care)

**STATUS: Implemented (August 2026).** The data model in 1.2, the alarm thresholds in 1.4, and the AI query pattern in 1.5 are built as specified in `specter/ward/specter_ward.py` and live at `/ward` (see `docs/MANUAL.md` Parts 5.2/7.1/4.5). The screen in 1.3 below is a simplified real implementation, not a pixel-for-pixel match of this mockup - no charted flowsheet yet (recent vitals render as a table instead), and multiple episodes are tabs across the top rather than a single fixed layout. Sections 1.1/1.2/1.4/1.5 remain the source of truth for *why* each number/threshold exists; treat this file as design intent that the real implementation should keep matching, not as documentation of current UI pixels.

### 1.1 What actually goes wrong in this window

Someone in bed for five days is not at risk from the illness alone. The predictable secondary harms, roughly in order of likelihood:

| Complication | Onset | What prevents it |
|---|---|---|
| **Dehydration** | 24–72 h | Measured intake, not estimated |
| **Pressure injury** | 2–6 h over a bony prominence | Repositioning every 2 h, skin checks |
| **Missed deterioration** | Any time | Scheduled vitals, trend not snapshot |
| **Secondary infection** | 48 h+ | **Elevated risk in both your patients** — line sites, urine, chest |
| **Aspiration** | Any time, worse when supine | Head of bed elevated 30°, cautious feeding |
| **DVT** | 72 h+ | Early mobilization, ankle pumps, hydration |
| **Constipation / urinary retention** | 48–72 h | Tracked output |
| **Delirium** | 48 h+, worse at night | Orientation, light/dark cycle, sleep |
| **Undernutrition** | 72 h+ | Caloric intake tracked, not assumed |

**Fluid balance is the highest-value tracked number in this mode**, and doubly so for a transplant recipient — graft function is exquisitely sensitive to both under- and over-filling. Running dry damages the kidney; overloading it strains a heart and floods lungs.

### 1.2 Ward data model

```
CareEpisode
  episode_id, patient_id, opened_utc, closed_utc, presenting_problem
  observation_interval_minutes   (default 240; 60 when NEWS2 >= 5)

FluidBalance                      running, per 24 h period
  intake[]   {utc, route: oral|iv, volume_ml, description}
  output[]   {utc, route: urine|emesis|stool|drain|blood, volume_ml, description}
  net_ml, cumulative_ml

CareTask                          scheduled, recurring
  task_id, type, interval_minutes, last_done_utc, next_due_utc,
  overdue_minutes, completed_by

  types: reposition | skin_check | vitals | oral_care | ankle_pumps |
         medication | fluid_offer | wound_check | temperature |
         orientation_check | bowel_check

SkinCheck
  utc, sites{sacrum, heels_l, heels_r, elbows_l, elbows_r, occiput,
             hips_l, hips_r, other}
  each site: intact | blanching_erythema | non_blanching | broken | photo_ref

NutritionLog
  utc, description, estimated_kcal, estimated_protein_g, percent_consumed

MobilityLog
  utc, level: bedbound | sat_edge | stood | walked_assisted | walked_alone
  duration_minutes
```

### 1.3 Ward screen

The flowsheet stays — it is still the right instrument for trends over days, just with the window defaulted to 24 h or 72 h rather than 6 h. What changes is what surrounds it.

```
┌──────────────────────────────────────────────────────────────┐
│ [ALARM RAIL]                        [RESUS][WARD•][CHRONIC]  │
├──────────────────────────────────────────────────────────────┤
│  DAY 3 OF EPISODE · pneumonia, community acquired            │
│  NEXT OBS 47m   ·   NEWS2 3 (was 5 yesterday)                │
├───────────────────────────┬──────────────────────────────────┤
│  FLUID BALANCE 24h        │  CARE DUE                        │
│  ┌─────────────────────┐  │  ⚠ Reposition        18m OVERDUE │
│  │ IN   1,850 mL       │  │    Skin check         1h 12m     │
│  │ OUT  1,240 mL       │  │    Vitals             47m        │
│  │ NET  + 610 mL       │  │    Prednisone         2h 30m     │
│  │ ▓▓▓▓▓▓▓▓░░ 74%      │  │    Fluid offer        22m        │
│  └─────────────────────┘  │  [Mark done]                     │
│  Cumulative +1,340 mL /3d │                                  │
├───────────────────────────┴──────────────────────────────────┤
│  FLOWSHEET                            [24h] [48h] [72h] [ALL]│
│  ... same stacked lanes, longer window ...                   │
├──────────────────────────────────────────────────────────────┤
│  SKIN  sacrum ● heels ● elbows ● occiput ●   last check 1h   │
│  MOBILITY  sat edge of bed 2× today · 12m total              │
│  INTAKE  breakfast 60% · lunch 40% · ~980 kcal               │
└──────────────────────────────────────────────────────────────┘
```

**Care Due panel is the operative element.** It is a countdown list, not a checklist — tasks surface as they approach due and go `--critical` when overdue. Repositioning overdue by 30 minutes is a real alarm, because that is how pressure injuries begin, and it is the single most preventable harm on this list.

**Fluid balance bar** shows the net against a target band, not a raw number alone. Cumulative multi-day balance is displayed underneath because a +200 mL day is fine and five of them in a row is not.

**Skin check row** uses four dots that go amber on blanching erythema and critical on non-blanching — because non-blanching erythema is already a stage 1 injury and the response is immediate offloading, not more frequent checking.

### 1.4 Ward alarm additions

| Condition | Level |
|---|---|
| Reposition overdue > 30 min | Critical |
| Non-blanching erythema at any site | Critical |
| Net fluid balance < −1,000 mL / 24 h | Critical (dehydration) |
| Net fluid balance > +1,500 mL / 24 h | Critical (overload — graft and lungs) |
| No urine output logged in 8 h | Critical |
| NEWS2 rising by ≥ 2 between consecutive observations | Critical — *deterioration trend beats absolute value* |
| Observation overdue > 60 min | Caution |
| Oral intake < 50% for 3 consecutive meals | Caution |
| No mobility logged in 24 h | Caution |
| Bowels not opened in 72 h | Caution |

### 1.5 AI role in ward mode

This is where MedGemma is genuinely most useful, and the query is not "what is this." It is:

- *"Day 3, NEWS2 went 5 → 3 → 5, temp trending up, intake dropping. Is this a secondary infection?"*
- *"Net positive 1.3 L over 3 days in a transplant recipient — concerned about overload. What am I looking for?"*
- *"She hasn't opened her bowels in 3 days and is on prednisone. What's safe to give?"*

The prompt builder should pass the **episode summary** — day number, presenting problem, NEWS2 series, fluid balance series, intake trend, care adherence — not just the latest vitals snapshot. Trend interpretation is the whole job here.

---

## PART 2 — RESUS MODE (trauma, mass casualty)

### 2.1 Design premise

**Trauma care is algorithmic, not diagnostic.** You do not need a differential for a gunshot wound; you need MARCH executed fast and in order. The screen's job in this mode is to be a checklist that survives panic and to timestamp everything so the record exists when someone eventually asks.

**MedGemma is close to irrelevant here** and should not be on the critical path. A 3–5 second inference is an eternity when someone is bleeding. Protocol screens are primary; the AI is available but never blocks.

### 2.2 Casualty data model

Casualties are **ephemeral and numbered**, never named. Names come later, if ever.

```
Casualty
  casualty_id            auto: C-1, C-2, C-3 ...
  found_utc, triaged_utc
  triage_category        IMMEDIATE | DELAYED | MINIMAL | EXPECTANT | DECEASED
  triage_history[]       {utc, category, by}       — retriage is expected
  mechanism              gsw | blast | fall | mvc | crush | burn | other
  age_estimate, sex_estimate
  interventions[]
  vitals[]               same schema as ward, sparser
  disposition            on_scene | moved | evacuated | died

Intervention
  utc, type, site, notes, applied_by
  types: tourniquet | wound_packing | pressure_dressing | chest_seal |
         needle_decompression | npa | igel | recovery_position |
         io_access | iv_access | txa | fluid | hypothermia_wrap |
         splint | pelvic_binder

TourniquetRecord                    — tracked separately, it has a clock
  utc_applied, limb, site, converted_utc, elapsed_minutes
```

**Tourniquet time is the single most important timestamp in trauma.** Concern begins around 2 hours; limb viability drops sharply past 6. The system must surface elapsed time continuously and unmissably, because in a chaotic scene it is the thing everyone forgets. Write the time on the tourniquet windlass *and* log it — the device marking is the ground truth if the electronics fail.

### 2.3 Triage screen

Card grid, one card per casualty, sorted by category then by time since last assessment.

```
┌──────────────────────────────────────────────────────────────┐
│ MASS CASUALTY · 4 CASUALTIES · 00:14:22 ELAPSED              │
│                                     [RESUS•][WARD][CHRONIC]  │
├──────────────────────────────────────────────────────────────┤
│ ┏━━━━━━━━━━━━━━━┓ ┏━━━━━━━━━━━━━━━┓ ┌───────────────┐        │
│ ┃ C-1 IMMEDIATE ┃ ┃ C-2 IMMEDIATE ┃ │ C-3 DELAYED   │        │
│ ┃ GSW L thigh   ┃ ┃ blast, chest  ┃ │ fall, arm     │        │
│ ┃ ⏱ TQ 00:47:12 ┃ ┃ seal ×2       ┃ │ splinted      │        │
│ ┃ HR 128 ↑      ┃ ┃ RR 32 ↑       ┃ │ HR 92         │        │
│ ┃ SI 1.24 ⚠     ┃ ┃ decomp ×1     ┃ │ last chk 6m   │        │
│ ┃ last chk 2m   ┃ ┃ last chk 4m   ┃ │               │        │
│ ┗━━━━━━━━━━━━━━━┛ ┗━━━━━━━━━━━━━━━┛ └───────────────┘        │
│ ┌───────────────┐                                            │
│ │ C-4 MINIMAL   │   [+ ADD CASUALTY]                         │
│ │ abrasions     │                                            │
│ └───────────────┘                                            │
└──────────────────────────────────────────────────────────────┘
```

Category is carried by **border weight and a text label**, not color alone — the conventional red/yellow/green mapping is preserved but never load-bearing on its own.

**Tourniquet clocks run on the card face.** Amber at 2 h, critical at 4 h.

**`last chk` is the discipline mechanism.** A casualty not reassessed in 10 minutes surfaces to the top with a caution state. Casualties deteriorate quietly while you are working on someone else; this is the countermeasure.

**Adding a casualty must take one tap.** Category and mechanism can be set afterward — never block the record's creation on classification.

### 2.4 MARCH protocol screen

Tap a casualty card, get MARCH. Five sections, sequential, each expanding to actions.

```
M  MASSIVE HEMORRHAGE                              [2 done]
   ☑ Tourniquet — L thigh, high and tight   00:47:12
   ☑ Wound packing — hemostatic gauze
   ☐ Pelvic binder
   ☐ Reassess bleeding control

A  AIRWAY                                          [1 done]
   ☑ NPA inserted
   ☐ Recovery position
   ☐ i-gel

R  RESPIRATION                                     [0 done]
   ☐ Expose and assess chest
   ☐ Vented chest seal — entry
   ☐ Vented chest seal — exit
   ☐ Needle decompression — 5th ICS AAL
   ☐ Reassess after seal — tension can develop under a seal

C  CIRCULATION                                     [0 done]
   ☐ IO access — proximal tibia
   ☐ TXA
   ☐ Fluid
   ☐ Reassess distal pulse below tourniquet

H  HYPOTHERMIA / HEAD                              [0 done]
   ☐ Insulate from ground
   ☐ Active warming
   ☐ Cover head
   ☐ AVPU
```

**Every checkbox timestamps on tap.** No confirmation dialog, no undo prompt — a mis-tap is corrected by tapping again, and the log keeps both events. Speed beats tidiness.

**Order is enforced visually but never blocked.** M sits above A because that is the order that saves lives, but the operator can work any section at any time. The system does not know what it is looking at; the operator does.

**Reassess steps are first-class checklist items, not reminders.** A chest seal can produce a tension pneumothorax; a tourniquet can loosen. The protocol that does not build in reassessment is the protocol that fails on the second look.

### 2.5 Hypothermia deserves its own note

It reads like the afterthought at the end of MARCH and it is not. Hypothermia, acidosis, and coagulopathy form the lethal triad, and a cold trauma patient stops clotting regardless of what you packed the wound with. A bleeding patient loses temperature fast, even in warm weather, even indoors, and the intervention is cheap: get them off the ground, wrap them, cover the head. **This is the highest benefit-to-cost item in the entire trauma kit** and it is usually the first thing forgotten.

Give it a persistent temperature readout on the casualty card and a hypothermia timer that starts on casualty creation.

---

## PART 3 — REVISED KIT

Current Tier 1–3 was built to monitor two immunosuppensed patients. It does not address trauma. Additions, by MARCH phase:

### Massive hemorrhage
| Item | Qty | Note |
|---|---|---|
| CAT Gen 7 tourniquet | 4 | Two per casualty is the standing guidance. Genuine CAT only — counterfeits fail under load. |
| Hemostatic gauze (kaolin) | 4 | Combat Gauze or Celox |
| Compressed gauze, plain | 6 | Packing volume |
| Israeli bandage 6" | 4 | |
| Pelvic binder (or improvised sheet) | 1 | Blunt trauma, suspected pelvic fracture |
| Trauma shears | 2 | One lives on the medical hub, one in the trauma kit |
| Permanent marker | 2 | Tourniquet time on the windlass. Redundancy for the electronics. |

### Airway
| Item | Qty | Note |
|---|---|---|
| NPA 28Fr + lube | 4 | Tolerated by semi-conscious patients; OPA is not |
| i-gel supraglottic, size 4 | 2 | **Training-gated** |

### Respiration
| Item | Qty | Note |
|---|---|---|
| Vented chest seal | 6 | Two per chest casualty — entry and exit |
| 14g × 3.25" decompression needle | 4 | Standard IV catheters are too short to reach the pleural space in most adults |

### Circulation
| Item | Qty | Note |
|---|---|---|
| EZ-IO driver + 25 mm and 45 mm needles | 1 set | Far more achievable than peripheral IV in a shocked patient — and you already need IV skills for the Belatacept |
| TXA 1 g vials | 4 | Prescription. Meaningful mortality benefit within 3 h, best given as early as possible. Discuss with your physician alongside the antibiotic stockpile. |
| 500 mL crystalloid | 4 | You already stock NaCl for infusions |

### Hypothermia
| Item | Qty | Note |
|---|---|---|
| Hypothermia prevention wrap | 2 | Purpose-built beats a space blanket |
| Chemical warming blanket | 4 | |
| Closed-cell foam pad | 2 | Ground insulation — conduction is the main loss path |

### Ward care (the 5-day case)
| Item | Qty | Note |
|---|---|---|
| Graduated urinal / measuring container | 2 | Fluid balance requires measurement, not estimation |
| Oral rehydration salts | 30 sachets | Highest value-per-gram item in the entire medical kit |
| Bed pan | 1 | |
| Barrier cream (zinc oxide) | 2 | Pressure area and moisture protection |
| Pressure-relief cushion or foam wedges | 2 | Offloading heels and sacrum |
| Thermometer probe covers | 200 | |
| Nitrile gloves | 200 | Both patients are immunosuppressed |
| Surgical masks | 50 | Protecting *them* from *you* |
| Alcohol hand gel | 4 | |

**Estimated addition: $900–1,200**, excluding TXA and the i-gel.

---

## PART 4 — TRAINING

**This is the binding constraint, not the purchasing.** Needle decompression performed wrong kills people. An i-gel in an inappropriate patient causes aspiration. Buying the kit without the training buys a false sense of readiness, which is worse than owning nothing.

| Course | Duration | Cost | Covers |
|---|---|---|---|
| **Stop the Bleed** | 2 h | Free | Tourniquet, packing, pressure. Do this first, this month. |
| **WFR** *(already locked)* | 70–80 h | ~$700 | Assessment, ward care, evacuation decisions |
| **TCCC-MP or equivalent** | 16–40 h | $300–800 | Chest seals, decompression, airway adjuncts, TXA |
| **IV/IO course or phlebotomy** | Varies | $200–600 | Self-cannulation for Belatacept *and* trauma access — one course serves both |

Order matters: Stop the Bleed now, WFR next, TCCC after, IV/IO alongside whichever fits your schedule.

---

## PART 5 — WHAT CHANGES IN THE CODE

**Medical hub (`specter_medical_hub.py`)**
- Remove `parse_alivecor_ecg` — fabricated, see prior correction
- Add mode awareness: publish to `shtf/medical/mode`, alter collection cadence per mode (Resus: on-demand only; Ward: on schedule; Chronic: 60 s)
- Add casualty registry with ephemeral IDs
- Add intervention logging with tourniquet clocks
- Add care task scheduler with overdue tracking
- Add fluid balance accumulator

**AI engine (`specter_medical_ai.py`)**
- `PatientProfile` becomes one of three context types: `PatientProfile` (chronic), `CareEpisode` (ward), `Casualty` (resus)
- Ward prompts carry the episode summary — NEWS2 series, fluid balance, intake trend, care adherence — not just the latest vitals
- Resus prompts are protocol lookups, non-blocking, never on the critical path
- Add an image path for MedGemma multimodal — wound photos, skin checks, ECG strips

**UI**
- Mode selector in the alarm rail
- Ward screen: fluid balance, care-due countdown, skin map, mobility, intake
- Resus screens: triage grid, MARCH checklist, casualty detail
- Protocol cards promoted from degraded-state fallback to a primary always-available screen

---

**Build order:** Ward mode first — it is the highest-probability scenario and the least designed. Resus second. The chronic mode already largely exists.

**Status (August 2026):** Ward mode built - see Part 1's status note. Resus stayed at real-logic-but-local-demo (`docs/MANUAL.md` Part 7.2) rather than being wired to live trauma MQTT state; that's still the next piece if this build order is followed through.
