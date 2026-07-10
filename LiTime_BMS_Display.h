#pragma once
// =============================================================================
//  LiTime_BMS_Display.h  –  Shared data structures
// =============================================================================
#include <Arduino.h>
#include <vector>
#include <algorithm>

// Mirror of BMSClient::BMSData plus connectivity flag + computed fields
struct BatteryData {
    bool     connected       = false;
    uint8_t  bleRetryAttempts = 0;     // consecutive failed reconnect attempts in current round
    bool     bleRetryPaused   = false; // true while waiting out the reconnect cooldown
    float    totalVoltage    = 0.0f;
    float    cellVoltageSum  = 0.0f;
    float    current         = 0.0f;
    int16_t  mosfetTemp      = 0;
    int16_t  cellTemp        = 0;
    uint8_t  soc             = 0;
    String   soh             = "0%";
    float    remainingAh     = 0.0f;
    float    fullCapacityAh  = 0.0f;
    String   protectionState = "0x00000000";
    // Raw 4-byte register at BLE notification offset 68. The BMSClient library
    // named this "heatState", but LiTime 48V 100Ah packs have no heater — the
    // value is a non-zero status/counter register, not a heater on/off flag.
    // Kept as a raw diagnostic hex string under a neutral name.
    String   statusReg68     = "0x00000000";
    String   balanceMemory   = "0x00000000";
    String   failureState    = "0x000000";
    String   balancingState  = "0000000000000000";
    String   batteryState    = "0x0000";
    uint32_t dischargesCount = 0;
    float    dischargesAhCount = 0.0f;
    std::vector<float> cellVoltages;
    unsigned long lastUpdateMs = 0;  // millis() when last successfully updated
};

// =============================================================================
//  VictronData – decoded MPPT solar controller BLE advertising data
// =============================================================================
struct VictronData {
    bool    valid         = false;
    uint8_t device_state  = 0;
    uint8_t charger_error = 0;
    float   batt_v        = 0.0f;
    float   batt_a        = 0.0f;
    float   yield_today   = 0.0f;
    float   pv_w          = 0.0f;
    unsigned long last_update_ms = 0;
    // ── Debug fields (always populated, even when decryption fails) ──────────
    // raw_hex  : hex dump of the full 26-byte BLE manufacturer data packet
    // dec_hex  : hex dump of the 16 decrypted bytes (or "DECRYPT_FAILED")
    // These appear in victron/state JSON so you can debug remotely via MQTT.
    char raw_hex[82] = "";   // 26 bytes * 3 chars ("XX ") + null
    char dec_hex[50] = "";   // 16 bytes * 3 chars + null
};

// =============================================================================
//  VictronScanResult – one BLE advertisement from any Victron company-ID device
//  Stored by VictronAdvCallback and published to victron/scan for remote debug
// =============================================================================
#define VICTRON_SCAN_MAX 4
struct VictronScanResult {
    char    mac[20]     = "";   // e.g. "ea:f3:a2:dc:a1:ec"
    uint8_t raw[26]     = {0};
    uint8_t rawLen      = 0;
    char    raw_hex[82] = "";   // printable hex dump
};
