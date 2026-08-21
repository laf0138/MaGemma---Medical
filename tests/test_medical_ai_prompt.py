"""
Tests for the pieces of medical/specter_medical_ai.py that assemble the
prompt MedGemma actually sees: PatientProfile formatting, VitalsCache
(staleness, trend, reference-range annotation), GuidelineRetriever's
passage formatting, and PromptBuilder's assembly. A bug in any of these
doesn't crash the service - it just quietly changes what the model is
told about a patient, which is a much harder failure to notice.
"""
from datetime import datetime, timedelta, timezone

import pytest

from medical.specter_medical_ai import (
    Config,
    GuidelineRetriever,
    MEDICATION_SOURCING_BLOCK,
    PatientProfile,
    PromptBuilder,
    SYSTEM_PREAMBLE,
    VitalReading,
    VitalsCache,
    default_profiles,
)


def iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


# ---------------------------------------------------------------------------
# PatientProfile.to_prompt_block
# ---------------------------------------------------------------------------

class TestPatientProfilePromptBlock:
    def test_minimal_profile_only_has_patient_line(self):
        p = PatientProfile(patient_id="p1")
        block = p.to_prompt_block()
        assert block == "PATIENT: p1"

    def test_display_name_preferred_over_id(self):
        p = PatientProfile(patient_id="p1", display_name="Jane")
        assert block_first_line(p) == "PATIENT: Jane"

    def test_age_and_sex_line_present_when_either_set(self):
        p = PatientProfile(patient_id="p1", age=45)
        assert "Age/Sex: 45 / unknown" in p.to_prompt_block()

    def test_conditions_medications_allergies_joined_with_semicolons(self):
        p = PatientProfile(
            patient_id="p1",
            conditions=["A", "B"],
            medications=["X"],
            allergies=["Penicillin"],
        )
        block = p.to_prompt_block()
        assert "Conditions: A; B" in block
        assert "Medications: X" in block
        assert "Allergies: Penicillin" in block

    def test_critical_flags_each_on_own_bullet(self):
        p = PatientProfile(patient_id="p1", critical_flags=["Flag one", "Flag two"])
        block = p.to_prompt_block()
        assert "    - Flag one" in block
        assert "    - Flag two" in block

    def test_infusion_schedule_formatted_as_key_colon_value(self):
        p = PatientProfile(patient_id="p1", infusion_schedule={"Belatacept": "every 28 days"})
        assert "Infusion schedule: Belatacept: every 28 days" in p.to_prompt_block()

    def test_default_operator_profile_carries_immunosuppression_flag(self):
        # Regression guard: this is the profile actually shipped by default,
        # and the immunosuppression flag is the whole point of the prompt.
        profiles = default_profiles()
        block = profiles["operator"].to_prompt_block()
        assert "IMMUNOSUPPRESSED" in block


def block_first_line(profile: PatientProfile) -> str:
    return profile.to_prompt_block().splitlines()[0]


# ---------------------------------------------------------------------------
# VitalReading.age_seconds
# ---------------------------------------------------------------------------

class TestVitalReadingAge:
    def test_recent_reading_has_small_age(self):
        r = VitalReading(value=72, unit="bpm", timestamp_utc=iso())
        assert r.age_seconds() < 5

    def test_malformed_timestamp_is_infinite_age(self):
        r = VitalReading(value=72, unit="bpm", timestamp_utc="not-a-timestamp")
        assert r.age_seconds() == float("inf")


# ---------------------------------------------------------------------------
# VitalsCache
# ---------------------------------------------------------------------------

class TestVitalsCacheStorage:
    def test_update_then_latest_round_trips(self):
        cache = VitalsCache()
        reading = VitalReading(value=72, unit="bpm", timestamp_utc=iso())
        cache.update("p1", "pulse", reading)
        assert cache.latest("p1")["pulse"] is reading

    def test_latest_for_unknown_patient_is_empty(self):
        cache = VitalsCache()
        assert cache.latest("nobody") == {}

    def test_history_bounded_to_history_limit_keeping_most_recent(self):
        cache = VitalsCache()
        for i in range(30):
            cache.update("p1", "pulse", VitalReading(value=i, unit="bpm", timestamp_utc=iso()))
        hist = cache.history("p1", "pulse")
        assert len(hist) == VitalsCache.HISTORY_LIMIT
        assert hist[0].value == 30 - VitalsCache.HISTORY_LIMIT
        assert hist[-1].value == 29


