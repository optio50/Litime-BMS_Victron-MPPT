// =============================================================================
//  config_server.cpp  –  Browser-based settings editor implementation
// =============================================================================
//  All routes are registered on the WebServer the OtaManager already runs
//  (port 80), so /update and /config live side by side on one server.
//  See config_server.h for the route list.
// =============================================================================
#include "config_server.h"
#include "app_config.h"
#include <WiFi.h>

// ---------------------------------------------------------------------------
//  Route registration
// ---------------------------------------------------------------------------
void ConfigServer::begin(WebServer& server) {
    _srv = &server;

    _srv->on("/config",        HTTP_GET,  [this]() { _handlePage();    });
    _srv->on("/config/save",   HTTP_POST, [this]() { _handleSave();    });
    _srv->on("/config/import", HTTP_POST, [this]() { _handleImport(); });
    _srv->on("/config.ini",    HTTP_GET,  [this]() { _handleIni();    });
    _srv->on("/config.json",   HTTP_GET,  [this]() { _handleJson();   });
    _srv->on("/config/wifi-scan", HTTP_GET,  [this]() { _handleWifiScan(); });
    _srv->on("/config/wifi-test", HTTP_POST, [this]() { _handleWifiTest(); });

    // Captive-portal behaviour while the litime-setup fallback AP is active:
    // any random URL the phone's browser probes lands on the config page.
    _srv->onNotFound([this]() { _handleNotFound(); });

    Serial.println("[CFG] Config page registered at /config");
}

// ---------------------------------------------------------------------------
bool ConfigServer::consumeRebootRequest() {
    bool r = _rebootRequested;
    _rebootRequested = false;
    return r;
}

// ---------------------------------------------------------------------------
//  Auth — same Basic auth (admin / ota_password) as the /update page.
//  Empty ota_password = no auth at all.
// ---------------------------------------------------------------------------
bool ConfigServer::_authOk() {
    if (appCfg.ota_password.length() == 0) return true;
    if (_srv->authenticate("admin", appCfg.ota_password.c_str())) return true;
    _srv->requestAuthentication();
    return false;
}

// ---------------------------------------------------------------------------
//  HTML helpers
// ---------------------------------------------------------------------------
static String jsonEscape(const String& in) {
    String out;
    out.reserve(in.length());
    for (size_t i = 0; i < in.length(); i++) {
        char c = in.charAt(i);
        if      (c == '"')  out += "\\\"";
        else if (c == '\\') out += "\\\\";
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else                out += c;
    }
    return out;
}

static String htmlEscape(const String& in) {
    String out;
    out.reserve(in.length());
    for (size_t i = 0; i < in.length(); i++) {
        char c = in.charAt(i);
        if      (c == '&')  out += "&amp;";
        else if (c == '"')  out += "&quot;";
        else if (c == '<')  out += "&lt;";
        else if (c == '>')  out += "&gt;";
        else                out += c;
    }
    return out;
}

String ConfigServer::_pageHead() {
    String s;
    s = "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>LiTime Monitor &mdash; Config</title><style>"
        "body{font-family:system-ui,Segoe UI,sans-serif;background:#0e0e10;"
        "color:#e6e6e6;margin:0;padding:14px;max-width:640px}"
        "h1{font-size:1.25em;margin:4px 0 2px}"
        "h2{font-size:1.05em;color:#31c7c7;border-bottom:1px solid #333;"
        "padding-bottom:4px;margin:30px 0 4px}"
        "label{display:block;font-weight:600;margin:12px 0 1px}"
        "small{color:#9a9a9a}"
        "input,select,textarea{width:100%;box-sizing:border-box;padding:8px;"
        "border-radius:6px;border:1px solid #444;background:#1b1b1e;"
        "color:#e6e6e6;font-size:15px}"
        "textarea{font-family:ui-monospace,monospace}"
        "button{margin:10px 6px 0 0;padding:9px 16px;border-radius:6px;"
        "border:0;background:#31c7c7;color:#000;font-weight:700;cursor:pointer}"
        "button.ghost{background:#333;color:#e6e6e6}"
        "a{color:#31c7c7}"
        ".note{color:#9a9a9a;font-size:.9em}"
        "#msg{margin-top:10px;font-weight:600;white-space:pre-wrap}"
        "</style></head><body>"
        "<h1>LiTime Dual BMS Monitor</h1>"
        "<p class=note>Save writes config.ini to flash and reboots the device "
        "(~15 s). If it can't reach WiFi afterwards, join the <b>litime-setup</b> "
        "access point and open <b>192.168.4.1/config</b>.</p>";
    return s;
}

