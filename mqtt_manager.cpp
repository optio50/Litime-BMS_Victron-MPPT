// =============================================================================
//  mqtt_manager.cpp  –  MQTT publishing implementation
// =============================================================================
#include "mqtt_manager.h"
#include "LiTime_BMS_Display.h"  // for BatteryData
#include "victron_mppt.h"        // for VictronData + victronStateStr/victronErrorStr + VictronScanResult

MQTTManager::MQTTManager()
    : _mqtt(_wifiClient) {}

// ---------------------------------------------------------------------------
void MQTTManager::begin() {
    _mqtt.setServer(MQTT_BROKER, MQTT_PORT);
    _mqtt.setBufferSize(1024);
    _reconnect();
}

// ---------------------------------------------------------------------------
bool MQTTManager::loop() {
    if (!_mqtt.connected()) {
        _reconnect();
    }
    _mqtt.loop();
    return _mqtt.connected();
}

// ---------------------------------------------------------------------------
bool MQTTManager::isConnected() {
    return _mqtt.connected();
}

// ---------------------------------------------------------------------------
bool MQTTManager::_reconnect() {
    if (_mqtt.connected()) return true;
    Serial.print("[MQTT] Connecting to ");
    Serial.println(MQTT_BROKER);

    const char* user = (strlen(MQTT_USER) > 0) ? MQTT_USER : nullptr;
    const char* pass = (strlen(MQTT_PASS) > 0) ? MQTT_PASS : nullptr;

    char lwtTopic[64];
    snprintf(lwtTopic, sizeof(lwtTopic), "%s/status", MQTT_TOPIC_BASE);

    bool ok = _mqtt.connect(MQTT_CLIENT_ID, user, pass,
                            lwtTopic, 1, true, "offline");
    if (ok) {
        Serial.println("[MQTT] Connected.");
        _mqtt.publish(lwtTopic, "online", true);
    } else {
        Serial.printf("[MQTT] Failed, rc=%d\n", _mqtt.state());
    }
    return ok;
}

// ---------------------------------------------------------------------------
void MQTTManager::publishAll(const BatteryData& b1, const BatteryData& b2) {
    if (!_mqtt.connected()) return;

    char prefix[48];
    snprintf(prefix, sizeof(prefix), "%s/battery1", MQTT_TOPIC_BASE);
    _publishBattery(b1, prefix, 0);

    snprintf(prefix, sizeof(prefix), "%s/battery2", MQTT_TOPIC_BASE);
    _publishBattery(b2, prefix, 1);

    _publishCombined(b1, b2);
}

