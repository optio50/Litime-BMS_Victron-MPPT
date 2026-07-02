// =============================================================================
//  victron_mppt.cpp  –  Victron MPPT BLE passive advertising reader
//
//  Based on: github.com/chrisj7903/Read-Victron-advertised-data  (MIT)
//  Reference: Victron Extra Manufacturer Data spec (Feb 2023)
//
//  Manufacturer data packet layout (as returned by getManufacturerData()):
//    [0]    0xE1  – Victron company ID low  (little-endian 0x02E1)
//    [1]    0x02  – Victron company ID high
//    [2]    0x10  – record type (Solar Controller)
//    [3]    model ID low
//    [4]    model ID high
//    [5]    readout type / flags
//    [6]    (unused for decryption)
//    [7]    IV / nonce low byte
//    [8]    IV / nonce high byte
//    [9]    (padding/reserved)
//    [10-25] 16 bytes of AES-128-CTR encrypted payload
//
//  Decrypted 16-byte Solar Charger payload:
//    [0]    device_state   (uint8: 0=Off,1=LoPwr,2=Fault,3=Bulk,4=Absorb,5=Float,6=Store,7=EqMan)
//    [1]    charger_error  (uint8)
//    [2-3]  battery voltage  (sign-magnitude 16-bit LE, units 10 mV, sentinel 0x7FFF=N/A)
//    [4-5]  battery current  (sign-magnitude 16-bit LE, units 100 mA, sentinel 0x7FFF=N/A)
//    [6-7]  yield today      (uint16 LE, units 10 Wh, sentinel 0xFFFF=N/A)
//    [8-9]  PV power         (uint16 LE, units 1 W, sentinel 0xFFFF=N/A)
//    [10-11] external device/load current (uint9 LE, units 100 mA) – NOT PV
//            voltage; there is no PV-voltage field in this broadcast. Left
//            undecoded here (not currently used by this project).
//    [12-15] reserved
//
//  Decryption uses wolfssl (install via Arduino Library Manager: "wolfssl" by wolfSSL Inc.)
// =============================================================================
#include "victron_mppt.h"
#include "config.h"

// wolfssl AES-CTR  –  requires wolfssl library installed in Arduino IDE
#include "wolfssl.h"
#include "wolfssl/wolfcrypt/aes.h"

// Fail at compile time if wolfssl is not configured for AES-CTR/AES-128.
// If you hit this: open Arduino Library Manager, remove wolfssl, re-install
// "wolfssl" by wolfSSL Inc. (the default build enables AES-CTR).
#ifdef NO_AES
  #error "wolfssl: AES is disabled (NO_AES). Reinstall wolfssl via Library Manager."
#endif

// ---------------------------------------------------------------------------
//  String lookup helpers
// ---------------------------------------------------------------------------
const char* victronStateStr(uint8_t s) {
    switch (s) {
        case 0: return "Off";
        case 1: return "Low Pwr";
        case 2: return "Fault";
        case 3: return "Bulk";
        case 4: return "Absorb";
        case 5: return "Float";
        case 6: return "Storage";
        case 7: return "Eq Man";
        default: return "Unknown";
    }
}

const char* victronErrorStr(uint8_t e) {
    switch (e) {
        case 0:  return "None";
        case 1:  return "Batt HiTemp";
        case 2:  return "Batt OV";
        case 3:  return "Batt UV";
        case 4:  return "Batt OC";
        case 5:  return "Batt RevPol";
        case 6:  return "Term HiTemp";
        case 7:  return "MPPT HiTemp";
        case 11: return "Batt LoTemp";
        case 14: return "Low Batt";
        case 17: return "Overcharged";
        case 20: return "Bulk > 10 hr";
        case 21: return "Curr sensor";
        case 26: return "Term err";
        case 28: return "Power stage";
        case 33: return "Input OC";
        case 34: return "Input OV";
        case 38: return "Input Shdn";
        case 39: return "Input Shdn";
        case 65: return "Comm warn";
        case 66: return "Conn mode";
        case 67: return "BMS error";
        case 68: return "Net miscfg";
        default: return "Err";
    }
}

