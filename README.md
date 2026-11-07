# LiTime Dual BMS + Victron MPPT Monitor

Real-time display + MQTT bridge for **two LiTime 48V 100Ah Smart ComFlex** batteries
**and** a **Victron MPPT solar charge controller**, plus a companion desktop
dashboard app.

- **Seeed Studio XIAO ESP32-S3** (dual-core, BLE + WiFi) running the firmware
- **Waveshare 2.4" ILI9341 LCD** (240×320, SPI) — *optional* physical display (see note below)
- **Local MQTT broker** (Mosquitto / Home Assistant) as the data bus
- **PyQt5 desktop dashboard** (`litime_monitor.py`) with live charts, history, and event logging

> ### 🖥️ The LCD display is optional
> Every value shown on the physical screen is also published over MQTT and
> shown live in the desktop dashboard and Home Assistant. If you don't wire
> up the Waveshare ILI9341 panel, the ESP32-S3 firmware still runs and
> functions completely normally (BLE polling, Victron decoding, MQTT
> publishing, HA discovery) — the display code simply has nothing physically
> attached to draw to. Skip the **Hardware Wiring** section entirely if you
> only plan to monitor via MQTT / the desktop app / Home Assistant.

> ### 🤖 About this project
> This entire application — the ESP32-S3 firmware, the display UI, the MQTT
> bridge, the Victron BLE decryption/decoding, and the full PyQt5 desktop
> dashboard — was designed and written by **GitHub Copilot (Claude Sonnet 5)**
> working interactively with the repo owner inside VS Code. Every file in this
> repository is AI-generated code, iterated on through a conversational
> development process.

---

## Screenshots

### Physical LCD (Waveshare 2.4" ILI9341)

| Overview | Battery 1 | Battery 2 |
|---|---|---|
| ![LCD Overview](images/lcd-overview.jpg) | ![LCD Battery 1](images/lcd-battery1.jpg) | ![LCD Battery 2](images/lcd-battery2.jpg) |

| Cell Voltages | MPPT |
|---|---|
| ![LCD Cell Voltages](images/lcd-cells.jpg) | ![LCD MPPT](images/lcd-mppt.jpg) |

### Desktop Dashboard

![Overview tab](images/screenshot-01-overview.png)

![Battery 1 tab](images/screenshot-02-battery1.png)

![Battery 2 tab](images/screenshot-03-battery2.png)

![MPPT tab](images/screenshot-04-mppt.png)

![Charger State tab](images/screenshot-05-log.png)

![BMS 16 Cells tab](images/screenshot-06-cells.png)

---

## Features