String ConfigServer::_fieldRow(const char* key, const char* label, const char* desc,
                                const String& value, bool numeric, bool secret) {
    String s;
    s += "<label for='" + String(key) + "'>" + htmlEscape(label) + "</label>";
    s += "<small>" + htmlEscape(desc) + "</small>";
    s += "<input id='" + String(key) + "' name='" + String(key) + "' value='" +
         htmlEscape(value) + "' autocomplete=off";
    if (numeric) s += " type=number inputmode=numeric";
    else if (secret) s += " type=password autocomplete=new-password";
    else s += " autocomplete=off";
    s += ">";
    return s;
}

String ConfigServer::_pageTail() {
    String s;
    s += "<button type=submit>Save & Reboot</button></form>";

    s += "<h2>Import config.ini</h2>";
    s += "<p class=note>Paste a whole config.ini here. This replaces every setting, writes flash, and reboots. Download the current file at <a href=/config.ini>/config.ini</a>.</p>";
    s += "<form method=POST action=/config/import autocomplete=off>";
    s += "<textarea name=ini rows=14 placeholder='[wifi]&#10;ssid = MyNetwork&#10;password = secret'></textarea>";
    s += "<button type=submit>Import & Reboot</button></form>";

    s += "<script>"
        "function el(id){return document.getElementById(id)}"
        "function scan(){fetch('/config/wifi-scan').then(function(r){return r.json()}).then(function(l){"
        "var s=el('nets');if(!s)return;s.innerHTML='';"
        "var ph=document.createElement('option');ph.value='';ph.textContent='-- pick a network --';"
        "s.appendChild(ph);"
        "l.forEach(function(n){var o=document.createElement('option');"
        "o.value=n.ssid;o.textContent=n.ssid+' ('+n.rssi+' dBm'+(n.secure?', secure':'')+')';"
        "s.appendChild(o)});"
        "var m=el('scanmsg');if(m)m.textContent=l.length+' networks';"
        "}).catch(function(){var m=el('scanmsg');if(m)m.textContent='scan failed'})}"
        "function chosen(){var n=el('nets');return n&&n.value?n.value:''}"
        "function test(){var ss=chosen();var pwEl=el('wifi_password');var pw=pwEl?pwEl.value:'';"
        "if(!ss){el('msg').textContent='Pick a network from Scan first';return}"
        "var b='wifi_ssid='+encodeURIComponent(ss)+'&wifi_password='+encodeURIComponent(pw);"
        "el('msg').textContent='testing (up to 15 s)...';"
        "fetch('/config/wifi-test',{method:'POST',"
        "headers:{'Content-Type':'application/x-www-form-urlencoded'},body:b})"
        ".then(function(r){return r.json()}).then(function(j){"
        "el('msg').textContent=j.ok?('WiFi OK: '+j.detail):('WiFi FAILED: '+j.detail)})"
        ".catch(function(){el('msg').textContent='test failed'})}"
        "</script></body></html>";
    return s;
}

// ---------------------------------------------------------------------------
//  GET /config — the settings form
// ---------------------------------------------------------------------------
void ConfigServer::_handlePage() {
    if (!_authOk()) return;

    String page = _pageHead();
    page += "<form method=POST action=/config/save autocomplete=off>";

    size_t count = 0;
    const CfgFieldInfo* fields = appConfigFields(count);
    String lastSection = "";
    for (size_t i = 0; i < count; i++) {
        const CfgFieldInfo& f = fields[i];
        if (String(f.section) != lastSection) {
            lastSection = f.section;
            page += "<h2>" + String(f.section) + "</h2>";
        }
        // During setup AP, don't render a visible SSID text box - phones
        // autofill the saved WiFi password into it. Scan writes a hidden
        if (g_apFallbackActive && String(f.key) == "wifi_ssid") {
            continue;  // SSID comes from the Scan dropdown (name=wifi_ssid)
        }
        page += _fieldRow(f.key, f.label, f.desc,
                      appConfigFieldValue(f.key), f.numeric, f.secret);
        if (String(f.key) == "wifi_password") {
            String nextUrl = g_apFallbackActive
                ? String("http://") + appCfg.hostname + ".local/config"
                : String("http://") + WiFi.localIP().toString() + "/config";
            page += "<p class=note><b>Tip:</b> Only WiFi SSID and password are required now. After saving and rebooting, finish the rest (MQTT broker, battery MACs, Victron settings) from any device at <code>";
            page += htmlEscape(nextUrl);
            page += "</code>.</p>";
            page += "<button class=ghost type=button onclick=\"scan()\">Scan networks</button>";
            page += "<select id=nets name=wifi_ssid style='width:100%;margin-top:8px'>"
                    "<option value=''>-- scan, then pick a network --</option></select> ";
            page += "<span id=scanmsg class=note></span>";
            page += "<br><button class=ghost type=button onclick=\"test()\">Test WiFi</button>";
            // Same form submit as the button at the bottom of the page.
            page += "<button type=submit>Save & Reboot</button>";
            page += "<div id=msg></div>";
        }
    }
    page += _pageTail();

    _srv->send(200, "text/html", page);
}

