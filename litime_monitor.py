#!/usr/bin/env python3
"""
LiTime Dual BMS + Victron MPPT MQTT Monitor
Dark-theme PyQt5 dashboard for two LiTime 48V 100Ah batteries and a
Victron MPPT solar controller.

Required packages:
    pip install PyQt5 pyqtgraph pglive paho-mqtt

Topics consumed:
    litime/status                - online/offline LWT
    litime/battery1/state        - full JSON payload (per battery)
    litime/battery2/state        - full JSON payload
    litime/combined/state        - combined JSON payload
    victron/state                - Victron MPPT full JSON payload
    victron/<field>              - flat scalar topics (optional)
"""

import os
import sys

# If not running inside the project's virtualenv, re-exec with venv Python.
# This lets ./litime_monitor.py work directly without manual activation.
if sys.prefix == sys.base_prefix:
    _venv_python = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'venv', 'bin', 'python')
    if os.path.exists(_venv_python):
        os.execv(_venv_python, [_venv_python] + sys.argv)

import base64
import hashlib
import sys
import os
import json
import time
import copy
import fcntl
import random
from datetime import datetime
from threading import Thread, Event
from itertools import islice

import numpy as np

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QProgressBar, QGroupBox,
    QTextBrowser, QSplitter, QFrame, QSizePolicy,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QPushButton, QFileDialog
)
from PyQt5 import QtCore, QtGui
from PyQt5.QtCore import (Qt, QTimer, QUrl, pyqtSignal, QObject, QIODevice,
                          QByteArray, QFile)
from PyQt5.QtGui import QFont, QColor, QPalette, QPixmap
from PyQt5.QtNetwork import (QNetworkAccessManager, QNetworkRequest,
                             QNetworkReply, QHttpMultiPart, QHttpPart)
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineView

import pyqtgraph as pg
from pglive.kwargs import Axis, Crosshair
from pglive.sources.data_connector import DataConnector
from pglive.sources.live_axis import LiveAxis
from pglive.sources.live_axis_range import LiveAxisRange
from pglive.sources.live_plot import LiveLinePlot
from pglive.sources.live_categorized_bar_plot import LiveCategorizedBarPlot
from pglive.sources.live_plot_widget import LivePlotWidget

import paho.mqtt.client as mqtt

import shared_config

# QtWebEngine profile directory selection. The weather site (e.g. MSN
# Weather) remembers its dark-theme choice in cookies / local storage, so
# the profile must SURVIVE restarts -- the old per-run /tmp dir meant the
# site came back with its blinding white theme every launch. BUT two
# instances running at once must never share a profile dir (Chromium's
# SQLite cookie "database is locked" errors), so a lock file decides: the
# FIRST instance gets the persistent profile under ~/.cache, any later
# concurrent instance falls back to an isolated throwaway per-PID dir.
# flock() is released automatically by the kernel when the process dies,
# so a crashed instance can never leave a stale lock behind. --log-level=3
# caps Chromium logging at FATAL, silencing noisy database.cc /
# service_worker_storage ERROR messages that --disable-logging alone
# doesn't suppress.
_persistent_profile = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "litime_qt_profile")
try:
    os.makedirs(_persistent_profile, exist_ok=True)
    _profile_lock_fd = os.open(_persistent_profile + ".lock",
                               os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(_profile_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:            # another instance holds the persistent profile
        os.close(_profile_lock_fd)
        raise
    _qt_profile_dir = _persistent_profile
except OSError:                  # no persistent profile available -> isolate
    _qt_profile_dir = os.path.join("/tmp", f"litime_qt_{os.getpid()}")
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    f"--disable-logging --log-level=3 --user-data-dir={_qt_profile_dir}"
)
os.putenv("QT_LOGGING_RULES", "*.debug=false;qt.qpa.*=false")

# =============================================================================
#  ─── CONFIGURATION ──────────────────────────────────────────────────────────
#  MQTT connection details and battery display names are fetched from the
#  ESP32 itself (http://<hostname>.local/config.json) via shared_config.py
#  -- edit settings once on the device /config page and both apps stay in
#  sync. Anything it can't fetch falls back to shared_config.DEFAULTS.
# =============================================================================
_cfg = shared_config.load()

MQTT_BROKER  = _cfg["MQTT_BROKER"]
MQTT_PORT    = _cfg["MQTT_PORT"]
MQTT_USER    = _cfg["MQTT_USER"]
MQTT_PASS    = _cfg["MQTT_PASS"]
TOPIC_BASE   = _cfg["MQTT_TOPIC_BASE"]
VICTRON_TOPIC_BASE = _cfg["MQTT_VICTRON_TOPIC_BASE"]
RAND_ID      = random.randint(1, 1000)

BAT1_NAME    = _cfg["BMS1_NAME"]
BAT2_NAME    = _cfg["BMS2_NAME"]
WEATHER_URL  = _cfg.get("WEATHER_URL", "")
SOC_RESERVE_PCT = _cfg.get("SOC_RESERVE_PCT", 10)
ARRAY_NAME   = _cfg.get("ARRAY_NAME", "Solar Array")
BAT_NAME     = {1: BAT1_NAME, 2: BAT2_NAME}
# OTA upload-page password (device ota_password) — used only to display a
# hint on the ESP Update tab; the embedded web view itself asks for it.
OTA_PASSWORD = _cfg.get("OTA_PASSWORD", "")

# All charts are fed once per UI refresh tick (self.timer, 2 s interval —
# see Window.__init__), regardless of how often MQTT messages actually
# arrive. That fixes the effective sample rate at exactly 1 point / 2 s, so
# 24 h of history = 24*3600/2 = 43 200 points. Keep this exact (not padded)
# so old data actually ages out of every chart after 24 hours instead of
# accumulating 48h+ of history.
CHART_MAX_PTS = 43200
CHART_ROLL    = 600   # roll_on_tick: x-axis shifts once per 600 plotted
                      # points — 20 min @ the fixed 1 pt / 2 s cadence
CHART_PT_S    = 2     # fixed chart cadence: 1 point per 2-s UI tick (above)
# The SOC / Combined-Power charts on the Overview page sit side-by-side in a
# splitter (half width each), so their DATETIME tick labels have less room
# than full-width charts and can overlap at the default zoom. A narrower
# rolling window (~74% of CHART_ROLL, matching one mouse-wheel zoom-in notch:
# 1.02**(120 * -1/8) ≈ 0.743) gives each label enough horizontal space
# without changing the tick format.
CHART_ROLL_SPLIT = 400

# =============================================================================
#  ─── CATEGORIZED BAR-CHART TIMING (used by MpptTab.refresh) ──────────────────
#    BAR_PIN_S  — repaint cadence: every 30 s of buffered data the bar picture
#                 is rebuilt (the bar's leading edge advances 30 s rightward
#                 each repaint).
#    BAR_TAIL_S — history behind the newest bar after a view shift (newest
#                 sits ~1/3 from the left edge of the window).
#    BAR_HEAD_S — room ahead of the newest bar: the window is FROZEN while
#                 the bar marches right, and only shifts (realigns) once the
#                 bar reaches this right edge — same "march then shift"
#                 behaviour as the line charts (LiveAxisRange roll).
#                 BAR_HEAD_S is thus also the shift period.
#                 Window = TAIL + HEAD = 900 s = 15 min.
#    All 24 h @ 1 pt/2 s (43 200 points) stay in the connector deque for
#    pan-back / "View All" look-backs. ANY manual view change (zoom, pan,
#    "View All") self-heals back to the proper marching window at the next
#    30-s refresh; the corner "A" button restores it instantly.
BAR_PIN_S  = 30
BAR_TAIL_S = 300
BAR_HEAD_S = 600
# =============================================================================
#  Line-chart view self-heal (used by _throttle_line_chart_pairs):
#    LINE_HEAL_S — if the newest point is more than this many seconds past the
#                  VISIBLE right edge, a roll is forced immediately. The roll
#                  counter is APPEND-count based while the window edge is
#                  timestamp based, so a wall-clock step (time.time() stamps)
#                  or missed timer ticks desync them and the line can run
#                  off-screen right for up to a full cycle. Natural march
#                  overshoot is ~1 chart tick (2 s), so this only trips on a
#                  genuine desync. Manual zoom/pan views are exempt (their
#                  manual_range flag is set); the corner "A" button resumes.
#                  Runs from the FIRST plotted points (not only once
#                  roll_on_tick points are buffered): healthy first-cycle
#                  windows (pglive's own startup window, or the one pinned
#                  by _pin_first_cycle_window) have a full roll cycle of
#                  room ahead, so the threshold alone still leaves the
#                  startup march untouched.
LINE_HEAL_S = 10
# =============================================================================

# =============================================================================
#  ─── COLORS ─────────────────────────────────────────────────────────────────
# =============================================================================
C_BG         = "#1e1e1e"
C_PANEL      = "#2a2a2a"
C_BORDER     = "#3a3a3a"
C_TEXT       = "#e0e0e0"
C_DIM        = "#888888"
C_GREEN      = "#038513"
C_YELLOW     = "yellow"
C_ORANGE     = "#ffa726"
C_ORANGERED  = "orangered"
C_RED        = "red"
C_BLUE       = "#42a5f5"
C_MEDBLUE    = "#206bee"
C_CYAN       = "#26c6da"
C_MAGENTA    = "#ab47bc"
C_BLACK      = "#000000"
C_DARKORANGE = "#584115"

DARK_STYLESHEET = f"""
    QMainWindow, QWidget {{ background-color: {C_BG}; color: {C_TEXT}; }}
    QTabWidget::pane {{ border: 1px solid {C_BORDER}; background: {C_BG}; }}
    QTabBar::tab {{
        background: {C_PANEL}; color: {C_DIM};
        padding: 8px 18px; border: 1px solid {C_BORDER};
        border-bottom: none; border-radius: 4px 4px 0 0; margin-right: 2px;
    }}
    QTabBar::tab:selected {{ background: {C_BG}; color: {C_TEXT}; border-bottom: 2px solid {C_CYAN}; }}
    QGroupBox {{
        border: 1px solid {C_BORDER}; border-radius: 6px;
        margin-top: 14px; padding: 8px;
        font-weight: bold; color: {C_DARKORANGE}; font-size: 12px;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
    QProgressBar {{
        background: #333; border: 1px solid {C_BORDER}; border-radius: 4px;
        text-align: center; color: #000; font-weight: bold;
    }}
    QProgressBar::chunk {{ background: {C_GREEN}; border-radius: 3px; }}
    QTextBrowser {{
        background: #111; color: {C_TEXT}; border: 1px solid {C_BORDER};
        font-family: monospace; font-size: 12px;
    }}
    QLabel {{ color: {C_TEXT}; }}
    QSplitter::handle {{ background: {C_BORDER}; }}
    QScrollBar:vertical {{ background: {C_PANEL}; width: 10px; }}
    QScrollBar::handle:vertical {{ background: {C_BORDER}; border-radius: 5px; }}
"""

# =============================================================================
#  ─── SHARED STATE ────────────────────────────────────────────────────────────
# =============================================================================
bat: dict = {
    1: {
        "connected": False, "soc": 0, "soh": "0%",
        "total_voltage": 0.0, "cell_voltage_sum": 0.0, "current": 0.0,
        "power": 0.0, "cell_temp": 0, "mosfet_temp": 0,
        "remaining_ah": 0.0, "full_capacity_ah": 0.0,
        "discharge_cycles": 0, "time_remaining_s": 0, "time_direction": "idle",
        "protection": "", "balancing": "", "battery_state": "",
        "cell_voltages": [], "cell_min_v": 0.0, "cell_max_v": 0.0,
        "cell_delta_mv": 0.0,
    },
    2: {
        "connected": False, "soc": 0, "soh": "0%",
        "total_voltage": 0.0, "cell_voltage_sum": 0.0, "current": 0.0,
        "power": 0.0, "cell_temp": 0, "mosfet_temp": 0,
        "remaining_ah": 0.0, "full_capacity_ah": 0.0,
        "discharge_cycles": 0, "time_remaining_s": 0, "time_direction": "idle",
        "protection": "", "balancing": "", "battery_state": "",
        "cell_voltages": [], "cell_min_v": 0.0, "cell_max_v": 0.0,
        "cell_delta_mv": 0.0,
    },
}
combined: dict = {
    "soc_avg": 0.0, "soc_b1": 0, "soc_b2": 0,
    "total_current": 0.0, "total_power": 0.0,
    "total_remaining_ah": 0.0, "total_remaining_wh": 0.0, "total_capacity_ah": 0.0,
    "time_remaining_s": 0, "time_direction": "idle", "flow": "idle",
    "uptime_s": 0,
}
victron_data: dict = {
    "valid": False, "state": 0, "state_str": "Off",
    "error": 0, "error_str": "None",
    "batt_v": 0.0, "batt_a": 0.0,
    "pv_w": 0.0,
    "yield_today": 0.0,
    "last_seen_s": 9999,
}
mqtt_connected: bool = False
broker_status: str   = "offline"


# =============================================================================
#  ─── MQTT THREAD ─────────────────────────────────────────────────────────────
# =============================================================================
class MQTTSignals(QObject):
    message_received = pyqtSignal(str, str)
    connected        = pyqtSignal()
    disconnected     = pyqtSignal(str)
    mqtt_error       = pyqtSignal(str)

mqtt_signals = MQTTSignals()

# Set just before the Qt app quits so the background MQTT thread stops
# trying to touch mqtt_signals (a QObject) once its underlying C++ object
# may already have been destroyed by Qt's teardown. Without this guard,
# a message/reason-code arriving in the tiny window between "app is
# quitting" and "daemon thread actually dies" raises a noisy
# "wrapped C/C++ object ... has been deleted" RuntimeError on exit.
_mqtt_shutdown = Event()
mqtt_client    = None   # set once start_mqtt() creates the paho client

def _safe_emit(signal, *args):
    if _mqtt_shutdown.is_set():
        return
    try:
        signal.emit(*args)
    except RuntimeError:
        # Signals object was torn down concurrently with app exit -- ignore.
        pass

def _on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected
    if reason_code.is_failure:
        mqtt_connected = False
        _safe_emit(mqtt_signals.mqtt_error, f"MQTT connect refused: {reason_code}")
    else:
        mqtt_connected = True
        client.subscribe(f"{TOPIC_BASE}/#")
        client.subscribe(f"{VICTRON_TOPIC_BASE}/#")
        _safe_emit(mqtt_signals.connected)

def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    global mqtt_connected
    mqtt_connected = False
    _safe_emit(mqtt_signals.disconnected, str(reason_code))

def _on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8")
        _safe_emit(mqtt_signals.message_received, msg.topic, payload)
    except Exception as e:
        _safe_emit(mqtt_signals.mqtt_error, f"MQTT payload decode error on {msg.topic}: {e}")

def shutdown_mqtt():
    """Called from the main (Qt) thread on app.aboutToQuit. Stops the
    background MQTT thread's loop cleanly (client.disconnect() makes
    loop_forever() return) *before* Qt finishes destroying mqtt_signals,
    avoiding the 'wrapped C/C++ object has been deleted' crash."""
    _mqtt_shutdown.set()
    if mqtt_client is not None:
        try:
            mqtt_client.disconnect()
        except Exception:
            pass

def start_mqtt():
    global mqtt_client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=(f"litime-pyqt-monitor-{RAND_ID}"))
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message    = _on_message
    mqtt_client = client

    # paho handles reconnection internally with exponential backoff
    # (min_delay -> max_delay, doubling each attempt) as long as we use
    # connect_async() + loop_forever(retry_first_connection=True). This
    # covers BOTH the very first connection attempt (broker unreachable
    # at startup) and every later drop/retry, so no hand-rolled fixed
    # sleep() is needed anymore.
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)

    # loop_forever() only returns if disconnect() is called explicitly or
    # the client thread is asked to terminate. In normal operation that
    # only happens via shutdown_mqtt() on app exit, in which case we must
    # NOT loop back into loop_forever() again -- just let the thread end.
    while True:
        try:
            client.loop_forever(retry_first_connection=True)
        except Exception as e:
            if not _mqtt_shutdown.is_set():
                _safe_emit(mqtt_signals.mqtt_error, f"MQTT loop error: {e}")
        if _mqtt_shutdown.is_set():
            break
        time.sleep(5)


# =============================================================================
#  ─── BMS BIT-DECODE HELPERS ──────────────────────────────────────────────────
# =============================================================================
# LiTime BMS protection state: 32-bit hex string (e.g. "0x00000000")
# Bit layout from the original reverse-engineering source:
#   https://github.com/calledit/LiTime_BMS_bluetooth (index.html)
#   0x00000004 = Over Charge Protection      → bit 2
#   0x00000020 = Over-discharge Protection   → bit 5
#   0x00000040 = Charging Over Current       → bit 6
#   0x00000080 = Discharging Over Current    → bit 7
#   0x00000100 = High-temp Protection (chg)  → bit 8
#   0x00000200 = High-temp Protection (dsg)  → bit 9
#   0x00000400 = Low-temp Protection (chg)   → bit 10
#   0x00000800 = Low-temp Protection (dsg)   → bit 11
#   0x00004000 = Short Circuit Protection    → bit 14
# The chg/dsg split for the temp bits is inferred from the over-current bit
# pattern (bit 6 = chg OC, bit 7 = dsg OC); the community doc lists both
# high-temp and both low-temp without specifying which is charge vs discharge.
_PROT_BITS = [
    (2,  "Over Charge"),       (5,  "Over Discharge"),
    (6,  "Chg Overcurrent"),   (7,  "Dsg Overcurrent"),
    (8,  "Chg High Temp"),     (9,  "Dsg High Temp"),
    (10, "Chg Low Temp"),      (11, "Dsg Low Temp"),
    (14, "Short Circuit"),
]
# Battery state: 16-bit hex string (e.g. "0x0001")
# Bit layout from the original reverse-engineering source:
#   https://github.com/calledit/LiTime_BMS_bluetooth (index.html)
#   0x0000 = idle (at rest)
#   0x0001 = charging              → bit 0
#   0x0002 = discharging           → bit 1
#   0x0004 = charge disabled       → bit 2
# Note: the original source labeled 0x0002 (bit 1) as "unknown", but real BMS
# readings show this bit is set when the battery is actively discharging.
# The Home Assistant YAML (mqtt_sensors.yaml) already decodes bit 1 as
# "Discharging"; this aligns the Python dashboard decode with that.
_STATE_BITS = [
    (0, "Charging"), (1, "Discharging"), (2, "Charge Disabled"),
]

def decode_protection(hex_str: str) -> str:
    """Return comma-separated list of active protection flags, or 'None'."""
    if not hex_str:
        return "None"
    try:
        val = int(hex_str, 16)
    except (ValueError, TypeError):
        return str(hex_str)
    if val == 0:
        return "None"
    flags = [name for bit, name in _PROT_BITS if val & (1 << bit)]
    return ", ".join(flags) if flags else "None"

def decode_battery_state(hex_str: str) -> str:
    """Return human-readable battery state for LiTime status register."""
    if not hex_str:
        return "Idle"
    try:
        val = int(hex_str, 16)
    except (ValueError, TypeError):
        return str(hex_str)
    if val == 0:
        return "Idle"
    flags = [name for bit, name in _STATE_BITS if val & (1 << bit)]
    return " / ".join(flags) if flags else f"Unknown (0x{val:04X})"

