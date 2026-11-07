// =============================================================================
//  LiTime_BMS_Display.ino
//
//  Dual LiTime 48V 100Ah BMS monitor via BLE for Seeed XIAO ESP32-S3
//  - Reads both batteries via BLE using the BMSClient library
//  - Displays real-time stats on a Waveshare 2.4" ILI9341 (240x320)
//  - Publishes to a local MQTT broker (with Home Assistant auto-discovery)
//
//  Required Arduino Libraries (install via Library Manager):
//    • "BMS Client"        (mirosieber/Litime_BMS_ESP32)
//    • Adafruit ILI9341
//    • Adafruit GFX Library
//    • PubSubClient        (Nick O'Leary)
//    • ArduinoJson         (Benoit Blanchon) v7+
//    • ElegantOTA          (Ayush Sharma)  – WiFi firmware updates at /update
//    Settings live on the device (http://<hostname>.local/config) — no
//    compile-time credentials. First boot starts a "litime-setup" AP if
//    WiFi isn't configured yet.
//
//  Board: "XIAO_ESP32S3"  (Seeed XIAO ESP32S3 in Arduino boards manager)
// =============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <DNSServer.h>
#include <esp_task_wdt.h>
#include <BMSClient.h>

#include "app_config.h"       // appCfg - runtime settings from /config.ini
#include "LiTime_BMS_Display.h"
#include "display_manager.h"
#include "mqtt_manager.h"
#include "victron_mppt.h"
#include "ota_manager.h"
#include "config_server.h"    // /config web page (same server as /update)

// ---------------------------------------------------------------------------
//  Globals
// ---------------------------------------------------------------------------
BMSClient     bms1, bms2;
BatteryData   bat1, bat2;
DisplayManager display;
MQTTManager   mqttMgr;
OtaManager    otaMgr;
VictronMPPT   victronMppt;
VictronData   mpptData;
ConfigServer  configSrv;

// Captive-portal DNS server - only running while the litime-setup fallback
// access point is active (boot-time WiFi connect failed).
DNSServer     dnsServer;

// MQTT started flag - begin() may be deferred (no broker configured yet,
// or waiting for STA recovery after a setup-AP session).
bool          mqttStarted = false;


// Timing
unsigned long lastBmsUpdateMs    = 0;
unsigned long lastDisplayRefresh = 0;
unsigned long lastMqttPublish    = 0;
unsigned long lastPageCycle      = 0;

// Which BMS to update next (alternating to avoid BLE contention)
uint8_t  bmsRound = 0;

// BLE reconnect state (per battery)
// Simple running counter - no more round/cooldown cycle. The global component
// watchdog (COMPONENT_TIMEOUT_MS) will force an ESP32 reboot if a battery
// stays disconnected for too long.
uint8_t       bleReconnectAttempts[2] = {0, 0};

// Component disconnect watchdog - tracks when each component was last "healthy"
unsigned long lastWifiOkMs   = 0;
unsigned long lastBms1OkMs   = 0;
unsigned long lastBms2OkMs   = 0;
unsigned long lastMpptOkMs   = 0;

// Boot button on XIAO ESP32-S3 = GPIO0 (active LOW)
#define BOOT_BTN_PIN  0
volatile bool btnPressed = false;

// Watchdog timeout (seconds)
#define WDT_TIMEOUT_S  60

// ---------------------------------------------------------------------------
//  ISR: boot button press
// ---------------------------------------------------------------------------
void IRAM_ATTR onButtonPress() {
    btnPressed = true;
}

// ---------------------------------------------------------------------------
//  Copy BMSClient data into our BatteryData struct
// ---------------------------------------------------------------------------
static void copyBmsData(BMSClient& src, BatteryData& dst) {
    dst.connected        = src.isConnected();
    dst.totalVoltage     = src.getTotalVoltage();
    dst.cellVoltageSum   = src.getCellVoltageSum();
    dst.current          = src.getCurrent();
    dst.mosfetTemp       = src.getMosfetTemp();
    dst.cellTemp         = src.getCellTemp();
    dst.soc              = src.getSOC();
    dst.soh              = src.getSOH();
    dst.remainingAh      = src.getRemainingAh();
    dst.fullCapacityAh   = src.getFullCapacityAh();
    dst.protectionState  = src.getProtectionState();
    dst.statusReg68      = src.getHeatState();  // library mislabels offset 68 as "heat"
    dst.balanceMemory    = src.getBalanceMemory();
    dst.failureState     = src.getFailureState();
    dst.balancingState   = src.getBalancingState();
    dst.batteryState     = src.getBatteryState();
    dst.dischargesCount  = src.getDischargesCount();
    dst.dischargesAhCount= src.getDischargesAhCount();
    dst.cellVoltages     = src.getCellVoltages();
    if (dst.connected) dst.lastUpdateMs = millis();
}

