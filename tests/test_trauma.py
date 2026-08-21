from datetime import datetime, timedelta, timezone

import pytest

from trauma.specter_trauma import (
    Casualty,
    Mechanism,
    SceneRegistry,
    TourniquetRecord,
    TriageAssessment,
    TriageCategory,
    elapsed_seconds,
    fmt_elapsed,
)


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


# ---------------------------------------------------------------------------
# TriageAssessment.suggest() - START triage decision tree
# ---------------------------------------------------------------------------

class TestTriageSuggest:
    def test_walking_is_minimal_regardless_of_other_inputs(self):
        ta = TriageAssessment(walking=True, breathing=False)
        category, reason = ta.suggest()
        assert category == TriageCategory.MINIMAL

    def test_not_breathing_and_airway_reposition_fails_is_deceased(self):
        ta = TriageAssessment(walking=False, breathing=False,
                               breathing_after_airway_opened=False)
        category, _ = ta.suggest()
        assert category == TriageCategory.DECEASED

    def test_not_breathing_then_breathing_after_airway_opened_is_immediate(self):
        ta = TriageAssessment(walking=False, breathing=False,
                               breathing_after_airway_opened=True)
        category, _ = ta.suggest()
        assert category == TriageCategory.IMMEDIATE

    def test_not_breathing_airway_not_yet_assessed_is_immediate(self):
        ta = TriageAssessment(walking=False, breathing=False)
        category, _ = ta.suggest()
        assert category == TriageCategory.IMMEDIATE

    def test_respiratory_rate_over_30_is_immediate(self):
        ta = TriageAssessment(walking=False, breathing=True, respiratory_rate=31)
        category, _ = ta.suggest()
        assert category == TriageCategory.IMMEDIATE

    def test_respiratory_rate_exactly_30_is_not_immediate_on_that_basis(self):
        ta = TriageAssessment(walking=False, breathing=True, respiratory_rate=30,
                               radial_pulse_present=True, follows_commands=True)
        category, reason = ta.suggest()
        assert category == TriageCategory.DELAYED

    def test_no_radial_pulse_is_immediate(self):
        ta = TriageAssessment(walking=False, breathing=True, respiratory_rate=20,
                               radial_pulse_present=False)
        category, _ = ta.suggest()
        assert category == TriageCategory.IMMEDIATE

    def test_cap_refill_over_2s_is_immediate(self):
        ta = TriageAssessment(walking=False, breathing=True, respiratory_rate=20,
                               radial_pulse_present=True, cap_refill_seconds=2.5)
        category, _ = ta.suggest()
        assert category == TriageCategory.IMMEDIATE

    def test_cap_refill_exactly_2s_does_not_trigger_immediate(self):
        ta = TriageAssessment(walking=False, breathing=True, respiratory_rate=20,
                               radial_pulse_present=True, cap_refill_seconds=2.0,
                               follows_commands=True)
        category, _ = ta.suggest()
        assert category == TriageCategory.DELAYED

    def test_does_not_follow_commands_is_immediate(self):
        ta = TriageAssessment(walking=False, breathing=True, respiratory_rate=20,
                               radial_pulse_present=True, follows_commands=False)
        category, _ = ta.suggest()
        assert category == TriageCategory.IMMEDIATE

    def test_all_intact_is_delayed(self):
        ta = TriageAssessment(walking=False, breathing=True, respiratory_rate=18,
                               radial_pulse_present=True, follows_commands=True)
        category, reason = ta.suggest()
        assert category == TriageCategory.DELAYED
        assert "intact" in reason

    def test_incomplete_assessment_defaults_to_delayed_pending_reassessment(self):
        ta = TriageAssessment(walking=False, breathing=True)
        category, reason = ta.suggest()
        assert category == TriageCategory.DELAYED
        assert "incomplete" in reason.lower()


# ---------------------------------------------------------------------------
# TourniquetRecord - the clock that drives conversion decisions
# ---------------------------------------------------------------------------