class TestVitalsCacheAnnotate:
    def test_within_range(self):
        cache = VitalsCache()
        assert cache._annotate("pulse", 75) == "  [within reference range]"

    def test_below_range(self):
        cache = VitalsCache()
        assert cache._annotate("pulse", 40) == "  [below reference range]"

    def test_above_range(self):
        cache = VitalsCache()
        assert cache._annotate("pulse", 180) == "  [above reference range]"

    def test_unknown_reading_type_has_no_annotation(self):
        cache = VitalsCache()
        assert cache._annotate("ecg_rhythm", "sinus") == ""

    def test_non_numeric_value_has_no_annotation(self):
        cache = VitalsCache()
        assert cache._annotate("pulse", "thready") == ""


class TestVitalsCacheTrend:
    def test_fewer_than_three_readings_has_no_trend(self):
        cache = VitalsCache()
        cache.update("p1", "pulse", VitalReading(value=70, unit="bpm", timestamp_utc=iso()))
        cache.update("p1", "pulse", VitalReading(value=72, unit="bpm", timestamp_utc=iso()))
        assert cache._trend("p1", "pulse") == ""

    def test_rising_trend(self):
        cache = VitalsCache()
        for v in (70, 80, 95):
            cache.update("p1", "pulse", VitalReading(value=v, unit="bpm", timestamp_utc=iso()))
        trend = cache._trend("p1", "pulse")
        assert "rising 70 -> 95" in trend

    def test_falling_trend(self):
        cache = VitalsCache()
        for v in (100, 90, 80):
            cache.update("p1", "pulse", VitalReading(value=v, unit="bpm", timestamp_utc=iso()))
        trend = cache._trend("p1", "pulse")
        assert "falling 100 -> 80" in trend

    def test_stable_trend(self):
        cache = VitalsCache()
        for v in (80, 85, 80):
            cache.update("p1", "pulse", VitalReading(value=v, unit="bpm", timestamp_utc=iso()))
        assert cache._trend("p1", "pulse") == "  (trend: stable)"