| Feature | Detail |
|---|---|
| BLE polling | Both LiTime batteries polled every 2 s (alternating) |
| Victron MPPT | Passive BLE advertising scan — decrypts Victron's encrypted "Extra Manufacturer Data" broadcast, no pairing/connection required |
| SOC display | Large color-coded bars, on both the physical LCD (optional) and the desktop app |
| Live data | Voltage · Current · Power · Cell temps · MOSFET temp · PV power · Yield |
| Cell voltages | Full grid per battery, min/max/delta highlighted |
| Estimated time | Time-to-full (charging) / time-to-10%-reserve (discharging) — see note below |
| Auto-page cycle | LCD rotates through 5 screens automatically; desktop app has tabs |
| Manual advance | Press **BOOT** button (GPIO0) on XIAO to advance the LCD page |
| MQTT JSON | Full JSON payload per device + flat convenience topics |
| HA discovery | Auto-creates entities in Home Assistant via MQTT discovery |
| Watchdog | 60 s hardware WDT recovers the ESP32 from BLE/WiFi hangs |
| OTA updates | WiFi firmware upload page at `http://<esp-ip>/update` (ElegantOTA) — flash new .bin from a browser, the dashboard, or `curl`; dual-partition rollback means a failed upload can't brick it |
| MQTT reboot | Publish `REBOOT` to `litime/cmd` (or use the dashboard's Reboot button) to restart the ESP32 remotely |
| Desktop dashboard | 24-hour live charts (PyQt5 + pyqtgraph/pglive), event log, dark theme |

> **Note on "Time Remaining":** the discharge estimate targets a **10% SOC
> reserve**, not literal 0%. LiFePO4 packs shouldn't routinely be run past
> that point, and the BMS's voltage-based low-cutoff can trip before true 0%
> Ah is reached anyway. This threshold is configurable via `soc_reserve_pct`
> on the device's `/config` page. "Time to full" while charging is unaffected and still
> targets 100%.

> **Note on PV Voltage:** the Victron BLE advertising broadcast used by this
> project does not include a PV (solar array) voltage field — only PV power,
> battery voltage/current, yield, state and error are broadcast.

---

## Intended Use: Supplementing an Anker SOLIX F3800 Plus

This whole battery + MPPT monitoring rig exists to support a specific
real-world setup: the two LiTime 48V batteries (kept topped up by the
Victron MPPT/solar array) are wired into the **two solar (PV) input ports**
of an **Anker SOLIX F3800 Plus**, delivering roughly **53V DC at up to 17A —
about 1800W combined**. As far as the F3800 is concerned, this looks like a
(very well-behaved) solar array, since it's within its accepted PV
voltage/current window.

The F3800 already supports up to 1800W recharge from an AC wall outlet, so
raw charge speed isn't the point — the real advantage is that **plugging
into AC wall power disables the F3800's 240VAC output**, while its solar
(PV) input does not. Feeding it through the PV ports instead means you can
recharge the unit at the same ~1800W rate while it's still actively
outputting 240VAC — something the F3800's built-in AC charger can't do.

> **Power outage backup path:** this also works without any sun. If solar
> isn't available (e.g. an extended outage), the two LiTime batteries can
> instead be topped up by an ordinary 120V AC battery charger — run from a
> portable generator — connected directly to the batteries. The F3800 keeps
> drawing from them through its PV ports exactly as before, so you're
> effectively recharging the whole system from 120V generator power while
> the F3800 continues to output 240VAC the entire time.

> **This project does not talk to the F3800 in any way.** There is no local
> API/BLE/serial link available for the F3800 Plus (unlike the Solarbank/Smart
> Plug/Smart Meter line, which Anker's official Home Assistant integration
> supports via local Modbus TCP). This repo's ESP32 + dashboard only monitor
> the LiTime batteries and Victron MPPT feeding into it — the F3800's own SOC,
> input/output power, etc. are only visible in the Anker app.

If you're replicating this setup:

- **No external current limiting is needed.** The F3800's solar (PV) input
  acts as its own charge controller — it draws current on its own terms (up
  to ~17A per port) rather than the battery bank "pushing" current into it,
  so there's no MPPT-style current-limiting curve to replicate on the
  battery side.
- **Match polarity and voltage window carefully.** These LiTime packs are
  16S LiFePO4 (nominal 51.2V, resting/charged in the ~53-58V range), which
  needs to stay inside whatever PV input voltage range the F3800 documents
  for its solar ports — going over (or under) that window can cause it to
  reject the input entirely rather than throttle.
- Split the ~1800W combined draw across both PV ports rather than one, per
  Anker's per-port PV input rating.
- This is a manual/passive DC connection, not a communicating "smart"
  integration — the LiTime/Victron monitoring this project provides is what
  lets you keep an eye on the source batteries (SOC, current draw, temps)
  while they feed the F3800, without needing the F3800 itself to report
  anything back.

---

## Repository Layout

```
Litime-BMS_Victron-MPPT.ino  Main Arduino sketch (setup/loop)
LiTime_BMS_Display.h       Shared BatteryData / VictronData structs
app_config.cpp/.h          Runtime settings (config.ini on LittleFS flash)
config_server.cpp/.h       Browser editor at http://<hostname>.local/config
config.h                   Hardware constants only (TFT pins + color palette)
config.ini.example         Template of every setting (paste into /config Import)
dashboard_config.ini.example  Optional template: device hostname for the dashboard
display_manager.cpp/.h     ILI9341 LCD rendering
mqtt_manager.cpp/.h        MQTT publishing
victron_mppt.cpp/.h        Victron BLE passive scan, AES-CTR decrypt, decode
ota_manager.cpp/.h         ElegantOTA firmware upload at /update

litime_monitor.py          PyQt5 desktop dashboard (reads MQTT, live charts)
shared_config.py           Fetches settings from the device's /config.json
requirements.txt           Python dependencies for the desktop app
icon.jpg                   App icon/logo (used in the desktop dashboard)
images/                    Screenshots used in this README
```

---

## Hardware Wiring

> **This entire section is optional.** The firmware runs fine with nothing
> wired to the display pins — you'll just have no physical screen output.
> All data is still available via MQTT, the desktop dashboard, and Home
> Assistant. Only follow this section if you want the physical LCD readout.

### XIAO ESP32-S3 → Waveshare 2.4" ILI9341

```
Waveshare Pin    XIAO ESP32-S3 Pin    Note
─────────────────────────────────────────────────────
VCC              3V3
GND              GND
DIN              D9  (GPIO8)          SPI MOSI (data in)
CLK              D8  (GPIO7)          SPI Clock
CS               D2  (GPIO3)          Chip select
DC               D3  (GPIO4)          Data/Command
RST              D1  (GPIO2)          Display reset
BL               5V (XIAO VBUS/5V)    3V3 already used for VCC; module is rated
                                      3.3V/5V and BL only enables the on-board
                                      backlight driver (not a direct LED pin)
```

**Schematic (ASCII):**
```
XIAO ESP32-S3
┌────────────────┐
│ 3V3 ───────────┼─── VCC  (Waveshare)
│ GND ───────────┼─── GND
│ 5V  ───────────┼─── BL   (backlight; module is rated 3.3V/5V)
│ D1/GPIO2 ──────┼─── RST
│ D2/GPIO3 ──────┼─── CS
│ D3/GPIO4 ──────┼─── DC
│ D8/GPIO7 ──────┼─── CLK
│ D9/GPIO8 ──────┼─── DIN
└────────────────┘
```

### Power Supply
- Power the XIAO via its USB-C port or BAT pin (3.6–4.2V LiPo)
- The LiTime batteries and Victron MPPT are **NOT** electrically connected to
  the XIAO — all communication is wireless (BLE)

---

## Software Setup (ESP32-S3 Firmware)

### 1. Install Board Support
In Arduino IDE → **Board Manager** → install **"esp32" by Espressif Systems**
(tested with **3.3.10**). That package already contains the **XIAO_ESP32S3**
board definition, so no extra board-manager URL is needed.

Select board: **XIAO_ESP32S3**

> **Sketch folder name:** Arduino requires the folder to have the same name as
> the main `.ino` file, so keep the cloned folder named `Litime-BMS_Victron-MPPT`
> (rename it if your clone has another name, e.g. `Litime-BMS_Victron-MPPT-main`).

### 2. Install Libraries
Open **Library Manager** (Ctrl+Shift+I) and install:

| Library Name | Author | Used for |
|---|---|---|
| **BMS Client** | mirosieber (Litime_BMS_ESP32) | LiTime BLE protocol |
| Adafruit ILI9341 | Adafruit | LCD driver |
| Adafruit GFX Library | Adafruit | LCD graphics |
| PubSubClient | Nick O'Leary | MQTT client |
| ArduinoJson | Benoit Blanchon (v7+) | JSON payloads |
| **ElegantOTA** | Ayush Sharma (v3+) | WiFi firmware updates (`/update` page) |
| **wolfssl** | wolfSSL Inc. | AES-128-CTR decryption of Victron BLE data |

**Versions this firmware was built and tested with** (newer versions usually work, but these are known good):

| Component | Version |
|---|---|
| ESP32 Arduino core (`esp32` by Espressif) | 3.3.10 |
| BMS Client | 1.0.0 |
| PubSubClient | 2.8 |
| ArduinoJson | 7.4.3 |
| ElegantOTA | 3.1.7 |
| wolfssl | 5.8.4 |
| Adafruit ILI9341 / Adafruit GFX | 1.6.3 / 1.12.6 |

Board options used: `XIAO_ESP32S3`, USB Mode *Hardware CDC and JTAG*, Flash Size *8MB*,
Partition Scheme *Default with spiffs (3MB APP/1.5MB SPIFFS)*, PSRAM *Disabled*.

### 3. Configuration
Settings live **on the device**, not in a compiled header. After the first
flash (step 4), open `http://litime.local/config` from any browser:

- Fill the form (WiFi, MQTT, battery MACs, Victron key, timings, names) **or**
  paste [`config.ini.example`](config.ini.example) into the Import box.
- **Save & Reboot.** Changing a setting never requires a rebuild.

If WiFi isn't configured yet (or the device can't join the network), it
starts a `litime-setup` access point. Join it from a phone/laptop and open
`http://192.168.4.1/config`. The page has a WiFi scan dropdown and a
**Test WiFi** button that associates without saving, so a typo can never
lock you out.