def decode_balance(bin_str: str, state_hex: str = "") -> str:
    """Return 'B1 B4 B8 …' for cells being balanced, or 'None'.

    The BMS reports the balancing register as a 32-bit value packed into a
    big-endian binary string (BMSClient::bytesToBinaryString emits the MSB
    of the highest byte first).

    Cell numbering uses the JBD/Xiaoxiang BMS convention (from the
    syssi/esphome-jbd-bms ESPHome component):
        bit (cells - 1 - i) → cell i+1
    For a 16-cell pack: bit 0 = cell 16, bit 15 = cell 1.
    This is the OPPOSITE of a naive "bit 0 = cell 1" assumption.

    The `state_hex` parameter (batteryState) is no longer used for balance
    detection — an earlier version checked bit 2 (0x04) as a "balancing"
    flag, but the community doc (calledit/LiTime_BMS_bluetooth) shows bit 2
    = "charge disabled", not "balancing". The per-cell shunting register is
    the only reliable source of balancing info; when no cells are shunting
    we report "None" rather than guessing.
    """
    if not bin_str:
        return "None"
    val = int(str(bin_str), 2)
    if val == 0:
        return "None"
    n_cells = 16  # LiTime 48V 100Ah = 16S pack
    cells = [i + 1 for i in range(n_cells) if val & (1 << (n_cells - 1 - i))]
    return " ".join(f"B{c}" for c in sorted(cells)) if cells else "None"


# =============================================================================
#  ─── VICTRON MPPT STATE / ERROR DECODE (official codes) ─────────────────────
# =============================================================================
# Authoritative Victron solar-charger state/error code tables, decoded here
# from the raw numeric `state` / `error` MQTT fields (rather than relying on
# the firmware's screen-space-constrained abbreviated state_str/error_str),
# so the desktop app can show full, accurate descriptions.
SOLAR_STATE_DICT = {
    0:   "Off",
    1:   "Low Power",  # not in the source table below, but a real Victron
                        # state some chargers report — included for completeness
    2:   "Fault",
    3:   "Bulk",
    4:   "Absorption",
    5:   "Float",
    6:   "Storage",
    7:   "Equalize",
    11:  "Power Supply",  # official VE.Direct OperationMode enum: state 11 = Power Supply
    245: "Wake-Up",
    252: "EXT Control",
}

SOLAR_ERROR_DICT = {
    0:   "No Error",
    1:   "Error 1: Battery temperature too high",
    2:   "Error 2: Battery voltage too high",
    3:   "Error 3: Battery temperature sensor miswired (+)",
    4:   "Error 4: Battery temperature sensor miswired (-)",
    5:   "Error 5: Remote temperature sensor failure (connection lost)",
    6:   "Error 6: Battery voltage sense miswired (+)",
    7:   "Error 7: Battery voltage sense miswired (-)",
    8:   "Error 8: Battery voltage sense disconnected",
    11:  "Error 11: Battery high ripple voltage",
    14:  "Error 14: Battery low temperature",
    17:  "Error 17: Controller overheated despite reduced output current",
    18:  "Error 18: Controller over-current",
    20:  "Error 20: Maximum Bulk-time exceeded",
    21:  "Error 21: Current sensor issue",
    22:  "Error 22: Internal temperature sensor failure",
    23:  "Error 23: Internal temperature sensor failure",
    24:  "Error 24: Fan failure",
    26:  "Error 26: Terminal overheated",
    27:  "Error 27: Charger short circuit",
    28:  "Error 28: Power stage issue",
    29:  "Error 29: Over-Charge protection",
    33:  "Error 33: PV Input over-voltage",
    34:  "Error 34: PV Input over-current",
    35:  "Error 35: PV Input over-power",
    38:  "Error 38: PV Input is internally shorted in order to protect the battery from over-charging",
    39:  "Error 39: PV Input is internally shorted in order to protect the battery from over-charging",
    40:  "Error 40: PV Input failed to shutdown",
    41:  "Error 41: Inverter shutdown (PV isolation)",
    42:  "Error 42: Inverter shutdown (PV isolation)",
    43:  "Error 43: Inverter shutdown (Ground Fault)",
    50:  "Error 50: Inverter overload, Inverter peak current",
    51:  "Error 51: Inverter temperature too high",
    52:  "Error 52: Inverter overload, Inverter peak current",
    53:  "Error 53: Inverter output voltage",
    54:  "Error 54: Inverter output voltage",
    55:  "Error 55: Inverter self test failed",
    56:  "Error 56: Inverter self test failed",
    57:  "Error 57: Inverter ac voltage on output",
    58:  "Error 58: Inverter self test failed",
    67:  "Error 67: BMS Connection lost",
    68:  "Error 68: Network misconfigured",
    69:  "Error 69: Network misconfigured",
    70:  "Error 70: Network misconfigured",
    71:  "Error 71: Network misconfigured",
    80:  "Error 80: PV Input is internally shorted in order to protect the battery from over-charging",
    81:  "Error 81: PV Input is internally shorted in order to protect the battery from over-charging",
    82:  "Error 82: PV Input is internally shorted in order to protect the battery from over-charging",
    83:  "Error 83: PV Input is internally shorted in order to protect the battery from over-charging",
    84:  "Error 84: PV Input is internally shorted in order to protect the battery from over-charging",
    85:  "Error 85: PV Input is internally shorted in order to protect the battery from over-charging",
    86:  "Error 86: PV Input is internally shorted in order to protect the battery from over-charging",
    87:  "Error 87: PV Input is internally shorted in order to protect the battery from over-charging",
    114: "Error 114: CPU temperature too high",
    116: "Error 116: Calibration data lost",
    117: "Error 117: Incompatible firmware",
    119: "Error 119: Settings data lost",
    121: "Error 121: Tester fail",
    200: "Error 200: Internal DC voltage error",
    201: "Error 201: Internal DC voltage error",
    202: "Error 202: Internal GFCI sensor error",
    203: "Error 203: Internal supply voltage error",
    205: "Error 205: Internal supply voltage error",
    212: "Error 212: Internal supply voltage error",
    215: "Error 215: Internal supply voltage error",
}

def decode_mppt_state(code) -> str:
    """Full Victron solar-charger state name from the raw numeric code."""
    try:
        code = int(code)
    except (ValueError, TypeError):
        return "Unknown"
    return SOLAR_STATE_DICT.get(code, f"Unknown ({code})")

def decode_mppt_error(code) -> str:
    """Full Victron solar-charger error description from the raw numeric code."""
    try:
        code = int(code)
    except (ValueError, TypeError):
        return "No Error"
    return SOLAR_ERROR_DICT.get(code, f"Error {code}: Unknown")

def decode_mppt_error_short(code) -> str:
    """Compact form for tight UI space; full text available via decode_mppt_error()."""
    try:
        code = int(code)
    except (ValueError, TypeError):
        return "—"
    return "No Error" if code == 0 else f"Error {code}"


# =============================================================================
#  ─── HELPER WIDGETS / FUNCTIONS ──────────────────────────────────────────────
# =============================================================================
def make_label(text, color=C_TEXT, bold=False, size=11, align=Qt.AlignLeft | Qt.AlignVCenter):

    lbl = QLabel(text)
    lbl.setAlignment(align)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color:{color}; font-size:{size}px; font-weight:{weight};")
    return lbl

def make_value_label(text="—", color=C_GREEN, size=13, bold=True):
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color:{color}; font-size:{size}px; font-weight:{weight};")
    return lbl


class MqttLedWidget(QWidget):
    """Tiny MQTT activity LED."""
    def __init__(self, parent=None):
        super().__init__(parent)
        lo = QHBoxLayout(self)
        lo.setContentsMargins(2, 1, 2, 1)
        lo.setSpacing(3)
        hdr = QLabel("MQTT")
        hdr.setStyleSheet(
            "color:#555555; font-size:8px; font-weight:bold;"
            " background:#000000; padding:1px 4px; border-radius:2px;")
        lo.addWidget(hdr)
        self.led = QLabel("●")
        self.led.setStyleSheet("color:#1a1a1a; font-size:7px;")
        lo.addWidget(self.led)
        self._connected = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._dim)

    def flash(self):
        self.led.setStyleSheet(f"color:{C_GREEN}; font-size:7px;")
        self._timer.start()

    def _dim(self):
        col = "#1a1a1a" if self._connected else C_RED
        self.led.setStyleSheet(f"color:{col}; font-size:7px;")

    def set_connected(self, connected: bool):
        self._connected = connected
        col = "#1a1a1a" if connected else C_RED
        self.led.setStyleSheet(f"color:{col}; font-size:7px;")


def soc_color(soc):
    if soc >= 50: return C_GREEN
    if soc >= 30: return C_ORANGE
    if soc >= 15: return C_ORANGERED
    return C_RED

def soc_bar_style(soc):
    col = soc_color(soc)
    return (
        f"QProgressBar::chunk {{ background: {col}; border-radius: 3px; }}"
        f"QProgressBar {{ background:#333; border:1px solid {C_BORDER};"
        f" border-radius:4px; text-align:center; color:#000; font-weight:bold; }}"
    )

def fmt_time(seconds, direction="idle") -> str:
    """
    Format time remaining with directional label.

    Note: "to_empty" is actually time-to-RESERVE (soc_reserve_pct on /config,
    currently 10%), not literal 0% SOC — LFP packs shouldn't
    routinely be discharged past that point, and the BMS's voltage-based
    low-cutoff can trip before true 0% Ah is reached anyway.

    Once the pack is already below the reserve (firmware reports
    time_remaining_s = 0 with direction "to_empty"), the Overview tab
    recomputes a time-to-0% estimate locally and passes it here with
    direction "to_zero" so the field shows how much longer the pack can
    keep discharging before true empty instead of going blank.

    Examples:
        fmt_time(20700, "to_empty")  →  "5h 45m to Reserve"
        fmt_time(8100,  "to_full")   →  "2h 15m to Full"
        fmt_time(3600,  "to_zero")   →  "1h 00m to 0%"
        fmt_time(0,     "idle")      →  "---"
    """
    if seconds is None or int(seconds) <= 0 or direction == "idle":
        return "---"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    parts = []
    if h:        parts.append(f"{h}h")
    if h or m:   parts.append(f"{m:02d}m")
    if not parts: parts.append("0m")
    if direction == "to_full":
        suffix = "to Full"
    elif direction == "to_zero":
        suffix = "to 0%"
    else:
        suffix = f"to Reserve ({SOC_RESERVE_PCT}%)"
    return " ".join(parts) + " " + suffix

def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0

def fmt_uptime(seconds) -> str:
    """Format ESP32 uptime (seconds since last boot/reset) as e.g.
    '3d 04h 12m' or '5h 03m' or '42s', matching the same value shown in the
    TFT footer."""
    try:
        seconds = int(seconds)
    except (ValueError, TypeError):
        return "—"
    if seconds <= 0:
        return "—"
    d, rem  = divmod(seconds, 86400)
    h, rem  = divmod(rem, 3600)
    m, s    = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"

def min_cell_num(d: dict) -> str:
    """1-based index of the cell matching cell_min_v, from the cell_voltages
    list. Returns '—' if unavailable."""
    cells = d.get("cell_voltages") or []
    if not cells:
        return "—"
    return str(cells.index(min(cells)) + 1)

def max_cell_num(d: dict) -> str:
    """1-based index of the cell matching cell_max_v, from the cell_voltages
    list. Returns '—' if unavailable."""
    cells = d.get("cell_voltages") or []
    if not cells:
        return "—"
    return str(cells.index(max(cells)) + 1)

def flow_color(flow):
    if "charg" in str(flow).lower(): return C_GREEN
    if "discharg" in str(flow).lower(): return C_ORANGE
    return C_DIM


# =============================================================================
#  ─── FastCategorizedBarPlot ──────────────────────────────────────────────────
#  Drop-in replacement for pglive's LiveCategorizedBarPlot (ported from
#  Multiple-Chargers/PyQT5-Multiple-Chargers.py). pglive's setData() redraws
#  EVERY bar of the (up to 43 200-point) history on every replot with a
#  per-point Python loop, and the replot fires every append — a sizeable
#  event-loop freeze per chart that also stalls the 2-s chart/UI cadence
#  (the stalls that make the update_rate caps drop whole seconds, and the
#  mouse/crosshair feel laggy while a full day is buffered). This subclass
#  draws the IDENTICAL picture (pixel-verified against pglive in the
#  Chargers app, including empty bars, multi-category points, duplicate
#  categories and deque head-rolls) from state TRANSITIONS only, vectorized
#  with a category bitmask, and caches the masks so a normal 30-s replot
#  encodes just the ~15 new points (a few ms). The cache validates its
#  prefix against the new x array and falls back to a full recompute on
#  any mismatch, clear() or new category.
# =============================================================================
class FastCategorizedBarPlot(LiveCategorizedBarPlot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache = None                     # (x_array, masks_array) of last build
        self._cat_index = {c: i for i, c in enumerate(self.categories)}

    def _register(self, cats_iter):
        for c in cats_iter:
            if c not in self._cat_index:
                self._cat_index[c] = len(self.categories)
                self.categories.append(c)

    def _encode(self, y_seq, n):
        # y_seq may be an ndarray (uniform point lengths, incl. 2-D when every
        # point holds one category) or the deque/list pglive emits when any
        # entry is [] (inhomogeneous -> np.asarray raises inside pglive).
        cache = {}
        ci = self._cat_index

        def mask_of(t):
            m = 0
            for c in t:
                m |= 1 << ci[c]               # OR: a duplicated category in one
            return m                           # point must not add its bit twice

        return np.fromiter((cache.setdefault(t, mask_of(t))
                            for t in (tuple(v) for v in y_seq)),
                           dtype=np.int64, count=n)
    def setData(self, x_data, y_data, **kwargs):
        self.x_data = x_data
        self.y_data = y_data
        if len(x_data) != len(y_data):
            raise Exception("Len of x_data must be the same as y_data")
        n = len(x_data)
        self.picture = QtGui.QPicture()
        p = QtGui.QPainter(self.picture)
        p.setPen(self.outline_pen)
        if n:
            xb = np.asarray(x_data, dtype=np.float64)
            # --- incremental masks: reuse the cached prefix when x still aligns ---
            masks = None
            if self._cache is not None and n >= 2:
                cx, cm = self._cache
                pos = int(np.searchsorted(xb, cx[-1]))
                if pos < n and xb[pos] == cx[-1]:
                    drop = len(cx) - 1 - pos    # head elements rolled off the deque
                    if drop >= 0 and np.array_equal(xb[:pos + 1], cx[drop:]):
                        new_n = n - 1 - pos
                        if new_n:
                            y_tail = list(islice(y_data, pos + 1, n))
                            self._register(c for t in y_tail for c in t)
                            masks = np.concatenate([cm[drop:], self._encode(y_tail, new_n)])
                        else:
                            masks = cm[drop:]
            if masks is None:                   # first call / clear() / mismatch
                self._register(c for t in y_data for c in t)
                masks = self._encode(y_data, n)
            self._cache = (xb, masks)
            # --- draw only start -> end transitions ---
            prev = np.zeros(n, dtype=np.int64)
            prev[1:] = masks[:-1]
            starts = masks & ~prev
            ends = ~masks & prev
            active = {}
            for i in np.nonzero(starts | ends)[0].tolist():
                xi = float(xb[i])
                e = int(ends[i])
                while e:                        # close bars that end here
                    b = e & -e
                    e ^= b
                    s = active.pop(b, xi)
                    p.setBrush(self.category_color.get(self.categories[b.bit_length() - 1],
                                                       self.bar_brush))
                    p.drawRect(QtCore.QRectF(s, b.bit_length() - 1 - self.bar_height / 2,
                                             xi - s, self.bar_height))
                st = int(starts[i])
                while st:                       # open bars that start here
                    b = st & -st
                    st ^= b
                    active[b] = xi
            for b in active:                    # still-open bars run to the newest x
                p.setBrush(self.category_color.get(self.categories[b.bit_length() - 1],
                                                   self.bar_brush))
                p.drawRect(QtCore.QRectF(active[b], b.bit_length() - 1 - self.bar_height / 2,
                                         float(xb[-1]) - active[b], self.bar_height))
        else:
            self._cache = None
        p.end()
        self.prepareGeometryChange()
        self.informViewBoundsChanged()
        self.bounds = [None, None]
        self.sigPlotChanged.emit(self)

    def clear(self):
        self._cache = None
        self._cat_index = {c: i for i, c in enumerate(self.categories)}
        super().clear()


# =============================================================================
#  ─── HIDDEN-CHART RENDER THROTTLING (ported from the Chargers app) ───────────
#  pglive re-plots EVERY connector's full deque on every plotted append
#  (setData of up to 43 200 points), even for charts on tabs that are not
#  visible and never paint — tens of milliseconds of event-loop stall per
#  tick across this app's ~50 line connectors (only a handful on the
#  visible tab at any time), which is what makes the mouse, crosshair and
#  the whole app feel laggy once a full day is buffered. Only the DATA needs
#  recording every tick, so hidden widgets' connectors get a huge
#  plot_timeout; the deque appends in the refresh() methods stay
#  unconditional, so NO data is ever lost. A hidden -> visible transition
#  re-plots the buffered history immediately, re-aligns rolling_index and,
#  during the first roll cycle, pins a full-width startup window (see
#  _catch_up_connector and _pin_first_cycle_window below). Each tab calls
#  its throttle at the top
#  of refresh() (the 2-s self-healing safety net, also covering minimize /
#  restore) and hooks its tab widgets' currentChanged for an instant
#  refresh on tab clicks.
# =============================================================================
def _throttle_line_chart_pairs(pairs):
    """Render only the visible charts; `pairs` is a list of
    (LivePlotWidget, [DataConnector, ...]) tuples."""
    for widget, conns in pairs:
        visible = widget.isVisible()
        roll = widget.x_range_controller.roll_on_tick
        if visible:
            caught_up = False
            for conn in conns:
                if conn.plot_timeout:
                    # Hidden -> visible transition: restore live plotting
                    # and re-plot the buffered history right now.
                    conn.plot_timeout = 0.0
                    _catch_up_connector(conn)
                    caught_up = True
            # A chart caught up during its FIRST roll cycle (< roll_on_tick
            # points buffered) never saw pglive's own startup ticks 0/1, so
            # the boundary emit above computed its window from the SHORT
            # buffered history — barely any room ahead of the newest point
            # while the next natural roll tick is a FULL roll cycle of
            # appends away: the line runs off-screen right within minutes
            # and sits there for up to ~20 min. Pin the full-width startup
            # window instead (see _pin_first_cycle_window).
            if (caught_up and roll and not widget.manual_range
                    and 0 < len(conns[0].x) < roll):
                _pin_first_cycle_window(widget, conns, roll)
            # Overshoot self-heal (see LINE_HEAL_S): if the newest point has
            # run past the visible right edge by more than LINE_HEAL_S
            # (clock step / missed ticks desyncing the append-count roll
            # counter from the timestamp-anchored window — or any leftover
            # short-room first-cycle window), force a roll NOW instead of
            # letting the line sit off-screen until the counter catches up.
            # Manual views are exempt. A healthy window — pglive's own
            # startup window or the pin above — always has a full roll
            # cycle of room ahead, so the threshold alone leaves the normal
            # startup march untouched, from the first plotted points on.
            if (not widget.manual_range and roll
                    and len(conns[0].x) > 2):
                edge = widget.getViewBox().viewRange()[0][1]
                if conns[0].x[-1] - edge > LINE_HEAL_S:
                    for conn in conns:
                        _catch_up_connector(conn)
                    if len(conns[0].x) < roll:
                        _pin_first_cycle_window(widget, conns, roll)
        else:
            for conn in conns:
                conn.plot_timeout = 3600.0             # effectively stop re-plotting
                conn.last_plot = time.perf_counter()   # keep the deferral rolling


def _catch_up_connector(conn):
    """Immediate catch-up for a connector whose chart just became visible:
    re-plot the current deque NOW and re-align rolling_index to a roll
    boundary so pglive's LiveAxisRange recomputes the marching window on
    this very roll tick. rolling_index only advances on PLOTTED appends,
    so it froze while the chart was hidden — at 0 for a chart hidden since
    app start. Emitting that frozen tick 0 would make pglive compute a
    degenerate single-point [x0, x0] window (visible for up to a full
    cycle), so a below-first-boundary index is bumped to roll_on_tick.
    Emits exactly what a plotted append emits — WITHOUT appending a data
    point."""
    if not len(conn.x):
        return
    widget = conn.plot.plot_widget
    roll = widget.x_range_controller.roll_on_tick
    if roll:
        if conn.rolling_index < roll:
            # Never plotted (hidden since app start, or still in the first
            # cycle): start at the first roll boundary, not tick 0.
            conn.rolling_index = roll
        elif conn.rolling_index % roll:
            conn.rolling_index += roll - (conn.rolling_index % roll)
    conn.last_plot = time.perf_counter() - 3600.0     # force past plot_timeout
    conn._update_data()                               # full-deque setData
    conn.sig_data_roll_tick.emit(conn, conn.rolling_index)


def _pin_first_cycle_window(widget, conns, roll):
    """Full-width startup window for a chart caught up (or healed) during
    its FIRST roll cycle — fewer than roll_on_tick points buffered.

    pglive computes a roll-tick window from data_bounds(offset=roll_on_tick),
    which for a short history spans only those few points: the newest point
    gets just that much room ahead, while the next natural roll tick is a
    full roll_on_tick of appends away (~20 min @ 1 pt / 2 s). The line runs
    off-screen right within minutes and sits there for most of a cycle —
    exactly what a chart first shown early after app start does (it was
    hidden while pglive's startup ticks 0/1 fired, so the catch-up boundary
    emit was its first-ever window).

    Pin the window the chart would have had if it had been visible from app
    start: a full roll cycle of room ahead (roll_on_tick points @ the fixed
    1 pt / 2 s cadence), left edge clamped to the oldest buffered point so
    the trace starts at the left edge and marches right — the same startup
    look as pglive's tick-1 window and the bar chart's BAR_TAIL_S clamp.
    The next natural boundary then lands roll_on_tick appends later, the
    same moment the line reaches the right edge, where pglive's own math
    takes over with a proper full-history window (>= roll_on_tick points
    exist by then, so the handover is seamless).

    Two details keep pglive from stomping the pin seconds later:
      * its cached "final" x-ranges — replayed by every non-boundary roll
        tick, which fires whenever the y-bounds move — are synced to the
        pin, and
      * each connector's rolling_index is stepped PAST the boundary just
        emitted, so the next append is a non-boundary tick instead of a
        boundary tick that would recompute the short window over the
        still-short history."""
    x = conns[0].x
    if not x or not roll:
        return
    room = roll * CHART_PT_S                  # full roll cycle of room ahead
    rng = [max(x[0], x[-1] - room / 2.0),     # steady-state left offset,
           x[-1] + room]                      # clamped to the oldest point
    widget.setXRange(rng[0], rng[1], padding=0)
    widget.x_range_controller.final_x_range = list(rng)
    widget.final_x_range = list(rng)
    for conn in conns:
        if conn.rolling_index % roll == 0:
            conn.rolling_index += 1

# =============================================================================
#  ─── LIVE CHART FACTORY ──────────────────────────────────────────────────────
# =============================================================================
CHART_KW = {
    Crosshair.ENABLED: True,
    Crosshair.LINE_PEN: pg.mkPen(color="yellow", width=0.5),
    Crosshair.TEXT_KWARGS: {"color": "white"},
}

def make_chart(title: str, y_label: str = "", roll: int = CHART_ROLL,
               tick_format: str = Axis.DATETIME) -> LivePlotWidget:
    bottom = LiveAxis("bottom", **{Axis.TICK_FORMAT: tick_format})
    w = LivePlotWidget(
        title=title,
        axisItems={"bottom": bottom},
        x_range_controller=LiveAxisRange(roll_on_tick=roll, offset_left=0.5),
        **CHART_KW,
    )
    w.x_range_controller.crop_left_offset_to_data = True
    w.showGrid(x=True, y=True, alpha=0.3)
    w.setLabel("bottom")
    if y_label:
        w.setLabel("left", y_label)
    w.addLegend()
    w.setBackground("#000000")
    return w

def make_connector(plot, max_points: int = CHART_MAX_PTS) -> DataConnector:
    return DataConnector(plot, max_points=max_points, update_rate=2)

def make_category_chart(title: str, categories: list) -> LivePlotWidget:
    """Chart with a datetime bottom axis and a categorical (named) left axis,
    used for the MPPT charger-state bar chart.

    Deliberately NO x_range_controller (pglive's LiveAxisRange): the state
    chart's connector is ignore_auto_range=True and its view is driven
    manually by MpptTab.refresh()'s data-time "march then shift" clocks
    (see the BAR_* constants) — exactly like the Chargers app's bar
    charts. pglive's rolling window would fight those re-pins."""
    bottom = LiveAxis("bottom", **{Axis.TICK_FORMAT: Axis.DATETIME})
    left   = LiveAxis("left",   **{Axis.TICK_FORMAT: Axis.CATEGORY, Axis.CATEGORIES: categories})
    w = LivePlotWidget(
        title=title,
        axisItems={"bottom": bottom, "left": left},
        **CHART_KW,
    )
    w.showGrid(x=True, y=True, alpha=0.3)
    w.setBackground("#000000")
    return w


# =============================================================================
#  ─── LIVE CELL SNAPSHOT ──────────────────────────────────────────────────────
# =============================================================================
class CellBarChart(pg.PlotWidget):
    """Instant cell voltages as scatter+line colored by deviation from average."""
    def __init__(self, title: str = "Cells Now", n_cells: int = 16):
        super().__init__()
        self.n_cells = n_cells
        self.setBackground("#000000")
        self.setTitle(title, color=C_CYAN, size="10pt")
        self.getAxis("bottom").setTicks(
            [[(i, f"C{i+1:02d}") for i in range(n_cells)]])
        self.getAxis("bottom").setStyle(tickFont=QFont("monospace", 7))
        self.getAxis("left").setLabel("V")
        self.showGrid(y=True, alpha=0.2)
        self.setYRange(3.0, 3.8)

        self._line = pg.PlotDataItem(
            x=list(range(n_cells)), y=[3.3] * n_cells,
            pen=pg.mkPen(color="#444444", width=1))
        self._scatter  = pg.ScatterPlotItem(size=9, pen=pg.mkPen(None))
        self._avg_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(color=C_CYAN, width=1, style=Qt.DashLine),
            label="avg",
            labelOpts={"color": C_CYAN, "position": 0.05})
        self.addItem(self._line)
        self.addItem(self._scatter)
        self.addItem(self._avg_line)

    def update_cells(self, voltages):
        if not voltages:
            return
        n    = min(len(voltages), self.n_cells)
        vals = list(voltages[:n])
        avg  = sum(vals) / n if n else 3.3
        xs   = list(range(n))
        self._line.setData(xs, vals)
        self._avg_line.setValue(avg)
        spots = []
        for i, v in enumerate(vals):
            delta = abs(v - avg)
            if delta > 0.05:   color = C_RED
            elif delta > 0.02: color = C_ORANGE
            elif delta > 0.01: color = C_YELLOW
            else:              color = C_GREEN
            spots.append({"pos": (i, v), "brush": pg.mkBrush(color), "size": 9})
        self._scatter.setData(spots)
        if vals:
            mn, mx = min(vals), max(vals)
            pad = max(0.005, (mx - mn) * 0.6)
            self.setYRange(mn - pad, mx + pad)