// ---------------------------------------------------------------------------
//  Connect to WiFi with timeout (credentials from appCfg / flash config)
// ---------------------------------------------------------------------------
static bool connectWiFi() {
    if (appCfg.wifi_ssid.length() == 0) {
        Serial.println("[WiFi] No credentials configured - starting setup AP.");
        return false;
    }
    Serial.printf("[WiFi] Connecting to \"%s\" (pass %u chars)\n",
                  appCfg.wifi_ssid.c_str(), (unsigned)appCfg.wifi_password.length());
    // Same radio path as Test WiFi (which succeeds): don't WIFI_OFF, don't
    // wipe NVS. Just STA + begin. Aggressive teardown was leaving the radio
    // unable to associate after reboot even with good credentials.
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.setHostname(appCfg.hostname.c_str());
    WiFi.begin(appCfg.wifi_ssid.c_str(), appCfg.wifi_password.c_str());

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        esp_task_wdt_reset();
        if (millis() - start > 25000) {
            Serial.printf("[WiFi] Timeout. status=%d\n", (int)WiFi.status());
            return false;
        }
        delay(500);
        Serial.print(".");
    }
    Serial.printf("\n[WiFi] Connected. IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
}

// ---------------------------------------------------------------------------
//  Start mDNS so the device is reachable at http://<hostname>.local
//  (also what litime_monitor.py uses to fetch /config.json)
// ---------------------------------------------------------------------------
static void startMdns() {
    if (MDNS.begin(appCfg.hostname.c_str())) {
        MDNS.addService("http", "tcp", 80);
        Serial.printf("[WiFi] mDNS ready: http://%s.local\n", appCfg.hostname.c_str());
    } else {
        Serial.println("[WiFi] mDNS begin FAILED");
    }
}

// ---------------------------------------------------------------------------
//  Boot-time WiFi failed (or never configured): start the "litime-setup"
//  fallback access point so credentials can be fixed from ANY browser.
//  WIFI_AP only (not AP_STA): a phone joining the AP must not look like
//  STA recovery, and we must not background-retry STA (that drops AP
//  clients). The AP stays up until /config Save & Reboot.
// ---------------------------------------------------------------------------
static void startFallbackAp() {
    WiFi.disconnect(true, true);
    delay(100);
    WiFi.mode(WIFI_AP);
    WiFi.softAP("litime-setup");
    // Captive DNS so the phone pops the config page after it joins.
    // DNS itself is async and cheap; the web server is what must not be
    // hammered during the join (see the delay in loop()).
    dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
    dnsServer.setTTL(1);
    dnsServer.start(53, "*", WiFi.softAPIP());
    g_apFallbackActive = true;
    display.showScanStatus("SETUP AP  litime-setup  192.168.4.1");
    Serial.printf("[WiFi] Setup AP active: litime-setup  ->  http://%s/config\n",
                  WiFi.softAPIP().toString().c_str());
    Serial.println("[WiFi] AP will stay up until you Save WiFi on /config.");
}


// ---------------------------------------------------------------------------
//  Try to connect a single BMSClient with status feedback on display
// ---------------------------------------------------------------------------
static void connectBMS(BMSClient& bms, BatteryData& data, const char* mac,
                        const char* name) {
    char msg[64];
    snprintf(msg, sizeof(msg), "BLE: connecting %s...", name);
    Serial.println(msg);
    display.showScanStatus(msg);

    bms.init(mac);
    unsigned long t = millis();
    bool ok = false;
    while (!ok && (millis() - t < appCfg.ble_scan_timeout_ms)) {
        esp_task_wdt_reset();
        ok = bms.connect();
        if (!ok) delay(1000);
    }

    if (ok) {
        snprintf(msg, sizeof(msg), "BLE: %s connected!", name);
        Serial.println(msg);
    } else {
        snprintf(msg, sizeof(msg), "BLE: %s FAILED", name);
        Serial.println(msg);
    }
    display.showScanStatus(msg);
    data.connected = ok;
    delay(1000);
}

