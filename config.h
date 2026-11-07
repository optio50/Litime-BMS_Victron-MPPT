#pragma once

// =============================================================================
//  config.h  –  HARDWARE CONSTANTS ONLY (pins + display color palette)
// =============================================================================
//  All user settings (WiFi, MQTT, battery MACs, Victron MPPT key, timings,
//  names, ...) live ON THE DEVICE in /config.ini (LittleFS flash), editable
//  from any browser at  http://<hostname>.local/config  —  see app_config.h
//  and config_server.cpp. Nothing there is compiled in; changing a setting
//  never requires a rebuild or a re-flash of the firmware.
//
//  Only wiring facts and the display palette stay here. Change this file ONLY
//  if you rewire the display or want different screen colors (recompile then).
// =============================================================================

// --- Display (ILI9341 240x320 SPI) ------------------------------------------
// XIAO ESP32-S3 hardware SPI + chosen DC/CS/RST pins
// Waveshare 2.4" LCD module pinout -> XIAO header:
//   LCD VCC   -> 3V3
//   LCD GND   -> GND
//   LCD DIN   -> D9  (GPIO8)  [SPI MOSI / data in]
//   LCD CLK   -> D8  (GPIO7)  [SPI SCK]
//   LCD CS    -> D2  (GPIO3)
//   LCD DC    -> D3  (GPIO4)
//   LCD RST   -> D1  (GPIO2)
//   LCD BL    -> 5V  (XIAO VBUS/5V pin)
//
// NOTE: On XIAO ESP32-S3, Arduino pin numbers = GPIO numbers (NOT D-pin numbers)
//   D1=GPIO2, D2=GPIO3, D3=GPIO4, D8=GPIO7, D9=GPIO8, D10=GPIO9
#define TFT_CS    3    // D2  = GPIO3
#define TFT_RST   2    // D1  = GPIO2
#define TFT_DC    4    // D3  = GPIO4
#define TFT_MOSI  8    // D9  = GPIO8
#define TFT_CLK   7    // D8  = GPIO7
#define TFT_MISO  -1   // not used

// Backlight pin (-1 = disabled/always-on; BL wired directly to the XIAO's
// 5V/VBUS pin). Set to a GPIO number only if you rewire BL for dimming.
#define TFT_BL    -1

// --- Color palette (RGB565) --------------------------------------------------
#define COL_BG        0x0000   // Black
#define COL_HEADER    0x1082   // Very dark grey
#define COL_ACCENT    0x07FF   // Cyan
#define COL_TEXT      0xFFFF   // White
#define COL_DIM       0x7BEF   // Light grey
#define COL_GREEN     0x07E0   // Green
#define COL_YELLOW    0xFFE0   // Yellow
#define COL_ORANGE    0xFD20   // Orange
#define COL_RED       0xF800   // Red
#define COL_BLUE      0x001F   // Blue
#define COL_DARKGREEN 0x03E0   // Dark green (SOC bar fill)
