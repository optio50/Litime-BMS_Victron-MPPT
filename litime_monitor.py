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

import sys
import os
import json
import time
import copy
import random
from datetime import datetime
from threading import Thread, Event

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QProgressBar, QGroupBox,
    QTextBrowser, QSplitter, QFrame, QSizePolicy
)
from PyQt5 import QtGui
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QColor, QPalette, QPixmap

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

os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-logging"
os.putenv("QT_LOGGING_RULES", "*.debug=false;qt.qpa.*=false")

# =============================================================================
#  ─── CONFIGURATION ──────────────────────────────────────────────────────────
#  MQTT connection details and battery display names are read from config.h
#  (the same file the ESP32-S3 firmware is compiled with) via shared_config.py
#  -- edit config.h once and both apps stay in sync. Anything it can't find
#  there falls back to shared_config.DEFAULTS.
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

BAT_NAME     = {1: BAT1_NAME, 2: BAT2_NAME}

# All charts are fed once per UI refresh tick (self.timer, 2 s interval —
# see Window.__init__), regardless of how often MQTT messages actually
# arrive. That fixes the effective sample rate at exactly 1 point / 2 s, so
# 24 h of history = 24*3600/2 = 43 200 points. Keep this exact (not padded)
# so old data actually ages out of every chart after 24 hours instead of
# accumulating 48h+ of history.
CHART_MAX_PTS = 43200
CHART_ROLL    = 600   # seconds of x-axis kept in view
# The SOC / Combined-Power charts on the Overview page sit side-by-side in a
# splitter (half width each), so their DATETIME tick labels have less room
# than full-width charts and can overlap at the default zoom. A narrower
# rolling window (~74% of CHART_ROLL, matching one mouse-wheel zoom-in notch:
# 1.02**(120 * -1/8) ≈ 0.743) gives each label enough horizontal space
# without changing the tick format.
CHART_ROLL_SPLIT = 400

