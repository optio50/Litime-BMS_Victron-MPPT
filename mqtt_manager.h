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

    // Publish Home-Assistant MQTT discovery messages (call once after connect)
    void publishHADiscovery(const BatteryData& b1, const BatteryData& b2);

private:
    WiFiClient   _wifiClient;
    PubSubClient _mqtt;
    bool         _haDiscoverySent;

    bool _reconnect();
    void _publishBattery(const BatteryData& b, const char* topicPrefix, uint8_t idx);
    void _publishCombined(const BatteryData& b1, const BatteryData& b2);
    void _haConfigSensor(const char* topicBase, const char* name,
                         const char* valueKey, const char* unit,
                         const char* devClass);
};
