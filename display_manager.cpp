// =============================================================================
//  display_manager.cpp  –  ILI9341 display implementation
// =============================================================================
#include "display_manager.h"
#include "LiTime_BMS_Display.h"   // for BatteryData struct
#include "victron_mppt.h"         // for victronStateStr(), victronErrorStr()

// ===========================================================================
//  Bit-decode helpers for human-readable protection, balance, and state
// ===========================================================================
struct ProtBit { uint8_t bit; const char* abbr; };
static const ProtBit PROT_BITS[] = {
    {0,  "CellOV"}, {1,  "CellUV"}, {2,  "PackOV"}, {3,  "PackUV"},
    {4,  "ChgOT"},  {5,  "ChgUT"},  {6,  "DsgOT"},  {7,  "DsgUT"},
    {8,  "ChgOC"},  {9,  "DsgOC"},  {10, "Short"},  {11, "ICErr"},
    {12, "Lock"},
};
static String _decodeProt(const String& hex) {
    uint32_t v = strtoul(hex.c_str() + (hex.startsWith("0x") || hex.startsWith("0X") ? 2 : 0), nullptr, 16);
    if (!v) return "None";
    String o;
    for (const auto& p : PROT_BITS) if (v & (1UL << p.bit)) { if (o.length()) o += " "; o += p.abbr; }
    return o.length() ? o : "None";
}
static String _decodeBalance(const String& bin32, const String& stateHex = "") {
    // bin32 is a big-endian binary string of the 32-bit balancing register
    // (char 0 = bit31 ... last char = bit0 = cell 1), so cell number for a
    // given index is (length - index), not (index + 1).
    //
    // The BMS also exposes a separate batteryState hex bitmask where bit 2
    // = "Balancing" — i.e. the pack is *in* a balancing phase. The per-cell
    // shunting register can be momentarily all-zeros between balancing pulses
    // or while the BMS evaluates which cells to shunt, so when the state says
    // "Balancing" but no cells are actively shunting right now we report
    // "Active (no cells)" instead of the misleading "None".
    String o;
    int len = bin32.length();
    for (int i = 0; i < len; i++)
        if (bin32[i] == '1') { if (o.length()) o += ' '; o += 'B'; o += String(len - i); }
    if (o.length()) return o;
    // No cells shunting right now — check the state bit.
    uint16_t sv = (uint16_t)strtoul(stateHex.c_str() + (stateHex.startsWith("0x") || stateHex.startsWith("0X") ? 2 : 0), nullptr, 16);
    return (sv & 0x04) ? "Active (no cells)" : "None";
}
static String _decodeBattState(const String& hex) {
    uint16_t v = (uint16_t)strtoul(hex.c_str() + (hex.startsWith("0x") || hex.startsWith("0X") ? 2 : 0), nullptr, 16);
    String o;
    if (v & 1)       o += "Chg ";
    if (v & 2)       o += "Dsg ";
    if (v & 4)       o += "Bal ";
    if (v & 8)       o += "Full ";
    if (v & 0x40)    o += "Heat ";
    if (!o.length()) o = "Idle";
    o.trim(); return o;
}
// Compute time-remaining seconds with correct direction:
//   charging    → time to full     ( dir=1  )
//   discharging → time to reserve  ( dir=-1 ), i.e. SOC_RESERVE_PCT%, not 0%
//   idle → 0                       ( dir=0  )
static uint32_t _calcTimeRem(const BatteryData& b, int8_t& dir) {
    float pwr       = b.totalVoltage * b.current;
    float absI      = fabs(b.current);
    float reserveAh = b.fullCapacityAh * (SOC_RESERVE_PCT / 100.0f);
    dir = 0;
    if (absI < 0.1f) return 0;
    if (pwr > 1.0f)  { dir = 1;  float t = b.fullCapacityAh - b.remainingAh; return t > 0 ? (uint32_t)((t / absI) * 3600.0f) : 0; }
    if (pwr < -1.0f) { dir = -1; float u = b.remainingAh - reserveAh; return u > 0 ? (uint32_t)((u / absI) * 3600.0f) : 0; }
    return 0;
}

// ---------------------------------------------------------------------------
DisplayManager::DisplayManager() : _spi(nullptr), _tft(nullptr), _page(PAGE_OVERVIEW), _lastPage(255) {}

