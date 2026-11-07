// =============================================================================
//  ota_manager.cpp  –  WiFi firmware update server (ElegantOTA)
// =============================================================================
#include "ota_manager.h"
#include <ElegantOTA.h>
#include <WiFi.h>
#include <esp_task_wdt.h>

#include "app_config.h"
#include "display_manager.h"   // ILI9341 status line during upload

// Defined in Litime-BMS_Victron-MPPT.ino
extern DisplayManager display;

// ---------------------------------------------------------------------------
void OtaManager::begin() {
    // HTTP auth for the upload page (empty ota_password in /config = none)
    if (appCfg.ota_password.length() > 0) {
        ElegantOTA.setAuth("admin", appCfg.ota_password.c_str());
    }

    // Flash to the inactive OTA partition, then reboot into it automatically
    // (ElegantOTA.loop() performs the deferred restart).
    ElegantOTA.setAutoReboot(true);

    ElegantOTA.onStart([]() {
        Serial.println("[OTA] Upload started.");
        esp_task_wdt_reset();   // the 60 s task watchdog must be fed by US now
        display.showScanStatus("OTA: receiving firmware...");
    });

    ElegantOTA.onProgress([](size_t current, size_t final) {
        // The synchronous web server streams the entire file inside
        // server.handleClient(), which blocks the main loop (and its
        // esp_task_wdt_reset) for the whole upload — a 2 MB image over WiFi
        // can easily exceed the 60 s WDT window, so feed it from here.
        esp_task_wdt_reset();

        // Throttled status update: redraw the LCD / serial line every 10 %
        static size_t lastStep = 1000;
        size_t step = (final > 0) ? (current * 10UL / final) : 0;
        if (step != lastStep) {
            lastStep = step;
            char msg[48];
            snprintf(msg, sizeof(msg), "OTA: %u%%  (%u / %u KB)",
                     (unsigned)((final > 0) ? (current * 100UL / final) : 0),
                     (unsigned)(current / 1024), (unsigned)(final / 1024));
            Serial.printf("[OTA] %s\n", msg);
            display.showScanStatus(msg);
        }
    });

    ElegantOTA.onEnd([this](bool success) {
        // Signal the main loop to reset the component-disconnect watchdog
        // timestamps: the blocked loop would otherwise compute >120 s of
        // "everything disconnected" and force a redundant reboot right
        // after a FAILED upload (a successful one reboots first anyway).
        _otaActivity = true;

        if (success) {
            Serial.println("[OTA] Upload OK - rebooting into new firmware.");
            display.showScanStatus("OTA: done! rebooting...");
        } else {
            Serial.println("[OTA] Upload FAILED - keeping current firmware.");
            display.showScanStatus("OTA: FAILED - kept old firmware");
        }
    });

    ElegantOTA.begin(&_server);   // registers the /update route + its assets
    _server.begin();

    Serial.printf("[OTA] Update page ready at http://%s/update\n",
                  WiFi.localIP().toString().c_str());
}

// ---------------------------------------------------------------------------
void OtaManager::handle() {
    _server.handleClient();
    ElegantOTA.loop();            // processes the deferred auto-reboot
}

// ---------------------------------------------------------------------------
bool OtaManager::consumeActivityFlag() {
    bool f = _otaActivity;
    _otaActivity = false;
    return f;
}
