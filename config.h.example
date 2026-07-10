#pragma once

// =============================================================================
//  LiTime Dual Battery Monitor - Configuration
//  Target: Seeed Studio XIAO ESP32-S3
// =============================================================================
// This is a combined config for both the LiTime_BMS_Display.ino sketch and the victron_mppt.h BLE scanner.


// --- WiFi -------------------------------------------------------------------
#define WIFI_SSID        "YOUR_WIFI_SSID"
#define WIFI_PASSWORD    "YOUR_WIFI_PASSWORD"

// --- MQTT Broker (local) ----------------------------------------------------
#define MQTT_BROKER      "192.168.1.100"    // IP of your local broker (e.g. Home Assistant / Mosquitto)
#define MQTT_PORT        1883
#define MQTT_USER        ""                // leave empty if no auth
#define MQTT_PASS        ""
#define MQTT_CLIENT_ID   "litime-monitor"
#define MQTT_TOPIC_BASE  "litime"          // Topics: litime/battery1, litime/battery2, litime/combined
#define MQTT_VICTRON_TOPIC_BASE  "victron"  // Topics: victron/state, victron/batt_v, etc.

// --- Weather Forecast URL (shown in MPPT "Weather" sub-tab) ----------------
// Point this at any weather page to get a quick solar/rain forecast.
// International users: change "en-us" to your locale (en-gb, de-de, etc.)
// and the city/state to your location (e.g. /in-London, /in-Berlin).
#define WEATHER_URL  "https://www.msn.com/en-us/weather/forecast/in-YourCity,ST"

// --- BLE MAC Addresses of each LiTime 48V 100Ah battery --------------------
// Run a BLE scan (see README) to find the MAC addresses of your batteries.
// Format: "AA:BB:CC:DD:EE:FF"  (case-insensitive)
//
// Easiest method: install the free "nRF Connect" app (Nordic Semiconductor,
// iOS/Android) on your phone, open it, tap SCAN, and look for devices
// advertising as your LiTime battery (e.g. name containing "LT" or similar,
// or by matching manufacturer data). Tap the device to see its MAC address
// listed at the top of the detail screen (iOS shows a UUID instead of a MAC
// due to OS restrictions - use Android or the BLE-scan-from-README method
// on iOS). Power-cycle or bring the phone close to each battery individually
// to be sure which MAC belongs to which physical unit.
#define BMS1_MAC         "AA:BB:CC:DD:EE:01"   // Battery 1 - replace with your MAC
#define BMS2_MAC         "AA:BB:CC:DD:EE:02"   // Battery 2 - replace with your MAC

#define BMS1_NAME        "Battery 1"
#define BMS2_NAME        "Battery 2"

// --- Display (ILI9341 240x320 SPI) ------------------------------------------
// XIAO ESP32-S3 hardware SPI + chosen DC/CS/RST pins
// Waveshare 2.4" LCD module pinout -> XIAO header:
//   LCD VCC   -> 3V3
//   LCD GND   -> GND
//   LCD DIN   -> D9  (GPIO8)  [SPI MOSI / data in]
//   LCD CLK   -> D8  (GPIO7)  [SPI SCK]
//   LCD CS    -> D2  (GPIO3)
//   LCD DC    -> D3  (GPIO4)
//   LCD RST   -> D1  (GPIO2)
//   LCD BL    -> 5V  (XIAO VBUS/5V pin)
//               Safe: Waveshare module is rated 3.3V/5V operating voltage.
//               The backlight LED is driven through an on-board series resistor;
//               BL is the enable input, not a direct LED pin.
//
// NOTE: On XIAO ESP32-S3, Arduino pin numbers = GPIO numbers (NOT D-pin numbers)
//   D1=GPIO2, D2=GPIO3, D3=GPIO4, D8=GPIO7, D9=GPIO8, D10=GPIO9
#define TFT_CS    3    // D2  = GPIO3
#define TFT_RST   2    // D1  = GPIO2
#define TFT_DC    4    // D3  = GPIO4
#define TFT_MOSI  8    // D9  = GPIO8
#define TFT_CLK   7    // D8  = GPIO7
#define TFT_MISO  -1  // not used