// ---------------------------------------------------------------------------
//  Attempt a runtime BLE reconnect for one battery.
//  Tries bms.connect() once per call. Periodically reinitializes the BLE
//  client (bms.init) to clear stale state that may prevent reconnection.
//  The global component watchdog handles forcing a reboot if this goes on
//  too long (COMPONENT_TIMEOUT_MS).
// ---------------------------------------------------------------------------
static void reconnectBmsIfNeeded(uint8_t idx, BMSClient& bms, BatteryData& data,
                                  const char* mac, const char* name) {
    if (bms.isConnected()) {
        // Reset disconnect tracking on successful connection
        if (bleReconnectAttempts[idx] > 0 || data.bleDisconnectMs > 0) {
            Serial.printf("[BLE] %s reconnected after %u attempts.\n",
                          name, bleReconnectAttempts[idx]);
        }
        bleReconnectAttempts[idx] = 0;
        data.bleRetryAttempts = 0;
        data.bleRetryPaused   = false;
        data.bleDisconnectMs  = 0;
        return;
    }

    // Mark as disconnected and track when it started
    data.connected = false;
    if (data.bleDisconnectMs == 0) {
        data.bleDisconnectMs = millis();
        Serial.printf("[BLE] %s lost. Reconnecting...\n", name);
    }

    // Periodically reinitialize the BLE client to clear stale state.
    // Every 5th attempt (i.e. every ~20s at 4s per battery per round).
    if (bleReconnectAttempts[idx] > 0 && (bleReconnectAttempts[idx] % 5) == 0) {
        Serial.printf("[BLE] %s: reinitializing client (attempt %u).\n",
                      name, bleReconnectAttempts[idx] + 1);
        bms.init(mac);
    }

    if (bms.connect()) {
        Serial.printf("[BLE] %s reconnected.\n", name);
        bleReconnectAttempts[idx] = 0;
        data.bleRetryAttempts = 0;
        data.bleRetryPaused   = false;
        data.bleDisconnectMs  = 0;
        return;
    }

    bleReconnectAttempts[idx]++;
    data.bleRetryAttempts = bleReconnectAttempts[idx];
    data.bleRetryPaused   = false;  // always false - no more cooldown cycle
}