// ---------------------------------------------------------------------------
void MQTTManager::_publishBattery(const BatteryData& b, const char* prefix, uint8_t idx) {
    // --- Main JSON payload -------------------------------------------------
    JsonDocument doc;
    doc["connected"]       = b.connected;
    doc["ble_retry_paused"]   = b.bleRetryPaused;
    doc["ble_retry_attempts"] = b.bleRetryAttempts;
    doc["soc"]             = b.soc;
    doc["soh"]             = b.soh.c_str();
    doc["total_voltage"]   = serialized(String(b.totalVoltage, 3));
    doc["cell_voltage_sum"]= serialized(String(b.cellVoltageSum, 3));
    doc["current"]         = serialized(String(b.current, 3));
    doc["power"]           = serialized(String(b.totalVoltage * b.current, 1));
    doc["cell_temp"]       = b.cellTemp;
    doc["mosfet_temp"]     = b.mosfetTemp;
    doc["remaining_ah"]    = serialized(String(b.remainingAh, 2));
    doc["full_capacity_ah"]= serialized(String(b.fullCapacityAh, 2));
    doc["discharge_cycles"]= b.dischargesCount;
    doc["protection"]      = b.protectionState.c_str();
    doc["balancing"]       = b.balancingState.c_str();
    doc["battery_state"]   = b.batteryState.c_str();
    doc["heat_state"]      = b.heatState.c_str();

    // Estimated time remaining (seconds) with correct charging/discharging direction
    // Charging    → time to FULL    = (fullAh - remainAh) / I
    // Discharging → time to RESERVE = (remainAh - reserveAh) / |I|
    //   reserveAh = SOC_RESERVE_PCT% of fullCapacityAh (see config.h) — not
    //   literal 0%, since LFP packs shouldn't routinely be run past that point.
    // Idle        → 0  (no meaningful estimate)
    float pwr       = b.totalVoltage * b.current;
    float absI      = fabs(b.current);
    float reserveAh = b.fullCapacityAh * (SOC_RESERVE_PCT / 100.0f);
    uint32_t timeRemSec = 0;
    const char* timeDir = "idle";
    if (absI > 0.1f) {
        if (pwr > 1.0f) {
            // Charging: time to full
            float toFill = b.fullCapacityAh - b.remainingAh;
            if (toFill > 0.0f)
                timeRemSec = (uint32_t)((toFill / absI) * 3600.0f);
            timeDir = "to_full";
        } else if (pwr < -1.0f) {
            // Discharging: time to reserve (not absolute empty)
            float usableAh = b.remainingAh - reserveAh;
            if (usableAh > 0.0f)
                timeRemSec = (uint32_t)((usableAh / absI) * 3600.0f);
            timeDir = "to_empty";
        }
    }
    doc["time_remaining_s"]  = timeRemSec;
    doc["time_direction"]    = timeDir;  // "to_full" | "to_empty" | "idle"

    // Cell voltages array
    JsonArray cells = doc["cell_voltages"].to<JsonArray>();
    for (float v : b.cellVoltages) {
        cells.add(serialized(String(v, 3)));
    }

    // Cell min/max/delta
    if (!b.cellVoltages.empty()) {
        float mn = *std::min_element(b.cellVoltages.begin(), b.cellVoltages.end());
        float mx = *std::max_element(b.cellVoltages.begin(), b.cellVoltages.end());
        doc["cell_min_v"]   = serialized(String(mn, 3));
        doc["cell_max_v"]   = serialized(String(mx, 3));
        doc["cell_delta_mv"]= serialized(String((mx - mn) * 1000.0f, 1));
    }

    char payload[800];
    size_t len = serializeJson(doc, payload, sizeof(payload));

    char topic[80];
    snprintf(topic, sizeof(topic), "%s/state", prefix);
    _mqtt.publish(topic, payload, false);

    // --- Individual cell topics (for graphing) ----------------------------
    char cellTopic[96];
    for (size_t i = 0; i < b.cellVoltages.size(); i++) {
        snprintf(cellTopic, sizeof(cellTopic), "%s/cells/cell%02d", prefix, (int)i + 1);
        char vbuf[12];
        dtostrf(b.cellVoltages[i], 5, 3, vbuf);
        _mqtt.publish(cellTopic, vbuf, false);
    }

    // --- Flat convenience topics ------------------------------------------
    char val[16];

    snprintf(topic, sizeof(topic), "%s/soc", prefix);
    snprintf(val, sizeof(val), "%d", b.soc);
    _mqtt.publish(topic, val, false);

    snprintf(topic, sizeof(topic), "%s/voltage", prefix);
    dtostrf(b.totalVoltage, 6, 3, val);
    _mqtt.publish(topic, val, false);

    snprintf(topic, sizeof(topic), "%s/current", prefix);
    dtostrf(b.current, 7, 3, val);
    _mqtt.publish(topic, val, false);

    snprintf(topic, sizeof(topic), "%s/power", prefix);
    dtostrf(b.totalVoltage * b.current, 7, 1, val);
    _mqtt.publish(topic, val, false);
}

// ---------------------------------------------------------------------------
void MQTTManager::_publishCombined(const BatteryData& b1, const BatteryData& b2) {
    float totalI     = b1.current + b2.current;
    float totalP     = (b1.totalVoltage * b1.current) + (b2.totalVoltage * b2.current);
    float totalRemAh = b1.remainingAh + b2.remainingAh;
    float totalCapAh = b1.fullCapacityAh + b2.fullCapacityAh;
    float avgSOC     = (b1.soc + b2.soc) / 2.0f;
    float absI       = fabs(totalI);

    // Time remaining with correct direction (discharge target = reserve, see
    // per-battery notes above; SOC_RESERVE_PCT applied to the combined pack)
    float reserveAh = totalCapAh * (SOC_RESERVE_PCT / 100.0f);
    uint32_t timeRem = 0;
    const char* timeDir = "idle";
    if (absI > 0.2f) {
        if (totalP > 1.0f) {
            float toFill = totalCapAh - totalRemAh;
            if (toFill > 0.0f)
                timeRem = (uint32_t)((toFill / absI) * 3600.0f);
            timeDir = "to_full";
        } else if (totalP < -1.0f) {
            float usableAh = totalRemAh - reserveAh;
            if (usableAh > 0.0f)
                timeRem = (uint32_t)((usableAh / absI) * 3600.0f);
            timeDir = "to_empty";
        }
    }

    JsonDocument doc;
    doc["soc_avg"]          = serialized(String(avgSOC, 1));
    doc["soc_b1"]           = b1.soc;
    doc["soc_b2"]           = b2.soc;
    doc["total_current"]    = serialized(String(totalI, 3));
    doc["total_power"]      = serialized(String(totalP, 1));
    doc["total_remaining_ah"] = serialized(String(totalRemAh, 2));
    doc["total_capacity_ah"]  = serialized(String(totalCapAh, 2));
    doc["time_remaining_s"]   = timeRem;
    doc["time_direction"]     = timeDir;  // "to_full" | "to_empty" | "idle"
    const char* flow = (totalP > 1.0f) ? "charging" :
                       (totalP < -1.0f) ? "discharging" : "idle";
    doc["flow"] = flow;

    char payload[512];
    serializeJson(doc, payload, sizeof(payload));

    char topic[64];
    snprintf(topic, sizeof(topic), "%s/combined/state", MQTT_TOPIC_BASE);
    _mqtt.publish(topic, payload, false);
}

// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Publish Victron MPPT solar controller data
// ---------------------------------------------------------------------------
void MQTTManager::publishVictron(const VictronData& v) {
    if (!_mqtt.connected()) return;

    char topic[64];
    char val[32];

    // Full JSON payload
    JsonDocument doc;
    doc["valid"]       = v.valid;
    doc["state"]       = v.device_state;
    doc["state_str"]   = victronStateStr(v.device_state);
    doc["error"]       = v.charger_error;
    doc["error_str"]   = victronErrorStr(v.charger_error);
    doc["batt_v"]      = serialized(String(v.batt_v,  2));
    doc["batt_a"]      = serialized(String(v.batt_a,  2));
    doc["pv_w"]        = serialized(String(v.pv_w,    1));
    doc["yield_today"] = serialized(String(v.yield_today, 3));  // kWh
    // Age in seconds (0 if never received)
    doc["last_seen_s"] = v.valid ? (uint32_t)((millis() - v.last_update_ms) / 1000UL) : 9999;

    char payload[512];
    serializeJson(doc, payload, sizeof(payload));
    snprintf(topic, sizeof(topic), "%s/state", MQTT_VICTRON_TOPIC_BASE);
    _mqtt.publish(topic, payload, false);

    // Flat convenience topics
    if (v.valid) {
        snprintf(val, sizeof(val), "%s", victronStateStr(v.device_state));
        snprintf(topic, sizeof(topic), "%s/state_str", MQTT_VICTRON_TOPIC_BASE);
        _mqtt.publish(topic, val, false);

        dtostrf(v.batt_v, 5, 2, val);
        snprintf(topic, sizeof(topic), "%s/batt_v", MQTT_VICTRON_TOPIC_BASE);
        _mqtt.publish(topic, val, false);

        dtostrf(v.batt_a, 6, 2, val);
        snprintf(topic, sizeof(topic), "%s/batt_a", MQTT_VICTRON_TOPIC_BASE);
        _mqtt.publish(topic, val, false);

        dtostrf(v.pv_w, 6, 1, val);
        snprintf(topic, sizeof(topic), "%s/pv_w", MQTT_VICTRON_TOPIC_BASE);
        _mqtt.publish(topic, val, false);

        dtostrf(v.yield_today, 7, 3, val);
        snprintf(topic, sizeof(topic), "%s/yield_today", MQTT_VICTRON_TOPIC_BASE);
        _mqtt.publish(topic, val, false);
    }
}

// ---------------------------------------------------------------------------
// Home Assistant integration
//
// This project does NOT publish MQTT Discovery from the firmware. All Home
// Assistant entities come from a single static YAML package instead — see
// homeassistant/mqtt_sensors.yaml and the README's "Home Assistant
// Integration" section. Firmware-side discovery was tried and removed: it
// only ever covered a subset of fields, had a device-merging bug, and once
// fixed would have duplicated entities already provided by the YAML package.
// Having one static, complete source of truth is simpler and avoids stale/
// duplicate retained discovery topics living on the broker indefinitely.
//
// If your broker still has old discovery topics retained from a firmware
// version prior to this change, see tools/clear_ha_discovery.py to remove
// them (one-time, run from your PC — not something the firmware should do
// on every boot/reconnect, since retained-message cleanup only needs to
// happen once, ever).
// ---------------------------------------------------------------------------
