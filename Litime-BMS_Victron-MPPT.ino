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
//
//  Board: "XIAO_ESP32S3"  (Seeed XIAO ESP32S3 in Arduino boards manager)
// =============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <esp_task_wdt.h>
#include <BMSClient.h>

#include "config.h"
#include "LiTime_BMS_Display.h"
#include "display_manager.h"
#include "mqtt_manager.h"
#include "victron_mppt.h"

// ---------------------------------------------------------------------------
//  Globals
// ---------------------------------------------------------------------------
BMSClient     bms1, bms2;
BatteryData   bat1, bat2;
DisplayManager display;
MQTTManager   mqttMgr;
VictronMPPT   victronMppt;
VictronData   mpptData;

// Timing
unsigned long lastBmsUpdateMs    = 0;
unsigned long lastDisplayRefresh = 0;
unsigned long lastMqttPublish    = 0;
unsigned long lastPageCycle      = 0;

// Which BMS to update next (alternating to avoid BLE contention)
uint8_t  bmsRound = 0;

// BLE reconnect backoff state (per battery)
uint8_t       bleReconnectAttempts[2] = {0, 0};
unsigned long bleReconnectCooldownUntil[2] = {0, 0};

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
    dst.heatState        = src.getHeatState();
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
//  Connect to WiFi with timeout
// ---------------------------------------------------------------------------
static bool connectWiFi() {
    Serial.printf("[WiFi] Connecting to %s\n", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start > 20000) {
            Serial.println("[WiFi] Timeout.");
            return false;
        }
        delay(500);
        Serial.print(".");
    }
    Serial.printf("\n[WiFi] Connected. IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
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
    while (!ok && (millis() - t < BLE_SCAN_TIMEOUT_MS)) {
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
//  Attempt a runtime BLE reconnect for one battery, with backoff.
//  Returns true if a connect() was attempted (used only for logging context).
// ---------------------------------------------------------------------------
static void reconnectBmsIfNeeded(uint8_t idx, BMSClient& bms, BatteryData& data,
                                  const char* name) {
    if (bms.isConnected()) return;

    data.connected = false;
    unsigned long now = millis();

    if (now < bleReconnectCooldownUntil[idx]) {
        // Still cooling down — skip this round entirely, no BLE traffic, no log spam.
        data.bleRetryPaused   = true;
        data.bleRetryAttempts = 0;
        return;
    }

    if (data.bleRetryPaused) {
        // Cooldown just expired — starting a fresh round of attempts.
        Serial.printf("[BLE] %s: cooldown elapsed, resuming reconnect attempts.\n", name);
        data.bleRetryPaused = false;
    }

    Serial.printf("[BLE] %s lost. Reconnecting... (attempt %u/%u)\n",
                  name, bleReconnectAttempts[idx] + 1, (unsigned)BLE_RECONNECT_MAX_ATTEMPTS);

    if (bms.connect()) {
        Serial.printf("[BLE] %s reconnected.\n", name);
        bleReconnectAttempts[idx]      = 0;
        bleReconnectCooldownUntil[idx] = 0;
        data.bleRetryAttempts = 0;
        data.bleRetryPaused   = false;
        return;
    }

    bleReconnectAttempts[idx]++;
    data.bleRetryAttempts = bleReconnectAttempts[idx];

    if (bleReconnectAttempts[idx] >= BLE_RECONNECT_MAX_ATTEMPTS) {
        Serial.printf("[BLE] %s: %u attempts failed. Pausing %lu s before retrying.\n",
                      name, (unsigned)BLE_RECONNECT_MAX_ATTEMPTS,
                      (unsigned long)(BLE_RECONNECT_COOLDOWN_MS / 1000UL));
        bleReconnectCooldownUntil[idx] = now + BLE_RECONNECT_COOLDOWN_MS;
        bleReconnectAttempts[idx]      = 0;
        data.bleRetryAttempts = 0;
        data.bleRetryPaused   = true;
    }
}

// ---------------------------------------------------------------------------
//  setup()
// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n=== LiTime Dual BMS Monitor ===");

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
    delay(800);

    // WiFi
    display.showScanStatus("WiFi: connecting...");
    bool wifiOk = connectWiFi();
    if (wifiOk) {
        display.showScanStatus("WiFi: connected. Starting MQTT...");
        delay(500);
        mqttMgr.begin();
    } else {
        display.showScanStatus("WiFi: FAILED. MQTT disabled.");
        delay(2000);
    }

    // BLE – connect to Battery 1
    connectBMS(bms1, bat1, BMS1_MAC, BMS1_NAME);

    // Give the BLE stack time to fully settle before opening the second connection
    delay(3000);

    // BLE – connect to Battery 2
    connectBMS(bms2, bat2, BMS2_MAC, BMS2_NAME);

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
        WiFi.reconnect();
        delay(500);
    } else {
        mqttMgr.loop();
    }

    // ------------------------------------------------------------------
    // 2. Poll BMS (alternating between the two each interval)
    // ------------------------------------------------------------------
    if (now - lastBmsUpdateMs >= BMS_UPDATE_MS) {
        lastBmsUpdateMs = now;

        if (bmsRound == 0) {
            // Battery 1
            reconnectBmsIfNeeded(0, bms1, bat1, BMS1_NAME);
            if (bms1.isConnected()) {
                bms1.update();
                delay(400);  // allow notification to arrive
                copyBmsData(bms1, bat1);
            }
        } else {
            // Battery 2
            reconnectBmsIfNeeded(1, bms2, bat2, BMS2_NAME);
            if (bms2.isConnected()) {
                bms2.update();
                delay(400);
                copyBmsData(bms2, bat2);
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
                              ? (unsigned long)SCREEN_OVERVIEW_S * 1000UL
                              : (unsigned long)SCREEN_OTHER_S    * 1000UL;
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
    bool freshData = (dataAge1 < (BMS_UPDATE_MS + 600UL)) ||
                     (dataAge2 < (BMS_UPDATE_MS + 600UL));

    if (freshData && (now - lastDisplayRefresh >= BMS_UPDATE_MS)) {
        lastDisplayRefresh = now;
        display.showPage(display.currentPage(), bat1, bat2, mpptData);
    }

    // ------------------------------------------------------------------
    // 6. Publish to MQTT
    // ------------------------------------------------------------------
    if (now - lastMqttPublish >= MQTT_PUBLISH_MS) {
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
    // 8. Debug dump every 30 s
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
