"""Tests for the installer-generated dashboard TLS boundary."""

from pathlib import Path

import pytest

from deploy.dashboard_tls import (
    certificate_sans,
    nginx_site_config,
    openssl_certificate_command,
)


def test_certificate_sans_cover_operator_addresses():
    sans = certificate_sans("192.168.1.1")
    assert sans == (
        "IP:192.168.1.1,IP:127.0.0.1,DNS:localhost,DNS:specter.local"
    )


@pytest.mark.parametrize("value", ["unknown", "192.168.1.999", "host name"])
def test_certificate_sans_reject_invalid_addresses(value):
    with pytest.raises(ValueError):
        certificate_sans(value)


def test_openssl_command_is_noninteractive_and_uses_san(tmp_path):
    cert = tmp_path / "dashboard.crt"
    key = tmp_path / "dashboard.key"
    command = openssl_certificate_command(cert, key, "192.168.1.1")

    assert command[:4] == ["openssl", "req", "-x509", "-newkey"]
    assert "rsa:3072" in command
    assert command[command.index("-days") + 1] == "3650"
    assert command[command.index("-keyout") + 1] == str(key)
    assert command[command.index("-out") + 1] == str(cert)
    assert any(
        item.startswith("subjectAltName=IP:192.168.1.1") for item in command
    )


def test_nginx_proxy_redirects_http_and_keeps_backend_private():
    cert = Path("/etc/specter/tls/dashboard.crt")
    key = Path("/etc/specter/tls/dashboard.key")
    config = nginx_site_config(cert, key)

    assert "return 308 https://$host$request_uri;" in config
    assert "listen 443 ssl;" in config
    assert f"ssl_certificate {cert};" in config
    assert f"ssl_certificate_key {key};" in config
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
    assert 'Strict-Transport-Security "max-age=31536000" always;' in config
    assert "proxy_pass http://127.0.0.1:5000;" in config
    assert "proxy_set_header Upgrade $http_upgrade;" in config
    assert "proxy_set_header X-Forwarded-Proto https;" in config
    assert "proxy_pass http://0.0.0.0" not in config