class TestVitalsCachePromptBlock:
    def test_no_readings_tells_model_not_to_assume_normal(self):
        cache = VitalsCache()
        block = cache.to_prompt_block("p1")
        assert "none received" in block
        assert "Do not assume normal values" in block

    def test_stale_reading_is_flagged(self):
        cache = VitalsCache(stale_seconds=60)
        cache.update("p1", "pulse", VitalReading(
            value=72, unit="bpm", timestamp_utc=iso(-timedelta(minutes=5))))
        block = cache.to_prompt_block("p1")
        assert "STALE" in block
        assert "one or more readings are stale" in block

    def test_fresh_reading_is_not_flagged_stale(self):
        cache = VitalsCache(stale_seconds=600)
        cache.update("p1", "pulse", VitalReading(value=72, unit="bpm", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "STALE" not in block

    def test_temperature_f_is_suppressed_when_c_is_canonical(self):
        cache = VitalsCache()
        cache.update("p1", "temperature_c", VitalReading(value=37.0, unit="C", timestamp_utc=iso()))
        cache.update("p1", "temperature_f", VitalReading(value=98.6, unit="F", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "Temperature (F)" not in block

    def test_missing_core_vitals_are_listed(self):
        cache = VitalsCache()
        cache.update("p1", "pulse", VitalReading(value=72, unit="bpm", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "NOT MEASURED" in block
        assert "Blood pressure (systolic)" in block
        assert "SpO2" in block

    def test_no_missing_line_when_all_core_vitals_present(self):
        cache = VitalsCache()
        for rtype, val, unit in (
            ("bp_systolic", 120, "mmHg"),
            ("pulse", 72, "bpm"),
            ("spo2", 98, "%"),
            ("temperature_c", 37.0, "C"),
        ):
            cache.update("p1", rtype, VitalReading(value=val, unit=unit, timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "NOT MEASURED" not in block

    def test_derived_metrics_section_appears_with_bp_and_hr(self):
        cache = VitalsCache()
        cache.update("p1", "bp_systolic", VitalReading(value=90, unit="mmHg", timestamp_utc=iso()))
        cache.update("p1", "bp_diastolic", VitalReading(value=60, unit="mmHg", timestamp_utc=iso()))
        cache.update("p1", "pulse", VitalReading(value=110, unit="bpm", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "DERIVED METRICS" in block
        assert "MAP: 70.0 mmHg" in block
        assert "Pulse pressure: 30.0 mmHg" in block
        assert "Shock index: 1.22" in block

    def test_derived_metrics_section_absent_with_no_readings(self):
        cache = VitalsCache()
        block = cache.to_prompt_block("p1")
        assert "DERIVED METRICS" not in block

    def test_news2_and_qsofa_are_marked_partial_in_prompt(self):
        cache = VitalsCache()
        cache.update("p1", "bp_systolic", VitalReading(value=85, unit="mmHg", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "NEWS2:" in block
        assert "PARTIAL" in block
        assert "qSOFA:" in block
        assert "cannot rule out positive" in block


class TestVitalsCacheEcgFields:
    """The Polar H10 / NeuroKit2 layer (medical/ecg_analysis.py) publishes
    both raw sample arrays and derived scalar metrics as vitals readings.
    Only the derived scalars belong in a text prompt - see
    RAW_ARRAY_READING_TYPES in specter_medical_ai.py."""

    def test_raw_ecg_waveform_is_excluded_from_prompt(self):
        cache = VitalsCache()
        cache.update("p1", "ecg_waveform_uv", VitalReading(
            value=[1, 2, 3, 4, 5], unit="uV", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "ecg_waveform_uv" not in block
        assert "[1, 2, 3" not in block

    def test_raw_rr_intervals_are_excluded_from_prompt(self):
        cache = VitalsCache()
        cache.update("p1", "rr_intervals_ms", VitalReading(
            value=[810, 795, 820], unit="ms", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "rr_intervals_ms" not in block
        assert "810" not in block

    def test_derived_qrs_duration_appears_with_label_and_unit(self):
        cache = VitalsCache()
        cache.update("p1", "ecg_qrs_duration_ms", VitalReading(
            value=84.6, unit="ms", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "ECG QRS duration: 84.6 ms" in block

    def test_derived_t_r_ratio_appears(self):
        cache = VitalsCache()
        cache.update("p1", "ecg_t_r_ratio", VitalReading(
            value=0.33, unit="ratio", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "ECG T/R amplitude ratio: 0.33 ratio" in block

    def test_advisory_flags_render_as_individual_lines(self):
        cache = VitalsCache()
        cache.update("p1", "ecg_advisory_flags", VitalReading(
            value=[
                "QRS duration 145ms is above the 120ms widened-QRS threshold - non-specific.",
                "T-wave amplitude is 0.80x the R-wave amplitude, above the 0.75x advisory threshold.",
            ],
            unit="text", timestamp_utc=iso(),
        ))
        block = cache.to_prompt_block("p1")
        assert "ECG advisory flags" in block
        assert "QRS duration 145ms is above the 120ms widened-QRS threshold" in block
        assert "T-wave amplitude is 0.80x the R-wave amplitude" in block

    def test_empty_advisory_flags_list_produces_no_output(self):
        cache = VitalsCache()
        cache.update("p1", "ecg_advisory_flags", VitalReading(
            value=[], unit="text", timestamp_utc=iso()))
        block = cache.to_prompt_block("p1")
        assert "ECG advisory flags" not in block


# ---------------------------------------------------------------------------
# VitalsCache.derived() - MAP / pulse pressure / shock index / NEWS2 /
# qSOFA / fever burden / delta-from-baseline. See docs/
# SPECTER_MEDICAL_UI_BRIEF.md 1.3 - these were previously documented but
# not implemented anywhere on the medical/chronic-patient path.
# ---------------------------------------------------------------------------

class TestVitalsCacheDerived:
    def test_missing_bp_gives_none_for_map_pp_shock_index(self):
        cache = VitalsCache()
        d = cache.derived("p1")
        assert d["map_mmhg"] is None
        assert d["pulse_pressure_mmhg"] is None
        assert d["shock_index"] is None

    def test_map_pulse_pressure_shock_index_computed_from_bp_and_hr(self):
        cache = VitalsCache()
        cache.update("p1", "bp_systolic", VitalReading(value=120, unit="mmHg", timestamp_utc=iso()))
        cache.update("p1", "bp_diastolic", VitalReading(value=80, unit="mmHg", timestamp_utc=iso()))
        cache.update("p1", "pulse", VitalReading(value=72, unit="bpm", timestamp_utc=iso()))
        d = cache.derived("p1")
        assert d["map_mmhg"] == pytest.approx(93.3)
        assert d["pulse_pressure_mmhg"] == 40.0
        assert d["shock_index"] == 0.6

    def test_news2_and_qsofa_always_partial_without_rr_or_consciousness(self):
        cache = VitalsCache()
        cache.update("p1", "bp_systolic", VitalReading(value=120, unit="mmHg", timestamp_utc=iso()))
        cache.update("p1", "spo2", VitalReading(value=98, unit="%", timestamp_utc=iso()))
        cache.update("p1", "pulse", VitalReading(value=72, unit="bpm", timestamp_utc=iso()))
        cache.update("p1", "temperature_c", VitalReading(value=37.0, unit="C", timestamp_utc=iso()))
        d = cache.derived("p1")
        assert d["news2"]["partial"] is True
        assert set(d["news2"]["missing_parameters"]) == {"rr", "supplemental_o2", "avpu"}
        assert d["qsofa"]["partial"] is True
        assert d["qsofa"]["positive"] is None  # never asserted "not positive" when incomplete
        assert "rr" in d["qsofa"]["missing_parameters"]
        assert "altered_mentation" in d["qsofa"]["missing_parameters"]

    def test_no_vitals_at_all_still_returns_fully_missing_scores_not_a_crash(self):
        cache = VitalsCache()
        d = cache.derived("nobody")
        assert d["news2"]["risk"] == "unknown"
        assert d["news2"]["total"] == 0
        assert d["qsofa"]["total"] == 0
        assert d["fever_burden_minutes_24h"] is None
        assert d["delta_from_baseline"] == {}

    def test_derived_note_always_present_and_explains_the_gap(self):
        cache = VitalsCache()
        d = cache.derived("p1")
        assert "always partial" in d["note"]
        assert "not a persisted" in d["note"]


class TestVitalsCacheFeverBurden:
    def test_no_temperature_history_returns_none(self):
        cache = VitalsCache()
        assert cache.fever_burden_minutes("p1") is None

    def test_sustained_fever_accumulates_minutes(self):
        cache = VitalsCache()
        cache.update("p1", "temperature_c", VitalReading(
            value=38.5, unit="C", timestamp_utc=iso(-timedelta(minutes=30))))
        cache.update("p1", "temperature_c", VitalReading(
            value=38.5, unit="C", timestamp_utc=iso()))
        burden = cache.fever_burden_minutes("p1")
        assert burden == pytest.approx(30.0, abs=0.5)

    def test_afebrile_readings_contribute_no_burden(self):
        cache = VitalsCache()
        cache.update("p1", "temperature_c", VitalReading(
            value=37.0, unit="C", timestamp_utc=iso(-timedelta(minutes=30))))
        cache.update("p1", "temperature_c", VitalReading(
            value=37.0, unit="C", timestamp_utc=iso()))
        assert cache.fever_burden_minutes("p1") == 0.0

    def test_readings_older_than_window_are_excluded(self):
        cache = VitalsCache()
        cache.update("p1", "temperature_c", VitalReading(
            value=39.0, unit="C", timestamp_utc=iso(-timedelta(hours=30))))
        assert cache.fever_burden_minutes("p1") is None


class TestVitalsCacheDeltaFromBaseline:
    def test_fewer_than_three_readings_returns_none(self):
        cache = VitalsCache()
        cache.update("p1", "pulse", VitalReading(value=70, unit="bpm", timestamp_utc=iso()))
        cache.update("p1", "pulse", VitalReading(value=75, unit="bpm", timestamp_utc=iso()))
        assert cache.delta_from_baseline("p1", "pulse") is None

    def test_delta_computed_against_median_of_prior_readings(self):
        cache = VitalsCache()
        for v in (70, 72, 74, 100):
            cache.update("p1", "pulse", VitalReading(value=v, unit="bpm", timestamp_utc=iso()))
        d = cache.delta_from_baseline("p1", "pulse")
        assert d["current"] == 100
        assert d["baseline_median"] == 72
        assert d["delta"] == 28
        assert d["baseline_sample_size"] == 3


# ---------------------------------------------------------------------------
# GuidelineRetriever.to_prompt_block / kiwix_search_url (pure formatting,
# no ChromaDB required - constructor degrades gracefully when it's absent)
# ---------------------------------------------------------------------------

@pytest.fixture
def retriever():
    return GuidelineRetriever(Config(chroma_path="/does/not/exist"))


class TestGuidelineRetrieverPromptBlock:
    def test_no_passages_tells_model_to_rely_on_general_knowledge(self, retriever):
        block = retriever.to_prompt_block([])
        assert "no matching passages retrieved" in block

    def test_passage_includes_source_and_page(self, retriever):
        block = retriever.to_prompt_block([{"text": "some guidance", "source": "WHO IMCI", "page": 12}])
        assert "WHO IMCI, p.12" in block
        assert "some guidance" in block

    def test_passage_without_page_omits_page_suffix(self, retriever):
        block = retriever.to_prompt_block([{"text": "guidance", "source": "MSF"}])
        assert "MSF, p." not in block
        assert "[1] MSF" in block

    def test_long_passage_text_is_truncated(self, retriever):
        long_text = "word " * 400  # far more than 900 chars
        block = retriever.to_prompt_block([{"text": long_text, "source": "TM"}])
        assert "..." in block
        assert len(block) < len(long_text)

    def test_whitespace_in_passage_text_is_collapsed(self, retriever):
        block = retriever.to_prompt_block([{"text": "line one\n\n   line two", "source": "TM"}])
        assert "line one line two" in block


class TestKiwixSearchUrl:
    def test_query_is_url_encoded(self, retriever):
        url = retriever.kiwix_search_url("crush syndrome & shock")
        assert "crush+syndrome+%26+shock" in url
        assert url.startswith(retriever.cfg.kiwix_host)


# ---------------------------------------------------------------------------
# PromptBuilder.build
# ---------------------------------------------------------------------------

@pytest.fixture
def prompt_builder():
    cache = VitalsCache()
    cache.update("operator", "pulse", VitalReading(value=110, unit="bpm", timestamp_utc=iso()))
    return PromptBuilder(cache, default_profiles()), GuidelineRetriever(Config(chroma_path="/x"))


class TestPromptBuilderBuild:
    def test_sections_appear_in_documented_order(self, prompt_builder):
        builder, retriever = prompt_builder
        prompt = builder.build(
            patient_id="operator",
            user_query="fever and cough",
            passages=[{"text": "guidance", "source": "WHO"}],
            retriever=retriever,
        )
        preamble_idx = prompt.index(SYSTEM_PREAMBLE.strip().splitlines()[0])
        patient_idx = prompt.index("PATIENT: Operator")
        vitals_idx = prompt.index("PATIENT VITALS")
        reference_idx = prompt.index("REFERENCE MATERIAL")
        question_idx = prompt.index("OPERATOR'S QUESTION:")
        assert preamble_idx < patient_idx < vitals_idx < reference_idx < question_idx
        assert "fever and cough" in prompt

    def test_sourcing_block_included_when_requested(self, prompt_builder):
        builder, retriever = prompt_builder
        prompt = builder.build(
            patient_id="operator", user_query="q", passages=[], retriever=retriever,
            include_sourcing=True,
        )
        assert MEDICATION_SOURCING_BLOCK in prompt

    def test_sourcing_block_omitted_by_default(self, prompt_builder):
        builder, retriever = prompt_builder
        prompt = builder.build(
            patient_id="operator", user_query="q", passages=[], retriever=retriever,
        )
        assert MEDICATION_SOURCING_BLOCK not in prompt

    def test_unregistered_patient_falls_back_to_bare_profile(self, prompt_builder):
        builder, retriever = prompt_builder
        prompt = builder.build(
            patient_id="stranger", user_query="q", passages=[], retriever=retriever,
        )
        assert "PATIENT: stranger" in prompt

    def test_user_query_is_stripped(self, prompt_builder):
        builder, retriever = prompt_builder
        prompt = builder.build(
            patient_id="operator", user_query="   fever?   ", passages=[], retriever=retriever,
        )
        assert "OPERATOR'S QUESTION:\nfever?" in prompt