// ---------------------------------------------------------------------------
//  setup()
// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n=== LiTime Dual BMS Monitor ===");

    WiFi.persistent(false);   // never store creds in NVS - config.ini is source of truth

    // Load settings BEFORE the watchdog: first-boot LittleFS format of the
    // 1.5 MB partition can take several seconds.
    appConfigLoad();

    // Watchdog
    esp_task_wdt_config_t wdt_cfg = {
        .timeout_ms  = (uint32_t)WDT_TIMEOUT_S * 1000,
        .trigger_panic = true
    };
    esp_task_wdt_reconfigure(&wdt_cfg);
    esp_task_wdt_add(nullptr);

    // Boot button (manual page advance)
    pinMode(BOOT_BTN_PIN, INPUT_PULLUP);
    attachInterrupt(BOOT_BTN_PIN, onButtonPress, FALLING);

    // Display
    display.begin();
    delay(200);

    // WiFi
    display.showScanStatus("WiFi: connecting...");
    bool wifiOk = connectWiFi();
    if (!wifiOk) startFallbackAp();   // fixable from a browser at 192.168.4.1/config

    if (wifiOk && appCfg.mqtt_broker.length() > 0) {
        display.showScanStatus("WiFi: connected. Starting MQTT...");
        delay(500);
        mqttMgr.begin();
        mqttStarted = true;
    } else if (wifiOk) {
        Serial.println("[MQTT] No broker configured - MQTT disabled "
                        "(set it at /config).");
    }

    // Web server: OTA update page + config page, on one server (port 80).
    // Always started - even in setup-AP mode - so anything can be fixed or
    // updated from a browser without ever needing USB again.
    otaMgr.begin();                      // http://<this-ip>/update
    configSrv.begin(otaMgr.server());     // http://<this-ip>/config

    if (wifiOk) startMdns();

    // WiFi-only until the rest of the config exists. Phone-first-boot only
    // sets WiFi; BLE/MQTT/watchdog would reboot while waiting for the
    // computer to finish /config. Stay online and serve /config + /update.
    bool wifiOnly = wifiOk &&
                    appCfg.mqtt_broker.length() == 0 &&
                    appCfg.bms1_mac.length() == 0 &&
                    appCfg.bms2_mac.length() == 0;

    if (g_apFallbackActive || wifiOnly) {
        if (wifiOnly) {
            String ipMsg = "WiFi OK  finish config at http://";
            ipMsg += WiFi.localIP().toString();
            ipMsg += "/config";
            display.showScanStatus(ipMsg.c_str());
            Serial.printf("[INIT] WiFi-only. Open http://%s/config or http://%s.local/config\n",
                          WiFi.localIP().toString().c_str(),
                          appCfg.hostname.c_str());
        } else {
            display.showScanStatus("SETUP AP  join litime-setup  192.168.4.1/config");
            Serial.println("[INIT] Setup-AP mode - BLE/MQTT skipped. "
                           "Open http://192.168.4.1/config");
        }
        lastWifiOkMs = lastBms1OkMs = lastBms2OkMs = lastMpptOkMs = millis();
        return;
    }

    // BLE – connect to Battery 1
    if (appCfg.bms1_mac.length() > 0) {
        connectBMS(bms1, bat1, appCfg.bms1_mac.c_str(), appCfg.bms1_name.c_str());
    } else {
        Serial.println("[BLE] Battery 1: no MAC configured - disabled.");
        display.showScanStatus("BMS1: no MAC (see /config)");
    }

    // Give the BLE stack time to fully settle before opening the second connection
    delay(3000);

    // BLE – connect to Battery 2
    if (appCfg.bms2_mac.length() > 0) {
        connectBMS(bms2, bat2, appCfg.bms2_mac.c_str(), appCfg.bms2_name.c_str());
    } else {
        Serial.println("[BLE] Battery 2: no MAC configured - disabled.");
        display.showScanStatus("BMS2: no MAC (see /config)");
    }

    // Initial data fetch – give a moment for BLE notify data to arrive
    delay(2500);
    bms1.update();
    bms2.update();
    delay(1500);
    copyBmsData(bms1, bat1);
    copyBmsData(bms2, bat2);

    // Victron MPPT passive BLE scan (must be after BLE is initialised by bms1/bms2)
    victronMppt.begin();

    // Draw first page
    display.showPage(PAGE_OVERVIEW, bat1, bat2, mpptData);

    lastBmsUpdateMs  = millis();
    lastDisplayRefresh= millis();
    lastMqttPublish  = millis();
    lastPageCycle    = millis();

    // Initialize component watchdog timestamps - all components are "OK" at
    // startup (even if a BLE connect failed, we don't want to immediately
    // trigger a reboot; give the reconnect logic time to work).
    {
        unsigned long now = millis();
        lastWifiOkMs = now;
        lastBms1OkMs = now;
        lastBms2OkMs = now;
        lastMpptOkMs = now;
    }

    Serial.println("[INIT] Setup complete.");
}