# =============================================================================
#  ─── COLORS ─────────────────────────────────────────────────────────────────
# =============================================================================
C_BG        = "#1e1e1e"
C_PANEL     = "#2a2a2a"
C_BORDER    = "#3a3a3a"
C_TEXT      = "#e0e0e0"
C_DIM       = "#888888"
C_GREEN     = "#038513"
C_YELLOW    = "yellow"
C_ORANGE    = "#ffa726"
C_ORANGERED = "orangered"
C_RED       = "red"
C_BLUE      = "#42a5f5"
C_CYAN      = "#26c6da"
C_MAGENTA   = "#ab47bc"

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
        font-weight: bold; color: {C_CYAN};
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
    "total_remaining_ah": 0.0, "total_capacity_ah": 0.0,
    "time_remaining_s": 0, "time_direction": "idle", "flow": "idle",
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
# LiTime / JK BMS protection state: 32-bit hex string (e.g. "0x00000000")
_PROT_BITS = [
    (0,  "Cell OV"),  (1,  "Cell UV"),  (2,  "Pack OV"),  (3,  "Pack UV"),
    (4,  "Chg OT"),   (5,  "Chg UT"),   (6,  "Dsg OT"),   (7,  "Dsg UT"),
    (8,  "Chg OC"),   (9,  "Dsg OC"),   (10, "Short"),     (11, "IC Err"),
    (12, "SW Lock"),
]
# Battery state: 16-bit hex string (e.g. "0x0003")
_STATE_BITS = [
    (0, "Charging"), (1, "Discharging"), (2, "Balancing"),
    (3, "Full"),     (6, "Heating"),
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
    """Return slash-separated list of active state names, or 'Idle'."""
    if not hex_str:
        return "Idle"
    try:
        val = int(hex_str, 16)
    except (ValueError, TypeError):
        return str(hex_str)
    flags = [name for bit, name in _STATE_BITS if val & (1 << bit)]
    return " / ".join(flags) if flags else "Idle"

def decode_balance(bin_str: str) -> str:
    """Return 'B1 B4 B8 …' for cells being balanced, or 'None'."""
    if not bin_str:
        return "None"
    cells = [str(i + 1) for i, c in enumerate(str(bin_str)) if c == "1"]
    return " ".join(f"B{c}" for c in cells) if cells else "None"


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
    11:  "Other Hub-1",
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
def make_label(text, color=C_TEXT, bold=False, size=11, align=Qt.AlignLeft):
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
    if soc >= 60: return C_GREEN
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

    Note: "to_empty" is actually time-to-RESERVE (SOC_RESERVE_PCT in config.h
    on the firmware, currently 10%), not literal 0% SOC — LFP packs shouldn't
    routinely be discharged past that point, and the BMS's voltage-based
    low-cutoff can trip before true 0% Ah is reached anyway.

    Examples:
        fmt_time(20700, "to_empty")  →  "5h 45m to Reserve"
        fmt_time(8100,  "to_full")   →  "2h 15m to Full"
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
    suffix = "to Full" if direction == "to_full" else "to Reserve"
    return " ".join(parts) + " " + suffix

def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0

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

def make_category_chart(title: str, categories: list, roll: int = CHART_ROLL) -> LivePlotWidget:
    """Chart with a datetime bottom axis and a categorical (named) left axis,
    used for the MPPT charger-state bar chart."""
    bottom = LiveAxis("bottom", **{Axis.TICK_FORMAT: Axis.DATETIME})
    left   = LiveAxis("left",   **{Axis.TICK_FORMAT: Axis.CATEGORY, Axis.CATEGORIES: categories})
    w = LivePlotWidget(
        title=title,
        axisItems={"bottom": bottom, "left": left},
        x_range_controller=LiveAxisRange(roll_on_tick=roll, offset_left=0.5),
        **CHART_KW,
    )
    w.x_range_controller.crop_left_offset_to_data = True
    w.showGrid(x=True, y=True, alpha=0.3)
    w.setBackground("#000000")
    return w


# =============================================================================
#  ─── CELL VOLTAGE CHART ──────────────────────────────────────────────────────
# =============================================================================
class CellBarChart(pg.PlotWidget):
    """Cell voltages as scatter+line colored by deviation from average."""
    def __init__(self, title: str = "Cell Voltages", n_cells: int = 16):
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
      mid row  : statistics panel   |  inner QTabWidget (5 charts)
                                         1. Power
                                         2. V & I
                                         3. Cells  (CellBarChart)
                                         4. Temps
                                         5. Cell Δ
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
        soc_grp.setMaximumWidth(240)
        soc_lay = QVBoxLayout(soc_grp)
        self.soc_bar   = QProgressBar()
        self.soc_bar.setRange(0, 100)
        self.soc_bar.setFixedHeight(26)
        self.soc_label = make_label("0%", C_GREEN, bold=True, size=14,
                                    align=Qt.AlignCenter)
        soc_lay.addWidget(self.soc_bar)
        soc_lay.addWidget(self.soc_label)
        top.addWidget(soc_grp)

        vi_grp = QGroupBox("Voltage / Current")
        vi_lay = QGridLayout(vi_grp)
        self.v_big    = make_label("—", C_CYAN,   bold=True, size=26, align=Qt.AlignCenter)
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
        p_plot = LiveLinePlot(pen=pg.mkPen(C_ORANGE, width=1.5), name="Power",
                              fillLevel=0, brush=(255, 238, 88, 60))
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

        # ── Tab 3: Cell voltages ──────────────────────────────────────────
        self.cell_chart = CellBarChart(f"{name} – Cell Voltages", n_cells=16)
        cw = QWidget(); cl = QVBoxLayout(cw); cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.cell_chart)
        chart_tabs.addTab(cw, "Cells")

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
        ah_plot = LiveLinePlot(pen=pg.mkPen(C_CYAN, width=1.5), name=f"{name} Remaining Ah",
                               fillLevel=0, brush=(88, 214, 238, 40))
        self.ah_connector = make_connector(ah_plot)
        self.ah_chart     = make_chart(f"{name} – Remaining Capacity (24 hrs)", y_label="Ah")
        self.ah_chart.addItem(ah_plot)
        ahw = QWidget(); ahl = QVBoxLayout(ahw); ahl.setContentsMargins(0, 0, 0, 0)
        ahl.addWidget(self.ah_chart)
        chart_tabs.addTab(ahw, "Ah Remaining")

        mid.addWidget(chart_tabs, 1)
        root.addLayout(mid, 1)

    # ─────────────────────────────────────────────────────────────────────────
    def refresh(self, d: dict):
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
        bal_str  = decode_balance(str(d.get("balancing", "")))
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

        # Cell scatter chart
        self.cell_chart.update_cells(d.get("cell_voltages", []))


