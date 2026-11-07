#pragma once
// =============================================================================
//  config_server.h  –  Browser-based settings editor (config.ini on flash)
// =============================================================================
//  Serves the /config web page on the SAME WebServer ElegantOTA uses (port 80)
//  so the whole device UI lives on one server:
//
//    http://<hostname>.local/config        Settings form (edit + Save & Reboot)
//    http://<hostname>.local/config.ini    Current settings as INI text
//    http://<hostname>.local/config.json   Settings as JSON (shared_config.py)
//    http://<hostname>.local/config/wifi-scan   Nearby WiFi networks (JSON)
//    http://<hostname>.local/config/wifi-test   Try new WiFi creds (no save)
//    http://<hostname>.local/config/save   POST target of the settings form
//    http://<hostname>.local/config/import POST target of the raw-INI import
//
//  Also reachable via the "litime-setup" fallback AP (192.168.4.1/config)
//  whenever the device can't connect to WiFi, so credentials can always be
//  fixed from any browser — never USB, never a recompile.
//
//  Protected by the same Basic auth as /update when an ota_password is set
//  (username "admin"; empty password = no auth).
// =============================================================================
#include <WebServer.h>

class ConfigServer {
public:
    // Register all routes on the given (already-running) WebServer.
    void begin(WebServer& server);

    // Returns true exactly once after settings were saved and the device
    // should reboot to apply them (the main sketch consumes this, lets the
    // HTTP response flush, then restarts).
    bool consumeRebootRequest();

private:
    WebServer* _srv = nullptr;
    bool _rebootRequested = false;

    bool _authOk();                    // Basic auth "admin"/ota_password
    String _pageHead();               // <head> + CSS + top of the form page
    String _fieldRow(const char* key, const char* label, const char* desc,
                     const String& value, bool numeric, bool secret);
    String _pageTail();               // scan/test/import + JS + </html>
    String _resultPage(const char* title, const char* msg, bool rebooting);

    void _handlePage();
    void _handleSave();
    void _handleImport();
    void _handleIni();
    void _handleJson();
    void _handleWifiScan();
    void _handleWifiTest();
    void _handleNotFound();           // captive-portal redirect while in setup AP
};
