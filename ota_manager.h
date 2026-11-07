#pragma once
// =============================================================================
//  ota_manager.h  –  WiFi firmware update server (ElegantOTA)
// =============================================================================
//  Serves an upload page at http://<esp-ip>/update where a new firmware .bin
//  can be uploaded from any browser (or the dashboard's "ESP Update" tab).
//  The image is flashed to the inactive OTA partition, verified, and the
//  device reboots into it automatically — a failed/interrupted upload can
//  never brick the device (the old image stays in the active partition).
//
//  The web server runs on the built-in SYNCHRONOUS WebServer (port 80) —
//  deliberately NOT ESPAsyncWebServer — to keep the heap footprint small in
//  this already BLE-heavy sketch.
//
//  NOTE: this requires an OTA-capable partition scheme (two app partitions).
//  For the XIAO ESP32-S3's 8 MB flash use PartitionScheme "default_8MB".
// =============================================================================
#include <WebServer.h>

class OtaManager {
public:
    // Start the web server + ElegantOTA (call once, after WiFi connects).
    void begin();

    // Service the web server + OTA machinery; call every main loop iteration.
    void handle();

    // Returns true exactly once after an OTA session has ended (whether the
    // upload succeeded or failed). The main sketch consumes this to reset the
    // component-disconnect watchdog timestamps: an upload blocks the main
    // loop (the sync web server streams the file inside handle()), so on a
    // FAILED upload the loop would otherwise immediately trip the component
    // watchdog and force a (harmless but confusing) reboot.
    // (A SUCCESSFUL upload never gets here — ElegantOTA reboots first.)
    bool consumeActivityFlag();

    // The /update and /config pages share this server (port 80).
    WebServer& server() { return _server; }

private:
    WebServer _server{80};
    bool      _otaActivity = false;
};