// ---------------------------------------------------------------------------
void DisplayManager::begin() {
    // Do NOT pass CS to _spi->begin() — let Adafruit_ILI9341 manage it.
    // Passing CS to both causes the chip-select to fight itself → white screen.
    _spi = new SPIClass(FSPI);
    _spi->begin(TFT_CLK, TFT_MISO, TFT_MOSI, -1);

    _tft = new Adafruit_ILI9341(_spi, TFT_DC, TFT_CS, TFT_RST);
    delay(100);  // let power rails stabilise before init
    _tft->begin(27000000UL);  // 27 MHz — safe limit for XIAO ESP32-S3
    delay(50);
    _tft->setRotation(1);  // Landscape: 320 wide, 240 tall

#if TFT_BL >= 0
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, HIGH);
#endif

    showBootScreen();
}

// ---------------------------------------------------------------------------
void DisplayManager::showBootScreen() {
    _tft->fillScreen(COL_BG);
    _tft->setTextColor(COL_ACCENT);
    _tft->setTextSize(2);
    _tft->setCursor(40, 60);
    _tft->print("LiTime BMS Monitor");
    _tft->setTextSize(1);
    _tft->setTextColor(COL_DIM);
    _tft->setCursor(60, 100);
    _tft->print("Dual 48V 100Ah  |  MQTT");
    _tft->setCursor(60, 116);
    _tft->print("XIAO ESP32-S3  +  ILI9341");
    _tft->setTextColor(COL_TEXT);
    _tft->setCursor(90, 150);
    _tft->print("Connecting...");
}

// ---------------------------------------------------------------------------
void DisplayManager::showScanStatus(const char* msg) {
    _tft->fillRect(0, 140, 320, 30, COL_BG);
    _tft->setTextSize(1);
    _tft->setTextColor(COL_YELLOW);
    _tft->setCursor(10, 150);
    _tft->print(msg);
}

// ---------------------------------------------------------------------------
void DisplayManager::nextPage(const BatteryData& b1, const BatteryData& b2, const VictronData& v) {
    _page = (_page + 1) % NUM_PAGES;
    showPage(_page, b1, b2, v);
}

// ---------------------------------------------------------------------------
void DisplayManager::showPage(uint8_t page, const BatteryData& b1, const BatteryData& b2,
                               const VictronData& v) {
    bool pageChanged = (page != _lastPage);
    _page     = page;
    _lastPage = page;
    // Only blank the screen on a page transition.
    // On same-page data refreshes, text background colours erase old glyphs
    // in-place, eliminating the full-screen flash every 2 seconds.
    if (pageChanged) _tft->fillScreen(COL_BG);
    switch (_page) {
        case PAGE_OVERVIEW: _pageOverview(b1, b2); break;
        case PAGE_BAT1:     _pageBattery(b1, b2, 0); break;
        case PAGE_BAT2:     _pageBattery(b2, b1, 1); break;
        case PAGE_CELLS:    _pageCells(b1, b2);     break;
        case PAGE_MPPT:     _pageMppt(v, b1.connected, b2.connected); break;
    }
}

// ===========================================================================
//  Private helpers
// ===========================================================================

// ---------------------------------------------------------------------------
// Header bar: dark strip at top with title + "page/total" indicator
// ---------------------------------------------------------------------------
void DisplayManager::_header(const char* title, uint8_t page, uint8_t total) {
    _tft->fillRect(0, 0, 320, 18, COL_HEADER);
    _tft->setTextSize(1);
    _tft->setTextColor(COL_ACCENT);
    _tft->setCursor(4, 5);
    _tft->print(title);

    // Page indicator right-aligned
    char buf[12];
    snprintf(buf, sizeof(buf), "[%u/%u]", page + 1, total);
    _tft->setTextColor(COL_DIM);
    _tft->setCursor(320 - strlen(buf) * 6 - 4, 5);
    _tft->print(buf);

    // Horizontal rule
    _tft->drawFastHLine(0, 18, 320, COL_ACCENT);
}

