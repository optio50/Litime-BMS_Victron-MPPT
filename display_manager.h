#pragma once
// =============================================================================
//  display_manager.h  –  ILI9341 display helper for LiTime Dual BMS Monitor
// =============================================================================
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <SPI.h>
#include <algorithm>
#include "config.h"
#include "LiTime_BMS_Display.h"  // also provides VictronData struct

// Number of display pages
#define NUM_PAGES  5

// Page indices
#define PAGE_OVERVIEW  0
#define PAGE_BAT1      1
#define PAGE_BAT2      2
#define PAGE_CELLS     3
#define PAGE_MPPT      4

class DisplayManager {
public:
    DisplayManager();
    void begin();
    void showPage(uint8_t page, const BatteryData& b1, const BatteryData& b2,
                  const VictronData& v);
    void showBootScreen();
    void showScanStatus(const char* msg);
    void nextPage(const BatteryData& b1, const BatteryData& b2, const VictronData& v);

    uint8_t currentPage() const { return _page; }

private:
    SPIClass*         _spi;
    Adafruit_ILI9341* _tft;
    uint8_t           _page;
    uint8_t           _lastPage;  // 255 = unset; skip fillScreen on same-page refresh

    // --- low-level drawing helpers ------------------------------------------
    void _header(const char* title, uint8_t page, uint8_t total);
    void _footer(bool b1ok, bool b2ok, unsigned long uptimeSec);
    void _socBar(int16_t x, int16_t y, int16_t w, int16_t h, uint8_t soc,
                 uint16_t fillCol = COL_DARKGREEN, uint16_t bgCol = COL_HEADER);
    void _kv(int16_t x, int16_t y, const char* label, const char* value,
             uint16_t valCol = COL_GREEN, uint8_t textSize = 1);
    uint16_t _socColour(uint8_t soc);
    uint16_t _cellColour(float v, float avg);

    // --- page renderers -----------------------------------------------------
    void _pageOverview(const BatteryData& b1, const BatteryData& b2);
    void _pageBattery(const BatteryData& b, const BatteryData& other, uint8_t idx);
    void _pageCells(const BatteryData& b1, const BatteryData& b2);
    void _pageMppt(const VictronData& v, bool b1ok, bool b2ok);
};