The desktop dashboard fetches the same settings from
`http://litime.local/config.json` on launch (`shared_config.py`). Edit
`dashboard_config.ini` (copy it from
[`dashboard_config.ini.example`](dashboard_config.ini.example)) only if the
hostname isn't `litime.local` or you've set an OTA password.

`config.h` now holds **hardware constants only** (TFT pins + color palette).
Change it only if you rewire the display.

### 4. Compile & Flash
```
Board:         XIAO_ESP32S3
Upload Speed:  921600
Flash Size:    8MB (64Mb)
Partition:     Default with spiffs (3MB APP/1.5MB SPIFFS)   ← board default,
                OTA-capable (two 3.25 MB app partitions)
```
> **OTA requirement:** the partition scheme must have **two app partitions** —
> the board's default "Default with spiffs (3MB APP/1.5MB SPIFFS)" does.
> Schemes whose name contains "No OTA" (e.g. "Maximum APP (7.9MB APP No
> OTA)") cannot take WiFi firmware updates. If you're on the default scheme,
> nothing needs to change — the one USB flash below installs the OTA feature
> itself; afterwards you never need the cable again.

Press **Upload** (the board auto-enters bootloader).

### 5. Updating firmware over WiFi (OTA)
After the first USB flash, new firmware can be uploaded without the cable:

1. Compile the sketch in Arduino IDE (same board settings — the default
   "Default with spiffs (3MB APP/1.5MB SPIFFS)" partition is OTA-capable).
2. **Sketch → Export Compiled Binary** (Ctrl+Alt+S) — produces a `.bin`
   next to the sketch folder.
3. Upload it, using whichever is most convenient:
   - **Dashboard**: **Log ▸ ESP Update** tab → *Choose .bin…* → *Upload firmware*
     (progress bar included; the ESP's IP is discovered automatically from
     the retained `litime/ip` MQTT topic and full instructions are shown
     alongside)
   - **Browser**: open `http://<esp-ip>/update` and drag the file in
   - **CLI**: the `curl` command below
4. The device flashes the **inactive** app partition, verifies it, and
   reboots into it automatically — a failed or interrupted upload just
   keeps the old firmware running.

Scriptable, no browser needed (start the OTA session, then stream the file):
```bash
curl http://<esp-ip>/ota/start
curl -F "file=@Litime-BMS_Victron-MPPT.ino.bin" http://<esp-ip>/ota/upload
```

The upload page (and `/config`) can be password-protected via
`ota_password` on the `/config` page (username is always `admin`; empty = no auth).

Rebooting the ESP32 remotely:
```bash
mosquitto_pub -h <broker> -t litime/cmd -m REBOOT
```
The firmware publishes `rebooting` on `litime/status`, cleanly disconnects,
and restarts. (The dashboard has a **Reboot ESP32** button that does the
same.)