// ---------------------------------------------------------------------------
//  BLE advertisement callback
//  Accepts ANY device with Victron company ID (0x02E1).
//  No MAC filter here — we store all found Victron devices so the main loop
//  can publish them to victron/scan/<mac> for remote debugging.
//  The target MAC filter is applied in tick() when choosing which entry to decrypt.
// ---------------------------------------------------------------------------
void VictronAdvCallback::onResult(BLEAdvertisedDevice advertisedDevice) {
    if (_count >= VICTRON_SCAN_MAX) return;  // buffer full
    if (!advertisedDevice.haveManufacturerData()) return;

    String mfr = advertisedDevice.getManufacturerData();
    if ((int)mfr.length() < 2) return;

    // Only keep Victron company ID: [0]=0xE1, [1]=0x02
    if ((uint8_t)mfr[0] != 0xE1 || (uint8_t)mfr[1] != 0x02) return;

    VictronScanResult& r = _results[_count];

    // Store MAC
    String addr = advertisedDevice.getAddress().toString();
    addr.toLowerCase();
    strncpy(r.mac, addr.c_str(), sizeof(r.mac) - 1);
    r.mac[sizeof(r.mac) - 1] = '\0';

    // Store raw bytes
    r.rawLen = (uint8_t)min((unsigned int)26, (unsigned int)mfr.length());
    memcpy(r.raw, mfr.c_str(), r.rawLen);

    // Build printable hex string
    r.raw_hex[0] = '\0';
    for (int i = 0; i < r.rawLen; i++) {
        char tmp[4];
        snprintf(tmp, sizeof(tmp), "%02X ", r.raw[i]);
        strncat(r.raw_hex, tmp, sizeof(r.raw_hex) - strlen(r.raw_hex) - 1);
    }

    Serial.printf("[Victron] Found device [%d]: MAC=%s  len=%d  bytes=%s\n",
                  _count, r.mac, r.rawLen, r.raw_hex);
    _count++;
}

// ---------------------------------------------------------------------------
//  VictronMPPT – begin
// ---------------------------------------------------------------------------
void VictronMPPT::begin() {
    _pScan = BLEDevice::getScan();
    _pScan->setAdvertisedDeviceCallbacks(&_cb, false);
    _pScan->setActiveScan(true);   // active scan – sends scan-request, needed for some Victron devices
    _pScan->setInterval(100);
    _pScan->setWindow(60);
    Serial.println("[Victron] BLE scan ready (active mode, all Victron company-ID devices).");
}

// ---------------------------------------------------------------------------
//  VictronMPPT – tick  (call each loop iteration)
// ---------------------------------------------------------------------------
bool VictronMPPT::tick() {
    unsigned long now = millis();

    if (!_scanning) {
        _cb.reset();
        _pScan->clearResults();
        _pScan->start(0, nullptr, false);
        _scanning    = true;
        _scanStartMs = now;
        return false;
    }

    // Wait for scan window to close
    if (now - _scanStartMs < SCAN_DURATION_MS) return false;

    // Scan window done – stop and process results
    _pScan->stop();
    _scanning = false;

    bool decoded = false;
    String target = VICTRON_MPPT_ADDRESS;
    target.toLowerCase();

    for (uint8_t i = 0; i < _cb.count(); i++) {
        const VictronScanResult& r = _cb.result(i);

        // Store raw_hex into _data so it appears in MQTT even before decryption
        strncpy(_data.raw_hex, r.raw_hex, sizeof(_data.raw_hex) - 1);

        if (String(r.mac) == target) {
            // This is our target device – attempt decryption.
            // Need at least 11 bytes (IV at [7-8], first cipher byte at [10]).
            // raw[26] is zero-initialised so bytes beyond rawLen are already 0.
            uint8_t output[16] = {0};
            if (_decrypt(r.raw, r.rawLen, output)) {
                _decode(output);
                decoded = true;
            } else {
                strncpy(_data.dec_hex, "DECRYPT_FAILED", sizeof(_data.dec_hex) - 1);
            }
        }
    }

    if (_cb.count() == 0) {
        Serial.println("[Victron] Scan window: no Victron company-ID devices found.");
        // Age-out after 30 s
        if (_data.valid && (now - _data.last_update_ms) > 30000UL) {
            _data.valid = false;
        }
    } else if (!decoded) {
        Serial.printf("[Victron] Scan found %d Victron device(s) but target MAC %s not matched.\n",
                      _cb.count(), VICTRON_MPPT_ADDRESS);
    }

    return decoded;
}

