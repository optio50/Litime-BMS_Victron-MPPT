// =============================================================================
//  app_config.cpp  –  Runtime configuration implementation
// =============================================================================
//  Owns the appCfg global: defaults, /config.ini on LittleFS (load/save),
//  INI parsing/serialization, validation, and the JSON dump the Python
//  dashboard fetches from /config.json.
//
//  INI format (one variable per line, one brief description above each):
//
//      [wifi]
//      ; WiFi network — SSID of your 2.4 GHz WiFi network
//      ssid = MyNetwork
//      password = secret
//
//  Full-line comments only (";" or "#"). No inline comments — values may
//  contain "#" or ";" (e.g. WiFi passwords). Sections are cosmetic; keys are
//  globally unique and matched case-insensitively.
// =============================================================================
#include "app_config.h"
#include <LittleFS.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include <esp_task_wdt.h>

AppConfig appCfg;
bool g_apFallbackActive = false;

// ---------------------------------------------------------------------------
//  Field descriptors — drive the INI serialization (comments), the /config
//  HTML form (labels + descriptions + input types) and the docs. Order is
//  also the display order, grouped by section.
// ---------------------------------------------------------------------------
static const CfgFieldInfo CFG_FIELDS[] = {
    // key, section, label, desc, numeric, secret
    {"wifi_ssid",     "WiFi", "WiFi network",     "SSID of your 2.4 GHz WiFi network", false, false},
    {"wifi_password", "WiFi", "WiFi password",    "WiFi network password", false, true},
    {"hostname",    "WiFi", "Hostname", "Device name: http://<hostname>.local (letters/digits/dashes only)", false, false},

    {"broker",      "MQTT", "Broker address",   "IP or hostname of your MQTT broker (empty = MQTT disabled)", false, false},
    {"port",        "MQTT", "Broker port",      "MQTT port, usually 1883", true, false},
    {"user",        "MQTT", "MQTT username",    "Leave empty if the broker has no auth", false, false},
    {"pass",        "MQTT", "MQTT password",    "Leave empty if the broker has no auth", false, true},
    {"client_id",   "MQTT", "Client ID",        "MQTT client id for this device", false, false},
    {"topic_base",  "MQTT", "Topic base",       "Battery topics: <base>/battery1, /battery2, /combined", false, false},
    {"victron_topic_base", "MQTT", "Victron topic base", "MPPT topics: <base>/state, /batt_v, /pv_w, ...", false, false},

    {"ota_password", "OTA", "OTA/config password", "Password for the /update and /config pages (username: admin). Empty = no auth.", false, true},

    {"bms1_mac",    "Batteries", "Battery 1 MAC", "BLE MAC of battery 1, format AA:BB:CC:DD:EE:FF (empty = disabled)", false, false},
    {"bms2_mac",    "Batteries", "Battery 2 MAC", "BLE MAC of battery 2 (empty = disabled)", false, false},
    {"bms1_name",   "Batteries", "Battery 1 name", "Display name of battery 1", false, false},
    {"bms2_name",   "Batteries", "Battery 2 name", "Display name of battery 2", false, false},

    {"mppt_name",   "Victron MPPT", "MPPT name", "Device name as shown in the VictronConnect app", false, false},
    {"mppt_address", "Victron MPPT", "MPPT MAC", "BLE MAC of the Victron MPPT, lower-case (empty = decryption disabled)", false, false},
    {"mppt_key",    "Victron MPPT", "MPPT AES key", "32 hex chars: VictronConnect > Settings > Product Info > Encryption Key", false, true},

    {"weather_url", "Dashboard", "Weather URL", "Forecast page URL shown in the dashboard weather tab", false, false},
    {"array_name",  "Dashboard", "Array name", "Solar array label shown in the dashboard overview", false, false},

    {"overview_s",  "Display", "Overview dwell (s)", "Seconds the Overview screen shows before cycling", true, false},
    {"other_s",     "Display", "Other screens dwell (s)", "Seconds the Battery/Cells screens show before cycling", true, false},
    {"soc_reserve_pct", "Display", "SOC reserve %", "Time-to-empty targets this SOC %, not literal 0%", true, false},

    {"bms_update_ms",         "Timing", "BMS poll interval (ms)", "How often each battery is polled", true, false},
    {"mqtt_publish_ms",       "Timing", "MQTT publish interval (ms)", "How often data is published to MQTT", true, false},
    {"ble_scan_timeout_ms",   "Timing", "BLE connect timeout (ms)", "Per-battery boot-time connect timeout", true, false},
    {"component_timeout_ms",  "Timing", "Component watchdog (ms)", "Any component down this long forces a reboot", true, false},
};

