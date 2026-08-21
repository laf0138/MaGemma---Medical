"""
Tests for dashboard/dashboard_server.py: the MQTT topic-matching dispatch
(_match/_on_message), the per-topic state handlers that update the shared
STATE dict pushed to the browser dashboard, and the plain HTTP routes.

socketio.emit() is mocked out via DashboardMQTT._push in most tests so
this suite checks SPECTER's own dispatch/state logic, not flask-socketio's
transport layer.
"""
import copy
import json

import pytest

import dashboard.dashboard_server as ds
from dashboard.dashboard_server import DashboardMQTT


# STATE is a module-level global mutated in place by the handlers under
# test - snapshot it once so every test starts from the same clean shape.
_INITIAL_STATE = copy.deepcopy(ds.STATE)


@pytest.fixture(autouse=True)
def reset_state():
    ds.STATE.clear()
    ds.STATE.update(copy.deepcopy(_INITIAL_STATE))
    yield


@pytest.fixture
def mqtt(monkeypatch):
    m = DashboardMQTT(broker="192.168.1.1", port=1883)
    pushed = []
    monkeypatch.setattr(m, "_push", lambda event, data: pushed.append((event, data)))
    m.pushed = pushed
    return m


def msg(topic: str, payload) -> object:
    class Msg:
        pass
    x = Msg()
    x.topic = topic
    x.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return x


# ---------------------------------------------------------------------------
# _match - MQTT single-level wildcard topic matching
# ---------------------------------------------------------------------------

class TestMatch:
    def test_exact_match(self):
        assert DashboardMQTT._match("shtf/tx/status", "shtf/tx/status") is True

    def test_exact_mismatch(self):
        assert DashboardMQTT._match("shtf/tx/status", "shtf/rx/status") is False

    def test_plus_wildcard_matches_one_segment(self):
        assert DashboardMQTT._match("shtf/pi/+/status", "shtf/pi/pi2/status") is True

    def test_plus_wildcard_does_not_match_across_multiple_segments(self):
        assert DashboardMQTT._match("shtf/pi/+/status", "shtf/pi/pi2/extra/status") is False

    def test_different_segment_counts_never_match_even_with_wildcard(self):
        assert DashboardMQTT._match("shtf/radar/contacts/+", "shtf/radar/contacts") is False

    def test_plus_wildcard_at_final_segment(self):
        assert DashboardMQTT._match("shtf/radar/contacts/+", "shtf/radar/contacts/c1") is True


# ---------------------------------------------------------------------------
# _parse
# ---------------------------------------------------------------------------

class TestParse:
    def test_valid_json_is_decoded_to_dict(self, mqtt):
        assert mqtt._parse(b'{"a": 1}') == {"a": 1}

    def test_non_json_payload_falls_back_to_decoded_string(self, mqtt):
        assert mqtt._parse(b"plain text status") == "plain text status"


# ---------------------------------------------------------------------------
# State handlers
# ---------------------------------------------------------------------------

class TestOnPiStatus:
    def test_records_last_seen_and_data(self, mqtt):
        mqtt._on_pi_status("shtf/pi/pi3/status", {"cpu": 50})
        assert ds.STATE["pis"]["pi3"]["data"] == {"cpu": 50}
        assert ds.STATE["pis"]["pi3"]["last_seen"] > 0
        # last_seen must travel with the push, not just live in STATE - the
        # client has no other way to judge node health over time (see
        # applyNodeHealthDot/refreshNodeHealth in dashboard.html).
        assert len(mqtt.pushed) == 1
        event, payload = mqtt.pushed[0]
        assert event == "pi_status"
        assert payload["pi"] == "pi3"
        assert payload["data"] == {"cpu": 50}
        assert payload["last_seen"] == ds.STATE["pis"]["pi3"]["last_seen"]


