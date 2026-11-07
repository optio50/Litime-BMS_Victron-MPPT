#pragma once
// =============================================================================
//  mqtt_manager.h  –  MQTT publishing for LiTime Dual BMS Monitor
// =============================================================================
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "LiTime_BMS_Display.h"  // for BatteryData + VictronData

class MQTTManager {
public:
    MQTTManager();
    void begin();
    bool loop();               // call every main loop iteration; returns true if connected
    bool isConnected();

    // True exactly once after a "REBOOT" command arrives on <base>/cmd —
    // the main sketch consumes it to do a clean disconnect + ESP.restart().
    bool consumeRebootRequest();

    // Clean MQTT disconnect before an ESP.restart(): sends an explicit
    // DISCONNECT so the broker does NOT publish the "offline" LWT — the
    // retained status then still reads "rebooting" (set by the callback)
    // until the fresh boot publishes "online" again.
    void prepareForReboot();

    // Publish full update for both batteries + combined topic
    void publishAll(const BatteryData& b1, const BatteryData& b2);

    // Publish Victron MPPT decoded data to victron/state and flat topics
    void publishVictron(const VictronData& v);

private:
    WiFiClient   _wifiClient;
    PubSubClient _mqtt;

    bool _rebootRequested = false;      // set from the /cmd subscription callback

    // PubSubClient only takes a plain function pointer for its callback, so a
    // static member + singleton pointer is used to route into the instance.
    static MQTTManager* s_instance;
    static void _onMessageStatic(char* topic, byte* payload, unsigned int length);
    void _onMessage(char* topic, byte* payload, unsigned int length);

    bool _reconnect();
    void _publishBattery(const BatteryData& b, const char* topicPrefix, uint8_t idx);
    void _publishCombined(const BatteryData& b1, const BatteryData& b2);
};