# =============================================================================
#  ─── OVERVIEW TAB ────────────────────────────────────────────────────────────
# =============================================================================
class OverviewTab(QWidget):
    def __init__(self):
        super().__init__()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── status strip ─────────────────────────────────────────────────────
        status_row = QHBoxLayout()

        self.mqtt_indicator = MqttLedWidget()
        self.b1_led   = make_label(f"⬤  {BAT1_NAME}", C_RED,  bold=True, size=11)
        self.b2_led   = make_label(f"⬤  {BAT2_NAME}", C_RED,  bold=True, size=11)
        self.mppt_led = make_label("⬤  MPPT",          C_RED,  bold=True, size=11)
        self.ts_lbl   = make_label("—",                 C_DIM,             size=10)
        status_row.addWidget(self.mqtt_indicator)
        status_row.addSpacing(12)
        status_row.addWidget(self.b1_led)
        status_row.addSpacing(12)
        status_row.addWidget(self.b2_led)
        status_row.addSpacing(12)
        status_row.addWidget(self.mppt_led)

        # App icon/logo, shown at native aspect ratio scaled to strip height
        status_row.addSpacing(12)
        self.logo_lbl = QLabel()
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.jpg")
        _pix = QPixmap(_icon_path)
        if not _pix.isNull():
            self.logo_lbl.setPixmap(
                _pix.scaledToHeight(48, Qt.SmoothTransformation))
        status_row.addWidget(self.logo_lbl)

        status_row.addStretch()
        status_row.addWidget(self.ts_lbl)

        root.addLayout(status_row)

        # ── SOC row: Combined | Battery 1 | Battery 2  (single horizontal row) ──
        soc_row = QHBoxLayout()
        soc_row.setSpacing(6)

        def make_soc_group(title):
            grp = QGroupBox(title)
            lay = QVBoxLayout(grp)
            lay.setSpacing(3)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setFixedHeight(20)
            bar.setMinimumWidth(100)
            v_lbl = make_label("0.00 V", C_CYAN, bold=True, size=11, align=Qt.AlignCenter)
            lay.addWidget(bar)
            lay.addWidget(v_lbl)
            return grp, bar, v_lbl

        comb_soc_grp, self.comb_soc_bar, self.comb_v_lbl = \
            make_soc_group("Combined")
        b1_soc_grp, self.soc1_bar, self.v1_lbl = \
            make_soc_group(BAT1_NAME)
        b2_soc_grp, self.soc2_bar, self.v2_lbl = \
            make_soc_group(BAT2_NAME)

        soc_row.addWidget(comb_soc_grp, 1)
        soc_row.addWidget(b1_soc_grp,   1)
        soc_row.addWidget(b2_soc_grp,   1)
        root.addLayout(soc_row)

        # ── MPPT + combined stats row ─────────────────────────────────────────
        mid = QHBoxLayout()
        mid.setSpacing(6)

        # MPPT Solar Controller panel — two aligned columns separated by a
        # vertical divider so left (measurements) and right (status) data
        # don't visually blend together, and every label/value row lines up.
        mppt_grp   = QGroupBox("MPPT Solar Controller")
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
        self.tot_time = cv("Time Rem",      4)
        self.flow_lbl = cv("Flow",          5)
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
            pr_ = bv("Protect",  4)
            ba_ = bv("Balance",  5)
            return grp, v_, i_, p_, t_, pr_, ba_

        b1m, self.b1_v, self.b1_i, self.b1_p, self.b1_tc, self.b1_prot, self.b1_bal = mini_group(BAT1_NAME)
        b2m, self.b2_v, self.b2_i, self.b2_p, self.b2_tc, self.b2_prot, self.b2_bal = mini_group(BAT2_NAME)
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

        pwr_plot = LiveLinePlot(pen=pg.mkPen(C_ORANGE, width=2), name="Total Power",
                                fillLevel=0, brush=(255, 238, 88, 50))
        self.pwr_connector = make_connector(pwr_plot)
        self.pwr_chart     = make_chart("Combined Power – 24 Hours", y_label="Watts", roll=CHART_ROLL_SPLIT)
        self.pwr_chart.addItem(pwr_plot)

        charts_split.addWidget(self.soc_chart)
        charts_split.addWidget(self.pwr_chart)
        root.addWidget(charts_split, 1)

    # ─────────────────────────────────────────────────────────────────────────
    def refresh(self, b1d, b2d, comb, vict):
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

        # SOC row
        s1      = b1d.get("soc", 0)
        s2      = b2d.get("soc", 0)
        avg_soc = comb.get("soc_avg", (s1 + s2) / 2.0)
        v1      = b1d.get("total_voltage", 0.0)
        v2      = b2d.get("total_voltage", 0.0)
        v_avg   = (v1 + v2) / 2.0

        for bar, v_lbl, soc, v in [
            (self.comb_soc_bar, self.comb_v_lbl, int(avg_soc), v_avg),
            (self.soc1_bar,     self.v1_lbl,     s1,           v1),
            (self.soc2_bar,     self.v2_lbl,     s2,           v2),
        ]:
            bar.setValue(soc)
            bar.setStyleSheet(soc_bar_style(soc))
            v_lbl.setText(f"{v:.2f} V")

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
        self.tot_time.setText(fmt_time(trs, tdir))
        self.flow_lbl.setText(flow.upper())
        self.flow_lbl.setStyleSheet(
            f"color:{flow_color(flow)}; font-size:13px; font-weight:bold;")

        # Per-battery quick view
        for lbl_v, lbl_i, lbl_p, lbl_t, lbl_prot, lbl_bal, d in [
            (self.b1_v, self.b1_i, self.b1_p, self.b1_tc, self.b1_prot, self.b1_bal, b1d),
            (self.b2_v, self.b2_i, self.b2_p, self.b2_tc, self.b2_prot, self.b2_bal, b2d),
        ]:
            bv   = d.get("total_voltage",    0.0)
            bi   = d.get("current",          0.0)
            bp   = d.get("power",            0.0)
            bct  = d.get("cell_temp",        0)
            lbl_v.setText(f"{bv:.2f} V")
            lbl_i.setText(f"{bi:+.2f} A")
            lbl_i.setStyleSheet(
                f"color:{C_GREEN if bi >= 0 else C_ORANGE}; font-size:13px; font-weight:bold;")
            bp_col = C_GREEN if bp >= 1 else (C_ORANGE if bp <= -1 else C_DIM)
            lbl_p.setText(f"{bp:+.0f} W")
            lbl_p.setStyleSheet(
                f"color:{bp_col}; font-size:13px; font-weight:bold;")
            lbl_t.setText(f"{c_to_f(bct):.0f} °F")

            prot_str = decode_protection(str(d.get("protection", "")))
            bal_str  = decode_balance(str(d.get("balancing", "")))
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
    "Other Hub-1": "pink",
    "Wake-Up":     "cyan",
    "EXT Control": "purple",
}

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
        sv.addStretch()
        mid.addWidget(stats_w)

        chart_tabs = QTabWidget()
        chart_tabs.setDocumentMode(True)

        def add_chart_tab(title, y_label, color=C_YELLOW, fill=False, brush=None):
            if fill:
                kw = {"fillLevel": 0, "brush": brush if brush is not None else _auto_fill_brush(color)}
            else:
                kw = {}
            plot = LiveLinePlot(pen=pg.mkPen(color, width=1.5), name=title, **kw)
            conn  = make_connector(plot)
            chart = make_chart(f"{title} – 24 hrs", y_label=y_label)
            chart.addItem(plot)
            w = QWidget()
            l = QVBoxLayout(w); l.setContentsMargins(0, 0, 0, 0)
            l.addWidget(chart)
            chart_tabs.addTab(w, title)
            return conn

        self.c_pv_w   = add_chart_tab("PV Power",       "Watts", C_BLUE, fill=True, brush=(0, 0, 108, 200))
        self.c_batt_v = add_chart_tab("Battery Voltage", "V",     C_GREEN)
        self.c_batt_a = add_chart_tab("Battery Current", "A",     C_ORANGE)
        self.c_yield  = add_chart_tab("Yield Today",     "kWh",   C_MAGENTA)

        # ── Charger State — categorized bar chart (pglive-style) ───────────

        self.state_plot = LiveCategorizedBarPlot(
            MPPT_STATE_CATEGORIES,
            category_color=MPPT_STATE_COLORS,
            bar_height=0.9,
        )
        self.state_connector = make_connector(self.state_plot)
        self.state_chart = make_category_chart("Charger State – 24 hrs", MPPT_STATE_CATEGORIES)
        self.state_chart.addItem(self.state_plot)
        sw = QWidget(); sl = QVBoxLayout(sw); sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.state_chart)
        chart_tabs.addTab(sw, "Charger State")

        mid.addWidget(chart_tabs, 1)
        root.addLayout(mid)

    def refresh(self, vict: dict):
        valid = vict.get("valid", False)

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

        if not valid:
            return
        ts = time.time()
        self.c_pv_w.cb_append_data_point(  float(pv_w),   ts)
        self.c_batt_v.cb_append_data_point(float(batt_v), ts)
        self.c_batt_a.cb_append_data_point(float(batt_a), ts)
        self.c_yield.cb_append_data_point( float(yld),    ts)

        self.state_connector.cb_append_data_point([state_s], ts)