class TestTourniquetRecord:
    def test_alert_level_normal_under_two_hours(self):
        tq = TourniquetRecord(tq_id="C-1-TQ1", limb="RLE", site="high-and-tight",
                               applied_utc=iso(-timedelta(hours=1, minutes=59)))
        assert tq.alert_level == "normal"

    def test_alert_level_caution_at_two_hours(self):
        tq = TourniquetRecord(tq_id="C-1-TQ1", limb="RLE", site="high-and-tight",
                               applied_utc=iso(-timedelta(hours=2, seconds=1)))
        assert tq.alert_level == "caution"

    def test_alert_level_caution_just_under_four_hours(self):
        tq = TourniquetRecord(tq_id="C-1-TQ1", limb="RLE", site="high-and-tight",
                               applied_utc=iso(-timedelta(hours=3, minutes=59)))
        assert tq.alert_level == "caution"

    def test_alert_level_critical_at_four_hours(self):
        tq = TourniquetRecord(tq_id="C-1-TQ1", limb="RLE", site="high-and-tight",
                               applied_utc=iso(-timedelta(hours=4, seconds=1)))
        assert tq.alert_level == "critical"

    def test_alert_level_converted_overrides_elapsed_time(self):
        tq = TourniquetRecord(tq_id="C-1-TQ1", limb="RLE", site="high-and-tight",
                               applied_utc=iso(-timedelta(hours=6)),
                               converted_utc=iso(-timedelta(hours=5)))
        assert tq.alert_level == "converted"

    def test_elapsed_uses_converted_time_not_now(self):
        applied = datetime.now(timezone.utc) - timedelta(hours=3)
        converted = applied + timedelta(minutes=45)
        tq = TourniquetRecord(tq_id="C-1-TQ1", limb="RLE", site="x",
                               applied_utc=applied.isoformat(),
                               converted_utc=converted.isoformat())
        assert tq.elapsed == pytest.approx(45 * 60, abs=1)

    def test_elapsed_malformed_timestamp_does_not_raise(self):
        tq = TourniquetRecord(tq_id="C-1-TQ1", limb="RLE", site="x",
                               applied_utc="not-a-timestamp")
        assert tq.elapsed == 0.0


# ---------------------------------------------------------------------------
# Casualty - staleness thresholds, shock index, alert surfacing
# ---------------------------------------------------------------------------

class TestCasualtyStale:
    @pytest.mark.parametrize("category,limit_seconds", [
        ("IMMEDIATE", 300),
        ("DELAYED", 900),
        ("MINIMAL", 1800),
        ("EXPECTANT", 900),
    ])
    def test_not_stale_just_under_threshold(self, category, limit_seconds):
        c = Casualty(casualty_id="C-1",
                      found_utc=iso(-timedelta(seconds=limit_seconds - 1)),
                      triage_category=category)
        assert c.stale is False

    @pytest.mark.parametrize("category,limit_seconds", [
        ("IMMEDIATE", 300),
        ("DELAYED", 900),
        ("MINIMAL", 1800),
        ("EXPECTANT", 900),
    ])
    def test_stale_just_over_threshold(self, category, limit_seconds):
        c = Casualty(casualty_id="C-1",
                      found_utc=iso(-timedelta(seconds=limit_seconds + 1)),
                      triage_category=category)
        assert c.stale is True

    def test_deceased_has_a_long_grace_period(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(-timedelta(hours=1)),
                      triage_category="DECEASED")
        assert c.stale is False


class TestCasualtyShockIndex:
    def test_no_vitals_is_none(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(timedelta()))
        assert c.shock_index is None

    def test_computes_hr_over_sbp_rounded(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(timedelta()),
                      vitals=[{"pulse": 110, "bp_systolic": 100}])
        assert c.shock_index == 1.1

    def test_zero_systolic_is_none_not_a_zero_division_error(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(timedelta()),
                      vitals=[{"pulse": 110, "bp_systolic": 0}])
        assert c.shock_index is None

    def test_non_numeric_vitals_are_none(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(timedelta()),
                      vitals=[{"pulse": "thready", "bp_systolic": 100}])
        assert c.shock_index is None

    def test_uses_latest_reading_only(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(timedelta()),
                      vitals=[{"pulse": 200, "bp_systolic": 100},
                              {"pulse": 80, "bp_systolic": 120}])
        assert c.shock_index == round(80 / 120, 2)