// ---------------------------------------------------------------------------
// Footer bar: connection status + uptime
// ---------------------------------------------------------------------------
void DisplayManager::_footer(bool b1ok, bool b2ok, unsigned long uptimeSec) {
    _tft->fillRect(0, 223, 320, 17, COL_HEADER);
    _tft->drawFastHLine(0, 222, 320, COL_DIM);
    _tft->setTextSize(1);

    // B1 status
    _tft->setTextColor(b1ok ? COL_GREEN : COL_RED);
    _tft->setCursor(4, 229);
    _tft->print(b1ok ? "B1:OK" : "B1:--");

    // B2 status
    _tft->setTextColor(b2ok ? COL_GREEN : COL_RED);
    _tft->setCursor(48, 229);
    _tft->print(b2ok ? "B2:OK" : "B2:--");

    // Uptime
    char buf[24];
    unsigned long s = uptimeSec % 60;
    unsigned long m = (uptimeSec / 60) % 60;
    unsigned long h = uptimeSec / 3600;
    snprintf(buf, sizeof(buf), "up %02luh%02lum%02lus", h, m, s);
    _tft->setTextColor(COL_DIM);
    _tft->setCursor(320 - strlen(buf) * 6 - 4, 229);
    _tft->print(buf);
}

// ---------------------------------------------------------------------------
// SOC progress bar
// ---------------------------------------------------------------------------
void DisplayManager::_socBar(int16_t x, int16_t y, int16_t w, int16_t h,
                              uint8_t soc, uint16_t fillCol, uint16_t bgCol) {
    uint16_t col = _socColour(soc);
    int16_t filled = (int16_t)((float)w * soc / 100.0f);
    _tft->fillRect(x, y, filled, h, col);
    _tft->fillRect(x + filled, y, w - filled, h, bgCol);
    _tft->drawRect(x, y, w, h, COL_DIM);

    // Percentage text centred inside bar.
    // Use black text – readable on green, yellow, orange, and red fills.
    char buf[5];
    snprintf(buf, sizeof(buf), "%d%%", soc);
    int16_t tx = x + w / 2 - strlen(buf) * 3;
    int16_t ty = y + (h - 7) / 2;
    _tft->setTextSize(1);
    _tft->setTextColor(0x0000, col);  // black on fill colour
    _tft->setCursor(tx, ty);
    _tft->print(buf);
}

// ---------------------------------------------------------------------------
// Key/value label pair on one line
// ---------------------------------------------------------------------------
void DisplayManager::_kv(int16_t x, int16_t y, const char* label,
                          const char* value, uint16_t valCol, uint8_t textSize) {
    _tft->setTextSize(textSize);
    // 2-arg setTextColor fills behind each glyph, erasing previous value.
    _tft->setTextColor(COL_DIM, COL_BG);
    _tft->setCursor(x, y);
    _tft->print(label);
    _tft->setTextColor(valCol, COL_BG);
    _tft->setCursor(x + strlen(label) * 6 * textSize, y);
    _tft->print(value);
}

// ---------------------------------------------------------------------------
uint16_t DisplayManager::_socColour(uint8_t soc) {
    if (soc >= 60) return COL_GREEN;
    if (soc >= 30) return COL_YELLOW;
    if (soc >= 15) return COL_ORANGE;
    return COL_RED;
}

// ---------------------------------------------------------------------------
uint16_t DisplayManager::_cellColour(float v, float avg) {
    float delta = v - avg;
    if (delta >  0.05f) return COL_RED;
    if (delta < -0.05f) return COL_ORANGE;
    if (delta >  0.02f) return COL_YELLOW;
    return COL_GREEN;
}

