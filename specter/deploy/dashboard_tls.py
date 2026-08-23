"""Pure configuration helpers for the SPECTER dashboard TLS proxy."""

from __future__ import annotations

import ipaddress
import textwrap
from pathlib import Path


def certificate_sans(public_ip: str) -> str:
    """Return a validated OpenSSL subjectAltName value."""
    address = ipaddress.ip_address(public_ip)
    return ",".join(
        [
            f"IP:{address}",
            "IP:127.0.0.1",
            "DNS:localhost",
            "DNS:specter.local",
        ]
    )


def openssl_certificate_command(
    certificate_path: Path,
    private_key_path: Path,
    public_ip: str,
) -> list[str]:
    """Build the non-interactive command for a ten-year local certificate."""
    return [
        "openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256",
        "-nodes", "-days", "3650",
        "-subj", "/CN=SPECTER Dashboard",
        "-addext", f"subjectAltName={certificate_sans(public_ip)}",
        "-keyout", str(private_key_path),
        "-out", str(certificate_path),
    ]


def nginx_site_config(certificate_path: Path, private_key_path: Path) -> str:
    """Return an HTTPS-only reverse-proxy configuration for Socket.IO."""
    return textwrap.dedent(f"""\
        server {{
            listen 80;
            listen [::]:80;
            server_name _;
            return 308 https://$host$request_uri;
        }}

        server {{
            listen 443 ssl;
            listen [::]:443 ssl;
            server_name _;

            ssl_certificate {certificate_path};
            ssl_certificate_key {private_key_path};
            ssl_protocols TLSv1.2 TLSv1.3;
            ssl_session_cache shared:SPECTER_TLS:10m;
            ssl_session_timeout 1d;
            ssl_session_tickets off;

            add_header Strict-Transport-Security "max-age=31536000" always;
            add_header X-Content-Type-Options "nosniff" always;
            add_header X-Frame-Options "DENY" always;
            add_header Referrer-Policy "no-referrer" always;

            location / {{
                proxy_pass http://127.0.0.1:5000;
                proxy_http_version 1.1;
                proxy_set_header Host $host;
                proxy_set_header X-Real-IP $remote_addr;
                proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                proxy_set_header X-Forwarded-Proto https;
                proxy_set_header Upgrade $http_upgrade;
                proxy_set_header Connection "upgrade";
                proxy_read_timeout 75s;
                proxy_send_timeout 75s;
            }}
        }}
    """)