class TestCasualtyActiveAlerts:
    def test_critical_tourniquet_alert_present(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(timedelta()),
                      last_assessed_utc=iso(timedelta()),
                      tourniquets=[TourniquetRecord(
                          tq_id="C-1-TQ1", limb="RLE", site="x",
                          applied_utc=iso(-timedelta(hours=5)))])
        levels = [a["level"] for a in c.active_alerts]
        assert "critical" in levels

    def test_shock_index_above_one_is_critical(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(timedelta()),
                      last_assessed_utc=iso(timedelta()),
                      vitals=[{"pulse": 120, "bp_systolic": 100}])
        alerts = c.active_alerts
        assert any(a["level"] == "critical" and "Shock index" in a["text"] for a in alerts)

    def test_shock_index_between_point9_and_one_is_caution(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(timedelta()),
                      last_assessed_utc=iso(timedelta()),
                      vitals=[{"pulse": 95, "bp_systolic": 100}])
        alerts = c.active_alerts
        assert any(a["level"] == "caution" and "Shock index" in a["text"] for a in alerts)

    def test_minimal_category_does_not_surface_staleness_alert(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(-timedelta(hours=1)),
                      last_assessed_utc=iso(-timedelta(hours=1)),
                      triage_category="MINIMAL")
        assert not any("reassessed" in a["text"] for a in c.active_alerts)

    def test_immediate_stale_casualty_surfaces_staleness_alert(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(-timedelta(minutes=10)),
                      last_assessed_utc=iso(-timedelta(minutes=10)),
                      triage_category="IMMEDIATE")
        assert any("reassessed" in a["text"] for a in c.active_alerts)

    def test_hypothermia_alert_after_15_minutes_unmanaged(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(-timedelta(minutes=16)),
                      last_assessed_utc=iso(timedelta()),
                      hypothermia_managed=False)
        assert any("Hypothermia" in a["text"] for a in c.active_alerts)

    def test_no_hypothermia_alert_once_managed(self):
        c = Casualty(casualty_id="C-1", found_utc=iso(-timedelta(minutes=16)),
                      last_assessed_utc=iso(timedelta()),
                      hypothermia_managed=True)
        assert not any("Hypothermia" in a["text"] for a in c.active_alerts)


# ---------------------------------------------------------------------------
# SceneRegistry - casualty lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path):
    return SceneRegistry(persist_path=str(tmp_path / "scene.json"))