class TestOnDfBearing:
    def test_dict_payload_sets_bearing_and_confidence(self, mqtt):
        mqtt._on_df_bearing("shtf/df/bearing", {"bearing": 271, "confidence": 0.8})
        assert ds.STATE["df"]["bearing"] == 271
        assert ds.STATE["df"]["confidence"] == 0.8

    def test_non_dict_payload_is_used_as_bearing_with_zero_confidence(self, mqtt):
        mqtt._on_df_bearing("shtf/df/bearing", 180)
        assert ds.STATE["df"]["bearing"] == 180
        assert ds.STATE["df"]["confidence"] == 0


class TestOnRadarContact:
    def test_contact_id_taken_from_final_topic_segment(self, mqtt):
        mqtt._on_radar_contact("shtf/radar/contacts/c7", {"range_m": 500})
        assert ds.STATE["radar"]["contacts"]["c7"] == {"range_m": 500}
        assert mqtt.pushed == [("radar_update", {"id": "c7", "contact": {"range_m": 500}})]


class TestOnRxStatus:
    def test_dict_payload_updates_rx_state(self, mqtt):
        mqtt._on_rx_status("shtf/rx/status", {
            "recording": True, "last_file": "x.wav", "last_trigger": "vox", "capture_count": 3,
        })
        assert ds.STATE["rx"]["recording"] is True
        assert ds.STATE["rx"]["last_file"] == "x.wav"
        assert ds.STATE["rx"]["capture_count"] == 3

    def test_non_dict_payload_leaves_state_untouched(self, mqtt):
        before = dict(ds.STATE["rx"])
        mqtt._on_rx_status("shtf/rx/status", "not a dict")
        assert ds.STATE["rx"] == before


class TestOnRxEvent:
    def test_pushes_event_without_touching_state(self, mqtt):
        mqtt._on_rx_event("shtf/rx/event", {"filename": "cap.wav", "reason": "vox"})
        assert mqtt.pushed == [("rx_event", {"filename": "cap.wav", "reason": "vox"})]


class TestOnRxRecording:
    def test_string_one_sets_recording_true(self, mqtt):
        mqtt._on_rx_recording("shtf/rx/recording", "1")
        assert ds.STATE["rx"]["recording"] is True

    def test_string_zero_sets_recording_false(self, mqtt):
        ds.STATE["rx"]["recording"] = True
        mqtt._on_rx_recording("shtf/rx/recording", "0")
        assert ds.STATE["rx"]["recording"] is False

    def test_int_one_sets_recording_true(self, mqtt):
        # str(1) == "1", so the int form works even though the field is
        # normally published as a string flag over MQTT.
        mqtt._on_rx_recording("shtf/rx/recording", 1)
        assert ds.STATE["rx"]["recording"] is True


class TestOnTxStatus:
    def test_sets_raw_status_value(self, mqtt):
        mqtt._on_tx_status("shtf/tx/status", "transmitting")
        assert ds.STATE["tx"]["status"] == "transmitting"


class TestOnSdrStatus:
    def test_dict_with_devices_updates_state(self, mqtt):
        mqtt._on_sdr_status("shtf/sdr/status", {"devices": {"rtl0": "ok"}})
        assert ds.STATE["sdr"]["devices"] == {"rtl0": "ok"}

    def test_non_dict_payload_leaves_state_untouched(self, mqtt):
        mqtt._on_sdr_status("shtf/sdr/status", {"devices": {"rtl0": "ok"}})
        mqtt._on_sdr_status("shtf/sdr/status", "garbage")
        assert ds.STATE["sdr"]["devices"] == {"rtl0": "ok"}


class TestOnThermal:
    def test_dict_payload_merges_into_thermal_state(self, mqtt):
        mqtt._on_thermal("shtf/system/thermal", {"cpu_temp_c": 62})
        assert ds.STATE["thermal"]["cpu_temp_c"] == 62

    def test_non_dict_payload_leaves_state_untouched(self, mqtt):
        before = dict(ds.STATE["thermal"])
        mqtt._on_thermal("shtf/system/thermal", "garbage")
        assert ds.STATE["thermal"] == before