// ===========================================================================
//  PAGE 0: Overview – combined view of both batteries
// ===========================================================================
void DisplayManager::_pageOverview(const BatteryData& b1, const BatteryData& b2) {
    _header("OVERVIEW", PAGE_OVERVIEW, NUM_PAGES);

    // ----- Battery SOC bars -----------------------------------------------
    // B1
    _tft->setTextSize(1);
    _tft->setTextColor(COL_DIM);
    _tft->setCursor(4, 24);
    _tft->print("B1");
    _socBar(22, 22, 170, 12, b1.soc);
    char buf[20];
    snprintf(buf, sizeof(buf), "%.1fV", b1.totalVoltage);
    _tft->setTextColor(COL_TEXT, COL_BG);
    _tft->setCursor(198, 24);
    _tft->print(buf);

    // B2
    _tft->setTextColor(COL_DIM, COL_BG);
    _tft->setCursor(4, 40);
    _tft->print("B2");
    _socBar(22, 38, 170, 12, b2.soc);
    snprintf(buf, sizeof(buf), "%.1fV", b2.totalVoltage);
    _tft->setTextColor(COL_TEXT, COL_BG);
    _tft->setCursor(198, 40);
    _tft->print(buf);

    // Divider
    _tft->drawFastHLine(0, 54, 320, COL_HEADER);

    // ----- Combined totals ------------------------------------------------
    // Only average SOC from connected batteries to avoid skewing the reading
    // when one BMS is disconnected (would otherwise report 0%)
    int   connectedCount = 0;
    float avgSOC       = 0.0f;
    if (b1.connected) { avgSOC += b1.soc; connectedCount++; }
    if (b2.connected) { avgSOC += b2.soc; connectedCount++; }
    if (connectedCount > 0) { avgSOC /= connectedCount; }
    float totalCurrent  = b1.current + b2.current;
    float totalPower    = (b1.totalVoltage * b1.current) + (b2.totalVoltage * b2.current);
    float totalRemainAh = b1.remainingAh + b2.remainingAh;
    float totalCapAh    = b1.fullCapacityAh + b2.fullCapacityAh;
    float absI          = fabs(totalCurrent);

    // Time-remaining with correct direction (discharge target = reserve, not 0%)
    float reserveAh = totalCapAh * (SOC_RESERVE_PCT / 100.0f);
    uint32_t timeRemSec = 0;
    const char* timeLabel = "Time Rem:";
    if (absI > 0.2f) {
        if (totalPower > 1.0f) {
            float toFill = totalCapAh - totalRemainAh;
            if (toFill > 0.0f) timeRemSec = (uint32_t)((toFill / absI) * 3600.0f);
            timeLabel = "To Full: ";
        } else if (totalPower < -1.0f) {
            float usableAh = totalRemainAh - reserveAh;
            if (usableAh > 0.0f) timeRemSec = (uint32_t)((usableAh / absI) * 3600.0f);
            timeLabel = "To Rsrv: ";
        }
    }

    // Power line
    _tft->setTextSize(1);
    const char* flowDir = (totalPower >= 1.0f) ? "CHARGING" :
                          (totalPower <= -1.0f) ? "DISCHARGING" : "IDLE";
    uint16_t pCol = (totalPower >= 1.0f) ? COL_GREEN :
                    (totalPower <= -1.0f) ? COL_ORANGE : COL_DIM;
    snprintf(buf, sizeof(buf), "%+.0f W", totalPower);
    _kv(4, 60, "Power:   ", buf, pCol);
    // flowDir varies in width (IDLE/CHARGING/DISCHARGING) – pad to fixed width
    _tft->setTextColor(pCol, COL_BG);
    _tft->setCursor(140, 60);
    char flowBuf[14];
    snprintf(flowBuf, sizeof(flowBuf), "%-12s", flowDir);
    _tft->print(flowBuf);

    // Time remaining
    if (absI > 0.2f && timeRemSec > 0) {
        uint32_t h = timeRemSec / 3600;
        uint32_t m = (timeRemSec % 3600) / 60;
        snprintf(buf, sizeof(buf), "%luh %02lum", (unsigned long)h, (unsigned long)m);
        _kv(4, 74, timeLabel, buf, COL_YELLOW);
    } else {
        _kv(4, 74, "Time Rem:", "---      ", COL_DIM);
    }

    // Voltages / currents
    snprintf(buf, sizeof(buf), "%.2fV", b1.totalVoltage + b2.totalVoltage);
    _kv(4, 88, "Sum V:   ", buf, COL_TEXT);
    snprintf(buf, sizeof(buf), "%+.2f A", totalCurrent);
    _kv(4, 102, "Total I: ", buf, (totalCurrent >= 0) ? COL_GREEN : COL_ORANGE);

    // Remaining Ah combined
    snprintf(buf, sizeof(buf), "%.1f / %.0f Ah", totalRemainAh,
             b1.fullCapacityAh + b2.fullCapacityAh);
    _kv(4, 116, "Stored:  ", buf, COL_ACCENT);

    // Avg SOC big number: "%3d%%" = always 4 chars (" 77%" or "100%").
    // At textSize=3 that is 4*18=72px; from x=248 it ends exactly at x=320.
    _tft->setTextSize(3);
    _tft->setTextColor(_socColour((uint8_t)avgSOC), COL_BG);
    snprintf(buf, sizeof(buf), "%3d%%", (int)avgSOC);
    _tft->setCursor(248, 68);
    _tft->print(buf);
    _tft->setTextSize(1);
    _tft->setTextColor(COL_DIM, COL_BG);
    _tft->setCursor(258, 96);
    _tft->print("avg SOC");

    // Divider
    _tft->drawFastHLine(0, 134, 320, COL_HEADER);

    // ----- Per-battery mini stats -----------------------------------------
    // Row headers
    _tft->setTextColor(COL_DIM);
    _tft->setCursor(4,  141);  _tft->print("       B1      B2");
    snprintf(buf, sizeof(buf), "%.2fA  %.2fA", b1.current, b2.current);
    _kv(4, 153, "Cur:   ", buf, COL_TEXT);
    snprintf(buf, sizeof(buf), "%dC      %dC", b1.cellTemp, b2.cellTemp);
    _kv(4, 165, "Tcell: ", buf, COL_YELLOW);
    snprintf(buf, sizeof(buf), "%dC      %dC", b1.mosfetTemp, b2.mosfetTemp);
    _kv(4, 177, "Tmos:  ", buf, COL_YELLOW);
    snprintf(buf, sizeof(buf), "%-5u      %-5u", b1.dischargesCount, b2.dischargesCount);
    _kv(4, 189, "Cyc:   ", buf, COL_DIM);
    snprintf(buf, sizeof(buf), "%-5s      %-5s", b1.soh.c_str(), b2.soh.c_str());
    _kv(4, 201, "SOH:   ", buf, COL_GREEN);

    _footer(b1.connected, b2.connected, millis() / 1000);
}