class TestSceneRegistry:
    def test_add_casualty_assigns_sequential_ids(self, registry):
        c1 = registry.add_casualty(mechanism="gsw")
        c2 = registry.add_casualty(mechanism="blast")
        assert c1.casualty_id == "C-1"
        assert c2.casualty_id == "C-2"

    def test_invalid_mechanism_falls_back_to_unknown(self, registry):
        c = registry.add_casualty(mechanism="werewolf-attack")
        assert c.mechanism == Mechanism.UNKNOWN.value

    def test_get_missing_casualty_returns_none(self, registry):
        assert registry.get("C-999") is None

    def test_triage_with_assessment_uses_suggested_category(self, registry):
        c = registry.add_casualty()
        registry.triage(c.casualty_id, assessment={
            "walking": False, "breathing": False,
            "breathing_after_airway_opened": False,
        })
        updated = registry.get(c.casualty_id)
        assert updated.triage_category == TriageCategory.DECEASED.value
        assert updated.triage_assessment["suggested"] == TriageCategory.DECEASED.value

    def test_explicit_category_overrides_suggested(self, registry):
        c = registry.add_casualty()
        registry.triage(c.casualty_id, category="MINIMAL", assessment={
            "walking": False, "breathing": False,
            "breathing_after_airway_opened": False,
        })
        updated = registry.get(c.casualty_id)
        assert updated.triage_category == "MINIMAL"
        # the algorithm's suggestion is still recorded even though overridden
        assert updated.triage_assessment["suggested"] == TriageCategory.DECEASED.value

    def test_invalid_explicit_category_falls_back_to_delayed(self, registry):
        c = registry.add_casualty()
        registry.triage(c.casualty_id, category="not-a-real-category")
        updated = registry.get(c.casualty_id)
        assert updated.triage_category == TriageCategory.DELAYED.value

    def test_triage_missing_casualty_returns_none(self, registry):
        assert registry.triage("C-999", category="MINIMAL") is None

    def test_triage_appends_history_entry(self, registry):
        c = registry.add_casualty()
        registry.triage(c.casualty_id, category="IMMEDIATE")
        registry.triage(c.casualty_id, category="DELAYED")
        updated = registry.get(c.casualty_id)
        assert len(updated.triage_history) == 2
        assert [h["category"] for h in updated.triage_history] == ["IMMEDIATE", "DELAYED"]

    def test_log_tourniquet_intervention_creates_tourniquet_record(self, registry):
        c = registry.add_casualty()
        registry.log_intervention(c.casualty_id, "tourniquet", site="RLE")
        updated = registry.get(c.casualty_id)
        assert len(updated.tourniquets) == 1
        assert updated.tourniquets[0].limb == "RLE"

    def test_log_hypothermia_wrap_sets_managed_flag(self, registry):
        c = registry.add_casualty()
        assert registry.get(c.casualty_id).hypothermia_managed is False
        registry.log_intervention(c.casualty_id, "hypothermia_wrap")
        assert registry.get(c.casualty_id).hypothermia_managed is True

    def test_log_intervention_sets_protocol_step(self, registry):
        c = registry.add_casualty()
        registry.log_intervention(c.casualty_id, "chest_seal", protocol_step="r2")
        assert registry.get(c.casualty_id).protocol_state.get("r2") is True

    def test_log_intervention_missing_casualty_returns_none(self, registry):
        assert registry.log_intervention("C-999", "tourniquet") is None

    def test_convert_tourniquet_sets_converted_timestamp(self, registry):
        c = registry.add_casualty()
        registry.log_intervention(c.casualty_id, "tourniquet", site="RLE")
        tq_id = registry.get(c.casualty_id).tourniquets[0].tq_id
        registry.convert_tourniquet(c.casualty_id, tq_id)
        assert registry.get(c.casualty_id).tourniquets[0].converted_utc is not None

    def test_convert_tourniquet_does_not_reconvert(self, registry):
        c = registry.add_casualty()
        registry.log_intervention(c.casualty_id, "tourniquet", site="RLE")
        tq_id = registry.get(c.casualty_id).tourniquets[0].tq_id
        registry.convert_tourniquet(c.casualty_id, tq_id)
        first_conversion = registry.get(c.casualty_id).tourniquets[0].converted_utc
        registry.convert_tourniquet(c.casualty_id, tq_id)
        assert registry.get(c.casualty_id).tourniquets[0].converted_utc == first_conversion

    def test_sorted_casualties_orders_immediate_before_delayed(self, registry):
        c1 = registry.add_casualty()
        c2 = registry.add_casualty()
        registry.triage(c1.casualty_id, category="DELAYED")
        registry.triage(c2.casualty_id, category="IMMEDIATE")
        ordered = registry.sorted_casualties()
        assert [c.casualty_id for c in ordered] == [c2.casualty_id, c1.casualty_id]

    def test_scene_summary_counts_by_category(self, registry):
        c1 = registry.add_casualty()
        c2 = registry.add_casualty()
        registry.triage(c1.casualty_id, category="IMMEDIATE")
        registry.triage(c2.casualty_id, category="IMMEDIATE")
        summary = registry.scene_summary()
        assert summary["counts_by_category"]["IMMEDIATE"] == 2
        assert summary["casualty_count"] == 2