class TestOnAlarm:
    def test_alarm_recorded_and_pushed(self, mqtt):
        mqtt._on_alarm("shtf/system/alarm", {"level": "critical", "text": "thermal shutdown"})
        assert len(ds.STATE["alarms"]) == 1
        assert ds.STATE["alarms"][0]["data"] == {"level": "critical", "text": "thermal shutdown"}
        # The push must carry the same {time, data} shape as STATE["alarms"]
        # entries - a bare `data` push (the old behavior) gives the client
        # no real timestamp, so it falls back to "whenever the browser
        # happened to render it" instead of the actual event time.
        assert len(mqtt.pushed) == 1
        event, payload = mqtt.pushed[0]
        assert event == "alarm"
        assert payload["data"] == {"level": "critical", "text": "thermal shutdown"}
        assert payload["time"] == ds.STATE["alarms"][0]["time"]

    def test_alarm_history_bounded_to_last_20(self, mqtt):
        for i in range(25):
            mqtt._on_alarm("shtf/system/alarm", {"i": i})
        assert len(ds.STATE["alarms"]) == 20
        assert ds.STATE["alarms"][0]["data"] == {"i": 5}
        assert ds.STATE["alarms"][-1]["data"] == {"i": 24}


class TestOnSystemState:
    def test_dict_payload_merges_into_system_state(self, mqtt):
        mqtt._on_system_state("shtf/system/state", {"uptime": 12345})
        assert ds.STATE["system"]["uptime"] == 12345

    def test_non_dict_payload_leaves_state_untouched(self, mqtt):
        before = dict(ds.STATE["system"])
        mqtt._on_system_state("shtf/system/state", "garbage")
        assert ds.STATE["system"] == before


class TestOnWardEpisode:
    def test_dict_payload_relays_episodes_and_stamps_updated(self, mqtt):
        episodes = [{"episode_id": "W-1", "patient_id": "p1"}]
        mqtt._on_ward_episode("shtf/ward/episode", {
            "timestamp_utc": "2026-01-01T00:00:00+00:00", "episodes": episodes,
        })
        assert ds.STATE["ward"]["episodes"] == episodes
        assert ds.STATE["ward"]["updated"] > 0
        assert mqtt.pushed == [("ward_episode", ds.STATE["ward"])]

    def test_non_dict_payload_leaves_state_untouched(self, mqtt):
        before = dict(ds.STATE["ward"])
        mqtt._on_ward_episode("shtf/ward/episode", "garbage")
        assert ds.STATE["ward"] == before


class TestOnWardAlert:
    def test_list_payload_feeds_each_alert_through_on_alarm(self, mqtt):
        mqtt._on_ward_alert("shtf/ward/alert", [
            {"level": "critical", "text": "Reposition overdue by 40m"},
            {"level": "caution", "text": "No mobility logged in 25.0h"},
        ])
        assert len(ds.STATE["alarms"]) == 2
        assert ds.STATE["alarms"][0]["data"]["text"] == "Reposition overdue by 40m"

    def test_non_list_payload_does_not_raise(self, mqtt):
        mqtt._on_ward_alert("shtf/ward/alert", "garbage")
        assert ds.STATE["alarms"] == []


class TestOnMeshStatus:
    def test_dict_payload_updates_status_and_stamps_updated(self, mqtt):
        mqtt._on_mesh_status("shtf/mesh/status", {"state": "connected", "timestamp_utc": "x"})
        assert ds.STATE["mesh"]["status"] == "connected"
        assert ds.STATE["mesh"]["updated"] > 0
        assert mqtt.pushed == [("mesh_status", ds.STATE["mesh"])]

    def test_non_dict_payload_leaves_state_untouched(self, mqtt):
        before = dict(ds.STATE["mesh"])
        mqtt._on_mesh_status("shtf/mesh/status", "garbage")
        assert ds.STATE["mesh"]["status"] == before["status"]