// ---------------------------------------------------------------------------
//  POST /config/save — validate every submitted field, write flash, reboot
// ---------------------------------------------------------------------------
void ConfigServer::_handleSave() {
    if (!_authOk()) return;

    AppConfig backup = appCfg;
    String errors = "";

    for (int i = 0; i < _srv->args(); i++) {
        String name = _srv->argName(i);
        String val  = _srv->arg(i);
        String ferr;
        appConfigSetField(appCfg, name, val, ferr);   // unknown names ignored
        if (!ferr.isEmpty())
            errors += (errors.length() ? "\n" : "") + ferr;
    }

    if (errors.length()) {
        appCfg = backup;   // nothing changed
        _srv->send(400, "text/plain",
                   "NOT saved - fix these and try again:\n" + errors +
                   "\n(Use your browser's Back button.)");
        return;
    }

    Serial.printf("[CFG] Save: ssid=\"%s\" pass_len=%u broker=\"%s\"\n",
                  appCfg.wifi_ssid.c_str(),
                  (unsigned)appCfg.wifi_password.length(),
                  appCfg.mqtt_broker.c_str());

    if (g_apFallbackActive && appCfg.wifi_ssid.length() == 0) {
        appCfg = backup;
        _srv->send(400, "text/plain",
                   "NOT saved - WiFi network is empty. Pick a network from Scan "
                   "(or type the SSID), then Save again.");
        return;
    }
    if (g_apFallbackActive && appCfg.wifi_password.length() == 0) {
        appCfg = backup;
        _srv->send(400, "text/plain",
                   "NOT saved - WiFi password is empty. Type it in the WiFi password "
                   "box (not the network box), then Save again.");
        return;
    }

    if (!appConfigSave()) {
        appCfg = backup;
        _srv->send(500, "text/plain",
                   "NOT saved - flash write failed. Try again or re-upload firmware.");
        return;
    }

    _rebootRequested = true;
    _srv->send(200, "text/html",
               _resultPage("Saved", "config.ini written to flash. Rebooting now - "
                           "the page reloads when the device is back (~15 s).",
                           true));
}

// ---------------------------------------------------------------------------
//  POST /config/import — paste a whole config.ini
// ---------------------------------------------------------------------------
void ConfigServer::_handleImport() {
    if (!_authOk()) return;

    String ini = _srv->arg("ini");
    String err;
    if (!appConfigApplyIni(ini, err, /*strict=*/true)) {
        _srv->send(400, "text/plain",
                   "NOT imported: " + err +
                   "\n(Use your browser's Back button.)");
        return;
    }

    if (!appConfigSave()) {
        _srv->send(500, "text/plain", "NOT saved - flash write failed.");
        return;
    }

    _rebootRequested = true;
    _srv->send(200, "text/html",
               _resultPage("Imported", "config.ini written to flash. Rebooting now - "
                           "the page reloads when the device is back (~15 s).",
                           true));
}

// ---------------------------------------------------------------------------
//  GET /config.ini — current settings as INI (same format Import accepts)
// ---------------------------------------------------------------------------
void ConfigServer::_handleIni() {
    if (!_authOk()) return;
    _srv->send(200, "text/plain; charset=utf-8", appConfigToIni());
}

// ---------------------------------------------------------------------------
//  GET /config.json — current settings as JSON (shared_config.py fetches this)
// ---------------------------------------------------------------------------
void ConfigServer::_handleJson() {
    if (!_authOk()) return;
    _srv->send(200, "application/json", appConfigToJsonForDashboard());
}

