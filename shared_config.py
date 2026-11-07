"""
shared_config.py
=================
Lets the Python dashboard (litime_monitor.py) read MQTT / naming settings
from the ESP32 itself — the same /config.ini the firmware uses at runtime.

On launch it:

  1. Reads a tiny local dashboard_config.ini  (device hostname + optional
     Basic-auth credentials for /config.json). Defaults: host=litime.local.
  2. Fetches http://<host>/config.json from the device.
  3. Falls back to DEFAULTS (and a warning on stderr) if the device is
     unreachable, so the app still starts.

The dict keys it returns are unchanged from the old config.h parser, so
litime_monitor.py needs no changes.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from configparser import ConfigParser

DEFAULTS = {
    "MQTT_BROKER":     "192.168.1.100",
    "MQTT_PORT":       1883,
    "MQTT_USER":       "",
    "MQTT_PASS":       "",
    "MQTT_TOPIC_BASE": "litime",
    "MQTT_VICTRON_TOPIC_BASE": "victron",
    "BMS1_NAME":       "Battery 1",
    "BMS2_NAME":       "Battery 2",
    "WEATHER_URL":     "https://www.msn.com/en-us/weather/forecast/in-Phoenix,AZ",
    "SOC_RESERVE_PCT": 10,
    "ARRAY_NAME":       "Solar Array",
    "OTA_PASSWORD":    "",
    "HOSTNAME":        "litime",
}


def _local_ini_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "dashboard_config.ini")


def _read_device_target(path: str | None = None) -> tuple[str, str, str]:
    """Return (host, user, password) from dashboard_config.ini."""
    host, user, password = "litime.local", "admin", ""
    cfg = ConfigParser()
    ini = path or _local_ini_path()
    if not os.path.isfile(ini):
        return host, user, password
    try:
        cfg.read(ini, encoding="utf-8")
    except OSError as e:
        print(f"shared_config: could not read {ini} ({e})", file=sys.stderr)
        return host, user, password
    if cfg.has_section("device"):
        host     = cfg.get("device", "host",     fallback=host).strip() or host
        user     = cfg.get("device", "user",     fallback=user).strip() or user
        password = cfg.get("device", "password", fallback=password)
    return host, user, password


def _fetch_device_json(host: str, user: str, password: str, timeout: float = 3.0) -> dict:
    url = f"http://{host}/config.json"
    req = urllib.request.Request(url, headers={"User-Agent": "litime-monitor"})
    if password:
        import base64
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("config.json was not a JSON object")
    return data


# Set by load(): None when the device answered, else a short human-readable
# reason (e.g. "could not reach litime.local"). The dashboard shows it so a
# fallback to the built-in defaults is never mistaken for real (all-zero) data.
LOAD_ERROR: str | None = None
LOAD_HOST: str = "litime.local"


def load(config_h_path: str | None = None) -> dict:
    """Return MQTT / naming settings.

    `config_h_path` is accepted for backward compatibility and ignored —
    settings now come from the device, not from a compiled header.
    """
    global LOAD_ERROR, LOAD_HOST
    cfg = dict(DEFAULTS)
    host, user, password = _read_device_target()
    LOAD_HOST = host
    LOAD_ERROR = None
    try:
        parsed = _fetch_device_json(host, user, password)
        missing = [k for k in DEFAULTS if k not in parsed]
        cfg.update(parsed)
        if missing:
            print(f"shared_config: device {host} did not define {missing}; "
                  f"using built-in defaults for those.", file=sys.stderr)
        else:
            print(f"shared_config: loaded settings from http://{host}/config.json",
                  file=sys.stderr)
    except Exception as e:
        LOAD_ERROR = f"could not reach the device at {host}"
        print(f"shared_config: could not fetch http://{host}/config.json ({e}); "
              f"using built-in defaults. Edit dashboard_config.ini if the "
              f"device hostname is not {host}.", file=sys.stderr)
    return cfg