// ===========================================================================
//  PAGE 1/2: Individual battery detail
// ===========================================================================
void DisplayManager::_pageBattery(const BatteryData& b, const BatteryData& other, uint8_t idx) {
    char title[20];
    snprintf(title, sizeof(title), (idx == 0) ? "BATTERY 1" : "BATTERY 2");
    _header(title, idx + 1, NUM_PAGES);

    char buf[40];
    uint8_t y = 22;

    if (!b.connected) {
        _tft->setTextSize(1);
        _tft->setTextColor(COL_RED);
        _tft->setCursor(60, 100);
        _tft->print("NOT CONNECTED");
        _footer(idx == 0 ? b.connected : other.connected,
                idx == 1 ? b.connected : other.connected, millis() / 1000);
        return;
    }

    // SOC bar
    _tft->setTextColor(COL_DIM);
    _tft->setTextSize(1);
    _tft->setCursor(4, y);
    _tft->print("SOC");
    _socBar(30, y - 1, 220, 13, b.soc);
    y += 16;

    // Voltage + current on same line (large)
    _tft->setTextSize(2);
    snprintf(buf, sizeof(buf), "%.2fV", b.totalVoltage);
    _tft->setTextColor(COL_TEXT, COL_BG);
    _tft->setCursor(4, y);
    _tft->print(buf);
    float pwr = b.totalVoltage * b.current;
    snprintf(buf, sizeof(buf), "%+.1fA  ", b.current);
    _tft->setTextColor(b.current >= 0 ? COL_GREEN : COL_ORANGE, COL_BG);
    _tft->setCursor(130, y);
    _tft->print(buf);
    y += 20;

    // Power
    _tft->setTextSize(1);
    snprintf(buf, sizeof(buf), "%+.0f W  (%s)", pwr,
             pwr > 1.0f ? "charging" : pwr < -1.0f ? "discharging" : "idle");
    _kv(4, y, "Power:    ", buf, pwr >= 0 ? COL_GREEN : COL_ORANGE);
    y += 12;

    // Time remaining  (correct direction: to-full when charging, to-empty when discharging)
    int8_t trDir;
    uint32_t trSec = _calcTimeRem(b, trDir);
    if (trDir != 0 && trSec > 0) {
        uint32_t h = trSec / 3600, m = (trSec % 3600) / 60;
        snprintf(buf, sizeof(buf), "%luh %02lum", (unsigned long)h, (unsigned long)m);
        _kv(4, y, (trDir == 1) ? "To Full:  " : "To Rsrv:  ", buf, COL_YELLOW);
    } else {
        _kv(4, y, "Time Rem: ", "---      ", COL_DIM);
    }
    y += 12;

    snprintf(buf, sizeof(buf), "%.1f / %.0f Ah  (%.0f%%)",
             b.remainingAh, b.fullCapacityAh, b.soc * 1.0f);
    _kv(4, y, "Stored:   ", buf, COL_ACCENT);
    y += 12;

    snprintf(buf, sizeof(buf), "%d\xF8""C", b.cellTemp);
    _kv(4, y, "Cell Temp:", buf, COL_YELLOW);
    snprintf(buf, sizeof(buf), "  MOS: %d\xF8""C  ", b.mosfetTemp);
    _tft->setTextColor(COL_YELLOW, COL_BG);
    _tft->print(buf);
    y += 12;

    snprintf(buf, sizeof(buf), "%s", b.soh.c_str());
    _kv(4, y, "SOH:      ", buf, COL_GREEN);
    snprintf(buf, sizeof(buf), "  Cycles: %-6lu", (unsigned long)b.dischargesCount);
    _tft->setTextColor(COL_DIM, COL_BG);
    _tft->print(buf);
    y += 12;

    // Protection / balance / state  (decoded to human-readable text)
    String protStr = _decodeProt(b.protectionState);
    String balStr  = _decodeBalance(b.balancingState, b.batteryState);
    String stStr   = _decodeBattState(b.batteryState);
    uint16_t protCol = (protStr == "None") ? COL_GREEN : COL_RED;
    uint16_t balCol  = (balStr  == "None") ? COL_DIM   : COL_BLUE;
    _kv(4, y, "Protect:  ", protStr.c_str(), protCol);  y += 12;
    _kv(4, y, "Balance:  ", balStr.c_str(),  balCol);   y += 12;
    _kv(4, y, "State:    ", stStr.c_str(),   COL_ACCENT); y += 14;

    // Min/max cell voltage
    if (!b.cellVoltages.empty()) {
        float mn = b.cellVoltages[0], mx = b.cellVoltages[0];
        for (float v : b.cellVoltages) { mn = min(mn, v); mx = max(mx, v); }
        snprintf(buf, sizeof(buf), "Min:%.3fV Max:%.3fV Delta:%.0fmV",
                 mn, mx, (mx - mn) * 1000.0f);
        _tft->setTextSize(1);
        _tft->setTextColor((mx - mn) > 0.05f ? COL_ORANGE : COL_GREEN, COL_BG);
        _tft->setCursor(4, y);
        _tft->print(buf);
    }

    _footer(idx == 0 ? b.connected : other.connected,
            idx == 1 ? b.connected : other.connected, millis() / 1000);
}

