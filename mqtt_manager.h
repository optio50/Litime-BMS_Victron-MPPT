#pragma once
// =============================================================================
//  mqtt_manager.h  –  MQTT publishing for LiTime Dual BMS Monitor
// =============================================================================
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "config.h"
#include "LiTime_BMS_Display.h"  // for BatteryData + VictronData

class MQTTManager {
public:
    MQTTManager();
    void begin();
    bool loop();               // call every main loop iteration; returns true if connected
    bool isConnected();

    // Publish full update for both batteries + combined topic
    void publishAll(const BatteryData& b1, const BatteryData& b2);

    // Publish Victron MPPT decoded data to victron/state and flat topics
    void publishVictron(const VictronData& v);

    // Deprecated no-op (kept for API stability). Home Assistant entities are
    // now provided entirely by homeassistant/mqtt_sensors.yaml — see README.
    void publishHADiscovery(const BatteryData& b1, const BatteryData& b2);

private:
    WiFiClient   _wifiClient;
    PubSubClient _mqtt;
    bool         _haDiscoverySent;

    bool _reconnect();
    void _publishBattery(const BatteryData& b, const char* topicPrefix, uint8_t idx);
    void _publishCombined(const BatteryData& b1, const BatteryData& b2);
};
