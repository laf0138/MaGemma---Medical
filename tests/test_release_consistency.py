"""Release-level checks for version, backend, and dependency consistency."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_component_versions_are_1_2_0():
    expected_assignments = [
        ("specter/core/mqtt_coordinator.py", 'VERSION = "1.2.0"'),
        ("specter/core/sdr_control.py", 'VERSION = "1.2.0"'),
        ("specter/core/thermal_monitor.py", 'VERSION = "1.2.0"'),
        ("specter/dashboard/dashboard_server.py", 'VERSION = "1.2.0"'),
        ("specter/deploy/clone_deploy.py", 'VERSION      = "1.2.0"'),
        ("specter/deploy/install_specter.py", 'VERSION = "1.2.0"'),
        ("specter/deploy/install_specter_library.py", 'VERSION       = "1.2.0"'),
        ("specter/services/library_api.py", 'VERSION         = "1.2.0"'),
        ("specter/services/specter_rx_ring_buffer.py", 'VERSION = "1.2.0"'),
    ]
    for relative_path, assignment in expected_assignments:
        assert assignment in (ROOT / relative_path).read_text(), relative_path

    assert json.loads((ROOT / "specter/config/specter.json").read_text())["version"] == "1.2.0"
    assert "'version': '1.2.0'" in (ROOT / "specter/deploy/build_payloads.sh").read_text()
    for relative_path in [
        "specter/medical/specter_medical_ai.py",
        "specter/medical/specter_medical_hub.py",
        "specter/trauma/specter_trauma.py",
        "specter/ward/specter_ward.py",
        "specter/mesh/specter_mesh_relay.py",
    ]:
        assert "Version: 1.2.0" in (ROOT / relative_path).read_text(), relative_path


def test_eventlet_is_absent_from_runtime_and_install_manifests():
    paths = [
        "requirements-dev.in",
        "requirements-dev.txt",
        "requirements-dev.lock.txt",
        "specter/deploy/clone_deploy.py",
        "specter/deploy/install_specter.py",
        "specter/deploy/install_specter_library.py",
    ]
    for relative_path in paths:
        assert "eventlet" not in (ROOT / relative_path).read_text().lower(), relative_path
    dashboard_source = (ROOT / "specter/dashboard/dashboard_server.py").read_text()
    assert 'async_mode="threading"' in dashboard_source


def test_dashboard_is_loopback_only_with_secure_cookie_and_https_edge():
    config = json.loads((ROOT / "specter/config/specter.json").read_text())
    assert config["dashboard"]["host"] == "127.0.0.1"
    assert config["dashboard"]["cookie_secure"] is True
    assert config["dashboard"]["external_url"].startswith("https://")

    dashboard_source = (ROOT / "specter/dashboard/dashboard_server.py").read_text()
    assert 'dash_cfg.get("host", "127.0.0.1")' in dashboard_source
    assert '.get("cookie_secure", True)' in dashboard_source

    installer = (ROOT / "specter/deploy/install_specter.py").read_text()
    assert '"host": "127.0.0.1"' in installer
    assert '"cookie_secure": True' in installer
    assert "configure_dashboard_tls(report)" in installer
    assert '"ward": {' in installer
    assert '("write", "shtf/ward/command/#")' in installer
    assert '("write", "shtf/medical/derived/#")' in installer
    assert 'SYSTEMD_UNITS["specter-ward.service"]' in installer
    assert 'SYSTEMD_UNITS["specter-mesh.service"]' in installer
    assert '"meshtastic==2.7.11"' in installer
    clone_deploy = (ROOT / "specter/deploy/clone_deploy.py").read_text()
    assert '"specter-mesh.service"' in clone_deploy
    assert '"meshtastic==2.7.11"' in clone_deploy


def test_resolved_lock_contains_only_exact_package_pins():
    requirement_lines = [
        line.strip()
        for line in (ROOT / "requirements-dev.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(requirement_lines) > 40
    assert all("==" in line.split(";", 1)[0] for line in requirement_lines)