// ===========================================================================
//  PAGE 3: Cell voltage grid – both batteries side by side
// ===========================================================================
void DisplayManager::_pageCells(const BatteryData& b1, const BatteryData& b2) {
    _header("CELL VOLTAGES", PAGE_CELLS, NUM_PAGES);

    // Each battery gets half the screen: 160px wide
    // CELL_H=22 (not 24): last row ends at y=204, clear of the delta indicator.
    const int16_t COL_W  = 76;
    const int16_t CELL_H = 22;
    const int16_t COLS   = 2;      // columns per battery
    const int16_t ROWS   = 8;
    const int16_t YSTART = 20;

    // Battery labels
    _tft->setTextSize(1);
    _tft->setTextColor(COL_ACCENT);
    _tft->setCursor(40, YSTART);
    _tft->print("BATTERY 1");
    _tft->setCursor(200, YSTART);
    _tft->print("BATTERY 2");

    auto drawBatteryCells = [&](const BatteryData& b, int16_t xOffset) {
        if (!b.connected || b.cellVoltages.empty()) {
            _tft->setTextColor(COL_RED);
            _tft->setCursor(xOffset + 10, 100);
            _tft->print("NO DATA");
            return;
        }
        // Compute average for delta colouring
        float avg = 0;
        for (float v : b.cellVoltages) avg += v;
        avg /= b.cellVoltages.size();

        char buf[20];  // enlarged to fit delta label too
        for (size_t i = 0; i < b.cellVoltages.size() && i < (size_t)(COLS * ROWS); i++) {
            int16_t col = i / ROWS;
            int16_t row = i % ROWS;
            int16_t x   = xOffset + col * COL_W;
            int16_t y   = YSTART + 10 + row * CELL_H;

            uint16_t col16 = _cellColour(b.cellVoltages[i], avg);
            _tft->drawRect(x, y, COL_W - 2, CELL_H - 2, COL_HEADER);
            _tft->fillRect(x + 1, y + 1, COL_W - 4, CELL_H - 4, COL_BG);

            // Cell number
            _tft->setTextSize(1);
            _tft->setTextColor(COL_DIM);
            _tft->setCursor(x + 2, y + 2);
            snprintf(buf, sizeof(buf), "C%02d", (int)i + 1);
            _tft->print(buf);

            // Voltage
            _tft->setTextColor(col16);
            _tft->setCursor(x + 2, y + 12);
            snprintf(buf, sizeof(buf), "%.3fV", b.cellVoltages[i]);
            _tft->print(buf);
        }

        // Delta indicator – below the cell grid (y=207 with CELL_H=22, last row ends y=204)
        if (!b.cellVoltages.empty()) {
            float mn = *std::min_element(b.cellVoltages.begin(), b.cellVoltages.end());
            float mx = *std::max_element(b.cellVoltages.begin(), b.cellVoltages.end());
            snprintf(buf, sizeof(buf), "delta:%.0fmV  ", (mx - mn) * 1000.0f);
            _tft->setTextColor((mx - mn) > 0.05f ? COL_ORANGE : COL_GREEN, COL_BG);
            _tft->setCursor(xOffset + 4, 207);
            _tft->print(buf);
        }
    };

    drawBatteryCells(b1, 0);
    _tft->drawFastVLine(160, YSTART, 202, COL_DIM);
    drawBatteryCells(b2, 162);

    _footer(b1.connected, b2.connected, millis() / 1000);
}