class TestOnMeshInbound:
    def test_dict_payload_appended_and_pushed(self, mqtt):
        entry = {"from": "!abc123", "text": "copy that", "timestamp_utc": "x"}
        mqtt._on_mesh_inbound("shtf/mesh/inbound", entry)
        assert ds.STATE["mesh"]["messages"] == [entry]
        assert mqtt.pushed == [("mesh_message", entry)]

    def test_non_dict_payload_does_not_raise(self, mqtt):
        mqtt._on_mesh_inbound("shtf/mesh/inbound", "garbage")
        assert ds.STATE["mesh"]["messages"] == []

    def test_message_log_bounded_to_50(self, mqtt):
        for i in range(55):
            mqtt._on_mesh_inbound("shtf/mesh/inbound", {"from": "x", "text": str(i)})
        assert len(ds.STATE["mesh"]["messages"]) == 50
        assert ds.STATE["mesh"]["messages"][0]["text"] == "5"


class TestPublishWardCommand:
    """The dashboard MQTT credential is deliberately read-only across
    shtf/# except one narrow write exception for shtf/ward/command/# (see
    deploy/install_specter.py's ACL comment) - publish_ward_command is the
    only path that's allowed to use it, and only for known ward commands."""

    def test_publishes_to_correct_topic(self, mqtt):
        published = []
        mqtt._client = type("FakeClient", (), {
            "publish": lambda self, topic, payload, qos=0: published.append((topic, payload, qos))
        })()
        result = mqtt.publish_ward_command("complete_task", {"episode_id": "W-1", "task_id": "W-1-T1"})
        assert result is True
        assert len(published) == 1
        topic, payload, qos = published[0]
        assert topic == "shtf/ward/command/complete_task"
        assert json.loads(payload) == {"episode_id": "W-1", "task_id": "W-1-T1"}

    def test_rejects_unknown_command(self, mqtt):
        mqtt._client = type("FakeClient", (), {"publish": lambda self, *a, **k: None})()
        assert mqtt.publish_ward_command("delete_everything", {}) is False

    def test_returns_false_when_mqtt_not_connected(self, mqtt):
        mqtt._client = None
        assert mqtt.publish_ward_command("intake", {"episode_id": "W-1"}) is False


class _FakeMqttClient:
    def subscribe(self, *a, **k):
        pass


class TestBrokerConnectionStatus:
    """mqtt_connected in STATE/mqtt_status push must reflect the real
    broker connection - previously nothing tracked this at all and the
    dashboard footer's MQTT indicator was permanently static text."""

    def test_connect_with_rc_zero_sets_connected_true(self, mqtt):
        mqtt._on_broker_connect(_FakeMqttClient(), None, None, 0)
        assert ds.STATE["mqtt_connected"] is True

    def test_connect_with_nonzero_rc_leaves_connected_false(self, mqtt):
        mqtt._on_broker_connect(_FakeMqttClient(), None, None, 5)
        assert ds.STATE["mqtt_connected"] is False

    def test_disconnect_sets_connected_false(self, mqtt):
        mqtt._on_broker_connect(_FakeMqttClient(), None, None, 0)
        assert ds.STATE["mqtt_connected"] is True
        mqtt._on_broker_disconnect(_FakeMqttClient(), None, 1)
        assert ds.STATE["mqtt_connected"] is False


# ---------------------------------------------------------------------------
# _on_message dispatch
# ---------------------------------------------------------------------------

class TestOnMessageDispatch:
    def test_wildcard_topic_routes_to_pi_status_handler(self, mqtt):
        mqtt._on_message(None, None, msg("shtf/pi/pi9/status", {"cpu": 10}))
        assert "pi9" in ds.STATE["pis"]

    def test_wildcard_topic_routes_to_radar_contact_handler(self, mqtt):
        mqtt._on_message(None, None, msg("shtf/radar/contacts/c1", {"range_m": 20}))
        assert "c1" in ds.STATE["radar"]["contacts"]

    def test_exact_topic_routes_to_thermal_handler(self, mqtt):
        mqtt._on_message(None, None, msg("shtf/system/thermal", {"cpu_temp_c": 55}))
        assert ds.STATE["thermal"]["cpu_temp_c"] == 55

    def test_unmatched_topic_is_silently_ignored(self, mqtt):
        mqtt._on_message(None, None, msg("shtf/nonexistent/topic", {"x": 1}))
        assert mqtt.pushed == []


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    ds.app.config["TESTING"] = True
    return ds.app.test_client()