# =============================================================================
#  ─── MAIN WINDOW ─────────────────────────────────────────────────────────────
# =============================================================================
class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LiTime Dual BMS + Victron Monitor")
        self.setWindowIcon(QtGui.QIcon('icon.jpg'))
        self.resize(1280, 800)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        self.overview_tab = OverviewTab()
        self.bat1_tab     = BatteryPanel(1)
        self.bat2_tab     = BatteryPanel(2)
        self.mppt_tab     = MpptTab()
        self.log_tab      = QTextBrowser()
        self.log_tab.setOpenExternalLinks(False)

        # Tabs: Overview | Battery 1 | Battery 2 | MPPT | Log
        # Cells / Cell Δ / Temps / Ah Remaining are inside each battery's inner
        # tab widget; MPPT's own charts are in its own inner tab widget.
        tabs.addTab(self.overview_tab, "Overview")
        tabs.addTab(self.bat1_tab,     BAT1_NAME)
        tabs.addTab(self.bat2_tab,     BAT2_NAME)
        tabs.addTab(self.mppt_tab,     "MPPT")
        tabs.addTab(self.log_tab,      "Log")

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

        # UI refresh every 2 s
        self.timer = QTimer()
        self.timer.timeout.connect(self._refresh_ui)
        self.timer.start(2000)

        self._init_event_tracking()
        self._log(f"App started – connecting to {MQTT_BROKER}:{MQTT_PORT}", C_DIM)

    # ── event tracking ────────────────────────────────────────────────────────
    # Battery flow (CHARGING/IDLE/DISCHARGING) chatter is suppressed with a
    # simple debounce: a new state must persist for FLOW_DEBOUNCE_S seconds
    # before it's logged, which collapses trickle-charge duty-cycling into
    # a single entry.
    #
    # MPPT errors are treated differently: every distinct error change is
    # logged IMMEDIATELY, no matter how brief, so a genuine one-off fault is
    # never missed. Only when the SAME error toggles more than
    # FLAP_MAX_TOGGLES times within FLAP_WINDOW_S seconds (rapid-fire,
    # spammy duplicates - e.g. a flaky sensor reading) do we collapse it
    # down to a single "flapping" notice until it settles.
    FLOW_DEBOUNCE_S  = 45
    FLAP_WINDOW_S    = 30
    FLAP_MAX_TOGGLES = 3

    def _init_event_tracking(self):
        _none = {"connected": None, "flow": None,
                 "flow_pending": None, "flow_pending_t": 0.0,
                 "soc": None, "ble_retry_paused": None,
                 "protection": None, "balancing": None,
                 "cell_delta_mv": None, "cell_temp_f": None, "mosfet_temp_f": None}
        self._prev = {1: copy.copy(_none), 2: copy.copy(_none)}
        self._prev_mppt_error    = None
        self._flap_history       = {}   # key -> [timestamps of recent transitions]
        self._flapping           = {}   # key -> bool (currently suppressed)
        self._last_summary_t     = 0.0
        self._summary_interval_s = 600

    def _flap_check(self, key: str, now: float) -> bool:
        """Record a transition timestamp for `key` and return True if it has
        toggled more than FLAP_MAX_TOGGLES times within FLAP_WINDOW_S
        seconds (i.e. it's flapping rapidly and should be suppressed)."""
        hist = self._flap_history.setdefault(key, [])
        hist.append(now)
        cutoff = now - self.FLAP_WINDOW_S
        while hist and hist[0] < cutoff:
            hist.pop(0)
        return len(hist) > self.FLAP_MAX_TOGGLES

    def _check_battery_events(self, bid: int, data: dict):
        prev = self._prev[bid]
        name = BAT_NAME[bid]

        conn = data.get("connected", False)
        if prev["connected"] is not None and conn != prev["connected"]:
            self._log(f"{name}: BLE {'connected' if conn else 'disconnected'}",
                      C_GREEN if conn else C_RED)
        prev["connected"] = conn

        # BLE reconnect backoff: ESP32 gives up after N failed attempts and
        # pauses BLE_RECONNECT_COOLDOWN_MS before trying again. Log only the
        # edge into/out of that pause, not every attempt.
        paused = bool(data.get("ble_retry_paused", False))
        if prev["ble_retry_paused"] is not None and paused != prev["ble_retry_paused"]:
            if paused:
                self._log(f"{name}: BLE reconnect attempts exhausted, pausing before next round", C_ORANGE)
            else:
                self._log(f"{name}: BLE reconnect round resuming", C_DIM)
        prev["ble_retry_paused"] = paused

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

        bal = decode_balance(str(data.get("balancing", ""))).strip()
        if prev["balancing"] is not None and bal != prev["balancing"]:
            col = C_CYAN if bal != "None" else C_DIM
            self._log(f"{name}: balance → {bal}", col)
        prev["balancing"] = bal

        delta = data.get("cell_delta_mv", 0.0)
        old_d = prev["cell_delta_mv"]
        if old_d is not None:
            if old_d < 50 and delta >= 50:
                self._log(f"{name}: cell delta ALARM  {delta:.1f} mV", C_RED)
            elif old_d < 20 and delta >= 20:
                self._log(f"{name}: cell delta WARNING  {delta:.1f} mV", C_ORANGE)
            elif old_d >= 20 and delta < 20:
                self._log(f"{name}: cell delta OK  {delta:.1f} mV", C_GREEN)
        prev["cell_delta_mv"] = delta

        ct_f = c_to_f(data.get("cell_temp",   0))
        mt_f = c_to_f(data.get("mosfet_temp", 0))
        for label, val, key in [("cell temp",  ct_f, "cell_temp_f"),
                                  ("MOSFET temp", mt_f, "mosfet_temp_f")]:
            old_v = prev[key]
            if old_v is not None:
                if old_v < 125 and val >= 125:
                    self._log(f"{name}: {label} ALARM  {val:.0f} °F", C_RED)
                elif old_v < 110 and val >= 110:
                    self._log(f"{name}: {label} WARNING  {val:.0f} °F", C_ORANGE)
                elif old_v >= 110 and val < 110:
                    self._log(f"{name}: {label} normal  {val:.0f} °F", C_GREEN)
            prev[key] = val

    def _check_mppt_events(self, vict: dict):
        """Edge-triggered MPPT error logging: log immediately every time the
        error code changes (active or cleared) so a genuine one-off error is
        never missed. Only suppressed (via _flap_check) when the same error
        toggles rapidly many times in a row, which indicates transient
        sensor/read noise rather than a real, distinct event."""
        if not vict.get("valid", False):
            return
        err_code = vict.get("error", 0)
        if self._prev_mppt_error is None:
            self._prev_mppt_error = err_code
            return
        if err_code == self._prev_mppt_error:
            return

        now = time.time()
        key = "mppt_error"
        flapping_now = self._flap_check(key, now)
        was_flapping = self._flapping.get(key, False)

        if flapping_now:
            if not was_flapping:
                self._log("MPPT: error flapping rapidly – "
                          "suppressing further messages until it stabilizes", C_ORANGE)
            self._flapping[key] = True
        else:
            if was_flapping:
                self._log("MPPT: error state stabilized", C_GREEN)
                self._flapping[key] = False
            if err_code:
                self._log(f"MPPT: ERROR ACTIVE – {decode_mppt_error(err_code)}", C_RED)
            else:
                self._log(f"MPPT: error cleared (was {decode_mppt_error(self._prev_mppt_error)})", C_GREEN)

        self._prev_mppt_error = err_code

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

        try:
            # LiTime status (LWT string, not JSON)
            if topic == f"{TOPIC_BASE}/status":
                broker_status = payload
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
        self.mppt_tab.refresh(vict)

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

    sys.exit(app.exec_())