# =============================================================================
#  ─── BATTERY DETAIL PANEL ────────────────────────────────────────────────────
# =============================================================================
class BatteryPanel(QWidget):
    """
    Per-battery tab.

    Layout:
      top row  : SOC progress bar   |  big Voltage / Current / Power labels
      mid row  : statistics panel   |  inner QTabWidget
                                         1. Power
                                         2. V & I
                                         3. Now    (CellBarChart, live snapshot)
                                         4. Temps
                                         5. Cell Δ
                                         6. Ah Remaining
                                         7. Cell Voltages (24 hrs)
    """

    def __init__(self, bat_id: int):
        super().__init__()
        self.bat_id = bat_id
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── top row ──────────────────────────────────────────────────────────
        top = QHBoxLayout()

        soc_grp = QGroupBox("State of Charge")
        soc_grp.setMaximumWidth(720)
        soc_lay = QVBoxLayout(soc_grp)
        self.soc_bar   = QProgressBar()
        self.soc_bar.setRange(0, 100)
        self.soc_bar.setFixedHeight(26)
        self.soc_label = make_label("0%", C_GREEN, bold=True, size=14,
                                    align=Qt.AlignCenter)
        soc_lay.addWidget(self.soc_bar)
        soc_lay.addWidget(self.soc_label)
        top.addWidget(soc_grp, 1)

        vi_grp = QGroupBox("Voltage / Current")
        vi_lay = QGridLayout(vi_grp)
        self.v_big    = make_label("—", C_MEDBLUE,   bold=True, size=26, align=Qt.AlignCenter)
        self.i_big    = make_label("—", C_GREEN,  bold=True, size=26, align=Qt.AlignCenter)
        self.p_big    = make_label("—", C_YELLOW, bold=True, size=18, align=Qt.AlignCenter)
        self.flow_lbl = make_label("IDLE", C_DIM, bold=True, size=12, align=Qt.AlignCenter)
        vi_lay.addWidget(make_label("Voltage", C_DIM, size=10, align=Qt.AlignCenter), 0, 0)
        vi_lay.addWidget(make_label("Current", C_DIM, size=10, align=Qt.AlignCenter), 0, 1)
        vi_lay.addWidget(self.v_big,    1, 0)
        vi_lay.addWidget(self.i_big,    1, 1)
        vi_lay.addWidget(make_label("Power",  C_DIM, size=10, align=Qt.AlignCenter), 2, 0)
        vi_lay.addWidget(make_label("Flow",   C_DIM, size=10, align=Qt.AlignCenter), 2, 1)
        vi_lay.addWidget(self.p_big,    3, 0)
        vi_lay.addWidget(self.flow_lbl, 3, 1)
        top.addWidget(vi_grp, 1)
        root.addLayout(top)

        # ── middle ───────────────────────────────────────────────────────────
        mid = QHBoxLayout()

        # Stats panel
        stats_w = QWidget()
        stats_w.setMinimumWidth(195)
        stats_w.setMaximumWidth(280)
        sv = QVBoxLayout(stats_w)
        sv.setSpacing(2)
        sv.setContentsMargins(0, 0, 0, 0)

        stats_grp = QGroupBox("Battery Statistics")
        sg = QGridLayout(stats_grp)
        sg.setSpacing(4)

        def kv(label: str):
            lbl = make_value_label("—")
            row = sg.rowCount()
            sg.addWidget(make_label(label, C_DIM, size=10), row, 0)
            sg.addWidget(lbl, row, 1)
            return lbl

        self.remain_ah  = kv("Remaining Ah")
        self.remain_wh  = kv("Remaining Wh")
        self.full_ah    = kv("Full Capacity")
        self.soh_val    = kv("SOH")
        self.cycles     = kv("Discharge Cycles")
        self.cell_t     = kv("Cell Temp")
        self.mosfet_t   = kv("MOSFET Temp")
        self.c_min      = kv("Cell Min V")
        self.c_max      = kv("Cell Max V")
        self.c_delta    = kv("Cell Δ mV")
        self.protect    = kv("Protection")
        self.balance    = kv("Balancing")
        self.batt_state = kv("State")

        sv.addWidget(stats_grp)

        sv.addStretch()
        mid.addWidget(stats_w)

        # Inner 5-chart tab widget
        chart_tabs = QTabWidget()
        chart_tabs.setDocumentMode(True)
        name = BAT_NAME[self.bat_id]

        # ── Tab 1: Power ──────────────────────────────────────────────────
        # Plain line, NO fillLevel/brush area fill: pyqtgraph renders an
        # alpha-blended area fill in 50-point chunks (~860 fillPath calls
        # across a full 24 h @ 1 pt/2 s), which makes a "View All" repaint
        # freeze the UI for many seconds. Plain lines repaint in ~ms.
        p_plot = LiveLinePlot(pen=pg.mkPen(C_ORANGERED, width=1.5), name="Power")
        self.p_connector = make_connector(p_plot)
        self.p_chart     = make_chart(f"{name} – Power (24 hrs)", y_label="Watts")
        self.p_chart.addItem(p_plot)
        pw = QWidget(); pl = QVBoxLayout(pw); pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.p_chart)
        chart_tabs.addTab(pw, "Power")

        # ── Tab 2: Voltage & Current ──────────────────────────────────────
        vi_plot  = LiveLinePlot(pen=pg.mkPen(C_CYAN,  width=1.5), name="Voltage")
        amp_plot = LiveLinePlot(pen=pg.mkPen(C_GREEN, width=1.5), name="Current")
        self.v_connector = make_connector(vi_plot)
        self.i_connector = make_connector(amp_plot)
        self.vi_chart = make_chart(f"{name} – Voltage & Current (24 hrs)")
        self.vi_chart.addItem(vi_plot)
        self.vi_chart.addItem(amp_plot)
        viw = QWidget(); vil = QVBoxLayout(viw); vil.setContentsMargins(0, 0, 0, 0)
        vil.addWidget(self.vi_chart)
        chart_tabs.addTab(viw, "V && I")

        # ── Tab 3: Cells now (live snapshot, not the 24 h history) ────────
        self.cell_chart = CellBarChart(f"{name} – Cells Now", n_cells=16)
        cw = QWidget(); cl = QVBoxLayout(cw); cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.cell_chart)
        chart_tabs.addTab(cw, "Now")

        # ── Tab 4: Temperatures ───────────────────────────────────────────
        ct_plot = LiveLinePlot(pen=pg.mkPen(C_ORANGE, width=1.5), name="Cell Temp")
        mt_plot = LiveLinePlot(pen=pg.mkPen(C_RED,    width=1.5), name="MOSFET Temp")
        self.ct_connector = make_connector(ct_plot)
        self.mt_connector = make_connector(mt_plot)
        self.temp_chart   = make_chart(f"{name} – Temperatures (24 hrs)", y_label="°F")
        self.temp_chart.addItem(ct_plot)
        self.temp_chart.addItem(mt_plot)
        tw = QWidget(); tl = QVBoxLayout(tw); tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.temp_chart)
        chart_tabs.addTab(tw, "Temps")

        # ── Tab 5: Cell Δ ─────────────────────────────────────────────────
        d_plot = LiveLinePlot(pen=pg.mkPen(C_CYAN, width=1.5), name=f"{name} Δ mV")
        self.d_connector  = make_connector(d_plot)
        self.delta_chart  = make_chart(f"{name} – Cell Balance Delta (24 hrs)", y_label="mV")
        self.delta_chart.addItem(d_plot)
        dw = QWidget(); dl = QVBoxLayout(dw); dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.delta_chart)
        chart_tabs.addTab(dw, "Cell Δ")

        # ── Tab 6: Ah Remaining ───────────────────────────────────────────
        # Plain line, NO fillLevel/brush area fill (see the Tab 1 note).
        ah_plot = LiveLinePlot(pen=pg.mkPen(C_CYAN, width=1.5), name=f"{name} Remaining Ah")
        self.ah_connector = make_connector(ah_plot)
        self.ah_chart     = make_chart(f"{name} – Remaining Capacity (24 hrs)", y_label="Ah")
        self.ah_chart.addItem(ah_plot)
        ahw = QWidget(); ahl = QVBoxLayout(ahw); ahl.setContentsMargins(0, 0, 0, 0)
        ahl.addWidget(self.ah_chart)
        chart_tabs.addTab(ahw, "Ah Remaining")

        # ── Tab 7: Per-Cell Voltages (24 hrs) ──────────────────────────────
        # One chart, one trace per physical cell (16 for this pack), so
        # individual cell drift/balancing behavior can be tracked over time.
        self.cellv_chart = make_chart(f"{name} – Cell Voltages (24 hrs)", y_label="V")
        self.cellv_connectors = []
        n_cells = 16
        for i in range(n_cells):
            color = pg.intColor(i, hues=n_cells, values=1, maxValue=255)
            cv_plot = LiveLinePlot(pen=pg.mkPen(color, width=1), name=f"C{i+1:02d}")
            self.cellv_chart.addItem(cv_plot)
            self.cellv_connectors.append(make_connector(cv_plot))

        # Reserve a blank column on the right (via an invisible right axis)
        # and anchor the auto-created legend into it, so 16 entries don't
        # overlap the plotted traces. Note: the legend is normally parented
        # to the ViewBox, which clips its children to the plot rect — so it
        # must be reparented to the PlotItem itself (unclipped) before it
        # can be anchored out into the reserved right-hand column.
        right_axis = self.cellv_chart.getAxis("right")
        self.cellv_chart.showAxis("right")
        right_axis.setStyle(showValues=False, tickLength=0)
        right_axis.setPen(None)
        right_axis.setWidth(120)
        legend = self.cellv_chart.plotItem.legend
        if legend is not None:
            legend.setParentItem(self.cellv_chart.plotItem)
            legend.setColumnCount(1)
            legend.anchor(itemPos=(1, 0), parentPos=(1, 0), offset=(-5, 30))

        cvw = QWidget(); cvl = QVBoxLayout(cvw); cvl.setContentsMargins(0, 0, 0, 0)
        cvl.addWidget(self.cellv_chart)
        chart_tabs.addTab(cvw, "Cell Voltages")

        # ── Render performance (ported from the Chargers app) ─────────────
        # Full-history "View All" views (all 24 h @ 1 pt/2 s = 43 200 points
        # stay buffered in each connector deque for pan-back) are drawn with
        # pyqtgraph min/max "peak" downsampling — roughly one min/max pair
        # per 5 screen pixels, so spikes are preserved and zooming back in
        # restores full resolution automatically. In the normal marching
        # window no downsampling kicks in (ds = 1) and behavior is identical
        # to before; only wide views get the per-curve repaint speedup.
        # NOTES: this must be the PlotItem-level call made AFTER addItem (a
        # per-curve setDownsampling is overwritten by addItem), and
        # setClipToView is deliberately NOT used — pglive's LiveAxisRange
        # steers the marching window from plot.getData() (the display
        # dataset) every tick, so feeding the visible range back into the
        # display data would couple the view into that computation.
        for _w in (self.p_chart, self.vi_chart, self.temp_chart,
                   self.delta_chart, self.ah_chart, self.cellv_chart):
            _w.getPlotItem().setDownsampling(auto=True, mode='peak')

        # Hidden-chart render throttling (see the module-level note above the
        # _throttle_line_chart_pairs helper): only the DATA needs recording
        # every tick; hidden charts stop re-plotting their full deque and a
        # hidden -> visible transition catches up instantly.
        self._line_chart_pairs = [
            (self.p_chart,    [self.p_connector]),
            (self.vi_chart,   [self.v_connector, self.i_connector]),
            (self.temp_chart, [self.ct_connector, self.mt_connector]),
            (self.delta_chart,[self.d_connector]),
            (self.ah_chart,   [self.ah_connector]),
            (self.cellv_chart, self.cellv_connectors),
        ]
        # Instant refresh on inner-chart-tab click (fires after the new page
        # is shown); the per-tick call in refresh() is the self-healing
        # safety net (startup, minimize/restore, top-level tab switches).
        chart_tabs.currentChanged.connect(lambda *_: self._throttle_hidden_line_charts())

        mid.addWidget(chart_tabs, 1)
        root.addLayout(mid, 1)

    # ─────────────────────────────────────────────────────────────────────────
    def _throttle_hidden_line_charts(self):
        """Render only this panel's visible charts (see the _build_ui note)."""
        _throttle_line_chart_pairs(self._line_chart_pairs)

    # ─────────────────────────────────────────────────────────────────────────
    def refresh(self, d: dict):
        # Render only the visible charts first; the appends below stay
        # unconditional, so no data is ever lost and a chart that just
        # became visible catches up immediately.
        self._throttle_hidden_line_charts()
        ts = time.time()

        # SOC bar
        soc = d.get("soc", 0)
        self.soc_bar.setValue(soc)
        self.soc_bar.setStyleSheet(soc_bar_style(soc))
        self.soc_label.setText(f"{soc}%")
        self.soc_label.setStyleSheet(
            f"color:{soc_color(soc)}; font-size:14px; font-weight:bold;")

        # Big V / I / P
        v = d.get("total_voltage", 0.0)
        i = d.get("current",       0.0)
        p = d.get("power",         0.0)
        self.v_big.setText(f"{v:.2f} V")
        i_col = C_GREEN if i >= 0 else C_ORANGE
        self.i_big.setText(f"{i:+.2f} A")
        self.i_big.setStyleSheet(
            f"color:{i_col}; font-size:26px; font-weight:bold;")
        p_col = C_GREEN if p >= 1 else (C_ORANGE if p <= -1 else C_DIM)
        self.p_big.setText(f"{p:+.0f} W")
        self.p_big.setStyleSheet(
            f"color:{p_col}; font-size:18px; font-weight:bold;")
        flow = "CHARGING" if p >= 1 else ("DISCHARGING" if p <= -1 else "IDLE")
        self.flow_lbl.setText(flow)
        self.flow_lbl.setStyleSheet(
            f"color:{p_col}; font-size:12px; font-weight:bold;")

        # Note: per-battery "Time Remaining" was removed (item 6) — the
        # combined estimate on the Overview tab is the single source of truth.

        self.remain_ah.setText(f"{d.get('remaining_ah', 0):.1f} Ah")
        # Wh remaining = remaining Ah × pack voltage, shown in kWh to match
        # the Overview tab (firmware combined: totalRemWh/1000).
        _rem_wh = d.get('remaining_ah', 0.0) * d.get('total_voltage', 0.0)
        self.remain_wh.setText(f"{_rem_wh / 1000.0:.2f} kWh")
        self.full_ah.setText(f"{d.get('full_capacity_ah', 0):.1f} Ah")
        self.soh_val.setText(str(d.get("soh", "—")))
        self.cycles.setText(str(d.get("discharge_cycles", 0)))

        ct = d.get("cell_temp",   0)
        mt = d.get("mosfet_temp", 0)
        self.cell_t.setText(f"{c_to_f(ct):.0f} °F")
        self.mosfet_t.setText(f"{c_to_f(mt):.0f} °F")

        self.c_min.setText(f"{d.get('cell_min_v', 0):.3f} V (#{min_cell_num(d)})")
        self.c_max.setText(f"{d.get('cell_max_v', 0):.3f} V (#{max_cell_num(d)})")

        delta     = d.get("cell_delta_mv", 0.0)
        delta_col = C_GREEN if delta < 20 else (C_YELLOW if delta < 50 else C_RED)
        self.c_delta.setText(f"{delta:.1f} mV")
        self.c_delta.setStyleSheet(
            f"color:{delta_col}; font-size:13px; font-weight:bold;")

        # Decoded protection / balance / state
        prot_str = decode_protection(str(d.get("protection", "")))
        bal_str  = decode_balance(str(d.get("balancing", "")),
                                  str(d.get("battery_state", "")))
        st_str   = decode_battery_state(str(d.get("battery_state", "")))

        prot_col = C_RED if prot_str != "None" else C_GREEN
        bal_col  = C_CYAN if bal_str  != "None" else C_DIM

        self.protect.setText(prot_str)
        self.protect.setStyleSheet(
            f"color:{prot_col}; font-size:11px; font-weight:bold;")
        self.balance.setText(bal_str)
        self.balance.setStyleSheet(f"color:{bal_col}; font-size:11px;")
        self.batt_state.setText(st_str)

        # Feed time-series charts
        self.v_connector.cb_append_data_point(v,              ts)
        self.i_connector.cb_append_data_point(i,              ts)
        self.p_connector.cb_append_data_point(p,              ts)
        self.ct_connector.cb_append_data_point(c_to_f(ct),    ts)
        self.mt_connector.cb_append_data_point(c_to_f(mt),    ts)
        self.d_connector.cb_append_data_point(delta,          ts)
        self.ah_connector.cb_append_data_point(d.get("remaining_ah", 0.0), ts)

        # Per-cell voltage history (24 hrs) — one trace per physical cell
        cvs = d.get("cell_voltages", [])
        for i, conn in enumerate(self.cellv_connectors):
            if i < len(cvs):
                conn.cb_append_data_point(cvs[i], ts)

        # Cell scatter chart
        self.cell_chart.update_cells(d.get("cell_voltages", []))