// ---------------------------------------------------------------------------
//  GET /config/wifi-scan — nearby networks, strongest first, deduped
//  (WiFi.scanNetworks() blocks ~2-3 s; harmless for this sync server)
// ---------------------------------------------------------------------------
void ConfigServer::_handleWifiScan() {
    if (!_authOk()) return;

    int n = WiFi.scanNetworks(/*async=*/false, /*hidden=*/false);

    struct Net { String ssid; int32_t rssi; bool secure; };
    Net nets[32];
    int count = 0;
    for (int i = 0; i < n && count < 32; i++) {
        String ssid = WiFi.SSID(i);
        int32_t rssi = WiFi.RSSI(i);
        bool secure = (WiFi.encryptionType(i) != WIFI_AUTH_OPEN);
        bool dup = false;
        int dupIdx = -1;
        for (int k = 0; k < count; k++) {
            if (nets[k].ssid == ssid) { dup = true; dupIdx = k; break; }
        }
        if (dup) {
            if (rssi > nets[dupIdx].rssi) nets[dupIdx] = {ssid, rssi, secure};
            continue;
        }
        nets[count++] = {ssid, rssi, secure};
    }
    for (int i = 0; i < count; i++)
        for (int j = i + 1; j < count; j++)
            if (nets[j].rssi > nets[i].rssi) { Net t = nets[i]; nets[i] = nets[j]; nets[j] = t; }

    String json = "[";
    for (int i = 0; i < count; i++) {
        if (i) json += ",";
        json += "{\"ssid\":\"" + jsonEscape(nets[i].ssid) + "\",\"rssi\":" + String(nets[i].rssi) +
                ",\"secure\":" + String(nets[i].secure ? "true" : "false") + "}";
    }
    json += "]";
    WiFi.scanDelete();
    _srv->send(200, "application/json", json);
}

// ---------------------------------------------------------------------------
//  POST /config/wifi-test — try credentials WITHOUT saving anything
//  Blocks up to 15 s (watchdog is fed inside appConfigTestWifi).
// ---------------------------------------------------------------------------
void ConfigServer::_handleWifiTest() {
    if (!_authOk()) return;

    String ssid = _srv->arg("wifi_ssid");
    if (!ssid.length()) ssid = _srv->arg("ssid");
    String pass = _srv->arg("wifi_password");
    if (!pass.length()) pass = _srv->arg("password");
    Serial.printf("[CFG] WiFi test request: ssid=\"%s\" pass_len=%u\n",
                  ssid.c_str(), (unsigned)pass.length());
    String detail;
    bool ok = appConfigTestWifi(ssid, pass, detail);

    String json = "{\"ok\":" + String(ok ? "true" : "false") +
                  ",\"detail\":\"" + jsonEscape(detail) + "\"}";
    _srv->send(200, "application/json", json);
}

// ---------------------------------------------------------------------------
//  Not-found — captive portal while the setup AP is up.
//  Phone probes (generate_204, hotspot-detect, etc.) get a tiny redirect,
//  NOT the full config page. Building that page for every probe was what
//  panicked the ESP the instant a phone joined.
// ---------------------------------------------------------------------------
void ConfigServer::_handleNotFound() {
    if (!g_apFallbackActive) {
        _srv->send(404, "text/plain", "404");
        return;
    }

    String uri = _srv->uri();
    uri.toLowerCase();
    bool probe = uri.indexOf("generate_204") >= 0 ||
                 uri.indexOf("gen_204") >= 0 ||
                 uri.indexOf("hotspot-detect") >= 0 ||
                 uri.indexOf("connecttest") >= 0 ||
                 uri.indexOf("ncsi") >= 0 ||
                 uri.indexOf("success.txt") >= 0 ||
                 uri.endsWith(".png") || uri.endsWith(".ico");
    if (probe) {
        // 200 + tiny HTML is what Android/iOS treat as "sign-in required"
        // and then open. A 302 storm of the full page is what crashed us.
        _srv->send(200, "text/html",
                   "<html><head><meta http-equiv='refresh' content='0;url=http://192.168.4.1/config'></head>"
                   "<body><a href='http://192.168.4.1/config'>WiFi setup</a></body></html>");
        return;
    }
    _srv->sendHeader("Location", "http://192.168.4.1/config", true);
    _srv->send(302, "text/plain", "http://192.168.4.1/config");
}

// ---------------------------------------------------------------------------
//  Small result page (Save / Import confirmation). Auto-reloads /config
//  after 20 s so the user lands back on the form once the device is back.
// ---------------------------------------------------------------------------
String ConfigServer::_resultPage(const char* title, const char* msg, bool rebooting) {
    String s = _pageHead();
    s += "<h2>" + String(title) + "</h2><p>" + String(msg) + "</p>";
    if (rebooting)
        s += "<meta http-equiv='refresh' content='20;url=/config'>";
    s += "<p class=note><a href=/config>Back to config</a></p></body></html>";
    return s;
}