static const size_t CFG_FIELD_COUNT = sizeof(CFG_FIELDS) / sizeof(CFG_FIELDS[0]);

static String fieldValue(const AppConfig& c, const String& keyIn);   // fwd decl

const CfgFieldInfo* appConfigFields(size_t& count) {
    count = CFG_FIELD_COUNT;
    return CFG_FIELDS;
}

String appConfigFieldValue(const String& key) {
    return fieldValue(appCfg, key);
}

// ---------------------------------------------------------------------------
//  Small helpers
// ---------------------------------------------------------------------------
static String sTrim(String s) { s.trim(); return s; }

static bool parseU32(const String& v, uint32_t& out) {
    if (v.length() == 0 || v.length() > 10) return false;
    for (size_t i = 0; i < v.length(); i++)
        if (!isDigit(v.charAt(i))) return false;
    out = (uint32_t)v.toInt();
    return true;
}

bool appConfigValidMac(const String& mac) {
    if (mac.length() != 17) return false;
    for (int i = 0; i < 17; i++) {
        char ch = mac.charAt(i);
        if ((i % 3) == 2) { if (ch != ':') return false; }
        else              { if (!isHexadecimalDigit(ch)) return false; }
    }
    return true;
}

bool appConfigParseHexKey(const String& hex32, uint8_t out[16]) {
    if (hex32.length() != 32) return false;
    for (int i = 0; i < 32; i++)
        if (!isHexadecimalDigit(hex32.charAt(i))) return false;
    for (int b = 0; b < 16; b++)
        out[b] = (uint8_t)strtol(hex32.substring(b * 2, b * 2 + 2).c_str(), nullptr, 16);
    return true;
}