// ===========================================================================
//  PAGE 4: MPPT Solar Controller (Victron BLE advertising data)
//
//  MPPT data arrives from a completely independent, non-blocking BLE scan
//  (VictronMPPT::tick() – see victron_mppt.cpp). A missing/invalid/stale
//  reading here never blocks or otherwise affects BMS polling, MQTT
//  publishing, or the rest of the display app — this page simply shows a
//  "not available" placeholder until valid data arrives.
// ===========================================================================
void DisplayManager::_pageMppt(const VictronData& v, bool b1ok, bool b2ok) {
    _header("MPPT SOLAR", PAGE_MPPT, NUM_PAGES);

    // Treat data older than 60s (or never received) as stale, same spirit as
    // the 30s "age-out" rule in VictronMPPT::tick().
    unsigned long ageSec = v.valid ? (millis() - v.last_update_ms) / 1000UL : 9999UL;
    bool ok = v.valid && ageSec < 60UL;

    if (!ok) {
        _tft->setTextSize(1);
        _tft->setTextColor(COL_RED);
        _tft->setCursor(50, 100);
        _tft->print("MPPT NOT AVAILABLE");
        _tft->setTextColor(COL_DIM);
        _tft->setCursor(30, 116);
        _tft->print("(no recent Victron BLE data)");
        _footer(b1ok, b2ok, millis() / 1000);
        return;
    }

    char buf[40];
    uint8_t y = 24;

    // Charger state (large)
    _tft->setTextSize(2);
    _tft->setTextColor(COL_ACCENT, COL_BG);
    _tft->setCursor(4, y);
    _tft->print(victronStateStr(v.device_state));
    y += 22;

    _tft->setTextSize(1);

    snprintf(buf, sizeof(buf), "%+.0f W", v.pv_w);
    _kv(4, y, "PV Power:  ", buf, v.pv_w >= 1.0f ? COL_GREEN : COL_DIM);
    y += 14;

    snprintf(buf, sizeof(buf), "%.2f V", v.batt_v);
    _kv(4, y, "Batt Volt: ", buf, COL_TEXT);
    y += 14;

    snprintf(buf, sizeof(buf), "%+.1f A", v.batt_a);
    _kv(4, y, "Batt Curr: ", buf, v.batt_a >= 0.0f ? COL_GREEN : COL_ORANGE);
    y += 14;

    snprintf(buf, sizeof(buf), "%.3f kWh", v.yield_today);
    _kv(4, y, "Yield Tdy: ", buf, COL_YELLOW);
    y += 14;

    const char* errStr = victronErrorStr(v.charger_error);
    _kv(4, y, "Error:     ", errStr,
        (v.charger_error == 0) ? COL_GREEN : COL_RED);
    y += 14;

    snprintf(buf, sizeof(buf), "%lus ago", ageSec);
    _kv(4, y, "Last Seen: ", buf, COL_DIM);

    _footer(b1ok, b2ok, millis() / 1000);
}
