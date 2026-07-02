"""
shared_config.py
=================
Lets the Python dashboard (litime_monitor.py) read its MQTT / naming
settings directly out of config.h -- the SAME file the ESP32-S3 firmware
(LiTime_BMS_Display.ino) is compiled with. That way there is exactly ONE
file to edit (config.h) when you change the broker address, credentials,
topic prefix, or battery names; the Python app just parses the relevant
`#define`s out of it at startup.

No firmware changes needed and no build step required -- config.h stays
plain C and compiles exactly as before. This module simply does a very
small, forgiving regex parse of the subset of #define lines the Python
app cares about:

    MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS,
    MQTT_TOPIC_BASE, BMS1_NAME, BMS2_NAME

Any value it can't find falls back to the DEFAULTS below, so the app
still runs (with a warning printed to stderr) even if config.h is
missing, moved, or edited into an unparsable state.
"""
from __future__ import annotations

import os
import re
import sys

# Fallback values used if config.h is missing or a given #define can't be
# found/parsed -- keeps the app usable even without the shared file.
DEFAULTS = {
    "MQTT_BROKER":     "192.168.1.100",
    "MQTT_PORT":       1883,
    "MQTT_USER":       "",
    "MQTT_PASS":       "",
    "MQTT_TOPIC_BASE": "litime",
    "MQTT_VICTRON_TOPIC_BASE": "victron",
    "BMS1_NAME":       "Battery 1",
    "BMS2_NAME":       "Battery 2",
}

# Matches:  #define NAME  "quoted string"      -> group('str')
#           #define NAME  123                  -> group('num')
_DEFINE_RE = re.compile(
    r'^\s*#define\s+(?P<name>\w+)\s+'
    r'(?:"(?P<str>[^"]*)"|(?P<num>-?\d+))',
    re.MULTILINE,
)


def _parse_config_h(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    found = {}
    for m in _DEFINE_RE.finditer(text):
        name = m.group("name")
        if m.group("str") is not None:
            found[name] = m.group("str")
        elif m.group("num") is not None:
            found[name] = int(m.group("num"))
    return found


def load(config_h_path: str | None = None) -> dict:
    """Return a dict with MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS,
    MQTT_TOPIC_BASE, BMS1_NAME, BMS2_NAME -- read from config.h when
    possible, falling back to DEFAULTS for anything missing."""
    if config_h_path is None:
        config_h_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.h")

    cfg = dict(DEFAULTS)
    try:
        parsed = _parse_config_h(config_h_path)
        missing = [k for k in DEFAULTS if k not in parsed]
        cfg.update(parsed)
        if missing:
            print(f"shared_config: {config_h_path} did not define {missing}; "
                  f"using built-in defaults for those.", file=sys.stderr)
    except OSError as e:
        print(f"shared_config: could not read {config_h_path} ({e}); "
              f"using built-in defaults for everything.", file=sys.stderr)
    return cfg