// ---------------------------------------------------------------------------
//  AES-128-CTR decryption using wolfssl  (same approach as reference VSC.cpp)
//
//  Packet byte offsets (0-indexed in the raw manufacturer data):
//    raw[7]      = IV low byte  (LSB)
//    raw[8]      = IV high byte (MSB)
//    raw[10..25] = 16 encrypted bytes
// ---------------------------------------------------------------------------
bool VictronMPPT::_decrypt(const uint8_t* raw, uint8_t len, uint8_t output[16]) {
    // Always build the raw_hex debug string regardless of success/failure
    _data.raw_hex[0] = '\0';
    for (int i = 0; i < (int)len && i < 26; i++) {
        char tmp[4];
        snprintf(tmp, sizeof(tmp), "%02X ", raw[i]);
        strncat(_data.raw_hex, tmp, sizeof(_data.raw_hex) - strlen(_data.raw_hex) - 1);
    }

    // Need at least byte[10] for 1 cipher byte; IV is at [7-8].
    // raw[] is declared as uint8_t raw[26]={0} in VictronScanResult, so bytes
    // beyond rawLen are already zero — no need to require a full 26 bytes.
    if (len < 11) {
        Serial.printf("[Victron] Packet too short: %d bytes (need >=11)\n", len);
        strncpy(_data.dec_hex, "TOO_SHORT", sizeof(_data.dec_hex));
        return false;
    }

    // IV: bytes 7 (LSB) and 8 (MSB), zero-padded to 16 bytes
    uint8_t iv[16]     = {0};
    iv[0] = raw[7];   // LSB
    iv[1] = raw[8];   // MSB

    // Cipher: 16 bytes starting at offset 10
    uint8_t cipher[16] = {0};
    memcpy(cipher, raw + 10, 16);

    Serial.printf("[Victron] IV: %02X %02X  Cipher[0..3]: %02X %02X %02X %02X\n",
                  iv[0], iv[1], cipher[0], cipher[1], cipher[2], cipher[3]);

    // wolfssl AES-CTR decrypt  (encrypt == decrypt in CTR mode)
    Aes aesDec;
    memset(&aesDec, 0, sizeof(Aes));
    wc_AesInit(&aesDec, NULL, INVALID_DEVID);
    wc_AesSetKey(&aesDec, VICTRON_MPPT_KEY, 16, iv, AES_ENCRYPTION);
    wc_AesCtrEncrypt(&aesDec, output, cipher, 16);
    wc_AesFree(&aesDec);

    // Build dec_hex debug string
    _data.dec_hex[0] = '\0';
    for (int i = 0; i < 16; i++) {
        char tmp[4];
        snprintf(tmp, sizeof(tmp), "%02X ", output[i]);
        strncat(_data.dec_hex, tmp, sizeof(_data.dec_hex) - strlen(_data.dec_hex) - 1);
    }

    Serial.printf("[Victron] Decrypted: %s\n", _data.dec_hex);
    return true;
}

// ---------------------------------------------------------------------------
//  Decode the 16 decrypted bytes into VictronData fields.
//  Encoding follows Victron Extra Manufacturer Data spec:
//    - Multi-byte values are little-endian
//    - Signed values use sign-magnitude encoding:
//        bit15 = sign, bits[14:0] = magnitude
//        negative: result = magnitude - 32768
//    - Sentinels: 0x7FFF = N/A (signed fields), 0xFFFF = N/A (unsigned)
// ---------------------------------------------------------------------------
void VictronMPPT::_decode(const uint8_t out[16]) {
    _data.device_state  = out[0];
    _data.charger_error = out[1];

    // Battery voltage: sign-magnitude 16-bit LE, units 10 mV -> V
    {
        bool    neg  = (out[3] & 0x80) >> 7;
        int16_t val  = (int16_t)(((out[3] & 0x7F) << 8) | out[2]);
        if (val == 0x7FFF) { _data.batt_v = 0.0f; }
        else {
            if (neg) val = val - 32768;
            _data.batt_v = val / 100.0f;
        }
    }

    // Battery current: sign-magnitude 16-bit LE, units 100 mA -> A
    {
        bool    neg  = (out[5] & 0x80) >> 7;
        int16_t val  = (int16_t)(((out[5] & 0x7F) << 8) | out[4]);
        if (val == 0x7FFF) { _data.batt_a = 0.0f; }
        else {
            if (neg) val = val - 32768;
            _data.batt_a = val / 10.0f;   // 100 mA per unit -> A
        }
    }

    // Yield today: uint16 LE (low byte first), units 10 Wh -> kWh
    {
        uint16_t val = (uint16_t)(out[6] | ((uint16_t)out[7] << 8));
        _data.yield_today = (val == 0xFFFF) ? 0.0f : val / 100.0f;
    }

    // PV power: uint16 LE (low byte first), units 1 W
    {
        uint16_t val = (uint16_t)(out[8] | ((uint16_t)out[9] << 8));
        _data.pv_w = (val == 0xFFFF) ? 0.0f : (float)val;
    }

    _data.valid          = true;
    _data.last_update_ms = millis();

    Serial.printf("[Victron] Decoded -> State:%s(%d) Err:%s(%d) "
                  "BattV:%.2fV BattA:%.2fA PV:%.0fW Yield:%.3fkWh\n",
                  victronStateStr(_data.device_state),  _data.device_state,
                  victronErrorStr(_data.charger_error), _data.charger_error,
                  _data.batt_v, _data.batt_a, _data.pv_w, _data.yield_today);
}