# =============================================================================
#  ─── OVERVIEW TAB ────────────────────────────────────────────────────────────
# =============================================================================
class ClickableLabel(QLabel):
    """QLabel that emits a `clicked` signal on left-button release.
    Used for the Overview tab's app icon, which acts as a shortcut to the
    MPPT tab's Weather forecast sub-tab (see Window's hookup)."""
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(ev)


class OverviewTab(QWidget):
    def __init__(self):
        super().__init__()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(3)

        # ── status strip ─────────────────────────────────────────────────────
        # Each item is wrapped in a small vertical-stretch container so it is
        # guaranteed to sit at the vertical center of the row regardless of
        # how tall the row ends up (driven by the MQTT box / app icon), since
        # relying solely on QBoxLayout's per-item `alignment` flag does not
        # reliably vertically center widgets whose own sizeHint is small
        # relative to the row.
        def vcenter(widget):
            container = QWidget()
            vlay = QVBoxLayout(container)
            vlay.setContentsMargins(0, 0, 0, 0)
            vlay.setSpacing(0)
            vlay.addStretch(1)
            vlay.addWidget(widget)
            vlay.addStretch(1)
            return container

        # ── Merged status + SOC row ────────────────────────────────────────
        # Left: [MQTT] [B1] [B2] [MPPT] [ICON]  |  Right: [SOC progressbar] [timestamp]
        # The SOC row is eliminated as a separate row — its content moves to
        # the right side of this merged row, saving ~50px of vertical space.
        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        self.mqtt_indicator = MqttLedWidget()
        self.b1_led   = make_label(f"⬤  {BAT1_NAME}", C_RED,  bold=True, size=11)
        self.b2_led   = make_label(f"⬤  {BAT2_NAME}", C_RED,  bold=True, size=11)
        self.mppt_led = make_label("⬤  MPPT",          C_RED,  bold=True, size=11)
        top_row.addWidget(vcenter(self.mqtt_indicator))
        top_row.addSpacing(12)
        top_row.addWidget(vcenter(self.b1_led))
        top_row.addSpacing(12)
        top_row.addWidget(vcenter(self.b2_led))
        top_row.addSpacing(12)
        top_row.addWidget(vcenter(self.mppt_led))

        # App icon/logo — the divider between left and right sides
        # Clickable: jumps to the MPPT tab's Weather forecast sub-tab.
        self.logo_lbl = ClickableLabel()
        self.logo_lbl.setCursor(Qt.PointingHandCursor)
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.jpg")
        _pix = QPixmap(_icon_path)
        if not _pix.isNull():
            self.logo_lbl.setPixmap(
                _pix.scaledToHeight(48, Qt.SmoothTransformation))
        top_row.addWidget(vcenter(self.logo_lbl))

        # Right side: SOC progressbar groupbox + timestamp, stacked vertically
        right_side = QWidget()
        right_side.setMinimumWidth(640)
        right_lay = QVBoxLayout(right_side)
        right_lay.setSpacing(2)
        right_lay.setContentsMargins(0, 0, 0, 0)

        def make_soc_group(title):
            grp = QGroupBox(title)
            lay = QVBoxLayout(grp)
            lay.setSpacing(1)
            lay.setContentsMargins(4, 4, 4, 4)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setFixedHeight(24)
            bar.setMinimumWidth(600)
            v_lbl = make_label("0.00 V", C_MEDBLUE, bold=True, size=15, align=Qt.AlignCenter)
            lay.addWidget(bar)
            lay.addWidget(v_lbl)
            return grp, bar, v_lbl

        comb_soc_grp, self.comb_soc_bar, self.comb_v_lbl = \
            make_soc_group("Combined")
        right_lay.addStretch(1)
        right_lay.addWidget(comb_soc_grp, alignment=Qt.AlignCenter)
        right_lay.addStretch(1)

        self.ts_lbl = make_label("—", C_DIM, size=10,
                                 align=Qt.AlignRight | Qt.AlignVCenter)
        right_lay.addWidget(self.ts_lbl)

        top_row.addWidget(right_side, 1)
        root.addLayout(top_row)

        # ── MPPT + combined stats row ─────────────────────────────────────────
        mid = QHBoxLayout()
        mid.setSpacing(6)

        # MPPT Solar Controller panel — two aligned columns separated by a
        # vertical divider so left (measurements) and right (status) data
        # don't visually blend together, and every label/value row lines up.
        mppt_grp   = QGroupBox(f"MPPT Solar Controller {ARRAY_NAME}")
        mppt_outer = QHBoxLayout(mppt_grp)
        mppt_outer.setSpacing(8)

        mppt_left_w  = QWidget()
        mppt_left_g  = QGridLayout(mppt_left_w)
        mppt_left_g.setContentsMargins(0, 0, 0, 0)
        mppt_left_g.setSpacing(4)
        mppt_left_g.setColumnStretch(1, 1)

        def mv(grid, label, r):
            lbl = make_label(label, C_DIM, size=10, align=Qt.AlignLeft | Qt.AlignVCenter)
            val = make_value_label("—", C_GREEN, 12)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(val, r, 1)
            return val

        # All measurement + status fields now share a single column so the
        # freed-up right-hand side can host a big at-a-glance power readout.
        self.mppt_pv_w   = mv(mppt_left_g,  "PV Power",     0)
        self.mppt_batt_v = mv(mppt_left_g,  "Batt Voltage", 1)
        self.mppt_batt_a = mv(mppt_left_g,  "Batt Current", 2)
        self.mppt_yield  = mv(mppt_left_g,  "Yield Today",  3)
        self.mppt_state  = mv(mppt_left_g,  "State",        4)
        self.mppt_error  = mv(mppt_left_g,  "Error",        5)
        self.mppt_age    = mv(mppt_left_g,  "Last Seen",    6)

        # Absorb any extra vertical space into a trailing stretch row instead
        # of letting Qt distribute it across the data rows.
        mppt_left_g.setRowStretch(7, 1)

        # Prominent solid divider (a plain sunken QFrame::VLine is barely
        # visible against the dark theme).
        mppt_divider = QFrame()
        mppt_divider.setFrameShape(QFrame.NoFrame)
        mppt_divider.setFixedWidth(3)
        mppt_divider.setStyleSheet("background-color:#333333; border-radius:1px;")

        # Right side: big at-a-glance PV power readout (replaces the old
        # State/Error/Last Seen column, which now lives in the left grid).
        mppt_power_w = QWidget()
        mppt_power_lay = QVBoxLayout(mppt_power_w)
        mppt_power_lay.setContentsMargins(0, 0, 0, 0)
        mppt_power_lay.setSpacing(0)
        mppt_power_lay.addStretch(1)
        self.mppt_power_big = QLabel("—")
        self.mppt_power_big.setAlignment(Qt.AlignCenter)
        big_font = QFont()
        big_font.setPointSize(61)
        big_font.setBold(True)
        self.mppt_power_big.setFont(big_font)
        self.mppt_power_big.setStyleSheet("color:rgb(0, 0, 108);")
        self.mppt_power_big.setMinimumWidth(226)  # room for 4 digits at size 61
        mppt_power_unit = QLabel("WATTS")
        mppt_power_unit.setAlignment(Qt.AlignCenter)
        mppt_power_unit.setStyleSheet(f"color:{C_DIM}; font-size:11px; font-weight:bold; letter-spacing:2px;")
        mppt_power_lay.addWidget(self.mppt_power_big)
        mppt_power_lay.addWidget(mppt_power_unit)
        mppt_power_lay.addStretch(1)

        mppt_outer.addWidget(mppt_left_w,   1)
        mppt_outer.addWidget(mppt_divider)
        mppt_outer.addWidget(mppt_power_w,  1)
        mid.addWidget(mppt_grp, 2)

        # Combined stats
        comb_grp = QGroupBox("Combined")
        cg = QGridLayout(comb_grp)

        def cv(label, r):
            lbl = make_value_label("—")
            cg.addWidget(make_label(label, C_DIM, size=10, align=Qt.AlignLeft | Qt.AlignVCenter), r, 0)
            cg.addWidget(lbl, r, 1)
            return lbl

        self.avg_soc  = cv("Avg SOC",       0)
        self.tot_pwr  = cv("Total Power",   1)
        self.tot_cur  = cv("Total Current", 2)
        self.tot_rem  = cv("Remaining Ah",  3)
        self.tot_rem_wh = cv("Remaining Wh", 4)
        self.tot_rem_wh.setToolTip(
            "Remaining Ah × Pack Voltage ÷ 1000 = kWh")
        self.tot_time = cv("Time Rem",      5)
        self.flow_lbl = cv("Flow",          6)
        self.esp_uptime = cv("ESP32 Uptime", 7)
        self._last_uptime = -1
        self._uptime_change_time = time.time()
        mid.addWidget(comb_grp, 1)

        # Per-battery mini stats
        def mini_group(title):
            grp = QGroupBox(title)
            gg  = QGridLayout(grp)

            def bv(label, r):
                lbl = make_value_label("—")
                gg.addWidget(make_label(label, C_DIM, size=10, align=Qt.AlignLeft | Qt.AlignVCenter), r, 0)
                gg.addWidget(lbl, r, 1)
                return lbl

            v_ = bv("Voltage",  0)
            i_ = bv("Current",  1)
            p_ = bv("Power",    2)
            t_ = bv("Cell °F",  3)
            d_ = bv("Cell Δ",   4)
            pr_ = bv("Protect",  5)
            ba_ = bv("Balance",  6)
            return grp, v_, i_, p_, t_, d_, pr_, ba_

        b1m, self.b1_v, self.b1_i, self.b1_p, self.b1_tc, self.b1_delta, self.b1_prot, self.b1_bal = mini_group(BAT1_NAME)
        b2m, self.b2_v, self.b2_i, self.b2_p, self.b2_tc, self.b2_delta, self.b2_prot, self.b2_bal = mini_group(BAT2_NAME)
        mid.addWidget(b1m, 1)
        mid.addWidget(b2m, 1)
        root.addLayout(mid)

        # ── 24h charts ────────────────────────────────────────────────────────
        charts_split = QSplitter(Qt.Horizontal)

        soc1_plot   = LiveLinePlot(pen=pg.mkPen(C_CYAN,    width=2), name=BAT1_NAME)
        soc2_plot   = LiveLinePlot(pen=pg.mkPen(C_MAGENTA, width=2), name=BAT2_NAME)
        socavg_plot = LiveLinePlot(pen=pg.mkPen(C_GREEN,   width=2,
                                                style=Qt.DashLine), name="Avg")
        self.soc1_connector   = make_connector(soc1_plot)
        self.soc2_connector   = make_connector(soc2_plot)
        self.socavg_connector = make_connector(socavg_plot)
        self.soc_chart = make_chart("State of Charge – 24 Hours", y_label="Percent", roll=CHART_ROLL_SPLIT)
        self.soc_chart.addItem(soc1_plot)
        self.soc_chart.addItem(soc2_plot)
        self.soc_chart.addItem(socavg_plot)
        self.soc_chart.setYRange(0, 105)

        pwr_plot = LiveLinePlot(pen=pg.mkPen(C_ORANGERED, width=2), name="Total Power",
                                fillLevel=0, brush=(255, 69, 0, 50))
        # NOTE: this is the ONE chart that keeps its area fill (owner's
        # preference). The peak downsampling below shrinks the fill's
        # 50-point-chunk draw calls proportionally on wide views, so it is
        # far cheaper than an unsampled fill — just not as fast as a plain
        # line if you ever zoom out to a full 24 h.
        self.pwr_connector = make_connector(pwr_plot)
        cur_plot = LiveLinePlot(pen=pg.mkPen(C_GREEN, width=2), name="Total Current")
        self.cur_connector = make_connector(cur_plot)
        self.pwr_chart     = make_chart("Combined Power – 24 Hours", y_label="Watts", roll=CHART_ROLL_SPLIT)
        self.pwr_chart.addItem(pwr_plot)
        self.pwr_chart.addItem(cur_plot)

        # Left half: tabbed widget holding the SOC chart and Wh Remaining chart
        left_tabs = QTabWidget()
        left_tabs.setDocumentMode(True)

        soc_w = QWidget(); soc_l = QVBoxLayout(soc_w); soc_l.setContentsMargins(0, 0, 0, 0)
        soc_l.addWidget(self.soc_chart)
        left_tabs.addTab(soc_w, "SOC")

        # Plain line, NO fillLevel/brush area fill (see the BatteryPanel
        # Tab-1 note): an alpha-blended fill in 50-point chunks freezes the
        # UI for many seconds on a full-history "View All" repaint.
        wh_plot = LiveLinePlot(pen=pg.mkPen(C_BLUE, width=1.5), name="Wh Remaining")
        self.wh_rem_connector = make_connector(wh_plot)
        self.wh_rem_chart = make_chart("Wh Remaining – 24 Hours", y_label="kWh", roll=CHART_ROLL_SPLIT)
        self.wh_rem_chart.addItem(wh_plot)
        wh_w = QWidget(); wh_l = QVBoxLayout(wh_w); wh_l.setContentsMargins(0, 0, 0, 0)
        wh_l.addWidget(self.wh_rem_chart)
        left_tabs.addTab(wh_w, "Wh Remaining")

        # Right half: wrap pwr_chart in a container with a top spacer matching
        # the left tab widget's tab bar height, so both charts have equal
        # plot area height (the tab bar eats ~30px on the left side).
        right_w = QWidget()
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(0)
        _tab_bar_h = left_tabs.tabBar().sizeHint().height()
        right_l.addSpacing(_tab_bar_h)
        right_l.addWidget(self.pwr_chart)

        charts_split.addWidget(left_tabs)
        charts_split.addWidget(right_w)
        root.addWidget(charts_split, 1)

        # ── Render performance (ported from the Chargers app) ─────────────
        # PlotItem-level peak downsampling, applied AFTER addItem (see the
        # full note in BatteryPanel._build_ui).
        for _w in (self.soc_chart, self.wh_rem_chart, self.pwr_chart):
            _w.getPlotItem().setDownsampling(auto=True, mode='peak')

        # Hidden-chart render throttling (see the module-level note above the
        # _throttle_line_chart_pairs helper). All three charts also go dark
        # when the whole Overview tab is hidden behind another top-level tab
        # — the per-tick call in refresh() self-heals that within 2 s, and
        # Window's top-level tabs.currentChanged hookup makes the
        # transition instant.
        self._line_chart_pairs = [
            (self.soc_chart,    [self.soc1_connector, self.soc2_connector,
                                 self.socavg_connector]),
            (self.wh_rem_chart, [self.wh_rem_connector]),
            (self.pwr_chart,    [self.pwr_connector, self.cur_connector]),
        ]
        # Instant refresh on the SOC <-> Wh Remaining sub-tab click.
        left_tabs.currentChanged.connect(lambda *_: self._throttle_hidden_line_charts())

    # ─────────────────────────────────────────────────────────────────────────
    def _throttle_hidden_line_charts(self):
        """Render only this tab's visible charts (see the __init__ note)."""
        _throttle_line_chart_pairs(self._line_chart_pairs)

    # ─────────────────────────────────────────────────────────────────────────
    def refresh(self, b1d, b2d, comb, vict):
        # Render only the visible charts first; the appends below stay
        # unconditional, so no data is ever lost and a chart that just
        # became visible catches up immediately.
        self._throttle_hidden_line_charts()
        ts      = time.time()
        now_str = datetime.now().strftime("%H:%M:%S")
        self.ts_lbl.setText(f"Updated: {now_str}")

        # Connection LEDs
        b1_col   = C_GREEN if b1d.get("connected") else C_RED
        b2_col   = C_GREEN if b2d.get("connected") else C_RED
        mppt_col = C_GREEN if vict.get("valid")     else C_RED
        self.mqtt_indicator.set_connected(mqtt_connected)
        self.b1_led.setStyleSheet(  f"color:{b1_col};   font-weight:bold;")
        self.b2_led.setStyleSheet(  f"color:{b2_col};   font-weight:bold;")
        self.mppt_led.setStyleSheet(f"color:{mppt_col}; font-weight:bold;")

        # SOC row – only average connected batteries' SOC to avoid skewing the reading
        # when one BMS is disconnected (would otherwise report 0%)
        b1_conn = b1d.get("connected", False)
        b2_conn = b2d.get("connected", False)
        s1      = b1d.get("soc", 0)
        s2      = b2d.get("soc", 0)
        # Calculate avg only from connected batteries
        connected_count = sum([b1_conn, b2_conn])
        if connected_count > 0:
            calc_avg = (s1 + s2) / connected_count
        else:
            calc_avg = 0.0
        avg_soc = comb.get("soc_avg", calc_avg)
        v1      = b1d.get("total_voltage", 0.0)
        v2      = b2d.get("total_voltage", 0.0)
        v_avg   = (v1 + v2) / 2.0

        self.comb_soc_bar.setValue(int(round(avg_soc * 10)))
        self.comb_soc_bar.setFormat(f"{avg_soc:.1f}%")
        self.comb_soc_bar.setStyleSheet(soc_bar_style(int(avg_soc)))
        self.comb_v_lbl.setText(f"{v_avg:.2f} V")

        # MPPT panel
        valid = vict.get("valid", False)
        self.mppt_pv_w.setText( f"{vict.get('pv_w', 0):.0f} W"   if valid else "—")
        self.mppt_pv_w.setStyleSheet(
            f"color:{C_GREEN if valid else C_DIM}; font-size:12px; font-weight:bold;")
        pv_w = vict.get('pv_w', 0)
        self.mppt_power_big.setText(f"{pv_w:.0f}" if valid else "—")
        self.mppt_power_big.setStyleSheet(f"color:{'rgb(0, 0, 70)' if valid else C_DIM};")
        self.mppt_batt_v.setText(f"{vict.get('batt_v', 0):.2f} V" if valid else "—")
        self.mppt_batt_a.setText(f"{vict.get('batt_a', 0):+.1f} A" if valid else "—")
        s_str = decode_mppt_state(vict.get("state", 0)) if valid else "—"
        self.mppt_state.setText(s_str)
        state_col = MPPT_STATE_COLORS.get(s_str, C_GREEN) if valid else C_DIM
        self.mppt_state.setStyleSheet(
            f"color:{state_col}; font-size:12px; font-weight:bold;")
        err_code = vict.get("error", 0)
        err_str  = decode_mppt_error_short(err_code) if valid else "—"
        self.mppt_error.setText(err_str)
        self.mppt_error.setToolTip(decode_mppt_error(err_code) if (valid and err_code) else "")
        self.mppt_error.setStyleSheet(
            f"color:{C_RED if (valid and err_code) else C_GREEN};"
            f" font-size:12px; font-weight:bold;")
        self.mppt_yield.setText(f"{vict.get('yield_today', 0):.3f} kWh" if valid else "—")
        age = vict.get("last_seen_s", 9999)
        self.mppt_age.setText(f"{age}s ago" if (valid and age < 9999) else "—")

        # Combined stats
        tp   = comb.get("total_power",        0.0)
        tc   = comb.get("total_current",      0.0)
        tr   = comb.get("total_remaining_ah", 0.0)
        trs  = comb.get("time_remaining_s",   0)
        tdir = comb.get("time_direction",     "idle")
        flow = comb.get("flow",               "idle")

        # Once the pack has discharged past the reserve threshold the firmware
        # reports time_remaining_s = 0 with direction "to_empty", which would
        # make fmt_time() show "---" even though the battery is still draining.
        # Recompute a time-to-0% estimate from the remaining Ah and current so
        # the field keeps counting down to true empty instead of going blank.
        if tdir == "to_empty" and int(trs) <= 0 and tp < -1.0 and abs(tc) > 0.2 and tr > 0.0:
            trs  = int((tr / abs(tc)) * 3600.0)
            tdir = "to_zero"

        self.avg_soc.setText(f"{avg_soc:.1f}%")
        p_col = C_GREEN if tp >= 1 else (C_ORANGE if tp <= -1 else C_DIM)
        self.tot_pwr.setText(f"{tp:+.0f} W")
        self.tot_pwr.setStyleSheet(
            f"color:{p_col}; font-size:13px; font-weight:bold;")
        i_col = C_GREEN if tc >= 0 else C_ORANGE
        self.tot_cur.setText(f"{tc:+.2f} A")
        self.tot_cur.setStyleSheet(
            f"color:{i_col}; font-size:13px; font-weight:bold;")
        self.tot_rem.setText(f"{tr:.1f} Ah")
        # Wh remaining comes from the firmware combined payload (total_remaining_wh),
        # computed there as sum of (per-battery remaining Ah × pack voltage) in kWh.
        trw = comb.get("total_remaining_wh", 0.0)
        self.tot_rem_wh.setText(f"{trw:.2f} kWh")
        self.tot_time.setText(fmt_time(trs, tdir))
        self.flow_lbl.setText(flow.upper())
        self.flow_lbl.setStyleSheet(
            f"color:{flow_color(flow)}; font-size:13px; font-weight:bold;")
        # ESP32 uptime with stale detection
        uptime_val = comb.get("uptime_s", 0)
        now_stale = ts - self._uptime_change_time >= 10
        was_stale = getattr(self, "_uptime_was_stale", False)
        if uptime_val > self._last_uptime:
            self._last_uptime = uptime_val
            self._uptime_change_time = ts
            uptime_col = C_GREEN
            if was_stale:
                self._uptime_was_stale = False
                parent = self.parentWidget()
                while parent and not hasattr(parent, "_log"):
                    parent = parent.parentWidget()
                if parent:
                    parent._log(f"ESP32: uptime update resumed (now {fmt_uptime(uptime_val)})", C_GREEN)
        elif uptime_val < self._last_uptime:
            # ESP32 rebooted (uptime reset) - treat as fresh data
            self._last_uptime = uptime_val
            self._uptime_change_time = ts
            uptime_col = C_GREEN
            if was_stale:
                self._uptime_was_stale = False
                parent = self.parentWidget()
                while parent and not hasattr(parent, "_log"):
                    parent = parent.parentWidget()
                if parent:
                    parent._log(f"ESP32: uptime update resumed after reboot (now {fmt_uptime(uptime_val)})", C_GREEN)
        elif now_stale:
            uptime_col = C_RED
            if not was_stale:
                self._uptime_was_stale = True
                parent = self.parentWidget()
                while parent and not hasattr(parent, "_log"):
                    parent = parent.parentWidget()
                if parent:
                    parent._log(f"ESP32: No uptime update received - data may be stale (was {fmt_uptime(self._last_uptime)})", C_RED)
        else:
            uptime_col = C_GREEN
        self.esp_uptime.setText(fmt_uptime(uptime_val))
        self.esp_uptime.setStyleSheet(f"color:{uptime_col}; font-size:13px; font-weight:bold;")

        # Per-battery quick view
        for lbl_v, lbl_i, lbl_p, lbl_t, lbl_delta, lbl_prot, lbl_bal, d in [
            (self.b1_v, self.b1_i, self.b1_p, self.b1_tc, self.b1_delta, self.b1_prot, self.b1_bal, b1d),
            (self.b2_v, self.b2_i, self.b2_p, self.b2_tc, self.b2_delta, self.b2_prot, self.b2_bal, b2d),
        ]:
            bv   = d.get("total_voltage",    0.0)
            bi   = d.get("current",          0.0)
            bp   = d.get("power",            0.0)
            bct  = d.get("cell_temp",        0)
            bdl  = d.get("cell_delta_mv",    0.0)
            lbl_v.setText(f"{bv:.2f} V")
            lbl_i.setText(f"{bi:+.2f} A")
            lbl_i.setStyleSheet(
                f"color:{C_GREEN if bi >= 0 else C_ORANGE}; font-size:13px; font-weight:bold;")
            bp_col = C_GREEN if bp >= 1 else (C_ORANGE if bp <= -1 else C_DIM)
            lbl_p.setText(f"{bp:+.0f} W")
            lbl_p.setStyleSheet(
                f"color:{bp_col}; font-size:13px; font-weight:bold;")
            lbl_t.setText(f"{c_to_f(bct):.0f} °F")
            dl_col = C_GREEN if bdl < 20 else (C_YELLOW if bdl < 50 else C_RED)
            lbl_delta.setText(f"{bdl:.1f} mV")
            lbl_delta.setStyleSheet(
                f"color:{dl_col}; font-size:13px; font-weight:bold;")

            prot_str = decode_protection(str(d.get("protection", "")))
            bal_str  = decode_balance(str(d.get("balancing", "")),
                                      str(d.get("battery_state", "")))
            lbl_prot.setText(prot_str)
            lbl_prot.setStyleSheet(
                f"color:{C_RED if prot_str != 'None' else C_GREEN}; font-size:11px; font-weight:bold;")
            lbl_bal.setText(bal_str)
            lbl_bal.setStyleSheet(
                f"color:{C_CYAN if bal_str != 'None' else C_DIM}; font-size:11px;")

        # Time-series charts
        self.soc1_connector.cb_append_data_point(float(s1),       ts)
        self.soc2_connector.cb_append_data_point(float(s2),       ts)
        self.socavg_connector.cb_append_data_point(float(avg_soc), ts)
        self.pwr_connector.cb_append_data_point(float(tp),         ts)
        self.cur_connector.cb_append_data_point(float(tc),         ts)
        trw = comb.get("total_remaining_wh", 0.0)
        self.wh_rem_connector.cb_append_data_point(float(trw),     ts)