# ---------------------------------------------------------------------------
# SceneRegistry persistence - restart must restore the active scene, not
# just write scene.json and never read it back (see _persist/_restore
# docstrings in specter_trauma.py).
# ---------------------------------------------------------------------------

class TestSceneRegistryPersistence:
    def test_restart_restores_full_casualty_state(self, tmp_path):
        path = str(tmp_path / "scene.json")
        r1 = SceneRegistry(persist_path=path)
        r1.open_scene()
        c = r1.add_casualty(mechanism="blast", notes="restore me")
        r1.triage(c.casualty_id, category="IMMEDIATE")
        r1.log_intervention(c.casualty_id, "tourniquet", site="right_leg", notes="windlass")
        r1.record_vitals(c.casualty_id, {"pulse": 130, "bp_systolic": 80})
        r1.record_vitals(c.casualty_id, {"pulse": 128, "bp_systolic": 82})

        r2 = SceneRegistry(persist_path=path)

        assert r2.scene_active is True
        restored = r2.get(c.casualty_id)
        assert restored is not None
        assert restored.mechanism == "blast"
        assert restored.notes == "restore me"
        assert restored.triage_category == "IMMEDIATE"
        # The full vitals history must survive, not just the latest reading -
        # scene_summary() (the old persisted shape) only keeps latest_vitals.
        assert len(restored.vitals) == 2
        assert restored.vitals[0]["pulse"] == 130
        assert restored.vitals[1]["pulse"] == 128
        assert len(restored.tourniquets) == 1
        assert restored.tourniquets[0].site == "right_leg"
        assert restored.tourniquets[0].applied_utc  # real timestamp survived

    def test_restart_does_not_reuse_casualty_ids(self, tmp_path):
        path = str(tmp_path / "scene.json")
        r1 = SceneRegistry(persist_path=path)
        r1.add_casualty()
        r1.add_casualty()

        r2 = SceneRegistry(persist_path=path)
        c3 = r2.add_casualty()
        assert c3.casualty_id == "C-3"

    def test_no_persisted_file_starts_with_empty_scene(self, tmp_path):
        r = SceneRegistry(persist_path=str(tmp_path / "does_not_exist.json"))
        assert r.casualties == {}
        assert r.scene_active is False

    def test_corrupt_persisted_file_starts_empty_without_raising(self, tmp_path):
        path = tmp_path / "scene.json"
        path.write_text("{not valid json")
        r = SceneRegistry(persist_path=str(path))
        assert r.casualties == {}

    def test_old_format_persisted_file_starts_empty_without_raising(self, tmp_path):
        # scene_summary()-shaped file (the previous, write-only "persistence"
        # format) - must not be misread as the new per-casualty format.
        path = tmp_path / "scene.json"
        path.write_text('{"scene_active": true, "casualties": [{"casualty_id": "C-1"}]}')
        r = SceneRegistry(persist_path=str(path))
        assert r.casualties == {}

    def test_persist_writes_atomically_no_leftover_tmp_file(self, tmp_path):
        path = tmp_path / "scene.json"
        r = SceneRegistry(persist_path=str(path))
        r.add_casualty()
        assert path.exists()
        assert not (tmp_path / "scene.json.tmp").exists()


# ---------------------------------------------------------------------------
# Small time helpers
# ---------------------------------------------------------------------------

class TestTimeHelpers:
    def test_elapsed_seconds_malformed_input_returns_zero(self):
        assert elapsed_seconds("garbage") == 0.0

    def test_fmt_elapsed_formats_hms(self):
        assert fmt_elapsed(3725) == "01:02:05"

    def test_fmt_elapsed_zero(self):
        assert fmt_elapsed(0) == "00:00:00"