@pytest.fixture
def auth():
    """Basic Auth kwarg for the Flask test client, matching the real
    default credential (see TestDashboardAuth for the gate itself)."""
    return (ds.DASHBOARD_AUTH_USERNAME, ds.DASHBOARD_AUTH_PASSWORD)


class TestDashboardAuth:
    """
    HTTP Basic Auth on every route but /api/status - closes the gap where
    same-origin CORS/no-anonymous-MQTT stopped a page on another origin or
    host, but not a person already on the LAN pointing a browser straight
    at port 5000, who could otherwise read /api/state and invoke
    trigger_rx with nothing else required.
    """

    def test_index_rejects_no_credentials(self, client):
        resp = client.get("/")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers

    def test_index_rejects_wrong_credentials(self, client):
        resp = client.get("/", auth=(ds.DASHBOARD_AUTH_USERNAME, "wrong"))
        assert resp.status_code == 401

    def test_index_accepts_correct_credentials(self, client, auth):
        resp = client.get("/", auth=auth)
        assert resp.status_code == 200

    def test_api_state_requires_auth(self, client, auth):
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/state", auth=auth).status_code == 200

    def test_resus_requires_auth(self, client, auth):
        assert client.get("/resus").status_code == 401
        assert client.get("/resus", auth=auth).status_code == 200

    def test_api_status_is_the_deliberate_unauthenticated_exception(self, client):
        # scripts/health_check.sh polls this without credentials, the same
        # way a load balancer health check normally would - it carries no
        # patient or system state, just service/version/uptime/ok.
        resp = client.get("/api/status")
        assert resp.status_code == 200

    def test_default_password_triggers_startup_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="specter.dashboard"):
            ds._dashboard_auth_credentials()
        assert any("DEFAULT operator password" in r.message for r in caplog.records)


class TestHttpRoutes:
    def test_index_serves_dashboard_html(self, client, auth):
        resp = client.get("/", auth=auth)
        assert resp.status_code == 200

    def test_resus_route_serves_resus_html(self, client, auth):
        # docs/SPECTER_MEDICAL_UI_BRIEF.md and the trauma docs both point
        # operators at /resus, but only "/", "/api/state", "/api/status"
        # were ever routed - the file was only reachable (if at all) via
        # Flask's static handler at a different, undocumented URL.
        resp = client.get("/resus", auth=auth)
        assert resp.status_code == 200
        assert b"<html" in resp.data.lower()

    def test_ward_route_serves_ward_html(self, client, auth):
        resp = client.get("/ward", auth=auth)
        assert resp.status_code == 200
        assert b"<html" in resp.data.lower()

    def test_api_state_returns_current_state(self, client, auth):
        ds.STATE["tx"]["status"] = "idle-test-marker"
        resp = client.get("/api/state", auth=auth)
        assert resp.status_code == 200
        assert resp.get_json()["tx"]["status"] == "idle-test-marker"

    def test_api_status_reports_ok(self, client):
        resp = client.get("/api/status")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["service"] == "specter-dashboard"
        assert data["ok"] is True

    def test_api_status_uptime_is_process_elapsed_not_epoch(self, client):
        # Previously "uptime" was int(time.time()) - the Unix epoch, not
        # elapsed time - so it read as billions of seconds of uptime.
        resp = client.get("/api/status")
        uptime = resp.get_json()["uptime"]
        assert 0 <= uptime < 3600  # test process has been up for seconds, not decades


class TestSecretKeyAndCors:
    def test_secret_key_is_not_the_old_hardcoded_value(self):
        # A secret hardcoded in source is the same value on every install
        # (it's in the git repo) - not a secret. Must be config-driven or
        # randomly generated instead.
        assert ds.app.config["SECRET_KEY"] != "specter-dashboard-key"

    def test_cors_allowed_origins_defaults_to_same_origin_only(self):
        # cors_allowed_origins="*" let any origin's page drive this
        # dashboard's WebSocket. None (flask-socketio's same-origin
        # default) unless the installer explicitly configures a trusted
        # origin list in specter.json.
        assert ds.socketio.server.eio.cors_allowed_origins != "*"