# =============================================================================
#  ─── MPPT CHART TAB ──────────────────────────────────────────────────────────
# =============================================================================
# Charger-state categories, seeded from SOLAR_STATE_DICT above. The bar chart
# (LiveCategorizedBarPlot) auto-adds any category it hasn't seen yet, so this
# list/color-map only needs to cover the common cases for nice initial colors.
MPPT_STATE_CATEGORIES = list(SOLAR_STATE_DICT.values())
MPPT_STATE_COLORS = {
    "Off":         "saddlebrown",
    "Low Power":   "#ffee58",
    "Fault":       "red",
    "Bulk":        "#206bee",
    "Absorption":  "orange",
    "Float":       "green",
    "Storage":     "orangered",
    "Equalize":    "magenta",
    "Power Supply": "pink",
    "Wake-Up":     "cyan",
    "EXT Control": "purple",
}

class _QuietWebPage(QWebEnginePage):
    """Suppress noisy JS console messages from embedded web pages, and make
    links that try to open a new window/tab (target="_blank", window.open(),
    ctrl-click, etc.) navigate in-place instead of silently doing nothing.

    QWebEngineView has no tab/window manager of its own, so by default
    createWindow() returns None and Chromium just drops the navigation —
    which is why most links on weather/news sites (many open forecasts,
    articles, and ads in a new tab) appear completely dead. Returning
    `self` here tells Chromium "navigate the requested URL in this same
    page" instead of trying to spawn a new top-level window we don't have.
    """
    def javaScriptConsoleMessage(self, level, message, line, source):
        pass  # discard all JS console output

    def createWindow(self, _type):
        return self


def _esp_help_html(esp_ip: str | None) -> str:
    """Instructions panel for the ESP Update tab. The ESP IP, OTA URL and
    curl line are filled in as soon as the retained <TOPIC_BASE>/ip message
    arrives; until then they show a 'waiting' placeholder. Regenerated by
    Window._apply_esp_ip() so the panel always matches the live device."""
    if esp_ip:
        base = f"http://{esp_ip}"
        ota_url = f"{base}/update"
        config_url = f"{base}/config"
        curl_cmd = (f"curl {base}/ota/start && "
                    f"curl -F &quot;file=@Litime-BMS_Victron-MPPT.ino.bin&quot; "
                    f"{base}/ota/upload")
        row_ip = f"<b style='color:{C_GREEN}'>{esp_ip}</b>"
        row_url = f"<a href='{ota_url}' style='color:{C_CYAN}'>{ota_url}</a>"
        row_config = f"<a href='{config_url}' style='color:{C_CYAN}'>{config_url}</a>"
        row_curl = f"<code style='color:{C_ORANGE}'>{curl_cmd}</code>"
        wait = ""
    else:
        row_ip = f"<i style='color:{C_DIM}'>waiting for {TOPIC_BASE}/ip…</i>"
        row_url = f"<i style='color:{C_DIM}'>waiting for {TOPIC_BASE}/ip…</i>"
        row_config = f"<i style='color:{C_DIM}'>waiting for {TOPIC_BASE}/ip…</i>"
        row_curl = f"<i style='color:{C_DIM}'>available once the IP is known</i>"
        wait = (f"<p style='color:{C_ORANGE}'>The ESP32 has not published its "
                f"IP yet — it does so on every MQTT (re)connect.</p>")

    auth = ("username <b>admin</b>, password from the device "
            "<code>/config</code> page") if OTA_PASSWORD else "none (no password set)"

    return f"""
<body style='margin:0;font-family:sans-serif;line-height:1.45'>
<h2 style='color:{C_CYAN};margin:2px 0 10px 0'>ESP32 Firmware Update (OTA)</h2>
{wait}
<p><b style='color:{C_TEXT}'>Workflow</b></p>
<ol style='padding-left:20px;margin:4px 0 10px 0'>
  <li>Compile the sketch in Arduino IDE — board <b>XIAO_ESP32S3</b>, Flash
      Size <b>8MB</b>, Partition <b>Default with spiffs (3MB APP/1.5MB
      SPIFFS)</b> (the board default — OTA-capable; any scheme whose name
      says &quot;No OTA&quot; will not work).</li>
  <li><i>Sketch → Export Compiled Binary</i> (Ctrl+Alt+S) — saves the
      <code>.bin</code> next to the sketch folder.</li>
  <li>On the panel to the right: click <b>Choose .bin…</b>, then press
      <b>Upload firmware</b>. Progress shows on the bar here (and on the
      physical LCD).</li>
  <li>The ESP32 flashes the <b>inactive</b> app partition, verifies it,
      and <b>auto-reboots</b> into it — it comes back online by itself
      after ~30 s.</li>
</ol>
<p><b style='color:{C_TEXT}'>This device</b></p>
<table style='margin:4px 0 10px 0'>
  <tr><td style='padding:2px 12px 2px 0;color:{C_DIM}'>ESP32 IP</td><td>{row_ip}</td></tr>
  <tr><td style='padding:2px 12px 2px 0;color:{C_DIM}'>OTA page</td><td>{row_url}</td></tr>
  <tr><td style='padding:2px 12px 2px 0;color:{C_DIM}'>Config page</td><td>{row_config}</td></tr>
  <tr><td style='padding:2px 12px 2px 0;color:{C_DIM}'>Page auth</td><td>{auth}</td></tr>
</table>
<p><b style='color:{C_TEXT}'>CLI shortcuts</b></p>
<p style='margin:4px 0'>Upload without a browser:</p>
<p style='margin:4px 0'>{row_curl}</p>
<p style='margin:4px 0'>Reboot via MQTT:</p>
<p style='margin:4px 0'><code style='color:{C_ORANGE}'>mosquitto_pub -h {MQTT_BROKER} -t {TOPIC_BASE}/cmd -m REBOOT</code>
&nbsp;— or just press <b>Reboot ESP32</b> above.</p>
<p><b style='color:{C_TEXT}'>Safety</b></p>
<p style='margin:4px 0;color:{C_DIM}'>OTA writes the <b>inactive</b> app
partition and swaps on reboot — a failed or interrupted upload can never
brick the device (the old firmware keeps running). The very first flash of
this feature (and any partition-scheme change) must still be done over
USB; after that the cable can stay in the drawer.</p>
</body>
"""