static bool validHostname(const String& h) {
    if (h.length() < 1 || h.length() > 31) return false;
    for (size_t i = 0; i < h.length(); i++) {
        char c = h.charAt(i);
        if (!isalnum(c) && c != '-') return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
//  Set one field by key (case-insensitive). Unknown keys are ignored so
//  future/extra lines in config.ini never break a load.
// ---------------------------------------------------------------------------
bool appConfigSetField(AppConfig& c, const String& keyIn, const String& valueIn,
                       String& err) {
    err = "";
    String key = sTrim(keyIn); key.toLowerCase();
    String v    = sTrim(valueIn);

    if (key == "wifi_ssid" || key == "ssid") { c.wifi_ssid = v; }
    else if (key == "wifi_password" || key == "password") { c.wifi_password = v; }

    else if (key == "broker")        { c.mqtt_broker = v; }
    else if (key == "port") {
        uint32_t p;
        if (!parseU32(v, p) || p < 1 || p > 65535) {
            err = "port must be 1-65535"; return false;
        }
        c.mqtt_port = (uint16_t)p;
    }
    else if (key == "user")          { c.mqtt_user  = v; }
    else if (key == "pass")          { c.mqtt_pass  = v; }
    else if (key == "client_id")     { if (v.length()) c.mqtt_client_id = v; }
    else if (key == "topic_base")    { if (v.length()) c.mqtt_topic_base = v; }
    else if (key == "victron_topic_base") { if (v.length()) c.mqtt_victron_topic_base = v; }

    else if (key == "ota_password")  { c.ota_password = v; }

    else if (key == "bms1_mac") {
        if (v.length() && !appConfigValidMac(v)) { err = "bms1_mac must look like AA:BB:CC:DD:EE:FF (or be empty)"; return false; }
        c.bms1_mac = v;
    }
    else if (key == "bms2_mac") {
        if (v.length() && !appConfigValidMac(v)) { err = "bms2_mac must look like AA:BB:CC:DD:EE:FF (or be empty)"; return false; }
        c.bms2_mac = v;
    }
    else if (key == "bms1_name")     { c.bms1_name = v.length() ? v : "Battery 1"; }
    else if (key == "bms2_name")     { c.bms2_name = v.length() ? v : "Battery 2"; }

    else if (key == "mppt_name")    { c.victron_mppt_name = v; }
    else if (key == "mppt_address") {
        if (v.length() && !appConfigValidMac(v)) { err = "mppt_address must look like aa:bb:cc:dd:ee:ff (or be empty)"; return false; }
        c.victron_mppt_address = v;
    }
    else if (key == "mppt_key") {
        if (v.length()) {
            uint8_t k[16];
            if (!appConfigParseHexKey(v, k)) { err = "mppt_key must be exactly 32 hex characters (or empty)"; return false; }
            memcpy(c.victron_mppt_key, k, 16);
            c.victron_mppt_key_hex = v;
        } else {
            memset(c.victron_mppt_key, 0, 16);
            c.victron_mppt_key_hex = "";
        }
    }

    else if (key == "weather_url")   { c.weather_url = v; }
    else if (key == "array_name")    { c.array_name = v.length() ? v : "Solar Array"; }
    else if (key == "hostname") {
        if (v.length() && !validHostname(v)) { err = "hostname may only contain letters, digits and dashes (max 31)"; return false; }
        c.hostname = v.length() ? v : "litime";
    }
    else if (key == "overview_s") {
        uint32_t n;
        if (!parseU32(v, n) || n < 2 || n > 600) { err = "overview_s must be 2-600"; return false; }
        c.screen_overview_s = n;
    }
    else if (key == "other_s") {
        uint32_t n;
        if (!parseU32(v, n) || n < 2 || n > 600) { err = "other_s must be 2-600"; return false; }
        c.screen_other_s = n;
    }
    else if (key == "soc_reserve_pct") {
        uint32_t n;
        if (!parseU32(v, n) || n > 100) { err = "soc_reserve_pct must be 0-100"; return false; }
        c.soc_reserve_pct = (uint8_t)n;
    }

    else if (key == "bms_update_ms") {
        uint32_t n;
        if (!parseU32(v, n) || n < 500 || n > 600000) { err = "bms_update_ms must be 500-600000"; return false; }
        c.bms_update_ms = n;
    }
    else if (key == "mqtt_publish_ms") {
        uint32_t n;
        if (!parseU32(v, n) || n < 500 || n > 600000) { err = "mqtt_publish_ms must be 500-600000"; return false; }
        c.mqtt_publish_ms = n;
    }
    else if (key == "ble_scan_timeout_ms") {
        uint32_t n;
        if (!parseU32(v, n) || n < 2000 || n > 120000) { err = "ble_scan_timeout_ms must be 2000-120000"; return false; }
        c.ble_scan_timeout_ms = n;
    }
    else if (key == "component_timeout_ms") {
        uint32_t n;
        if (!parseU32(v, n) || n < 30000 || n > 86400000UL) { err = "component_timeout_ms must be 30000-86400000"; return false; }
        c.component_timeout_ms = n;
    }

    // Unknown keys: silently ignored (forward compatibility).

    // Bound string lengths so a huge paste into one field can never blow up
    // the heap or the config file on flash.
    struct StrCap { String* s; size_t maxLen; const char* name; };
    const StrCap caps[] = {
        { &c.wifi_ssid,     32,  "ssid" },
        { &c.wifi_password, 64,  "wifi password" },
        { &c.mqtt_broker,    63,  "broker" },
        { &c.mqtt_user,      32,  "user" },
        { &c.mqtt_pass,      64,  "pass" },
        { &c.mqtt_client_id, 32,  "client_id" },
        { &c.mqtt_topic_base, 31, "topic_base" },
        { &c.mqtt_victron_topic_base, 31, "victron_topic_base" },
        { &c.ota_password,  63,  "ota_password" },
        { &c.bms1_name,     31,  "bms1_name" },
        { &c.bms2_name,     31,  "bms2_name" },
        { &c.weather_url,   127, "weather_url" },
        { &c.array_name,    31,  "array_name" },
        { &c.victron_mppt_name, 31, "mppt_name" },
    };
    // caps point directly into c, whose members were just assigned above.
    for (const auto& cap : caps) {
        if (cap.s->length() > cap.maxLen) {
            err = String(cap.name) + " is too long (max " + String(cap.maxLen) + " chars)";
            return false;
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
//  Field value as a String (for INI serialization and the HTML form)
// ---------------------------------------------------------------------------
static String fieldValue(const AppConfig& c, const String& keyIn) {
    String key = keyIn; key.toLowerCase();
    if      (key == "wifi_ssid" || key == "ssid")         return c.wifi_ssid;
    else if (key == "wifi_password" || key == "password") return c.wifi_password;
    else if (key == "broker")          return c.mqtt_broker;
    else if (key == "port")            return String(c.mqtt_port);
    else if (key == "user")            return c.mqtt_user;
    else if (key == "pass")            return c.mqtt_pass;
    else if (key == "client_id")       return c.mqtt_client_id;
    else if (key == "topic_base")      return c.mqtt_topic_base;
    else if (key == "victron_topic_base") return c.mqtt_victron_topic_base;
    else if (key == "ota_password")    return c.ota_password;
    else if (key == "bms1_mac")        return c.bms1_mac;
    else if (key == "bms2_mac")        return c.bms2_mac;
    else if (key == "bms1_name")       return c.bms1_name;
    else if (key == "bms2_name")       return c.bms2_name;
    else if (key == "mppt_name")       return c.victron_mppt_name;
    else if (key == "mppt_address")    return c.victron_mppt_address;
    else if (key == "mppt_key")        return c.victron_mppt_key_hex;
    else if (key == "weather_url")     return c.weather_url;
    else if (key == "array_name")      return c.array_name;
    else if (key == "hostname")        return c.hostname;
    else if (key == "overview_s")      return String(c.screen_overview_s);
    else if (key == "other_s")         return String(c.screen_other_s);
    else if (key == "soc_reserve_pct") return String(c.soc_reserve_pct);
    else if (key == "bms_update_ms")         return String(c.bms_update_ms);
    else if (key == "mqtt_publish_ms")       return String(c.mqtt_publish_ms);
    else if (key == "ble_scan_timeout_ms")    return String(c.ble_scan_timeout_ms);
    else if (key == "component_timeout_ms")  return String(c.component_timeout_ms);
    return "";
}

// ---------------------------------------------------------------------------
//  Defaults
// ---------------------------------------------------------------------------
void appConfigSetDefaults() {
    appCfg = AppConfig();   // struct literal defaults in app_config.h
}

// ---------------------------------------------------------------------------
//  Load /config.ini from LittleFS
// ---------------------------------------------------------------------------
bool appConfigLoad() {
    if (!LittleFS.begin(true)) {
        Serial.println("[CFG] LittleFS mount FAILED - using defaults "
                       "(configure via the litime-setup AP: http://192.168.4.1/config)");
        return false;
    }
    if (!LittleFS.exists("/config.ini")) {
        Serial.println("[CFG] /config.ini not found - defaults active. "
                       "Open http://<device-ip>/config in a browser to set up.");
        return false;
    }
    File f = LittleFS.open("/config.ini", "r");
    if (!f) { Serial.println("[CFG] /config.ini unreadable - defaults active"); return false; }
    if (f.size() > 8192) {
        f.close();
        Serial.println("[CFG] /config.ini suspiciously large - ignored");
        return false;
    }
    String ini = f.readString();
    f.close();

    String err;
    appConfigApplyIni(ini, err, /*strict=*/false);  // lenient at boot
    if (!err.isEmpty())
        Serial.printf("[CFG] config.ini warnings: %s\n", err.c_str());
    Serial.printf("[CFG] Loaded: ssid=\"%s\" pass_len=%u broker=\"%s\" hostname=%s\n",
                  appCfg.wifi_ssid.c_str(),
                  (unsigned)appCfg.wifi_password.length(),
                  appCfg.mqtt_broker.c_str(),
                  appCfg.hostname.c_str());
    return true;
}

// ---------------------------------------------------------------------------
//  INI parsing
// ---------------------------------------------------------------------------
bool appConfigApplyIni(const String& ini, String& error, bool strict) {
    error = "";
    AppConfig backup = appCfg;
    String warnings = "";
    int start = 0;
    int lineNo = 0;

    while (start < (int)ini.length()) {
        int nl = ini.indexOf('\n', start);
        int end = (nl < 0) ? (int)ini.length() : nl;
        String line = ini.substring(start, end);
        start = (nl < 0) ? (int)ini.length() + 1 : nl + 1;
        lineNo++;

        line = sTrim(line);
        if (line.length() == 0) continue;                // blank
        if (line[0] == ';' || line[0] == '#') continue;  // comment
        if (line[0] == '[') continue;                    // section header (cosmetic)

        int eq = line.indexOf('=');
        if (eq < 0) {
            if (strict) {
                appCfg = backup;
                error = "line " + String(lineNo) + " has no '=' : \"" + line + "\"";
                return false;
            }
            continue;   // lenient: skip junk lines at boot
        }

        String key = sTrim(line.substring(0, eq));
        String val = sTrim(line.substring(eq + 1));

        String ferr;
        appConfigSetField(appCfg, key, val, ferr);
        if (!ferr.isEmpty()) {
            if (strict) {
                appCfg = backup;
                error = "line " + String(lineNo) + ": " + ferr +
                        " (got \"" + val + "\")";
                return false;
            }
            warnings += (warnings.length() ? "; " : "") + ferr;
        }
    }

    error = warnings;
    return true;
}

// ---------------------------------------------------------------------------
//  Serialization
// ---------------------------------------------------------------------------
String appConfigToIni() {
    String out;
    out += "; LiTime Dual BMS Monitor - config.ini\n";
    out += "; Edit at http://" + appCfg.hostname + ".local/config (or paste this\n";
    out += "; whole file into the Import box there). Full-line comments only.\n";
    String lastSection = "";
    for (size_t i = 0; i < CFG_FIELD_COUNT; i++) {
        const CfgFieldInfo& f = CFG_FIELDS[i];
        if (String(f.section) != lastSection) {
            lastSection = f.section;
            String tag = lastSection; tag.toLowerCase();
            out += "\n[" + tag + "]\n";
        }
        out += "; " + String(f.label) + " - " + String(f.desc) + "\n";
        out += String(f.key) + " = " + fieldValue(appCfg, f.key) + "\n";
    }
    return out;
}

bool appConfigSave() {
    File f = LittleFS.open("/config.ini", "w");
    if (!f) {
        Serial.println("[CFG] FAILED to open /config.ini for writing");
        return false;
    }
    String ini = appConfigToIni();
    size_t n = f.print(ini);
    f.flush();
    f.close();
    if (n != ini.length()) {
        Serial.println("[CFG] FAILED: short write to /config.ini");
        return false;
    }
    Serial.printf("[CFG] Saved /config.ini (%u bytes)\n", (unsigned)n);
    return true;
}

// ---------------------------------------------------------------------------
//  JSON for the Python dashboard (GET /config.json)
//  Keys are the names shared_config.py / litime_monitor.py have always read.
//  Deliberately excludes wifi_password and the Victron AES key.
// ---------------------------------------------------------------------------
String appConfigToJsonForDashboard() {
    JsonDocument doc;
    doc["MQTT_BROKER"]             = appCfg.mqtt_broker;
    doc["MQTT_PORT"]               = appCfg.mqtt_port;
    doc["MQTT_USER"]               = appCfg.mqtt_user;
    doc["MQTT_PASS"]               = appCfg.mqtt_pass;
    doc["MQTT_CLIENT_ID"]          = appCfg.mqtt_client_id;
    doc["MQTT_TOPIC_BASE"]         = appCfg.mqtt_topic_base;
    doc["MQTT_VICTRON_TOPIC_BASE"] = appCfg.mqtt_victron_topic_base;
    doc["OTA_PASSWORD"]            = appCfg.ota_password;
    doc["BMS1_MAC"]                = appCfg.bms1_mac;
    doc["BMS2_MAC"]                = appCfg.bms2_mac;
    doc["BMS1_NAME"]               = appCfg.bms1_name;
    doc["BMS2_NAME"]               = appCfg.bms2_name;
    doc["VICTRON_MPPT_NAME"]       = appCfg.victron_mppt_name;
    doc["VICTRON_MPPT_ADDRESS"]    = appCfg.victron_mppt_address;
    doc["WEATHER_URL"]             = appCfg.weather_url;
    doc["ARRAY_NAME"]              = appCfg.array_name;
    doc["HOSTNAME"]                = appCfg.hostname;
    doc["SOC_RESERVE_PCT"]         = appCfg.soc_reserve_pct;
    doc["SCREEN_OVERVIEW_S"]       = appCfg.screen_overview_s;
    doc["SCREEN_OTHER_S"]          = appCfg.screen_other_s;
    doc["BMS_UPDATE_MS"]           = appCfg.bms_update_ms;
    doc["MQTT_PUBLISH_MS"]         = appCfg.mqtt_publish_ms;
    doc["BLE_SCAN_TIMEOUT_MS"]     = appCfg.ble_scan_timeout_ms;
    doc["COMPONENT_TIMEOUT_MS"]    = appCfg.component_timeout_ms;
    String out;
    serializeJson(doc, out);
    return out;
}

// ---------------------------------------------------------------------------
//  Try associating with new WiFi credentials WITHOUT saving anything.
//  Restores the previously configured STA connection afterwards, so a test
//  can never leave the device offline. Called from the web page's
//  "Test WiFi" button (blocks up to 15 s - the watchdog is fed meanwhile).
// ---------------------------------------------------------------------------
bool appConfigTestWifi(const String& ssid, const String& pass, String& detail) {
    if (ssid.length() == 0) { detail = "SSID is empty"; return false; }

    String curSsid = appCfg.wifi_ssid;
    String curPass = appCfg.wifi_password;

    Serial.printf("[CFG] WiFi test: associating with \"%s\" (pass %u chars)...\n",
                  ssid.c_str(), (unsigned)pass.length());
    WiFi.persistent(false);

    if (g_apFallbackActive) {
        // Keep the setup AP up so the phone stays on this page. STA is
        // added alongside (WIFI_AP_STA) only for the duration of the test.
        WiFi.mode(WIFI_AP_STA);
        WiFi.softAP("litime-setup");
    } else {
        WiFi.mode(WIFI_STA);
    }
    WiFi.begin(ssid.c_str(), pass.c_str());

    unsigned long t0 = millis();
    bool ok = false;
    while (millis() - t0 < 15000) {
        esp_task_wdt_reset();
        if (WiFi.status() == WL_CONNECTED) { ok = true; break; }
        delay(250);
    }

    if (ok) {
        detail = "Associated - IP " + WiFi.localIP().toString();
        Serial.printf("[CFG] WiFi test OK: %s\n", detail.c_str());
    } else {
        detail = "Could not associate (check password / 2.4 GHz band)";
        Serial.println("[CFG] WiFi test FAILED");
    }

    // Drop only the STA association. Leave the AP running if we were in
    // setup mode so the phone never has to rejoin.
    WiFi.disconnect(/*wifioff=*/false, /*eraseap=*/false);
    if (g_apFallbackActive) {
        WiFi.mode(WIFI_AP);
        WiFi.softAP("litime-setup");
        Serial.println("[CFG] WiFi test done - setup AP still up");
    } else if (curSsid.length() > 0) {
        WiFi.mode(WIFI_STA);
        WiFi.begin(curSsid.c_str(), curPass.c_str());
    }
    return ok;
}
