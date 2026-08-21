#!/usr/bin/env python3
"""
SPECTER Trauma Scene Monitor
A readable terminal view of the trauma service. Subscribes to shtf/trauma/scene
and redraws a triage board on every update.

This is a functional prototype of the RESUS triage screen - same sort order,
same alert logic, same clocks - rendered in a terminal instead of on the lid
display.

Usage:
    python3 specter_trauma_monitor.py --mqtt-host 127.0.0.1
"""

import os
import sys
import json
import argparse
from pathlib import Path

import paho.mqtt.client as mqtt

# --- paho-mqtt 1.x / 2.x compatibility -------------------------------------
def _mqtt_client(client_id: str = ""):
    """Construct an MQTT client that works on paho-mqtt 1.x and 2.x."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x has no CallbackAPIVersion - fall back to the
        # old-style constructor (deprecated but functional on 2.x too),
        # NOT a recursive call to this same function, which would hit the
        # same AttributeError every time and blow the stack.
        return mqtt.Client(client_id=client_id)
# ---------------------------------------------------------------------------

# --- MQTT auth --------------------------------------------------------------
# See docs/MANUAL.md Part 3.3 - the broker requires auth. This is an
# operator-invoked CLI viewer, not a systemd service, so it shares the
# broad "operator" role rather than getting its own dedicated ACL account.
MQTT_DEFAULT_USERNAME = "specter-operator"
MQTT_DEFAULT_PASSWORD = "specter-change-me"


def _mqtt_credentials() -> tuple:
    """Read MQTT username/password from /etc/specter/specter.json (written
    by the installer) if available, else fall back to the documented default."""
    try:
        cfg = json.loads(Path("/etc/specter/specter.json").read_text())
        mqtt_cfg = cfg.get("mqtt", {})
        return (
            mqtt_cfg.get("username", MQTT_DEFAULT_USERNAME),
            mqtt_cfg.get("password", MQTT_DEFAULT_PASSWORD),
        )
    except Exception:
        return MQTT_DEFAULT_USERNAME, MQTT_DEFAULT_PASSWORD
# ---------------------------------------------------------------------------

# ANSI. Category is carried by label AND symbol, never color alone - same rule
# as the real display, because color fails in sunlight and for some operators.
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
GREY = "\033[90m"
CYAN = "\033[96m"
WHITE = "\033[97m"

CATEGORY_STYLE = {
    "IMMEDIATE": (RED, "###"),
    "DELAYED":   (YELLOW, "=="),
    "MINIMAL":   (GREEN, "--"),
    "EXPECTANT": (GREY, ".."),
    "DECEASED":  (GREY, "xx"),
}


def clear():
    os.system("clear" if os.name != "nt" else "cls")


def rule(char="-", width=78):
    return char * width


def render(scene):
    lines = []

    if not scene.get("scene_active") and scene.get("casualty_count", 0) == 0:
        lines.append("")
        lines.append(f"  {DIM}No active scene.{RESET}")
        lines.append("")
        lines.append(f"  {DIM}Open one:{RESET}")
        lines.append(f"  {DIM}mosquitto_pub -u \"$MQTT_USER\" -P \"$MQTT_PASSWORD\" "
                     f"-t shtf/trauma/command/open_scene -m '{{}}'{RESET}")
        lines.append("")
        return "\n".join(lines)

    counts = scene.get("counts_by_category", {})
    header = (
        f"{BOLD}SCENE {scene.get('scene_elapsed_display', '--:--:--')}{RESET}"
        f"   {scene.get('casualty_count', 0)} casualties   "
    )
    parts = []
    for cat in ("IMMEDIATE", "DELAYED", "MINIMAL", "EXPECTANT", "DECEASED"):
        n = counts.get(cat, 0)
        if n:
            color, _ = CATEGORY_STYLE[cat]
            parts.append(f"{color}{cat[:3]} {n}{RESET}")
    header += "  ".join(parts)

    lines.append("")
    lines.append("  " + header)
    lines.append("  " + rule("="))

    # Scene-level alerts first - this is the thing you must not miss
    alerts = scene.get("alerts", [])
    if alerts:
        lines.append("")
        for a in alerts:
            color = RED if a["level"] == "critical" else YELLOW
            tag = "CRITICAL" if a["level"] == "critical" else "CAUTION "
            lines.append(
                f"  {color}{BOLD}[{tag}]{RESET} {color}{a['casualty_id']}: {a['text']}{RESET}"
            )
        lines.append("")
        lines.append("  " + rule("="))

    for c in scene.get("casualties", []):
        cat = c.get("triage_category", "DELAYED")
        color, symbol = CATEGORY_STYLE.get(cat, (WHITE, "??"))

        lines.append("")
        stale_mark = f"  {YELLOW}[STALE]{RESET}" if c.get("stale") else ""
        lines.append(
            f"  {color}{BOLD}{symbol} {c['casualty_id']}  {cat}{RESET}"
            f"   {DIM}{c.get('mechanism', '').upper()}"
            f"   found {c.get('elapsed_display', '--:--:--')} ago{RESET}{stale_mark}"
        )

        if c.get("notes"):
            lines.append(f"       {DIM}{c['notes']}{RESET}")

        # Tourniquet clocks - the timestamp that drives decisions
        for tq in c.get("tourniquets", []):
            lvl = tq.get("alert_level", "normal")
            tq_color = {
                "critical": RED, "caution": YELLOW,
                "converted": GREY, "normal": CYAN,
            }.get(lvl, CYAN)
            status = " CONVERTED" if tq.get("converted_utc") else ""
            lines.append(
                f"       {tq_color}TQ {tq.get('limb', '?'):<12} "
                f"{tq.get('elapsed_display', '--:--:--')}{status}{RESET}"
            )

        # Vitals + shock index
        v = c.get("latest_vitals") or {}
        if v:
            bits = []
            if v.get("bp_systolic"):
                bits.append(f"BP {v['bp_systolic']}/{v.get('bp_diastolic', '--')}")
            if v.get("pulse"):
                bits.append(f"HR {v['pulse']}")
            if v.get("spo2"):
                bits.append(f"SpO2 {v['spo2']}%")
            if v.get("respiratory_rate"):
                bits.append(f"RR {v['respiratory_rate']}")
            si = c.get("shock_index")
            if si is not None:
                si_color = RED if si > 1.0 else (YELLOW if si > 0.9 else GREEN)
                bits.append(f"{si_color}SI {si}{RESET}")
            lines.append("       " + "   ".join(bits))
        else:
            lines.append(f"       {DIM}no vitals recorded{RESET}")

        # Interventions
        ivs = c.get("interventions", [])
        if ivs:
            summary = {}
            for i in ivs:
                summary[i["type"]] = summary.get(i["type"], 0) + 1
            txt = ", ".join(
                f"{k.replace('_', ' ')}{f' x{v}' if v > 1 else ''}"
                for k, v in summary.items()
            )
            lines.append(f"       {DIM}done: {txt}{RESET}")

        if not c.get("hypothermia_managed"):
            lines.append(f"       {DIM}hypothermia prevention not logged{RESET}")

        lines.append(
            f"       {DIM}last assessed {c.get('seconds_since_assessed', 0)}s ago{RESET}"
        )

    lines.append("")
    lines.append("  " + rule())
    lines.append(f"  {DIM}Ctrl-C to exit{RESET}")
    lines.append("")
    return "\n".join(lines)


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe("shtf/trauma/scene", qos=1)
    else:
        print(f"MQTT connect failed rc={rc}", file=sys.stderr)


def on_message(client, userdata, msg):
    try:
        scene = json.loads(msg.payload.decode())
    except Exception:
        return
    clear()
    print(render(scene))


def main():
    ap = argparse.ArgumentParser(description="SPECTER trauma scene monitor")
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    args = ap.parse_args()

    client = _mqtt_client("specter-trauma-monitor")
    client.username_pw_set(*_mqtt_credentials())
    client.on_connect = on_connect
    client.on_message = on_message

    clear()
    print(f"\n  {DIM}Connecting to {args.mqtt_host}:{args.mqtt_port}...{RESET}\n")

    client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        clear()
        print()


if __name__ == "__main__":
    main()