class MpptTab(QWidget):
    """Inner tab widget with one 24-hour live chart per MPPT metric,
    matching the per-battery inner chart-tab layout. A statistics panel
    (mirroring the Battery Statistics panel on the battery tabs) sits to
    the left of the charts."""

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        mid = QHBoxLayout()

        # ── Stats panel (mirrors Battery Statistics on the battery tabs) ──
        stats_w = QWidget()
        stats_w.setMinimumWidth(195)
        stats_w.setMaximumWidth(280)
        sv = QVBoxLayout(stats_w)
        sv.setSpacing(2)
        sv.setContentsMargins(0, 0, 0, 0)

        stats_grp = QGroupBox("MPPT Statistics")
        sg = QGridLayout(stats_grp)
        sg.setSpacing(4)

        def kv(label: str):
            lbl = make_value_label("—")
            row = sg.rowCount()
            sg.addWidget(make_label(label, C_DIM, size=10), row, 0)
            sg.addWidget(lbl, row, 1)
            return lbl

        self.pv_w_val    = kv("PV Power")
        self.batt_v_val  = kv("Batt Voltage")
        self.batt_a_val  = kv("Batt Current")
        self.yield_val   = kv("Yield Today")
        self.state_val   = kv("State")
        self.error_val   = kv("Error")
        self.age_val     = kv("Last Seen")

        sv.addWidget(stats_grp)

        # ── Combined SOC bar (mirrors the Overview tab's combined SOC) ────
        # Placed inside stats_w (max width 280) so it's width-constrained to
        # match the MPPT Statistics groupbox above it — no chart area lost.
        comb_grp = QGroupBox("Combined SOC")
        comb_lay = QVBoxLayout(comb_grp)
        comb_lay.setSpacing(3)
        self.comb_soc_bar = QProgressBar()
        self.comb_soc_bar.setRange(0, 1000)
        self.comb_soc_bar.setFixedHeight(20)
        self.comb_soc_bar.setFormat("0.0%")
        self.comb_soc_bar.setStyleSheet(soc_bar_style(0))
        comb_lay.addWidget(self.comb_soc_bar)
        sv.addWidget(comb_grp)

        sv.addStretch()
        mid.addWidget(stats_w)

        chart_tabs = QTabWidget()
        chart_tabs.setDocumentMode(True)
        self.chart_tabs = chart_tabs  # kept for show_weather() (icon shortcut)

        # One (chart, connector) pair per line-chart tab, collected for the
        # peak-downsampling pass and the hidden-chart throttle pairs below.
        self._mppt_charts = []

        def add_chart_tab(title, y_label, color=C_YELLOW, fill_brush=None):
            # Plain line by default, NO fillLevel/brush area fill (see the
            # BatteryPanel Tab-1 note): an alpha-blended fill in 50-point
            # chunks freezes the UI for many seconds on a full-history
            # "View All" repaint. fill_brush opts a chart into an area fill
            # (owner preference — same tradeoff as the Combined Power chart).
            if fill_brush is not None:
                plot = LiveLinePlot(pen=pg.mkPen(color, width=1.5), name=title,
                                    fillLevel=0, brush=fill_brush)
            else:
                plot = LiveLinePlot(pen=pg.mkPen(color, width=1.5), name=title)
            conn  = make_connector(plot)
            chart = make_chart(f"{title} – 24 hrs", y_label=y_label)
            chart.addItem(plot)
            self._mppt_charts.append((chart, conn))
            w = QWidget()
            l = QVBoxLayout(w); l.setContentsMargins(0, 0, 0, 0)
            l.addWidget(chart)
            chart_tabs.addTab(w, title)
            return conn

        self.c_pv_w   = add_chart_tab("PV Power",       "Watts", C_BLUE,
                                       fill_brush=(0, 0, 102, 200))
        self.c_batt_v = add_chart_tab("Battery Voltage", "V",     C_GREEN)
        self.c_batt_a = add_chart_tab("Battery Current", "A",     C_ORANGE)
        self.c_yield  = add_chart_tab("Yield Today",     "kWh",   C_MAGENTA)

        # ── Charger State — categorized bar chart ─────────────────────────
        # FastCategorizedBarPlot: drop-in replacement for pglive's
        # LiveCategorizedBarPlot (see the class note above) — full-day
        # history repaints encode only the new points instead of
        # re-drawing every bar with a per-point Python loop.
        self.state_plot = FastCategorizedBarPlot(
            MPPT_STATE_CATEGORIES,
            category_color=MPPT_STATE_COLORS,
            bar_height=0.9,
        )
        # Fixed cadence: 1 point / 2-s UI tick, NO update_rate cap: any cap
        # silently drops ticks that fire back-to-back after an event-loop
        # stall, losing whole seconds — and with them any state event
        # shorter than the stall. plot_rate=1/30 is only a repaint backstop;
        # refresh() forces the real repaint at each 30-s repaint step.
        self.state_connector = DataConnector(self.state_plot, max_points=CHART_MAX_PTS,
                                             plot_rate=1/30, ignore_auto_range=True)
        self.state_chart = make_category_chart("Charger State – 24 hrs", MPPT_STATE_CATEGORIES)
        self.state_chart.addItem(self.state_plot)
        # Fixed y-range: pglive autoranging is off for this chart
        # (ignore_auto_range=True on the connector).
        self.state_chart.setYRange(-1, len(MPPT_STATE_CATEGORIES))
        sw = QWidget(); sl = QVBoxLayout(sw); sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.state_chart)
        chart_tabs.addTab(sw, "Charger State")

        # Bar-chart view clocks (data-time driven, so UI-timer jitter can
        # never skip or double a step):
        #   _bar_paint_last — data-time of the last 30-s repaint
        #   _bar_view_right — right edge of the frozen window; the view
        #                     shifts only when the bar reaches it
        self._bar_paint_last = None
        self._bar_view_right = {}

        # Bar-chart corner "A" button: instantly restore the proper marching
        # window (newest bar ~1/3 from the left, fresh room on the right) —
        # replaces pglive's default handler, which replays a range that is
        # never refreshed for ignore_auto_range connectors. User zoom/pan
        # (sigRangeChangedManually — emitted by wheel, drag and rect-zoom,
        # but NEVER by our own programmatic setXRange re-pins) schedules
        # the same restore for the next 30-s refresh
        # (see _bar_manual_view_changed).
        self.state_chart.getPlotItem().autoBtn.clicked.disconnect()
        self.state_chart.getPlotItem().autoBtn.clicked.connect(
            lambda *_, c=self.state_connector, w=self.state_chart, n=len(MPPT_STATE_CATEGORIES):
                self._bar_autorange(c, w, n))
        self.state_chart.getPlotItem().vb.sigRangeChangedManually.connect(
            lambda *_, w=self.state_chart: self._bar_manual_view_changed(w))

        # ── Weather forecast browser ──────────────────────────────────────
        # A small nav toolbar (Back / Forward / Reload / Home) is added above
        # the view because many weather sites (e.g. MSN Weather) use
        # client-side/AJAX navigation for location changes rather than full
        # page loads, so Chromium's history often has nothing to go "back"
        # to and the right-click context-menu "Back" entry appears to do
        # nothing. These buttons give a reliable way to navigate regardless.
        self.weather_tab_index = None
        if WEATHER_URL:
            self.weather_view = QWebEngineView()
            _wpage = _QuietWebPage(self.weather_view)
            # Chromium's default page background is white; paint it the
            # dashboard dark grey instead so the tab never flashes a
            # harsh white surface while the page is loading/compositing
            # (most visible the first time the tab is shown).
            _wpage.setBackgroundColor(QColor(C_BG))
            self.weather_view.setPage(_wpage)
            self.weather_view.setUrl(QUrl(WEATHER_URL))

            nav_bar = QHBoxLayout()
            nav_bar.setContentsMargins(4, 4, 4, 4)
            nav_bar.setSpacing(4)

            btn_style = (
                f"QPushButton {{ background:{C_PANEL}; color:{C_TEXT};"
                f" border:1px solid {C_BORDER}; border-radius:3px; padding:3px 10px; }}"
                f"QPushButton:hover {{ background:#3a3a3a; }}"
                f"QPushButton:disabled {{ color:{C_DIM}; }}"
            )

            self.weather_back_btn = QPushButton("◀ Back")
            self.weather_fwd_btn  = QPushButton("Forward ▶")
            self.weather_reload_btn = QPushButton("⟲ Reload")
            self.weather_home_btn   = QPushButton("⌂ Home")
            for b in (self.weather_back_btn, self.weather_fwd_btn,
                      self.weather_reload_btn, self.weather_home_btn):
                b.setStyleSheet(btn_style)
                b.setCursor(Qt.PointingHandCursor)

            self.weather_back_btn.clicked.connect(self.weather_view.back)
            self.weather_fwd_btn.clicked.connect(self.weather_view.forward)
            self.weather_reload_btn.clicked.connect(self.weather_view.reload)
            self.weather_home_btn.clicked.connect(
                lambda: self.weather_view.setUrl(QUrl(WEATHER_URL)))

            self.weather_back_btn.setEnabled(False)
            self.weather_fwd_btn.setEnabled(False)

            def _update_nav_buttons(_url=None):
                hist = self.weather_view.page().history()
                self.weather_back_btn.setEnabled(hist.canGoBack())
                self.weather_fwd_btn.setEnabled(hist.canGoForward())

            self.weather_view.urlChanged.connect(_update_nav_buttons)
            self.weather_view.loadFinished.connect(_update_nav_buttons)

            nav_bar.addWidget(self.weather_back_btn)
            nav_bar.addWidget(self.weather_fwd_btn)
            nav_bar.addWidget(self.weather_reload_btn)
            nav_bar.addWidget(self.weather_home_btn)
            nav_bar.addStretch()

            ww = QWidget(); wl = QVBoxLayout(ww); wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(0)
            wl.addLayout(nav_bar)
            wl.addWidget(self.weather_view)
            chart_tabs.addTab(ww, "Weather")
            self.weather_tab_index = chart_tabs.count() - 1


        # ── Render performance (ported from the Chargers app) ─────────────
        # PlotItem-level peak downsampling for the MPPT line charts, applied
        # AFTER addItem (see the full note in BatteryPanel._build_ui). The
        # state bar chart is not a PlotDataItem and already repaints on the
        # throttled 30-s clock, so it is excluded.
        for _chart, _conn in self._mppt_charts:
            _chart.getPlotItem().setDownsampling(auto=True, mode='peak')

        # Hidden-chart render throttling (see the module-level note above
        # the _throttle_line_chart_pairs helper). The state bar chart is
        # excluded: with FastCategorizedBarPlot its forced 30-s repaints are
        # a few ms of incremental work even when hidden, and its view is
        # driven by the data-time march clocks in refresh(), not by pglive's
        # roll machinery.
        self._line_chart_pairs = [(chart, [conn]) for chart, conn in self._mppt_charts]
        # Instant refresh on inner-chart-tab click; the per-tick call in
        # refresh() is the self-healing safety net.
        chart_tabs.currentChanged.connect(lambda *_: self._throttle_hidden_line_charts())

        mid.addWidget(chart_tabs, 1)
        root.addLayout(mid)

    # ─────────────────────────────────────────────────────────────────────────
    def _throttle_hidden_line_charts(self):
        """Render only this tab's visible line charts (see the __init__ note)."""
        _throttle_line_chart_pairs(self._line_chart_pairs)

    def show_weather(self):
        """Select the inner Weather forecast sub-tab (no-op when WEATHER_URL
        is unset and the sub-tab was never added). Invoked by the Window
        when the Overview tab's app icon is clicked."""
        if self.weather_tab_index is not None:
            self.chart_tabs.setCurrentIndex(self.weather_tab_index)

    # ─────────────────────────────────────────────────────────────────────────
    def refresh(self, vict: dict, comb: dict = None):
        valid = vict.get("valid", False)

        # Render only this tab's visible charts; the appends below stay
        # unconditional, so no data is ever lost and a chart that just
        # became visible catches up immediately.
        self._throttle_hidden_line_charts()

        # Combined SOC bar
        if comb is not None:
            avg_soc = comb.get("soc_avg", 0.0)
            self.comb_soc_bar.setValue(int(round(avg_soc * 10)))
            self.comb_soc_bar.setFormat(f"{avg_soc:.1f}%")
            self.comb_soc_bar.setStyleSheet(soc_bar_style(int(avg_soc)))

        pv_w    = vict.get("pv_w", 0.0)
        batt_v  = vict.get("batt_v", 0.0)
        batt_a  = vict.get("batt_a", 0.0)
        yld     = vict.get("yield_today", 0.0)
        state_s = decode_mppt_state(vict.get("state", 0)) if valid else "—"
        err_c   = vict.get("error", 0)
        err_s   = decode_mppt_error(err_c) if valid and err_c else ("None" if valid else "—")
        age_s   = vict.get("last_seen_s", 9999)

        self.pv_w_val.setText(f"{pv_w:.0f} W"      if valid else "—")
        self.batt_v_val.setText(f"{batt_v:.2f} V"  if valid else "—")
        self.batt_a_val.setText(f"{batt_a:+.2f} A" if valid else "—")
        self.yield_val.setText(f"{yld:.3f} kWh"    if valid else "—")

        self.state_val.setText(state_s)
        self.state_val.setStyleSheet(
            f"color:{MPPT_STATE_COLORS.get(state_s, C_GREEN) if valid else C_DIM}; "
            f"font-size:13px; font-weight:bold;")

        err_col = C_RED if (valid and err_c) else (C_GREEN if valid else C_DIM)
        self.error_val.setText(err_s)
        self.error_val.setStyleSheet(f"color:{err_col}; font-size:13px; font-weight:bold;")

        if valid and age_s < 9999:
            self.age_val.setText(f"{age_s}s ago")
            self.age_val.setStyleSheet(
                f"color:{C_GREEN if age_s < 30 else C_ORANGE}; font-size:13px; font-weight:bold;")
        else:
            self.age_val.setText("—")
            self.age_val.setStyleSheet(f"color:{C_DIM}; font-size:13px; font-weight:bold;")

        # ---- Fixed-cadence chart appends (Chargers-style): one point per
        # 2-s UI tick no matter what, so max_points maps EXACTLY to wall
        # time (43 200 pts @ 2 s = precisely 24 hours). An invalid/missing
        # MPPT reading appends 0.0 — an honest flat line instead of a
        # misleading interpolation across the gap — and an EMPTY list to
        # the state bar chart, which closes any open bar so a stale state
        # can't falsely bridge the outage, while the 24-h dequeue, the 30-s
        # repaints and the view clocks all stay alive.
        ts = time.time()
        self.c_pv_w.cb_append_data_point(  float(pv_w),   ts)
        self.c_batt_v.cb_append_data_point(float(batt_v), ts)
        self.c_batt_a.cb_append_data_point(float(batt_a), ts)
        self.c_yield.cb_append_data_point( float(yld),    ts)

        # ---- Bar-chart view control: repaint every 30 s; shift only when
        # the bar reaches the right edge (march behaviour like the line
        # charts' LiveAxisRange roll). The window is FROZEN between shifts:
        # the bar's leading edge marches right 30 s per repaint across the
        # BAR_HEAD_S of room, and only when it reaches the right edge does
        # the view shift forward — newest bar back to ~1/3 from the left.
        # Both clocks are data-time driven (not wall-clock modulo), so
        # UI-timer jitter can never skip or double a step, and both run
        # BEFORE this tick's append so the forced repaint lands on this
        # very append. ANY manual view change (zoom, pan, "View All")
        # self-heals back to the proper marching window at the next 30-s
        # refresh, and the corner "A" button restores it instantly; the
        # full 24 h @ 2 s/pt stays in the connector deque for pan-back and
        # look-backs.
        conn = self.state_connector
        if self._bar_paint_last is None or (ts - self._bar_paint_last) >= BAR_PIN_S:
            self._bar_paint_last = ts
            # Age last_plot so this tick's append repaints the picture
            # (exactly one repaint per 30-s step, the leading edge
            # advancing 30 s rightward each time).
            conn.last_plot = time.perf_counter() - 3600.0
            # Stale-view self-heal: if this chart's VISIBLE right edge is
            # behind the newest buffered point (right-click "View All" or
            # a zoom the bar has outrun — neither is reported through
            # sigRangeChangedManually), the bar end is off-screen. Expire
            # the frozen window NOW so the shift block below restores the
            # proper view on this very 30-s refresh.
            if len(conn.x) and self.state_chart.getViewBox().viewRange()[0][1] < conn.x[-1]:
                self._bar_view_right[self.state_chart] = ts

        right = self._bar_view_right.get(self.state_chart)
        if right is None or ts >= right:
            # Frozen window expired (or first tick): shift forward. Left
            # clamps to the oldest buffered point until BAR_TAIL_S of
            # history exists, so the trace STARTS at the LEFT edge and
            # marches right.
            left = max(conn.x[0], ts - BAR_TAIL_S) if len(conn.x) else ts
            self.state_chart.setXRange(left, ts + BAR_HEAD_S, padding=0)
            # Correct the y-range (authoritative category count)
            self.state_chart.setYRange(-1, len(MPPT_STATE_CATEGORIES))
            self._bar_view_right[self.state_chart] = ts + BAR_HEAD_S
            # Repaint together with the shift
            conn.last_plot = time.perf_counter() - 3600.0

        # NEVER append the "—" placeholder as a category: an invalid reading
        # gets an empty list, which closes any open bar.
        self.state_connector.cb_append_data_point([state_s] if valid else [], ts)

    # ─────────────────────────────────────────────────────────────────────────
    def _bar_autorange(self, conn, widget, n_cats):
        """Corner "A" button for the categorized bar chart: instantly
        restore the proper marching window — newest bar ~1/3 from the left
        edge, BAR_HEAD_S of fresh room on the right — exactly like the line
        charts' auto button resuming the live roll. Replaces pglive's
        default handler, which replays a range that is never refreshed for
        ignore_auto_range connectors. Full-history look-back remains
        available via pan or right-click "View All" (both self-heal back to
        the marching window at the next 30-s refresh)."""
        if len(conn.x):
            newest = conn.x[-1]
            left = max(conn.x[0], newest - BAR_TAIL_S)
            widget.setXRange(left, newest + BAR_HEAD_S, padding=0)
            self._bar_view_right[widget] = newest + BAR_HEAD_S
        widget.setYRange(-1, n_cats)

    def _bar_manual_view_changed(self, widget):
        """User zoom/pan/rect-zoom on the bar chart (sigRangeChangedManually
        — never emitted by our own programmatic setXRange re-pins): schedule
        the proper marching window to be restored at the NEXT 30-s refresh
        by shortening this chart's frozen right edge to the upcoming repaint
        step, where the regular shift logic re-pins the view."""
        new_edge = (self._bar_paint_last + BAR_PIN_S) if self._bar_paint_last is not None else time.time()
        edge = self._bar_view_right.get(widget)
        if edge is None or new_edge < edge:
            self._bar_view_right[widget] = new_edge


# =============================================================================
#  ─── LATCHED ALARM STATE MACHINE ─────────────────────────────────────────────
# =============================================================================
class LatchedAlarm:
    """Hysteresis-based alarm with one-shot logging and recovery cooldown.

    States:  idle → active → recovering → idle

    * idle → active:        value crosses an enter threshold → log the alarm once.
    * active (same):        value stays above exit threshold → suppress all repeats.
    * active → escalate:    value crosses a higher enter threshold → log escalation.
    * active → recovering:  value drops below ALL exit thresholds → log recovery.
    * recovering → active (within cooldown): same condition returns → suppress.
    * recovering → idle (after cooldown): silently re-arm for new events.

    The hysteresis gap (enter > exit) prevents threshold-jitter from causing
    false recoveries, and the cooldown window prevents the same flappy event
    from re-logging for ALARM_COOLDOWN_S after it first clears.
    """

    def __init__(self, levels: list, cooldown_s: float = 900):
        """
        levels: list of level dicts, highest severity first:
            [{"name": "ALARM",   "enter": 50, "exit": 45, "color": C_RED},
             {"name": "WARNING", "enter": 20, "exit": 15, "color": C_ORANGE}]
        cooldown_s: seconds after recovery before re-arming (idle).
        """
        self.levels       = levels
        self.cooldown     = cooldown_s
        self.state        = "idle"          # idle | active | recovering
        self.logged_level = None            # highest severity name logged this period
        self.logged       = False           # whether we logged anything this period
        self.active_t     = 0.0
        self.recover_t    = 0.0

    def _level_index(self, name: str) -> int:
        for i, lv in enumerate(self.levels):
            if lv["name"] == name:
                return i
        return 999

    def _current_level(self, val: float):
        """Return the highest-severity level whose enter threshold is met."""
        for lv in self.levels:
            if val >= lv["enter"]:
                return lv
        return None

    def _recovered(self, val: float) -> bool:
        """True when val is below ALL exit thresholds."""
        return all(val < lv["exit"] for lv in self.levels)

    def update(self, val: float, now: float):
        """Process a new value. Returns a result dict or None (suppress).

        Result dict keys:
            action:   "enter" | "escalate" | "recover"
            level:    the level dict (for enter/escalate)
            duration: seconds since active_t (for recover)
        """
        lvl = self._current_level(val)

        # ── idle ───────────────────────────────────────────────────────────
        if self.state == "idle":
            if lvl is not None:
                self.state        = "active"
                self.logged_level = lvl["name"]
                self.logged       = True
                self.active_t     = now
                return {"action": "enter", "level": lvl}
            return None

        # ── active ─────────────────────────────────────────────────────────
        if self.state == "active":
            if lvl is not None:
                # Still in an alarm/warning zone – check for escalation
                if self._level_index(lvl["name"]) < self._level_index(self.logged_level):
                    self.logged_level = lvl["name"]
                    self.logged       = True
                    return {"action": "escalate", "level": lvl}
                return None    # same or lower level – suppress
            if self._recovered(val):
                self.state     = "recovering"
                self.recover_t = now
                if self.logged:
                    return {"action": "recover", "duration": now - self.active_t}
                return None
            return None        # hysteresis gap – still active, suppress

        # ── recovering ─────────────────────────────────────────────────────
        if self.state == "recovering":
            if lvl is not None:
                # Condition returned within cooldown – suppress
                self.state  = "active"
                self.logged = False
                return None
            if now - self.recover_t >= self.cooldown:
                self.state        = "idle"
                self.logged_level = None
                self.logged       = False
                return None
            return None

        return None