---

## Victron MPPT Setup (important — read this section)

This project reads your Victron MPPT solar charge controller **passively**,
over its normal Bluetooth *advertising* broadcast — the same encrypted data
the VictronConnect app reads. No pairing or BLE connection is made to the
controller; the ESP32 just listens.

Credit: the BLE advertising protocol, AES-CTR decryption approach, and byte
layout used here are based on the excellent work in
**[chrisj7903/Read-Victron-advertised-data](https://github.com/chrisj7903/Read-Victron-advertised-data)**
(MIT licensed).

### Step 1 — Enable Instant Readout on the controller
1. Open the **VictronConnect** app and connect to your MPPT once (Bluetooth).
2. Go to **Settings (⋮) → Product Info**.
3. Make sure **Instant Readout** is enabled (it broadcasts data over BLE
   advertising even when nothing is actively connected — this is what makes
   passive reading possible).

### Step 2 — Get the 32-character Encryption Key
1. Still in **Settings (⋮) → Product Info**, scroll down to **Encryption Key**.
2. ⚠️ **Known VictronConnect UI bug:** tapping the key field may only show
   31 of the 32 hex characters on-screen. Don't copy it by hand — instead
   tap the key, then use the **copy / share** dialogue that pops up to
   capture the full, correct 32-character string.
3. Paste the 32-character hex string into the **MPPT AES key** field on
   `/config` (or `mppt_key = ...` in a pasted `config.ini`). Don't convert
   it to C bytes — the firmware does that.

### Step 3 — Get the controller's BLE MAC address
Use the free **nRF Connect** app (Nordic Semiconductor, iOS/Android) — this
is the easiest method:
1. Open nRF Connect → tap **Scan**.
2. Find your MPPT in the list (it advertises using the name you set in
   VictronConnect, e.g. "SmartSolar MPPT 100/30").
3. Tap it to view details — the MAC address is shown at the top (Android).
   On iOS, Apple hides real BLE MAC addresses from apps; use an Android
   device, or fall back to the ESP32 scan sketch in
   [Finding BLE MAC Addresses](#finding-ble-mac-addresses-litime--victron)
   below and match by RSSI/proximity or manufacturer data instead.

### Step 4 — Fill in `/config`
On the device's config page (or in a pasted `config.ini`):

```
mppt_name    = My Solar Controller   ; cosmetic only
mppt_address = aa:bb:cc:dd:ee:ff     ; lower-case, from Step 3
mppt_key     = 00112233445566778899aabbccddeeff   ; from Step 2
```

### How it works (for the curious)
`victron_mppt.cpp` runs a repeating passive BLE scan looking for any
advertisement carrying Victron's company ID (`0x02E1`). For the packet
matching the configured `mppt_address`, it:
1. Extracts the 2-byte IV (nonce) and the encrypted payload from the
   manufacturer-data bytes.
2. Decrypts the payload with **AES-128-CTR** (via wolfSSL) using your key.
3. Decodes the 16 decrypted bytes into device state, error code, battery
   voltage/current, PV power, and yield-today — all per Victron's published
   "Extra Manufacturer Data" bit layout.

If the MPPT is out of range or fails to decode, the MPPT screen/tab simply
shows "not available" — nothing else in the firmware or dashboard is
affected.

---

## Finding BLE MAC Addresses (LiTime + Victron)

**Easiest method — nRF Connect app** (Nordic Semiconductor, free, iOS/Android):
1. Open the app → tap **Scan**.
2. Look for your LiTime battery (name usually contains "LT" or similar) or
   your Victron device (its VictronConnect name).
3. Tap the device to see its MAC address at the top of the detail screen.
4. Android shows a real MAC; iOS shows a random per-app UUID instead due to
   OS restrictions — use Android, or the sketch below, for iOS.
5. Power-cycle or bring the phone close to each battery individually to be
   sure which MAC belongs to which physical unit.

**Alternative — quick ESP32 scan sketch** (paste into a blank `.ino`):
```cpp
#include <BLEDevice.h>
#include <BLEScan.h>
BLEScan* pScan;
void setup() {
    Serial.begin(115200);
    BLEDevice::init("");
    pScan = BLEDevice::getScan();
    pScan->setActiveScan(true);
    BLEScanResults results = pScan->start(10);
    for (int i = 0; i < results.getCount(); i++) {
        BLEAdvertisedDevice d = results.getDevice(i);
        Serial.printf("%s  %s  RSSI:%d\n",
            d.getAddress().toString().c_str(),
            d.getName().c_str(), d.getRSSI());
    }
}
void loop() {}
```

---

## Desktop Dashboard (`litime_monitor.py`)

A PyQt5 dark-theme desktop app that subscribes to the same MQTT topics the
firmware publishes, showing live values plus 24-hour rolling charts.

> ### 📈 Powered by pglive
> The smooth, genuinely real-time charts throughout this dashboard (Power,
> Voltage & Current, Temperatures, Cell Delta, Ah Remaining, per-cell
> voltage history, PV Power, Yield, and the Charger State timeline) are all
> built on
> **[pglive](https://github.com/domarm-comat/pglive)**, layered on top of
> **[pyqtgraph](https://github.com/pyqtgraph/pyqtgraph)**. pglive's
> `DataConnector` model made it trivial to get thread-safe, continuously
> scrolling 24-hour rolling charts with crosshairs, live tooltips, and zero
> flicker — updating every 2 seconds without any manual redraw/repaint
> bookkeeping. Genuinely one of the best live-charting experiences available
> for PyQt, and this dashboard would look far less polished without it.

### Install
```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Dependencies (`requirements.txt`): `PyQt5`, `PyQtWebEngine`, `pyqtgraph`, `pglive`, `numpy`, `paho-mqtt`.

### Configure
The dashboard fetches MQTT broker, credentials, and battery names from the
ESP32 (`http://litime.local/config.json` via `shared_config.py`) —
**edit settings once on `/config` and both the firmware and the desktop app
pick them up**. If the device is unreachable, the app falls back to safe
defaults and prints a warning. Override the hostname in
`dashboard_config.ini` (see `dashboard_config.ini.example`) if it isn't `litime.local`.

### Run
```bash
python3 litime_monitor.py
```

### Tabs
- **Overview** — combined SOC bars, power flow, per-battery summary, MPPT summary strip
- **Battery 1 / Battery 2** — big V/I/P readout, full statistics panel, 7 inner chart tabs (Power, V&I, Now, Temps, Cell Δ, Ah Remaining, Cell Voltages). **Now** is the live 16-cell snapshot (colored by deviation from the pack average); **Cell Voltages** is the 24-hour per-cell history.
- **MPPT** — MPPT statistics panel (mirrors the battery stats panel) + inner chart tabs (PV Power, Battery Voltage, Battery Current, Yield Today, Charger State)
- **Log** — running event/timestamped log of state changes and periodic summaries, plus **MQTT Raw** (live topic/payload table) and **ESP Update** sub-tabs
- **ESP Update** (Log sub-tab) — native OTA firmware upload panel (file picker + progress bar + one-click **Reboot ESP32**) styled like the rest of the app, next to the full how-to-update instructions; uploads go straight to the ESP's `/update` endpoint

---

## MQTT Topics

### Per-battery JSON state
```
litime/battery1/state     { full JSON object }
litime/battery2/state     { full JSON object }
```

**Example payload:**
```json
{
  "connected": true,
  "soc": 78,
  "soh": "99%",
  "total_voltage": 52.346,
  "current": 12.500,
  "power": 654.3,
  "cell_temp": 27,
  "mosfet_temp": 31,
  "remaining_ah": 78.00,
  "full_capacity_ah": 100.00,
  "discharge_cycles": 12,
  "time_remaining_s": 22464,
  "time_direction": "to_full",
  "cell_voltages": [3.272, 3.271, 3.270, "..."],
  "cell_min_v": 3.270,
  "cell_max_v": 3.274,
  "cell_delta_mv": 4.0
}
```

### Flat convenience topics
```
litime/battery1/soc
litime/battery1/voltage
litime/battery1/current
litime/battery1/power
litime/battery1/cells/cell01 … cell16
```

### Combined totals
```
litime/combined/state     { soc_avg, total_power, total_current,
                            total_remaining_ah, time_remaining_s,
                            time_direction, flow }
```

### Victron MPPT
```
victron/state              { full JSON: valid, state, state_str, error,
                             error_str, batt_v, batt_a, pv_w, yield_today,
                             last_seen_s }
victron/state_str
victron/batt_v
victron/batt_a
victron/pv_w
victron/yield_today
```

### Status / LWT
```
litime/status              "online" / "offline" / "rebooting"
litime/ip                  "<esp-ip>"   (retained — published on every MQTT
                                         connect; used by the dashboard to
                                         build the OTA page URL)
litime/cmd                 subscribe: "REBOOT"  (publish to it, QoS 1, to
                                               restart the ESP32)
```

---

## Home Assistant Integration

All Home Assistant entities for this project come from **one MQTT package
file** — the firmware itself does **not** use MQTT discovery. (An earlier
version did auto-discover a handful of core sensors, but keeping two
overlapping sources of truth caused duplicate/inconsistent entities once the
YAML package below was added, so firmware-side discovery was removed
entirely in favor of a single, complete, well-organized source.)

### Full MQTT sensor coverage (YAML package)

A ready-to-use Home Assistant **package** file covering every single topic
this project publishes is provided at
[`homeassistant/mqtt_sensors.yaml`](homeassistant/mqtt_sensors.yaml),
including:

- Per battery: voltage, current, power, SOC, cell/MOSFET temp, remaining Ah,
  full capacity, SOH, discharge cycles, cell min/max/delta voltage, all 16
  individual cell voltages, human-readable protection/balancing/state text,
  a raw status-register diagnostic, direction, and a friendly "3h 43m to
  Reserve" time-remaining sensor
- The entire Victron MPPT (charger state, error, battery V/A, PV power,
  yield today) — the firmware has no native discovery for this at all
- True system-wide combined values (average SOC, total power/current/
  remaining Ah/capacity, flow direction, time remaining) — **not** a
  per-battery duplicate; each battery's own values live on its own device

**Install:**
1. Enable packages in `configuration.yaml` (skip if already enabled):
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. Copy [`homeassistant/mqtt_sensors.yaml`](homeassistant/mqtt_sensors.yaml)
   into `<ha-config>/packages/litime_victron_mqtt.yaml`.
3. If you changed `topic_base` / `victron_topic_base` from the
   defaults (`litime` / `victron`) on `/config`, update the topic strings
   in the copied file to match.
4. **Developer Tools → YAML → Check Configuration**, then **Restart**.

This creates four devices — **LiTime Battery 1**, **LiTime Battery 2**,
**LiTime System** (combined/system-wide values), and **Victron MPPT** — with
an entity for every field published over MQTT. No firmware reflash is
required to use it; it reads the raw MQTT topics directly.

---

## BLE Protocol Reference — LiTime BMS

The firmware uses the **BMSClient** library
([mirosieber/Litime_BMS_ESP32](https://github.com/mirosieber/Litime_BMS_ESP32)),
which implements:

| Item | Value |
|---|---|
| Service UUID | `0000FFE0-0000-1000-8000-00805F9B34FB` |
| Write UUID (command) | `0000FFE2-0000-1000-8000-00805F9B34FB` |
| Notify UUID (data) | `0000FFE1-0000-1000-8000-00805F9B34FB` |
| Query command | `00 00 04 01 13 55 AA 17` |
| Response length | 104 bytes |

Key byte offsets in the 104-byte response:

| Field | Bytes | Scale |
|---|---|---|
| Total voltage | [8..11] | ÷ 1000 → V |
| Cell voltage sum | [12..15] | ÷ 1000 → V |
| Cell voltages | [16..47] | 2 bytes each ÷ 1000 → V |
| Current | [48..51] | ÷ 1000 → A (signed) |
| Cell temp | [52..53] | °C (signed) |
| MOSFET temp | [54..55] | °C (signed) |
| Remaining Ah | [62..63] | ÷ 100 → Ah |
| Full capacity Ah | [64..65] | ÷ 100 → Ah |
| SOC | [90..91] | % |
| SOH | [92..95] | % |
| Discharge cycles | [96..99] | count |
| Status register (0x68) | [68..71] | raw hex (unidentified — library labeled "heat" but pack has no heater) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "BLE: FAILED" on boot | Check MAC address on `/config`; ensure battery is powered on; move closer |
| Only one battery connects | BLE on ESP32 supports multiple connections but range/interference matters; try staggered `init()` with a delay |
| Display blank / white | Check SPI wiring; verify TFT_CS, TFT_DC, TFT_RST pin assignments |
| MQTT not connecting | Verify broker IP; check firewall; test with `mosquitto_sub -t '#'` |
| Wrong cell count | 48V 16S battery → 16 cells; if you see <16, some cells may read 0V and are skipped by the library |
| Watchdog reset | Battery BLE response stalled; the 60 s WDT will auto-reboot the ESP32 |
| MPPT never decodes | Confirm Instant Readout is enabled in VictronConnect, MAC address is correct, and the 32-char key was captured via copy/share (not typed by hand) |
| MPPT "Yield Today" doesn't reset at midnight | This is normal Victron firmware behavior in locations with very long daylight / no true dark period — the MPPT has no RTC and relies on a full day/night voltage cycle to reset the counter. Not a bug in this project. |
| Dashboard shows all zeros, log says "Settings NOT loaded" | The dashboard could not fetch `/config.json` from the device, so it fell back to placeholder defaults (broker `192.168.1.100`). Check the device is powered and on your network, then **restart the dashboard** (right after a config save the device reboots and `litime.local` takes a few seconds to appear). If `litime.local` still doesn't resolve on your PC (mDNS is off or unsupported, common on some Windows/Android setups), copy `dashboard_config.ini.example` to `dashboard_config.ini` and set `host = <the device's IP address>` (see your router's client list or the `litime/ip` MQTT topic) |
| Desktop app can't reach the device | Check `dashboard_config.ini` (`host = litime.local`); confirm `http://litime.local/config.json` opens in a browser |

---

## Credits

This project builds directly on the work of:

- **[chrisj7903/Read-Victron-advertised-data](https://github.com/chrisj7903/Read-Victron-advertised-data)**
  (MIT) — Victron BLE advertising protocol research, AES-CTR decryption
  approach, and Solar Charger byte-field layout that `victron_mppt.cpp` is
  based on.
- **[mirosieber/Litime_BMS_ESP32](https://github.com/mirosieber/Litime_BMS_ESP32)**
  — the `BMSClient` Arduino library used to read the LiTime batteries over BLE.
- **[pglive](https://github.com/domarm-comat/pglive)** — the real-time charting
  layer behind every live chart in the desktop dashboard. Its `DataConnector`
  API made smooth, thread-safe, continuously scrolling live plots almost
  trivial to implement — genuinely excellent library.
- **[pyqtgraph](https://github.com/pyqtgraph/pyqtgraph)** — the fast
  scientific plotting library that pglive itself is built on top of.

## License
MIT — based on [mirosieber/Litime_BMS_ESP32](https://github.com/mirosieber/Litime_BMS_ESP32)
and [chrisj7903/Read-Victron-advertised-data](https://github.com/chrisj7903/Read-Victron-advertised-data).
