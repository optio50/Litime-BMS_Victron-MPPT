#pragma once
// =============================================================================
//  app_config.h  –  Runtime configuration (config.ini on LittleFS flash)
// =============================================================================
//  ALL user settings live in /config.ini on the device's LittleFS partition —
//  NOT compiled into the firmware. They are edited from any browser at:
//
//      http://<hostname>.local/config      (e.g. http://litime.local/config)
//
//  ...or, when WiFi can't connect at all, from the "litime-setup" fallback
//  access point (http://192.168.4.1/config). Changing a setting = edit the
//  web form + Save; the device writes config.ini to flash and reboots. No
//  recompile, no firmware upload, ever.
//
//  appCfg is loaded in setup() BEFORE WiFi/BLE/MQTT start, so every other
//  module just reads the already-populated global. If config.ini is missing
//  or partially broken, safe defaults below keep the device bootable and
//  reachable via the setup AP.
//
//  Hardware constants (TFT pins, RGB565 palette) stay compiled in config.h —
//  they are wiring facts, not user settings.
// =============================================================================
#include <Arduino.h>

struct AppConfig {
    // --- [wifi] ---
    String     wifi_ssid;              // empty = unconfigured -> setup AP
    String     wifi_password;          // empty = open network

    // --- [mqtt] ---
    String     mqtt_broker;            // empty = MQTT disabled
    uint16_t   mqtt_port = 1883;
    String     mqtt_user;              // empty = no auth
    String     mqtt_pass;              // empty = no auth
    String     mqtt_client_id = "litime-monitor";
    String     mqtt_topic_base = "litime";     // <base>/battery1, battery2, combined
    String     mqtt_victron_topic_base = "victron";  // <base>/state, <base>/batt_v, ...

    // --- [ota] ---
    String     ota_password;           // /update + /config auth (user "admin"); empty = none

    // --- [batteries] ---
    String     bms1_mac;               // empty = battery 1 disabled
    String     bms2_mac;               // empty = battery 2 disabled
    String     bms1_name = "Battery 1";
    String     bms2_name = "Battery 2";

    // --- [victron] ---
    String     victron_mppt_name;      // informational (VictronConnect name)
    String     victron_mppt_address;  // empty = MPPT decryption disabled
    String     victron_mppt_key_hex;  // 32 hex chars
    uint8_t    victron_mppt_key[16] = {0};   // parsed from key_hex at load

    // --- [display] ---
    String     weather_url = "https://www.msn.com/en-us/weather/forecast/in-Phoenix,AZ";   // any URL; empty = no weather tab
    String     array_name = "Solar Array";
    String     hostname = "litime";    // mDNS/DHCP name -> http://<hostname>.local
    uint32_t   screen_overview_s = 15; // Overview page dwell (s)
    uint32_t   screen_other_s    = 10; // Battery / Cells page dwell (s)
    uint8_t    soc_reserve_pct   = 10; // time-to-empty targets this SOC %, not 0

    // --- [timing] ---
    uint32_t   bms_update_ms          = 2000;    // poll each BMS every (ms)
    uint32_t   mqtt_publish_ms        = 2000;    // publish to MQTT every (ms)
    uint32_t   ble_scan_timeout_ms    = 15000;   // per-battery boot connect timeout
    uint32_t   component_timeout_ms   = 120000;  // any component down this long -> reboot
};

// The single global instance — populated by appConfigLoad() in setup().
extern AppConfig appCfg;

// True while the "litime-setup" fallback access point is active (set by the
// main sketch when boot-time WiFi connect fails; cleared when STA recovers).
extern bool g_apFallbackActive;

// Reset appCfg to the built-in defaults above.
void appConfigSetDefaults();

// Mount LittleFS and parse /config.ini into appCfg.
//  - file missing            -> defaults stay active, returns false
//  - file present but flawed -> good lines applied, bad lines keep defaults
//                                (warnings printed), returns true
bool appConfigLoad();

// Write appCfg back to /config.ini as INI text (with brief descriptions).
bool appConfigSave();

// Current values as INI text — same format /config.ini serves and the
// Import box accepts. Includes a one-line description of every variable.
String appConfigToIni();

// Parse INI text into appCfg. strict=true (web Save/Import): any bad line
// aborts, appCfg keeps its previous values, error says which line.
// strict=false (boot-time file load): bad lines are skipped with a warning.
bool appConfigApplyIni(const String& ini, String& error, bool strict);

// Set a single field by canonical key ("ssid", "broker", "bms1_mac", ...).
// Validates the value; err gets a human message when invalid. Unknown keys
// are silently ignored (forward compatibility). Used by ApplyIni and the
// web form Save handler (which iterates POST args).
bool appConfigSetField(AppConfig& c, const String& key, const String& value,
                       String& err);

// JSON dump for the Python dashboard (GET /config.json) — keys match what
// litime_monitor.py / shared_config.py have always consumed (MQTT_BROKER,
// MQTT_PORT, ... BMS1_NAME, WEATHER_URL, ARRAY_NAME, OTA_PASSWORD, ...).
// Deliberately excludes wifi_password and the Victron AES key.
String appConfigToJsonForDashboard();

// Validation helpers (also used by the config web page).
bool appConfigValidMac(const String& mac);
bool appConfigParseHexKey(const String& hex32, uint8_t out[16]);

// Field metadata — one entry per setting (used by the /config web form and
// the INI serializer to label/describe every variable).
struct CfgFieldInfo {
    const char* key;      // canonical (lowercase) INI/form key
    const char* section;  // section heading (cosmetic grouping)
    const char* label;    // human name
    const char* desc;     // very brief description
    bool        numeric;   // number input instead of text
    bool        secret;   // password input (masked)
};
const CfgFieldInfo* appConfigFields(size_t& count);   // read-only field table
String appConfigFieldValue(const String& key);        // current value of a key


// Try associating with ssid/pass (without saving anything). Returns true on
// association; restores the previously configured STA connection afterwards.
// Used by the "Test WiFi" button so a typo can never lock you out.
bool appConfigTestWifi(const String& ssid, const String& pass, String& detail);