# =============================================================================
#  ─── MAIN WINDOW ─────────────────────────────────────────────────────────────
# =============================================================================
class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LiTime Dual BMS + Victron Monitor")
        self.setWindowIcon(QtGui.QIcon('icon.jpg'))
        self.resize(1280, 800)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.overview_tab = OverviewTab()
        self.bat1_tab     = BatteryPanel(1)
        self.bat2_tab     = BatteryPanel(2)
        self.mppt_tab     = MpptTab()
        self.log_tab      = QTextBrowser()
        self.log_tab.setOpenExternalLinks(False)

        # MQTT Raw tab — QTableWidget for flash-free, in-place cell updates.
        self.mqtt_raw_tab = QTableWidget()
        self.mqtt_raw_tab.setColumnCount(2)
        self.mqtt_raw_tab.setHorizontalHeaderLabels(["Topic", "Payload"])
        self.mqtt_raw_tab.verticalHeader().setVisible(False)
        self.mqtt_raw_tab.horizontalHeader().setStretchLastSection(True)
        self.mqtt_raw_tab.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.mqtt_raw_tab.setEditTriggers(QTableWidget.NoEditTriggers)
        self.mqtt_raw_tab.setAlternatingRowColors(True)
        self.mqtt_raw_tab.setWordWrap(True)
        self.mqtt_raw_tab.setTextElideMode(Qt.ElideNone)
        # Per-pixel scroll mode so the scrollbar operates in pixels (not whole
        # rows), which makes setSingleStep actually take effect and gives
        # smooth, fine-grained scrolling over the tall word-wrapped JSON rows.
        self.mqtt_raw_tab.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.mqtt_raw_tab.verticalScrollBar().setSingleStep(10)
        self.mqtt_raw_tab.setStyleSheet(
            f"QTableWidget {{ background: #111; color: {C_TEXT};"
            f" gridline-color: {C_BORDER}; alternate-background-color: #1a1a1a; }}"
            f"QHeaderView::section {{ background: {C_PANEL}; color: {C_DARKORANGE};"
            f" font-weight: bold; border: 1px solid {C_BORDER}; padding: 4px; }}")
        self._raw_mqtt: dict[str, str] = {}

        # ESP Update tab — embedded OTA upload page + instructions. The
        # ESP32's IP arrives later via the retained <TOPIC_BASE>/ip message
        # (published by the firmware on every MQTT connect).
        self._esp_ip = None
        self.esp_update_tab = self._build_esp_update_tab()

        # Wrap Log + MQTT Raw + ESP Update in a sub-tab widget under a single "Log" tab.
        self.log_container = QTabWidget()
        self.log_container.setDocumentMode(True)
        self.log_container.addTab(self.log_tab,      "Event Log")
        self.log_container.addTab(self.mqtt_raw_tab, "MQTT Raw")
        self.log_container.addTab(self.esp_update_tab, "ESP Update")

        # Tabs: Overview | Battery 1 | Battery 2 | MPPT | Log
        self.tabs.addTab(self.overview_tab,  "Overview")
        self.tabs.addTab(self.bat1_tab,      BAT1_NAME)
        self.tabs.addTab(self.bat2_tab,      BAT2_NAME)
        self.tabs.addTab(self.mppt_tab,      "MPPT")
        self.tabs.addTab(self.log_container, "Log")

        # Instant catch-up on top-level tab switches (ported from the
        # Chargers app's tabWidget.currentChanged hookup): re-plot the
        # buffered history of the newly visible tab's charts right away
        # instead of at the next 2-s refresh tick. The per-tick throttle call
        # at the top of each tab's refresh() is the self-healing safety net
        # (startup, minimize/restore); this makes the transition instant.
        self.tabs.currentChanged.connect(lambda *_: (
            self.overview_tab._throttle_hidden_line_charts(),
            self.bat1_tab._throttle_hidden_line_charts(),
            self.bat2_tab._throttle_hidden_line_charts(),
            self.mppt_tab._throttle_hidden_line_charts(),
        ))

        # Clicking the app icon on the Overview tab jumps straight to the
        # MPPT tab's Weather forecast sub-tab (switch the top-level tab
        # first, then select the inner sub-tab).
        self.overview_tab.logo_lbl.clicked.connect(
            lambda: (self.tabs.setCurrentWidget(self.mppt_tab),
                     self.mppt_tab.show_weather()))

        # Status bar
        self.status_mqtt = QLabel("MQTT: connecting…")
        self.status_mqtt.setStyleSheet(f"color:{C_DIM}; padding:0 8px;")
        self.statusBar().addPermanentWidget(self.status_mqtt)
        self.statusBar().setStyleSheet(f"background:{C_PANEL}; color:{C_DIM};")

        # MQTT signals
        mqtt_signals.connected.connect(self._on_mqtt_connect)
        mqtt_signals.disconnected.connect(self._on_mqtt_disconnect)
        mqtt_signals.message_received.connect(self._on_message)
        mqtt_signals.mqtt_error.connect(self._on_mqtt_error)

        # Force an immediate MQTT Raw refresh when the user switches to it
        self.log_container.currentChanged.connect(self._on_log_subtab_changed)

        # UI refresh every 2 s
        self.timer = QTimer()
        self.timer.timeout.connect(self._refresh_ui)
        self.timer.start(2000)

        self._init_event_tracking()
        self._log(f"App started – connecting to {MQTT_BROKER}:{MQTT_PORT}", C_DIM)

    # ─────────────────────────────────────────────────────────────────────────
    #  ESP Update tab (Log ▸ ESP Update)
    #  Left: workflow instructions + live IP/URL. Right: a native Qt upload
    #  panel (file picker + progress bar + status) that POSTs the .bin
    #  straight to the firmware's ElegantOTA endpoint at
    #  http://<esp-ip>/update, plus a Reboot button that publishes REBOOT
    #  to <TOPIC_BASE>/cmd. Styled by the app's global dark stylesheet.
    # ─────────────────────────────────────────────────────────────────────────
    def _build_esp_update_tab(self) -> QWidget:
        w = QWidget()
        root = QHBoxLayout(w)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # ── Left: instructions panel (QTextBrowser, like the Event Log) ───
        self.esp_help = QTextBrowser()
        self.esp_help.setOpenExternalLinks(True)   # OTA URL opens in a browser
        self.esp_help.setStyleSheet(
            f"QTextBrowser {{ background:{C_BG}; color:{C_TEXT};"
            f" border:1px solid {C_BORDER}; font-size:13px; }}"
        )
        self.esp_help.setMinimumWidth(340)
        self.esp_help.setMaximumWidth(560)
        self.esp_help.setHtml(_esp_help_html(None))
        root.addWidget(self.esp_help)

        # ── Right: native upload panel (matches the app's dark theme). No
        #    embedded web view — ElegantOTA's stock UI is light-themed and
        #    its dark toggle misbehaves inside QtWebEngine, so we drive the
        #    SAME HTTP POST endpoint (http://<esp-ip>/update) directly from
        #    Qt — exactly what curl -F does. ────────────────────────────────
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.setSpacing(10)

        grp = QGroupBox("Firmware Upload")
        gl = QVBoxLayout(grp)
        gl.setSpacing(8)

        file_row = QHBoxLayout()
        self.ota_file_lbl = QLabel("No firmware file selected")
        self.ota_file_lbl.setStyleSheet(f"color:{C_DIM};")
        self.ota_choose_btn = QPushButton("Choose .bin…")
        self.ota_choose_btn.setCursor(Qt.PointingHandCursor)
        self.ota_choose_btn.clicked.connect(self._ota_choose_file)
        file_row.addWidget(self.ota_file_lbl, 1)
        file_row.addWidget(self.ota_choose_btn)
        gl.addLayout(file_row)

        btn_row = QHBoxLayout()
        self.ota_upload_btn = QPushButton("⬆ Upload firmware")
        self.reboot_btn     = QPushButton("⟳ Reboot ESP32")
        for b in (self.ota_upload_btn, self.reboot_btn):
            b.setCursor(Qt.PointingHandCursor)
        self.ota_upload_btn.setEnabled(False)
        self.reboot_btn.setEnabled(False)
        self.ota_upload_btn.clicked.connect(self._ota_start_upload)
        self.reboot_btn.clicked.connect(self._reboot_esp32)
        btn_row.addWidget(self.ota_upload_btn)
        btn_row.addWidget(self.reboot_btn)
        btn_row.addStretch()
        gl.addLayout(btn_row)

        self.ota_progress = QProgressBar()
        self.ota_progress.setRange(0, 100)
        self.ota_progress.setValue(0)
        gl.addWidget(self.ota_progress)

        self.ota_status_lbl = QLabel("Waiting for the ESP32 to report its IP…")
        self.ota_status_lbl.setWordWrap(True)
        gl.addWidget(self.ota_status_lbl)

        rl.addWidget(grp)
        rl.addStretch()

        # QNetworkAccessManager for the OTA POST (kept alive on the Window)
        self._ota_nam = QNetworkAccessManager(self)

        # Post-flash / post-reboot return-watch state: after a successful
        # OTA upload (or a Reboot command) we follow the device through its
        # reboot cycle (LWT "offline" → fresh boot "online") and report it,
        # with a 3-minute safety net in case it never comes back.
        self._esp_reboot_t0 = None            # None = not watching
        self._esp_reboot_reason = ""          # "ota" | "cmd"
        self._esp_reboot_timer = QTimer(self)
        self._esp_reboot_timer.setSingleShot(True)
        self._esp_reboot_timer.timeout.connect(self._esp_reboot_timeout)

        root.addWidget(right, 1)
        return w

    def _ota_page_url(self) -> str:
        """ElegantOTA's browser page (kept for the help panel / browser use);
        the native uploader talks to /ota/start + /ota/upload instead."""
        return f"http://{self._esp_ip}/update" if self._esp_ip else ""

    def _apply_esp_ip(self, ip: str):
        """Fill in everything that depends on the ESP32's IP: the help
        panel's live rows and the upload-panel buttons."""
        self._esp_ip = ip
        self.esp_help.setHtml(_esp_help_html(ip))
        self._ota_update_buttons()
        self.ota_status_lbl.setText("Ready — the ESP32 is available for uploads.")

    # ── OTA upload (HTTP POST to ElegantOTA's /update endpoint) ──────────────
    def _ota_choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select firmware binary", os.path.expanduser("~"),
            "Firmware (*.bin *.bin.gz)")
        if not path:
            return
        self._ota_file_path = path
        try:
            size_kb = os.path.getsize(path) / 1024
        except OSError:
            size_kb = 0
        self.ota_file_lbl.setText(
            f"{os.path.basename(path)}  ({size_kb:,.0f} KB)")
        self.ota_file_lbl.setStyleSheet(f"color:{C_TEXT};")
        self._ota_update_buttons()

    def _ota_update_buttons(self):
        """Upload needs: a chosen file + the ESP IP + no upload in flight.
        Reboot needs: the ESP online (broker_status) + no upload in flight."""
        uploading = (getattr(self, "_ota_reply", None) is not None
                     or getattr(self, "_ota_start_reply", None) is not None)
        have_file = getattr(self, "_ota_file_path", None) is not None
        self.ota_upload_btn.setEnabled(have_file and self._esp_ip is not None
                                       and not uploading)
        self.reboot_btn.setEnabled(broker_status == "online"
                                   and self._esp_ip is not None
                                   and not uploading)

    def _ota_start_upload(self):
        """Two-step ElegantOTA 3.x handshake (exactly what its stock web UI
        does behind the scenes):
          1. GET /ota/start?hash=<md5>   — opens the OTA session on the
             device; the MD5 makes the flasher verify the written image.
          2. POST multipart/form-data to /ota/upload — the .bin itself.
        The device flashes the inactive app partition, and reboots ~2 s
        after the POST returns 200."""
        if not self._esp_ip or getattr(self, "_ota_file_path", None) is None:
            return

        # MD5 so Update.end() verifies the written image against the file
        try:
            with open(self._ota_file_path, "rb") as fh:
                md5 = hashlib.md5(fh.read()).hexdigest()
        except OSError as e:
            self.ota_status_lbl.setText(f"Cannot read {self._ota_file_path}: {e}")
            self._log(f"OTA upload: cannot read {self._ota_file_path}: {e}", C_RED)
            return

        start_url = QUrl(f"http://{self._esp_ip}/ota/start?hash={md5}")
        self._ota_start_reply = self._ota_nam.get(QNetworkRequest(start_url))
        self._ota_start_reply.finished.connect(self._ota_start_done)

        self.ota_progress.setRange(0, 100)   # (re-)arm after any busy animation
        self.ota_progress.setValue(0)
        self.ota_status_lbl.setText("Starting OTA session on the ESP32…")
        self.ota_upload_btn.setEnabled(False)
        self.reboot_btn.setEnabled(False)
        self._log(f"OTA session starting: {os.path.basename(self._ota_file_path)}"
                  f" (md5 {md5})", C_CYAN)

    def _ota_start_done(self):
        """Step 2: the /ota/start GET finished — now stream the file."""
        reply = self._ota_start_reply
        self._ota_start_reply = None
        code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if code != 200:
            body = bytes(reply.readAll()).decode(errors="replace")[:120]
            self.ota_status_lbl.setText(
                f"Could not start OTA session (HTTP {code}) {body}")
            self._log(f"OTA start FAILED (HTTP {code}) {body}", C_RED)
            reply.deleteLater()
            self._ota_update_buttons()
            return
        reply.deleteLater()

        f = QFile(self._ota_file_path)
        if not f.open(QIODevice.ReadOnly):
            self.ota_status_lbl.setText(f"Cannot open {self._ota_file_path}")
            self._log(f"OTA upload: cannot open {self._ota_file_path}", C_RED)
            self._ota_update_buttons()
            return
        self._ota_file = f   # keep the QFile alive for the whole upload

        part = QHttpPart()
        part.setHeader(QNetworkRequest.ContentTypeHeader,
                       "application/octet-stream")
        part.setHeader(QNetworkRequest.ContentDispositionHeader,
                       'form-data; name="file"; filename="firmware.bin"')
        part.setBodyDevice(f)

        multi = QHttpMultiPart(QHttpMultiPart.FormDataType)
        f.setParent(multi)   # QFile lifetime tied to the multipart container
        multi.append(part)

        upload_url = QUrl(f"http://{self._esp_ip}/ota/upload")
        req = QNetworkRequest(upload_url)
        if OTA_PASSWORD:
            token = base64.b64encode(
                f"admin:{OTA_PASSWORD}".encode()).decode()
            req.setRawHeader(b"Authorization", b"Basic " + token.encode())

        self._ota_multi = multi            # keep alive during the upload
        self._ota_reply = self._ota_nam.post(req, multi)
        self._ota_reply.uploadProgress.connect(self._ota_upload_progress)
        self._ota_reply.finished.connect(self._ota_upload_finished)

        self.ota_status_lbl.setText(
            f"Uploading to http://{self._esp_ip}/ota/upload …")

    def _ota_upload_progress(self, sent, total):
        if total > 0:
            self.ota_progress.setValue(int(sent * 100 / total))
            self.ota_status_lbl.setText(
                f"Uploading… {sent / 1024:,.0f} / {total / 1024:,.0f} KB")

    def _ota_upload_finished(self):
        reply = self._ota_reply
        self._ota_reply = None
        code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        err  = reply.error()
        if err == QNetworkReply.NoError and code == 200:
            self.ota_progress.setRange(0, 0)   # busy animation: awaiting reboot
            self.ota_status_lbl.setText(
                "Upload complete — ESP32 is rebooting…")
            self._log("OTA upload OK (verified md5, HTTP 200) – ESP32 "
                      "rebooting into the new firmware", C_GREEN)
            self._esp_reboot_begin("ota")
        else:
            body = bytes(reply.readAll()).decode(errors="replace")[:160]
            self.ota_status_lbl.setText(
                f"Upload failed (HTTP {code} — {err}). {body} "
                "The old firmware is still running.")
            self._log(f"OTA upload FAILED (HTTP {code} — {err}) {body} – "
                      "device kept its old firmware", C_RED)
        reply.deleteLater()
        self._ota_file = None
        self._ota_multi = None
        self._ota_update_buttons()

    def _reboot_esp32(self):
        """Publish REBOOT to <TOPIC_BASE>/cmd — the firmware's MQTT callback
        publishes "rebooting", cleanly disconnects, and restarts."""
        if mqtt_client is None or not mqtt_connected:
            self._log("ESP32 reboot NOT sent – MQTT is not connected", C_RED)
            return
        mqtt_client.publish(f"{TOPIC_BASE}/cmd", "REBOOT", qos=1)
        self._log(f"ESP32 reboot requested (published REBOOT to {TOPIC_BASE}/cmd)",
                  C_ORANGE)
        self.reboot_btn.setEnabled(False)
        self.ota_status_lbl.setText("Reboot requested — waiting for the ESP32…")
        self._esp_reboot_begin("cmd")

    def _on_esp_ip(self, ip: str):
        ip = ip.strip()
        if not ip or ip == self._esp_ip:
            return
        self._apply_esp_ip(ip)
        self._log(f"ESP32 reports IP {ip} – OTA page http://{ip}/update", C_DIM)

    def _on_esp_status(self, status: str):
        if status == "rebooting":
            self._log("ESP32: rebooting…", C_ORANGE)
        # Reboot button only works while the device is actually online
        self.reboot_btn.setEnabled(status == "online" and self._esp_ip is not None)

        # ── Reboot-cycle tracking ─────────────────────────────────────────
        # After an OTA flash the device reboots uncleanly (broker publishes
        # the "offline" LWT) and after a REBOOT command it disconnects
        # cleanly ("rebooting" was published, no LWT) — both then come back
        # with "online" from the fresh boot. Follow it live on the tab.
        if self._esp_reboot_t0 is not None:
            if status == "offline":
                self._log("ESP32 offline — rebooting…", C_ORANGE)
                if self._esp_reboot_reason == "ota":
                    self.ota_status_lbl.setText(
                        "ESP32 is rebooting — waiting for it to come back…")
            elif status == "online":
                dur = int(time.time() - self._esp_reboot_t0)
                reason = self._esp_reboot_reason
                self._esp_reboot_end()
                # Completed cycle → idle the progress bar at 0% so it only
                # ever reflects an in-flight operation; the ✓ status text
                # and Event Log entry carry the success record.
                self.ota_progress.setRange(0, 100)
                self.ota_progress.setValue(0)
                if reason == "ota":
                    self.ota_status_lbl.setText(
                        f"✓ New firmware running — ESP32 back online "
                        f"after {dur} s")
                    self._log(f"ESP32 rebooted into the new firmware — back "
                              f"online after {dur} s", C_GREEN)
                else:
                    self.ota_status_lbl.setText(
                        f"✓ ESP32 back online after {dur} s")
                    self._log(f"ESP32 back online after {dur} s", C_GREEN)

    def _esp_reboot_begin(self, reason: str):
        """Start watching for the ESP32's reboot cycle (offline → online).
        reason: "ota" (after a successful flash) or "cmd" (Reboot button)."""
        self._esp_reboot_t0 = time.time()
        self._esp_reboot_reason = reason
        self._esp_reboot_timer.start(180 * 1000)   # 3-min safety net

    def _esp_reboot_end(self):
        self._esp_reboot_t0 = None
        self._esp_reboot_reason = ""
        if self._esp_reboot_timer.isActive():
            self._esp_reboot_timer.stop()

    def _esp_reboot_timeout(self):
        """The ESP32 never came back online within 3 minutes of a flash or
        reboot request — surface it instead of leaving the tab 'waiting'."""
        reason = self._esp_reboot_reason
        self._esp_reboot_end()
        msg = ("ESP32 did not come back online within 3 min — check the "
               "LCD / power.")
        if reason == "ota":
            self.ota_progress.setRange(0, 100)
            self.ota_progress.setValue(0)
        self.ota_status_lbl.setText(msg)
        self._log(msg, C_RED)

    # ── event tracking ────────────────────────────────────────────────────────
    # Battery flow (CHARGING/IDLE/DISCHARGING) chatter is suppressed with a
    # simple debounce: a new state must persist for FLOW_DEBOUNCE_S seconds
    # before it's logged, which collapses trickle-charge duty-cycling into
    # a single entry.
    #
    # Alarms (cell delta, temperatures, MPPT errors) use a latched state
    # machine with hysteresis (LatchedAlarm): the alarm is logged ONCE when
    # it first triggers, then all repeated triggers are suppressed while it
    # remains active. When the value drops below the exit threshold, a
    # recovery message is logged. After ALARM_COOLDOWN_S of being recovered,
    # the alarm re-arms and is ready to fire as a new event.
    FLOW_DEBOUNCE_S  = 45
    ALARM_COOLDOWN_S = 900   # 15 minutes – time after recovery before re-arming

    def _init_event_tracking(self):
        _none = {"connected": None, "flow": None,
                 "flow_pending": None, "flow_pending_t": 0.0,
                 "soc": None, "ble_retry_paused": None,
                 "ble_disconnect_s": 0,
                 "protection": None, "balancing": None,
                 "cell_delta_mv": None, "cell_temp_f": None, "mosfet_temp_f": None}
        self._prev = {1: copy.copy(_none), 2: copy.copy(_none)}

        # Latched alarm state machines – one per battery per metric.
        # Hysteresis: enter threshold > exit threshold, so jitter around
        # the boundary stays in "active" and is silently suppressed.
        delta_levels = [
            {"name": "ALARM",   "enter": 50, "exit": 45, "color": C_RED},
            {"name": "WARNING", "enter": 20, "exit": 15, "color": C_ORANGE},
        ]
        temp_levels = [
            {"name": "ALARM",   "enter": 125, "exit": 120, "color": C_RED},
            {"name": "WARNING", "enter": 110, "exit": 105, "color": C_ORANGE},
        ]
        self._alarms = {}
        for bid in (1, 2):
            self._alarms[bid] = {
                "cell_delta":  LatchedAlarm(delta_levels, self.ALARM_COOLDOWN_S),
                "cell_temp":    LatchedAlarm(temp_levels,  self.ALARM_COOLDOWN_S),
                "mosfet_temp":  LatchedAlarm(temp_levels,  self.ALARM_COOLDOWN_S),
            }

        # MPPT error latched state (error-code based, not threshold)
        self._mppt_err_state     = "idle"   # idle | active | recovering
        self._mppt_err_code      = 0
        self._mppt_err_active_t  = 0.0
        self._mppt_err_recover_t = 0.0
        self._mppt_err_logged    = False
        self._last_summary_t     = 0.0
        self._summary_interval_s = 600

    def _log_alarm_result(self, name, label, val_str, result):
        """Format and log a LatchedAlarm.update() result."""
        action = result["action"]
        if action in ("enter", "escalate"):
            level = result["level"]
            self._log(f"{name}: {label} {level['name']}  {val_str}", level["color"])
        elif action == "recover":
            duration = result["duration"]
            mins = int(duration) // 60
            secs = int(duration) % 60
            dur_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
            self._log(f"{name}: {label} recovered after {dur_str}  (now {val_str})", C_GREEN)

    def _check_battery_events(self, bid: int, data: dict):
        prev = self._prev[bid]
        name = BAT_NAME[bid]

        conn = data.get("connected", False)
        if prev["connected"] is not None and conn != prev["connected"]:
            self._log(f"{name}: BLE {'connected' if conn else 'disconnected'}",
                      C_GREEN if conn else C_RED)
        prev["connected"] = conn

        # BLE disconnect tracking: the firmware now tries to reconnect every
        # BMS_UPDATE_MS tick (no more round/cooldown cycle). Log a periodic
        # "still disconnected" reminder every 30s instead of every state change.
        ble_disc_s = int(data.get("ble_disconnect_s", 0) or 0)
        prev["ble_retry_paused"] = bool(data.get("ble_retry_paused", False))
        if not conn and ble_disc_s > 0:
            # Log at 0s (first detection), then every 30s
            if prev["ble_disconnect_s"] == 0 or ble_disc_s - prev["ble_disconnect_s"] >= 30:
                self._log(f"{name}: BLE still disconnected ({ble_disc_s}s, "
                          f"attempt #{data.get('ble_retry_attempts', 0)})", C_ORANGE)
                prev["ble_disconnect_s"] = ble_disc_s
        elif conn:
            prev["ble_disconnect_s"] = 0

        p        = data.get("power", 0.0)
        raw_flow = "CHARGING" if p >= 1 else ("DISCHARGING" if p <= -1 else "IDLE")
        now = time.time()

        if prev["flow"] is None:
            # First reading for this battery: seed state without logging.
            prev["flow"]           = raw_flow
            prev["flow_pending"]   = raw_flow
            prev["flow_pending_t"] = now
        else:
            if raw_flow != prev["flow_pending"]:
                prev["flow_pending"]   = raw_flow
                prev["flow_pending_t"] = now
            if (raw_flow != prev["flow"]
                    and now - prev["flow_pending_t"] >= self.FLOW_DEBOUNCE_S):
                col = C_GREEN if raw_flow == "CHARGING" else (C_ORANGE if raw_flow == "DISCHARGING" else C_DIM)
                self._log(f"{name}: {prev['flow']} → {raw_flow}  ({p:+.0f} W)", col)
                prev["flow"] = raw_flow

        soc     = data.get("soc", 0)
        old_soc = prev["soc"]
        if old_soc is not None:
            for thresh, col in [(15, C_RED), (30, C_ORANGE)]:
                if old_soc > thresh >= soc:
                    self._log(f"{name}: SOC dropped below {thresh}%  (now {soc}%)", col)
        prev["soc"] = soc

        # Decoded protection string
        prot = decode_protection(str(data.get("protection", ""))).strip()
        if prev["protection"] is not None and prot != prev["protection"]:
            if prot != "None":
                self._log(f"{name}: PROTECTION ACTIVE – {prot}", C_RED)
            else:
                self._log(f"{name}: protection cleared", C_GREEN)
        prev["protection"] = prot

        bal = decode_balance(str(data.get("balancing", "")),
                             str(data.get("battery_state", ""))).strip()
        if prev["balancing"] is not None and bal != prev["balancing"]:
            col = C_CYAN if bal != "None" else C_DIM
            self._log(f"{name}: balance → {bal}", col)
        prev["balancing"] = bal

        delta = data.get("cell_delta_mv", 0.0)
        old_d = prev["cell_delta_mv"]
        if old_d is not None:
            result = self._alarms[bid]["cell_delta"].update(delta, now)
            if result:
                self._log_alarm_result(name, "cell delta", f"{delta:.1f} mV", result)
        prev["cell_delta_mv"] = delta

        ct_f = c_to_f(data.get("cell_temp",   0))
        mt_f = c_to_f(data.get("mosfet_temp", 0))
        for label, val, key, alarm_key in [("cell temp",   ct_f, "cell_temp_f",   "cell_temp"),
                                            ("MOSFET temp", mt_f, "mosfet_temp_f", "mosfet_temp")]:
            old_v = prev[key]
            if old_v is not None:
                result = self._alarms[bid][alarm_key].update(val, now)
                if result:
                    self._log_alarm_result(name, label, f"{val:.0f} °F", result)
            prev[key] = val

    def _check_mppt_events(self, vict: dict):
        """Latched MPPT error logging: log an error once when it first
        appears, suppress re-occurrences of the same error within
        ALARM_COOLDOWN_S of clearing, and log the recovery when the error
        clears. A different error code is always logged as a new event.
        After the cooldown, the same error is also treated as new."""
        if not vict.get("valid", False):
            return
        err_code = vict.get("error", 0)
        now = time.time()

        if self._mppt_err_state == "idle":
            if err_code:
                self._mppt_err_state    = "active"
                self._mppt_err_code     = err_code
                self._mppt_err_active_t = now
                self._mppt_err_logged   = True
                self._log(f"MPPT: ERROR ACTIVE – {decode_mppt_error(err_code)}", C_RED)

        elif self._mppt_err_state == "active":
            if err_code == 0:
                # Error cleared
                self._mppt_err_state     = "recovering"
                self._mppt_err_recover_t = now
                if self._mppt_err_logged:
                    duration = now - self._mppt_err_active_t
                    mins = int(duration) // 60
                    secs = int(duration) % 60
                    dur_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
                    self._log(
                        f"MPPT: error cleared (was {decode_mppt_error(self._mppt_err_code)}) "
                        f"after {dur_str}", C_GREEN)
            elif err_code != self._mppt_err_code:
                # Different error code while active – log as new error
                self._mppt_err_code     = err_code
                self._mppt_err_active_t = now
                self._mppt_err_logged   = True
                self._log(f"MPPT: ERROR ACTIVE – {decode_mppt_error(err_code)}", C_RED)

        elif self._mppt_err_state == "recovering":
            if err_code:
                if err_code == self._mppt_err_code:
                    # Same error returned within cooldown – suppress
                    self._mppt_err_state  = "active"
                    self._mppt_err_logged = False
                else:
                    # Different error – log as new event
                    self._mppt_err_state    = "active"
                    self._mppt_err_code     = err_code
                    self._mppt_err_active_t = now
                    self._mppt_err_logged   = True
                    self._log(f"MPPT: ERROR ACTIVE – {decode_mppt_error(err_code)}", C_RED)
            elif now - self._mppt_err_recover_t >= self.ALARM_COOLDOWN_S:
                # Cooldown expired – back to idle
                self._mppt_err_state  = "idle"
                self._mppt_err_code   = 0
                self._mppt_err_logged = False

    # ── MQTT slots ────────────────────────────────────────────────────────────
    def _on_mqtt_connect(self):
        global mqtt_connected
        mqtt_connected = True
        self.status_mqtt.setStyleSheet(
            f"color:{C_GREEN}; font-weight:bold; padding:0 8px;")
        self.status_mqtt.setText(f"MQTT: {MQTT_BROKER}  ✔")
        self._log(f"Connected to MQTT broker {MQTT_BROKER}", C_GREEN)

    def _on_mqtt_disconnect(self, reason: str = ""):
        global mqtt_connected
        mqtt_connected = False
        self.status_mqtt.setStyleSheet(
            f"color:{C_RED}; font-weight:bold; padding:0 8px;")
        self.status_mqtt.setText("MQTT: disconnected – reconnecting…")
        suffix = f" – {reason}" if reason and reason.lower() != "success" else ""
        self._log(f"MQTT broker disconnected{suffix}", C_RED)

    def _on_mqtt_error(self, msg: str):
        self._log(msg, C_RED)

    def _on_message(self, topic: str, payload: str):
        global bat, combined, broker_status, victron_data
        self.overview_tab.mqtt_indicator.flash()

        # Capture raw topic→payload for the MQTT Raw debug tab (latest only)
        self._raw_mqtt[topic] = payload

        try:
            # LiTime status (LWT string, not JSON)
            if topic == f"{TOPIC_BASE}/status":
                broker_status = payload
                self._on_esp_status(payload)
                return

            # ESP32 IP announcement (plain string, retained; not JSON) –
            # published by the firmware on every MQTT (re)connect
            if topic == f"{TOPIC_BASE}/ip":
                self._on_esp_ip(payload)
                return

            # All other LiTime and Victron topics are JSON
            data = json.loads(payload)

            if topic == f"{TOPIC_BASE}/battery1/state":
                bat[1].update(data)
                bat[1]["connected"] = data.get("connected", False)
                self._check_battery_events(1, bat[1])

            elif topic == f"{TOPIC_BASE}/battery2/state":
                bat[2].update(data)
                bat[2]["connected"] = data.get("connected", False)
                self._check_battery_events(2, bat[2])

            elif topic == f"{TOPIC_BASE}/combined/state":
                combined.update(data)

            # ── Victron MPPT ──────────────────────────────────────────────
            elif topic == f"{VICTRON_TOPIC_BASE}/state":
                victron_data.update(data)
                self._check_mppt_events(victron_data)

        except json.JSONDecodeError:
            # Flat scalar Victron topics (non-JSON)
            _flat = {
                f"{VICTRON_TOPIC_BASE}/state_str":   ("state_str",   str),
                f"{VICTRON_TOPIC_BASE}/batt_v":      ("batt_v",      float),
                f"{VICTRON_TOPIC_BASE}/batt_a":      ("batt_a",      float),
                f"{VICTRON_TOPIC_BASE}/pv_w":        ("pv_w",        float),
                f"{VICTRON_TOPIC_BASE}/yield_today": ("yield_today", float),
            }
            if topic in _flat:
                key, cast = _flat[topic]
                try:
                    victron_data[key] = cast(payload)
                except (ValueError, TypeError):
                    pass

        except Exception as e:
            self._log(f"Parse error on {topic}: {e}", C_RED)

    # ── UI refresh timer ──────────────────────────────────────────────────────
    def _refresh_ui(self):
        b1   = bat[1]
        b2   = bat[2]
        vict = victron_data

        self.overview_tab.refresh(b1, b2, combined, vict)
        self.bat1_tab.refresh(b1)
        self.bat2_tab.refresh(b2)
        self.mppt_tab.refresh(vict, combined)

        # Update MQTT Raw table when visible (QTableWidget = no flash)
        if self._mqtt_raw_visible():
            self._refresh_mqtt_raw()

        # Periodic 10-minute summary in Log tab
        now = time.time()
        if now - self._last_summary_t >= self._summary_interval_s:
            self._last_summary_t = now
            for bid, d in [(1, b1), (2, b2)]:
                self._log(
                    f"{BAT_NAME[bid]} │ SOC {d.get('soc',0)}%  "
                    f"{d.get('total_voltage',0):.2f} V  "
                    f"{d.get('current',0):+.1f} A  "
                    f"{d.get('power',0):+.0f} W  "
                    f"Δ {d.get('cell_delta_mv',0):.1f} mV  "
                    f"Cell {c_to_f(d.get('cell_temp',0)):.0f} °F  "
                    f"MOS {c_to_f(d.get('mosfet_temp',0)):.0f} °F",
                    C_DIM,
                )
            if vict.get("valid"):
                mppt_err_code = vict.get("error", 0)
                mppt_state_s  = decode_mppt_state(vict.get("state", 0))
                mppt_line = (
                    f"MPPT │ State:{mppt_state_s}  "
                    f"PV:{vict.get('pv_w',0):.0f}W  "
                    f"Batt:{vict.get('batt_v',0):.2f}V {vict.get('batt_a',0):+.1f}A  "
                    f"Yield:{vict.get('yield_today',0):.3f}kWh"
                )
                if mppt_err_code:
                    mppt_line += f"  {decode_mppt_error(mppt_err_code)}"
                self._log(mppt_line, MPPT_STATE_COLORS.get(mppt_state_s, C_CYAN))

    def _log(self, msg: str, color: str = C_TEXT):
        ts = datetime.now().strftime("%a %d %b %Y %I:%M:%S %p")
        self.log_tab.append(
            f'<span style="color:{C_DIM}">{ts}</span> '
            f'<span style="color:{color}">│</span> '
            f'<span style="color:{color}">{msg}</span>'
        )



    def _on_log_subtab_changed(self, index: int):
        """Force an immediate MQTT Raw refresh when the user switches to it."""
        if self.log_container.widget(index) is self.mqtt_raw_tab:
            self._refresh_mqtt_raw()

    def _mqtt_raw_visible(self) -> bool:
        """True only when the top-level Log tab AND MQTT Raw sub-tab are active."""
        if self.tabs.currentWidget() is not self.log_container:
            return False
        return self.log_container.currentWidget() is self.mqtt_raw_tab

    def _refresh_mqtt_raw(self):
        """Update the MQTT Raw table from the latest self._raw_mqtt snapshot.
        Uses QTableWidget so individual cells are updated in-place — no flash,
        no scroll reset. Only changed rows are touched."""
        _REDUNDANT_SUFFIXES = (
            "/soc", "/voltage", "/current", "/power",
            "/cells/cell01", "/cells/cell02", "/cells/cell03", "/cells/cell04",
            "/cells/cell05", "/cells/cell06", "/cells/cell07", "/cells/cell08",
            "/cells/cell09", "/cells/cell10", "/cells/cell11", "/cells/cell12",
            "/cells/cell13", "/cells/cell14", "/cells/cell15", "/cells/cell16",
        )
        filtered = {
            t: v for t, v in self._raw_mqtt.items()
            if not any(t.endswith(s) for s in _REDUNDANT_SUFFIXES)
        }

        _GROUP_ORDER = [
            [f"{TOPIC_BASE}/status"],
            [f"{TOPIC_BASE}/battery1/state"],
            [f"{TOPIC_BASE}/battery2/state"],
            [f"{TOPIC_BASE}/combined/state"],
            [f"{VICTRON_TOPIC_BASE}/state",
             f"{VICTRON_TOPIC_BASE}/state_str",
             f"{VICTRON_TOPIC_BASE}/batt_v",
             f"{VICTRON_TOPIC_BASE}/batt_a",
             f"{VICTRON_TOPIC_BASE}/pv_w",
             f"{VICTRON_TOPIC_BASE}/yield_today"],
        ]
        seen = set()
        for topics in _GROUP_ORDER:
            seen.update(topics)
        extra = [t for t in sorted(filtered) if t not in seen]
        ordered_topics = []
        for topics in _GROUP_ORDER:
            ordered_topics.extend(topics)
        ordered_topics.extend(extra)
        ordered_topics = [t for t in ordered_topics if t in filtered]

        if not ordered_topics:
            self.mqtt_raw_tab.setRowCount(1)
            self.mqtt_raw_tab.setItem(0, 0, QTableWidgetItem(""))
            item = QTableWidgetItem("Waiting for MQTT messages…")
            item.setForeground(QColor(C_DIM))
            self.mqtt_raw_tab.setItem(0, 1, item)
            return

        self.mqtt_raw_tab.setRowCount(len(ordered_topics))

        for row, topic in enumerate(ordered_topics):
            payload = filtered[topic]
            try:
                parsed = json.loads(payload)
                display = json.dumps(parsed, indent=2)
            except (json.JSONDecodeError, ValueError):
                display = payload or "(empty)"

            old_topic = self.mqtt_raw_tab.item(row, 0)
            if old_topic is None or old_topic.text() != topic:
                item = QTableWidgetItem(topic)
                item.setForeground(QColor(C_CYAN))
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignTop)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                self.mqtt_raw_tab.setItem(row, 0, item)

            old_payload = self.mqtt_raw_tab.item(row, 1)
            if old_payload is None or old_payload.text() != display:
                item = QTableWidgetItem(display)
                item.setForeground(QColor(C_TEXT))
                font = item.font()
                font.setFamily("monospace")
                font.setPointSize(9)
                item.setFont(font)
                self.mqtt_raw_tab.setItem(row, 1, item)

        if len(ordered_topics) != getattr(self, "_last_raw_row_count", 0):
            self._last_raw_row_count = len(ordered_topics)
            self.mqtt_raw_tab.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeToContents)
        self.mqtt_raw_tab.resizeRowsToContents()