// ---------------------------------------------------------------------------
//  loop()
// ---------------------------------------------------------------------------
void loop() {
    esp_task_wdt_reset();

    unsigned long now = millis();

    // ------------------------------------------------------------------
    // 1. Reconnect WiFi / MQTT if needed
    // ------------------------------------------------------------------
    if (WiFi.status() != WL_CONNECTED) {
        if (!g_apFallbackActive && appCfg.wifi_ssid.length() > 0)
            WiFi.reconnect();
        delay(50);
    } else if (!g_apFallbackActive) {
        if (!mqttStarted && appCfg.mqtt_broker.length() > 0) {
            mqttMgr.begin();
            mqttStarted = true;
        }
        if (mqttStarted) mqttMgr.loop();
    }

    if (g_apFallbackActive) {
        // Give the phone a few seconds to finish joining before the web
        // server answers anything. Instant captive-portal probes were
        // panicking the ESP (InstrFetchProhibited) on AP connect.
        static bool webReady = false;
        static unsigned long apUpMs = 0;
        if (apUpMs == 0) apUpMs = now;
        if (!webReady && (now - apUpMs) > 8000) webReady = true;
        if (webReady) otaMgr.handle();
        if (configSrv.consumeRebootRequest()) {
            Serial.println("[CFG] Settings saved - restarting to apply.");
            delay(800);
            ESP.restart();
        }
        return;
    }

    // WiFi is up but the rest of config isn't: keep /config reachable,
    // don't hunt BLE, don't watchdog-reboot.
    if (appCfg.mqtt_broker.length() == 0 &&
        appCfg.bms1_mac.length() == 0 &&
        appCfg.bms2_mac.length() == 0) {
        otaMgr.handle();
        if (configSrv.consumeRebootRequest()) {
            Serial.println("[CFG] Settings saved - restarting to apply.");
            delay(800);
            ESP.restart();
        }
        return;
    }

    // ------------------------------------------------------------------
    // 1b. OTA + config web server (http://<ip>/update and /config)
    // ------------------------------------------------------------------
    otaMgr.handle();

    if (otaMgr.consumeActivityFlag()) {
        // A (failed) OTA upload just finished — server.handleClient() blocks
        // the main loop for the whole upload, so reset the component
        // watchdog timestamps to "now" instead of tripping an immediate
        // spurious reboot. (Successful uploads reboot inside ElegantOTA
        // before ever reaching here.)
        lastWifiOkMs = lastBms1OkMs = lastBms2OkMs = lastMpptOkMs = now;
    }

    if (configSrv.consumeRebootRequest()) {
        Serial.println("[CFG] Settings saved - restarting to apply.");
        if (mqttStarted) mqttMgr.prepareForReboot();
        delay(400);   // let the HTTP response flush
        ESP.restart();
    }

    // ------------------------------------------------------------------
    // 1c. MQTT reboot command (litime/cmd = "REBOOT")
    // ------------------------------------------------------------------
    if (mqttMgr.consumeRebootRequest()) {
        Serial.println("[MQTT] REBOOT requested - restarting.");
        mqttMgr.prepareForReboot();   // clean DISCONNECT: no LWT, status stays "rebooting"
        delay(300);                   // flush serial + let the DISCONNECT go out
        ESP.restart();
    }

    // ------------------------------------------------------------------
    // 2. Poll BMS (alternating between the two each interval)
    // ------------------------------------------------------------------
    if (now - lastBmsUpdateMs >= appCfg.bms_update_ms) {
        lastBmsUpdateMs = now;

        if (bmsRound == 0) {
            // Battery 1
            if (appCfg.bms1_mac.length() > 0) {
                reconnectBmsIfNeeded(0, bms1, bat1, appCfg.bms1_mac.c_str(), appCfg.bms1_name.c_str());
                if (bms1.isConnected()) {
                    bms1.update();
                    delay(400);  // allow notification to arrive
                    copyBmsData(bms1, bat1);
                }
            }
        } else {
            // Battery 2
            if (appCfg.bms2_mac.length() > 0) {
                reconnectBmsIfNeeded(1, bms2, bat2, appCfg.bms2_mac.c_str(), appCfg.bms2_name.c_str());
                if (bms2.isConnected()) {
                    bms2.update();
                    delay(400);
                    copyBmsData(bms2, bat2);
                }
            }
        }
        bmsRound ^= 1;  // toggle 0/1
    }

    // ------------------------------------------------------------------
    // 3. Auto-cycle display pages  (Overview=15 s, others=10 s)
    // ------------------------------------------------------------------
    {
        uint8_t curP = display.currentPage();
        unsigned long dwell = (curP == PAGE_OVERVIEW)
                              ? appCfg.screen_overview_s * 1000UL
                              : appCfg.screen_other_s    * 1000UL;
        if (now - lastPageCycle >= dwell) {
            lastPageCycle = now;
            display.nextPage(bat1, bat2, mpptData);
            lastDisplayRefresh = now;
        }
    }

    // ------------------------------------------------------------------
    // 4. Button press – manual page advance
    // ------------------------------------------------------------------
    if (btnPressed) {
        btnPressed = false;
        delay(50);  // debounce
        if (digitalRead(BOOT_BTN_PIN) == LOW) {
            display.nextPage(bat1, bat2, mpptData);
            lastPageCycle    = now;
            lastDisplayRefresh = now;
        }
    }

    // ------------------------------------------------------------------
    // 5. Refresh current page if new BMS data arrived
    //    (only refresh if we're not about to cycle)
    // ------------------------------------------------------------------
    unsigned long dataAge1 = (bat1.lastUpdateMs > 0) ? now - bat1.lastUpdateMs : 99999UL;
    unsigned long dataAge2 = (bat2.lastUpdateMs > 0) ? now - bat2.lastUpdateMs : 99999UL;
    bool freshData = (dataAge1 < (appCfg.bms_update_ms + 600UL)) ||
                     (dataAge2 < (appCfg.bms_update_ms + 600UL));

    if (freshData && (now - lastDisplayRefresh >= appCfg.bms_update_ms)) {
        lastDisplayRefresh = now;
        display.showPage(display.currentPage(), bat1, bat2, mpptData);
    }

    // ------------------------------------------------------------------
    // 6. Publish to MQTT
    // ------------------------------------------------------------------
    if (now - lastMqttPublish >= appCfg.mqtt_publish_ms) {
        lastMqttPublish = now;
        if (mqttMgr.isConnected()) {
            mqttMgr.publishAll(bat1, bat2);
            mqttMgr.publishVictron(mpptData);
            Serial.println("[MQTT] Published.");
        }
    }

    // ------------------------------------------------------------------
    // 7. Victron MPPT active BLE scan tick
    // ------------------------------------------------------------------
    if (victronMppt.tick()) {
        mpptData = victronMppt.data();
    }

    // ------------------------------------------------------------------
    // 8. Component disconnect watchdog
    //    Disabled while the setup AP is up (that's a deliberate "WiFi
    //    failed" state, not a hang). Otherwise if ANY component has been
    //    disconnected longer than component_timeout_ms, reboot.
    // ------------------------------------------------------------------
    if (g_apFallbackActive) {
        lastWifiOkMs = lastBms1OkMs = lastBms2OkMs = lastMpptOkMs = now;
    } else {
        if (WiFi.status() == WL_CONNECTED) lastWifiOkMs = now;
        if (appCfg.bms1_mac.length() == 0 || bms1.isConnected()) lastBms1OkMs = now;
        if (appCfg.bms2_mac.length() == 0 || bms2.isConnected()) lastBms2OkMs = now;
        if (appCfg.victron_mppt_address.length() == 0 ||
            (mpptData.valid && (now - mpptData.last_update_ms < 60000UL))) lastMpptOkMs = now;
    }

    unsigned long wifiDown = now - lastWifiOkMs;
    unsigned long b1Down   = now - lastBms1OkMs;
    unsigned long b2Down   = now - lastBms2OkMs;
    unsigned long mpptDown = now - lastMpptOkMs;

    if (wifiDown > appCfg.component_timeout_ms ||
        b1Down   > appCfg.component_timeout_ms ||
        b2Down   > appCfg.component_timeout_ms ||
        mpptDown > appCfg.component_timeout_ms) {

        Serial.println("[WDT] Component disconnect timeout! Forcing reboot:");
        Serial.printf("  WiFi down: %lus  B1 down: %lus  B2 down: %lus  MPPT down: %lus\n",
                      wifiDown / 1000UL, b1Down / 1000UL,
                      b2Down / 1000UL, mpptDown / 1000UL);
        Serial.printf("  Threshold: %lus\n", appCfg.component_timeout_ms / 1000UL);

        // Give the serial output time to flush before rebooting
        delay(200);
        ESP.restart();
    }

    // ------------------------------------------------------------------
    // 9. Debug dump every 30 s
    // ------------------------------------------------------------------
    static unsigned long lastDebug = 0;
    if (now - lastDebug >= 30000) {
        lastDebug = now;
        Serial.printf("\n--- Status @ %lus ---\n", now / 1000);
        Serial.printf("B1: connected=%d  SOC=%d%%  V=%.2fV  I=%.2fA  Tcell=%d°C\n",
                      bat1.connected, bat1.soc, bat1.totalVoltage,
                      bat1.current, bat1.cellTemp);
        Serial.printf("B2: connected=%d  SOC=%d%%  V=%.2fV  I=%.2fA  Tcell=%d°C\n",
                      bat2.connected, bat2.soc, bat2.totalVoltage,
                      bat2.current, bat2.cellTemp);
        Serial.printf("WiFi: %s  MQTT: %s\n",
                      WiFi.status() == WL_CONNECTED ? "OK" : "FAIL",
                      mqttMgr.isConnected() ? "OK" : "FAIL");
        if (bat1.connected && !bat1.cellVoltages.empty()) {
            Serial.print("B1 Cells: ");
            for (size_t i = 0; i < bat1.cellVoltages.size(); i++)
                Serial.printf("%.3f ", bat1.cellVoltages[i]);
            Serial.println();
        }
        if (bat2.connected && !bat2.cellVoltages.empty()) {
            Serial.print("B2 Cells: ");
            for (size_t i = 0; i < bat2.cellVoltages.size(); i++)
                Serial.printf("%.3f ", bat2.cellVoltages[i]);
            Serial.println();
        }
    }
}