// Backlight pin (-1 = disabled/always-on; BL wired directly to the XIAO's
// 5V/VBUS pin, not jumpered on the module itself and not GPIO-controlled).
// 3V3 was already used for LCD VCC, so BL was tied to 5V instead - safe since
// the Waveshare module is rated for 3.3V/5V and BL only enables the
// on-board backlight driver (not a direct LED pin).
// Set to a GPIO number only if you rewire BL to an XIAO pin for dimming/control.
#define TFT_BL    -1

// --- Screen cycling ---------------------------------------------------------
// Each page has its own dwell time (seconds)
#define SCREEN_OVERVIEW_S   15   // Overview page dwell time
#define SCREEN_OTHER_S      10   // Battery / Cells page dwell time

// --- Update intervals -------------------------------------------------------
#define BMS_UPDATE_MS    2000    // How often to poll each BMS (ms)
#define MQTT_PUBLISH_MS  2000   // How often to publish to MQTT (ms)

// --- BLE scan settings ------------------------------------------------------
// Time (ms) to scan for each battery at startup before giving up
#define BLE_SCAN_TIMEOUT_MS  15000

// --- BLE reconnect backoff (runtime, after initial connection is lost) -----
// After this many consecutive failed reconnect attempts (one attempt per
// BMS_UPDATE_MS round), stop trying for BLE_RECONNECT_COOLDOWN_MS before
// starting a fresh round of attempts. Prevents endless rapid-fire BLE
// connect() calls when a battery is genuinely out of range / powered off.
#define BLE_RECONNECT_MAX_ATTEMPTS   5
#define BLE_RECONNECT_COOLDOWN_MS    60000    // 1 minute

// --- Time-to-empty reserve threshold ----------------------------------------
// "Time remaining" while discharging is estimated down to this % SOC, not
// literal 0%. LFP packs shouldn't routinely be run past this point, and the
// BMS's voltage-based low-cutoff can trip before true 0% Ah is reached anyway.
// "Time to full" while charging is unaffected and still targets 100%.
#define SOC_RESERVE_PCT   10   // percent (0-100)

// --- Victron MPPT Bluetooth -------------------------------------------------
// Passive BLE advertising scan – no connection required.
// 1) VICTRON_MPPT_NAME : device name as shown in VictronConnect app.
// 2) VICTRON_MPPT_ADDRESS : BLE MAC address (lower-case, colon-separated).
// 3) VICTRON_MPPT_KEY : 16-byte AES-128 encryption key from
//    VictronConnect > Settings (⋮) > Product Info > Encryption Key.
//    NB: The VC app may only show 31 of 32 hex chars on-screen; use the
//    copy/share dialogue to capture all 32 characters safely.
#define VICTRON_MPPT_NAME     "My Solar Controller"
#define VICTRON_MPPT_ADDRESS  "aa:bb:cc:dd:ee:ff"
const uint8_t VICTRON_MPPT_KEY[16] = {
    0x00,0x11,0x22,0x33,0x44,0x55,0x66,0x77,
    0x88,0x99,0xaa,0xbb,0xcc,0xdd,0xee,0xff
};

// --- Color palette (RGB565) ------------------------------------------------
#define COL_BG        0x0000   // Black
#define COL_HEADER    0x1082   // Very dark grey
#define COL_ACCENT    0x07FF   // Cyan
#define COL_TEXT      0xFFFF   // White
#define COL_DIM       0x7BEF   // Light grey
#define COL_GREEN     0x07E0   // Green
#define COL_YELLOW    0xFFE0   // Yellow
#define COL_ORANGE    0xFD20   // Orange
#define COL_RED       0xF800   // Red
#define COL_BLUE      0x001F   // Blue
#define COL_DARKGREEN 0x03E0   // Dark green (SOC bar fill)