# =============================================================================
#  ─── ENTRY POINT ──────────────────────────────────────────────────────────────
# =============================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(30, 30, 30))
    pal.setColor(QPalette.WindowText,      QColor(224, 224, 224))
    pal.setColor(QPalette.Base,            QColor(18, 18, 18))
    pal.setColor(QPalette.AlternateBase,   QColor(42, 42, 42))
    pal.setColor(QPalette.ToolTipBase,     QColor(30, 30, 30))
    pal.setColor(QPalette.ToolTipText,     QColor(224, 224, 224))
    pal.setColor(QPalette.Text,            QColor(224, 224, 224))
    pal.setColor(QPalette.Button,          QColor(42, 42, 42))
    pal.setColor(QPalette.ButtonText,      QColor(224, 224, 224))
    pal.setColor(QPalette.BrightText,      QColor(255, 255, 255))
    pal.setColor(QPalette.Link,            QColor(38, 198, 218))
    pal.setColor(QPalette.Highlight,       QColor(38, 198, 218))
    pal.setColor(QPalette.HighlightedText, QColor(0, 0, 0))
    app.setPalette(pal)
    app.setStyleSheet(DARK_STYLESHEET)

    pg.setConfigOption("background", "#000000")
    pg.setConfigOption("foreground", C_TEXT)

    win = Window()
    win.show()

    # Ensure the background MQTT thread is told to stop *before* Qt starts
    # tearing down mqtt_signals -- otherwise a message/reason-code arriving
    # in that teardown window raises "wrapped C/C++ object ... has been
    # deleted" in the daemon thread on exit.
    app.aboutToQuit.connect(shutdown_mqtt)

    mqtt_thread = Thread(target=start_mqtt, daemon=True)
    mqtt_thread.start()

    sys.exit(app.exec())
