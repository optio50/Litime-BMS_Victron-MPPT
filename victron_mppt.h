#pragma once
// =============================================================================
//  victron_mppt.h  –  Passive BLE reader for Victron MPPT Solar Controller
//
//  Scans for ANY device with Victron company ID (0x02E1) and stores all found
//  devices.  The main loop can publish them to victron/scan/<mac> via MQTT so
//  you can see what's in range without needing a serial monitor.
//  Decryption is only attempted for the device matching VICTRON_MPPT_ADDRESS.
//
//  Required Arduino library (install via Library Manager):
//    "wolfssl" by wolfSSL Inc. (tested with v5.8.4)
//
//  Credits:
//    Victron Bluetooth Advertising Protocol (Feb 2023, Victron Community)
//    github.com/chrisj7903/Read-Victron-advertised-data  (MIT)
// =============================================================================
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEAdvertisedDevice.h>
#include "LiTime_BMS_Display.h"  // for VictronData struct

const char* victronStateStr(uint8_t state);
const char* victronErrorStr(uint8_t error);

// VictronScanResult and VICTRON_SCAN_MAX are defined in LiTime_BMS_Display.h

// ---------------------------------------------------------------------------
class VictronAdvCallback : public BLEAdvertisedDeviceCallbacks {
public:
    VictronAdvCallback() : _count(0) {}
    void onResult(BLEAdvertisedDevice advertisedDevice) override;
    void    reset()          { _count = 0; }
    uint8_t count()    const { return _count; }
    const VictronScanResult& result(uint8_t i) const { return _results[i]; }
private:
    VictronScanResult _results[VICTRON_SCAN_MAX];
    uint8_t           _count;
};

// ---------------------------------------------------------------------------
class VictronMPPT {
public:
    VictronMPPT() : _pScan(nullptr), _scanning(false), _scanStartMs(0) {}

    void begin();
    bool tick();  // call each loop(); returns true when data freshly decoded

    const VictronData&       data()      const { return _data; }
    uint8_t                  scanCount() const { return _cb.count(); }
    const VictronScanResult& scanResult(uint8_t i) const { return _cb.result(i); }

private:
    BLEScan*           _pScan;
    VictronAdvCallback _cb;
    VictronData        _data;
    bool               _scanning;
    unsigned long      _scanStartMs;

    static const unsigned long SCAN_DURATION_MS = 800;

    bool _decrypt(const uint8_t* raw, uint8_t rawLen, uint8_t output[16]);
    void _decode(const uint8_t output[16]);
};
