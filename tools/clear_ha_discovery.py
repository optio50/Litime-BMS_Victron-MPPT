#!/usr/bin/env python3
"""
clear_ha_discovery.py
======================
One-time cleanup tool: removes stale/legacy Home Assistant MQTT Discovery
messages that older versions of this project's firmware published to your
broker.

BACKGROUND
----------
This project used to have the ESP32 firmware publish MQTT Discovery
messages directly (`homeassistant/sensor/<uid>/config`, retain=true) for a
small subset of fields. That approach had a bug (both batteries collided on
the same topic) and was fully replaced by a single, complete YAML package
(homeassistant/mqtt_sensors.yaml) that now provides every Home Assistant
entity for this project. The firmware no longer publishes any discovery
messages at all.

However, because those old messages were published with the MQTT `retain`
flag, they don't just disappear when you reflash the ESP32 with newer
firmware -- retained messages live on the broker forever until something
publishes an empty payload to the same topic (which is how you delete a
retained message in MQTT) or you wipe the broker's persistence store
entirely (NOT recommended -- that would nuke every other retained message
on your broker, unrelated to this project).

This script does the narrow, correct thing: it connects to your broker,
publishes an empty retained payload to every discovery topic this project's
firmware has EVER used (across all historical versions), and disconnects.
Run it once, from your PC -- there is no need (and it would be wasteful) for
the firmware itself to do this on every boot/reconnect.

USAGE
-----
    python3 tools/clear_ha_discovery.py

By default this reads your broker address/port/credentials straight out of
config.h (via shared_config.py), exactly like litime_monitor.py does, so
there is nothing to edit. Override with flags if you want to point it at a
different broker:

    python3 tools/clear_ha_discovery.py --host 192.168.1.50 --port 1883 \\
        --user myuser --password mypass

Add --dry-run to just print what would be cleared without publishing
anything.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("This script requires paho-mqtt. Install it with:\n"
          "    pip install paho-mqtt", file=sys.stderr)
    sys.exit(1)

# shared_config.py lives one directory up (repo root), alongside config.h.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shared_config

# Every unique_id ever used by firmware-side HA discovery, across all
# historical versions of mqtt_manager.cpp. Safe to run even if your broker
# never had some (or any) of these -- publishing an empty retained payload
# to a topic that doesn't exist is a harmless no-op.
LEGACY_DISCOVERY_UIDS = [
    # --- original version (buggy: shared "litime_<field>" uid for BOTH
    #     batteries, so battery1/battery2 collided on the same topic) ---
    "litime_soc", "litime_total_voltage", "litime_current", "litime_power",
    "litime_cell_temp", "litime_mosfet_temp", "litime_remaining_ah",
    "litime_cell_delta_mv", "litime_time_remaining_s",
    "litime_total_power", "litime_total_current", "litime_soc_avg",
    "litime_total_remaining_ah",
    # --- interim fix (unique per-battery uid, still duplicated the YAML
    #     package once that was added) ---
    "litime_fw_battery1_remaining_ah", "litime_fw_battery1_cell_delta_mv",
    "litime_fw_battery2_remaining_ah", "litime_fw_battery2_cell_delta_mv",
    "litime_fw_combined_total_power", "litime_fw_combined_total_current",
    "litime_fw_combined_soc_avg", "litime_fw_combined_total_remaining_ah",
    "litime_fw_combined_time_remaining_s",
]


def main() -> int:
    cfg = shared_config.load()

    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=cfg["MQTT_BROKER"],
                     help=f"MQTT broker host (default: {cfg['MQTT_BROKER']}, from config.h)")
    ap.add_argument("--port", type=int, default=cfg["MQTT_PORT"],
                     help=f"MQTT broker port (default: {cfg['MQTT_PORT']}, from config.h)")
    ap.add_argument("--user", default=cfg["MQTT_USER"] or None,
                     help="MQTT username (default: from config.h, if set)")
    ap.add_argument("--password", default=cfg["MQTT_PASS"] or None,
                     help="MQTT password (default: from config.h, if set)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Print what would be cleared without publishing anything")
    args = ap.parse_args()

    topics = [f"homeassistant/sensor/{uid}/config" for uid in LEGACY_DISCOVERY_UIDS]

    print(f"Broker: {args.host}:{args.port}")
    print(f"Clearing {len(topics)} legacy discovery topic(s):")
    for t in topics:
        print(f"  - {t}")

    if args.dry_run:
        print("\n--dry-run set: nothing published.")
        return 0

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                          client_id="litime-ha-discovery-cleanup")
    if args.user:
        client.username_pw_set(args.user, args.password)

    connected = {"ok": False}

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        connected["ok"] = (reason_code == 0)

    client.on_connect = on_connect
    client.connect(args.host, args.port, keepalive=10)
    client.loop_start()

    # Give the connection a moment to establish before publishing.
    for _ in range(50):
        if connected["ok"]:
            break
        time.sleep(0.1)

    if not connected["ok"]:
        print("\nERROR: could not connect to broker.", file=sys.stderr)
        client.loop_stop()
        return 1

    for t in topics:
        client.publish(t, payload=None, qos=1, retain=True)

    # Let the publishes flush before disconnecting.
    time.sleep(1.0)
    client.loop_stop()
    client.disconnect()

    print("\nDone. Refresh Home Assistant's entity list (or restart HA) to "
          "confirm the stale entities/devices are gone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
