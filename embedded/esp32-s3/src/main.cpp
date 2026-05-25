/*
 * Solectrac CAN monitor for ESP32
 *
 * Reads J1939 CAN frames from the Solectrac 25G tractor (250 kbit/s) via the
 * ESP32's built-in TWAI peripheral, decodes all known signals, and serves
 * them as JSON over a simple HTTP endpoint.
 *
 * Hardware:
 *   Connect a CAN transceiver (SN65HVD230, TJA1050, MCP2551) between the
 *   ESP32 and the CAN bus. Adjust CAN_TX_PIN and CAN_RX_PIN below to match
 *   your wiring.
 *
 * Endpoints:
 *   GET /       — mobile-friendly dashboard (auto-refreshing)
 *   GET /json   — raw JSON
 */

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include "driver/twai.h"

// ── Configuration ─────────────────────────────────────────────────────────────

#define CAN_TX_PIN GPIO_NUM_8
#define CAN_RX_PIN GPIO_NUM_14

// WiFi runs as a soft-AP only — the board broadcasts its own hotspot and
// clients (phone, laptop) join it directly. AP IP is 192.168.4.1.
#define AP_SSID "solectrac"
#define AP_PASS "electricity"

// ── J1939 source addresses ────────────────────────────────────────────────────

#define SRC_BMS         0xF3   // BMS broadcast
#define SRC_BMS_CHGR_IF 0xF4   // BMS charger-interface role (sends 0x000600)
#define SRC_CHARGER     0xE5   // External charger
#define SRC_VEHICLE     0xD0   // Vehicle controller
#define SRC_MOTOR       0xCA   // Motor / drive ECU
#define SRC_DASH        0x12   // Dashboard heartbeat

// ── PGN constants ─────────────────────────────────────────────────────────────

#define PGN_CELL_FIRST  0xF113   // BMS cell voltage frames (4 cells each)
#define PGN_CELL_LAST   0xF13C
#define PGN_TEMP_FIRST  0xF155   // BMS module temp frames (8 temps each)
#define PGN_TEMP_LAST   0xF15E
#define PGN_F100        0xF100   // Pack status: V, I, SoC
#define PGN_F102        0xF102   // Cell min/max summary
#define PGN_F104        0xF104   // Temp min/max summary
#define PGN_F106        0xF106   // BMS state flags
#define PGN_F107        0xF107   // BMS current limits
#define PGN_F108        0xF108   // BMS active fault bitmap
#define PGN_FF50        0xFF50   // Charger telemetry
#define PGN_FF21        0xFF21   // Motor telemetry / dash heartbeat
#define PGN_FECA        0xFECA   // DM1 (Active Diagnostic Trouble Codes)
#define PGN_PROP_0600   0x0600   // BMS→charger command (PDU1, dest 0xE5)

// ── Decode constants ──────────────────────────────────────────────────────────

#define NUM_CELLS               20
#define NUM_TEMPS               7
#define TEMP_OFFSET_C           40
#define PACK_CURRENT_BIAS_RAW   0x7D00   // raw u16 value at 0 A
#define PACK_CURRENT_LSB_A      0.1f     // A per bit
#define PACK_VOLTAGE_LSB_V      0.1f     // V per bit
#define PACK_VOLTAGE_OFFSET_V   76.8f    // V offset (shared by BMS and charger)
#define RPM_BIAS                0x0C80   // raw u16 value at 0 RPM
#define LIMIT_CURRENT_LSB_A     0.01f    // A per bit for F107 limits
#define CHARGER_I_LSB_A         0.1f     // A per bit for charger current
#define PACK_CAPACITY_WH        25000.0f // nominal usable pack energy (Solectrac e25 spec)

// ── BMS fault code tables ─────────────────────────────────────────────────────
// Bytes 0–6: each element maps one bit (LSB first) to a vendor fault code.
// 0 = silent (no code on dashboard for this bit).
// Based on injection sweep 2026-05-10.

static const uint8_t FAULT_BYTES_0_TO_6[7][8] = {
    {100, 100, 101, 101, 102, 102, 103, 103},   // byte 0
    {104, 104, 105, 105, 106, 106, 107, 107},   // byte 1
    {108, 108, 109, 109, 110, 110, 111, 111},   // byte 2
    {112, 112, 113, 113,   0,   0,   0,   0},   // byte 3 (114/115 reserved)
    {116, 117, 118, 119, 120, 121, 122, 123},   // byte 4
    {124, 125, 126, 127,   0,   0,   0,   0},   // byte 5
    {  0,   0,   0,   0,   0,   0,   0,   0},   // byte 6 (all silent)
};

// Byte 7: bit → code. 0 = silent. Bit 5 and bit 6 both map to 144 (confirmed
// duplicate). Code 146 does NOT appear in F108.
static const uint8_t FAULT_BYTE7[8] = {140, 0, 0, 142, 143, 144, 144, 145};

// ── State structs ─────────────────────────────────────────────────────────────

struct PackState {
    float   voltage_v  = NAN;
    float   current_a  = NAN;
    int32_t current_raw = -1;
    float   power_w    = NAN;
    uint8_t soc_raw    = 0;
    float   soc_pct    = NAN;
    // F102
    int16_t cell_max_mv   = -1;
    int16_t cell_min_mv   = -1;
    int16_t cell_spread_mv = -1;
    uint8_t cell_max_n    = 0;
    uint8_t cell_min_n    = 0;
    int16_t cell_spread_mv_reported = -1;
    float   v_estimate    = NAN;
    // F104
    int8_t  temp_max_c   = INT8_MIN;
    int8_t  temp_min_c   = INT8_MIN;
    uint8_t temp_max_n   = 0;
    uint8_t temp_min_n   = 0;
    int8_t  temp_spread_c = -1;
};

struct BmsStateFlags {
    uint8_t byte0 = 0, byte1 = 0;
    bool output_enable   : 1;
    bool main_contactor  : 1;
    bool operating       : 1;
    bool standby         : 1;
    bool charging        : 1;
    bool charger_present : 1;
    bool drive_mode      : 1;
    bool awake           : 1;
    bool valid           : 1;
    BmsStateFlags() : output_enable(false), main_contactor(false),
        operating(false), standby(false), charging(false),
        charger_present(false), drive_mode(false), awake(false),
        valid(false) {}
};

struct BmsLimits {
    float   discharge_a = NAN;
    float   charge_a    = NAN;
    uint8_t mode        = 0;
    uint8_t byte5       = 0;
    bool    valid       = false;
};

struct BmsFaults {
    uint8_t  bytes[8]          = {};
    uint64_t active_codes_mask = 0;  // bit (code-100) set if code active
    bool     any_fault         = false;
};

struct MotorState {
    int16_t  rpm_signed    = 0;
    uint16_t rpm_magnitude = 0;
    int8_t   direction     = 0;
    uint8_t  range_gear    = 1;
    uint8_t  throttle_raw  = 0;
    int8_t   controller_temp_c = INT8_MIN;
    int8_t   motor_temp_c      = INT8_MIN;
    bool     valid             = false;
};

struct ChargerState {
    uint8_t  status   = 0;
    uint16_t v_raw    = 0;
    uint16_t i_raw    = 0;
    uint8_t  flags    = 0;
    float    voltage_v = NAN;
    float    current_a = NAN;
    bool output_disabled : 1;
    bool line_ok         : 1;
    bool no_line         : 1;
    bool valid           : 1;
    ChargerState() : output_disabled(false), line_ok(false),
        no_line(false), valid(false) {}
};

struct ChgrCmdState {
    float    voltage_v = NAN;
    float    current_a = NAN;
    uint8_t  enable    = 1;
    uint16_t v_raw     = 0;
    uint16_t i_raw     = 0;
    bool     valid     = false;
};

struct Dm1State {
    uint8_t  lamp_byte0 = 0, lamp_byte1 = 0;
    uint32_t dtc_spn    = 0;
    uint8_t  dtc_fmi    = 0, dtc_cm = 0, dtc_oc = 0;
    bool     valid      = false;
};

// ── Global state ──────────────────────────────────────────────────────────────
// All updated from the CAN decode path inside loop(), read when building JSON
// from the same thread — no locking needed.

float       g_cell_v[NUM_CELLS];
float       g_temp_c[NUM_TEMPS];
PackState   g_pack;
BmsStateFlags g_bms_state;
BmsLimits   g_bms_limit;
BmsFaults   g_bms_faults;
MotorState  g_motor;
ChargerState g_charger;
ChgrCmdState g_chgr_cmd;
uint8_t     g_vc_state   = 0xFF;   // 0xFF = never seen
uint8_t     g_dash_alive = 0xFF;
Dm1State    g_dm1;

// CAN bus health counters
uint32_t    g_frames_rx      = 0;   // total frames received
uint32_t    g_frames_decoded = 0;   // frames matching a known PGN/source
uint32_t    g_last_frame_ms  = 0;   // millis() at last received frame
bool        g_can_initialized = false;
bool        g_ap_running      = false;

// Session energy tracking (integrated power since boot)
uint32_t    g_session_last_ms   = 0;
uint32_t    g_session_active_ms = 0;   // sum of valid dt's — excludes bus-silent gaps
float       g_session_wh_drawn  = 0.0f;
float       g_session_wh_charged = 0.0f;

WebServer server(80);
DNSServer  dns_server;

// Dashboard HTML, embedded at build time via board_build.embed_txtfiles in
// platformio.ini. The linker generates these symbols from the file path:
// src/dashboard.html → _binary_src_dashboard_html_{start,end}. The data is
// null-terminated (embed_txtfiles), so the start pointer is usable as a
// C-string for server.send_P().
extern const uint8_t dashboard_html_start[] asm("_binary_src_dashboard_html_start");
extern const uint8_t dashboard_html_end[]   asm("_binary_src_dashboard_html_end");

// ── LED status indicator ──────────────────────────────────────────────────────
// Adafruit ESP32-S3 Reverse TFT Feather has a NeoPixel on GPIO 33 whose power
// rail is gated by GPIO 21 — both must be driven before any colour is visible.
//   Red blink     — CAN driver failed to initialize
//   Amber blink   — No Wi-Fi up at all (AP failed and STA not connected)
//   Dim white     — Alive, no CAN frames received recently
//   Green blink   — CAN frames arriving (toggles on bus activity)

#define LED_PIN          GPIO_NUM_33
#define LED_POWER_PIN    GPIO_NUM_21
#define LED_BLINK_MS     50
#define LED_ACTIVE_MS    200

static uint32_t g_led_last_toggle = 0;
static bool     g_led_on = false;

static inline void ledInit() {
    pinMode(LED_POWER_PIN, OUTPUT);
    digitalWrite(LED_POWER_PIN, HIGH);   // enable NeoPixel power rail
}

static inline void ledWrite(uint8_t r, uint8_t g, uint8_t b) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    rgbLedWrite(LED_PIN, r, g, b);
#else
    neopixelWrite(LED_PIN, r, g, b);
#endif
}

void updateLed() {
    uint32_t now = millis();
    bool toggle = (now - g_led_last_toggle) >= LED_BLINK_MS;

    if (!g_can_initialized) {
        if (toggle) { g_led_last_toggle = now; g_led_on = !g_led_on; }
        ledWrite(g_led_on ? 32 : 0, 0, 0);
        return;
    }
    if (!g_ap_running) {
        if (toggle) { g_led_last_toggle = now; g_led_on = !g_led_on; }
        ledWrite(g_led_on ? 24 : 0, g_led_on ? 12 : 0, 0);
        return;
    }
    bool active = (g_frames_rx > 0) && (now - g_last_frame_ms < LED_ACTIVE_MS);
    if (!active) {
        ledWrite(4, 4, 4);
        g_led_on = false;
        g_led_last_toggle = now;
        return;
    }
    if (toggle) {
        g_led_last_toggle = now;
        g_led_on = !g_led_on;
    }
    ledWrite(0, g_led_on ? 32 : 0, 0);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

static inline uint16_t be16(uint8_t hi, uint8_t lo) {
    return ((uint16_t)hi << 8) | lo;
}

static inline uint16_t le16(uint8_t lo, uint8_t hi) {
    return ((uint16_t)hi << 8) | lo;
}

static bool allZero(const uint8_t* d) {
    for (int i = 0; i < 8; i++) if (d[i]) return false;
    return true;
}

// ── CAN decoder ───────────────────────────────────────────────────────────────

void decodeCAN(uint32_t can_id, const uint8_t* raw, uint8_t len) {
    uint8_t d[8] = {};
    memcpy(d, raw, len < 8 ? len : 8);

    uint8_t  src = can_id & 0xFF;
    uint8_t  pf  = (can_id >> 16) & 0xFF;
    uint8_t  ps  = (can_id >> 8)  & 0xFF;
    uint16_t pgn = ((uint16_t)pf << 8) | (pf >= 0xF0 ? ps : 0);

    // Count any frame from a known source as decoded, before the per-PGN
    // decoders below — they often early-return on all-zero idle frames.
    if (src == SRC_BMS || src == SRC_BMS_CHGR_IF || src == SRC_CHARGER ||
        src == SRC_VEHICLE || src == SRC_MOTOR || src == SRC_DASH)
        g_frames_decoded++;

    if (src == SRC_BMS) {

        if (pgn >= PGN_CELL_FIRST && pgn <= PGN_CELL_LAST) {
            if (allZero(d)) return;
            int base = (pgn - PGN_CELL_FIRST) * 4;
            for (int slot = 0; slot < 4; slot++) {
                int idx = base + slot;
                if (idx >= NUM_CELLS) break;
                uint16_t mv = be16(d[2*slot], d[2*slot+1]);
                if (mv && mv != 0xFFFF)
                    g_cell_v[idx] = mv / 1000.0f;
            }

        } else if (pgn >= PGN_TEMP_FIRST && pgn <= PGN_TEMP_LAST) {
            if (allZero(d)) return;
            int base = (pgn - PGN_TEMP_FIRST) * 8;
            for (int slot = 0; slot < 8; slot++) {
                int idx = base + slot;
                if (idx >= NUM_TEMPS) break;
                if (d[slot] && d[slot] != 0xFF)
                    g_temp_c[idx] = (float)(d[slot] - TEMP_OFFSET_C);
            }

        } else if (pgn == PGN_F100) {
            if (allZero(d)) return;
            uint16_t raw_cur = be16(d[2], d[3]);
            // Sign convention: positive = charging, negative = discharging.
            float amps  = -((int32_t)raw_cur - PACK_CURRENT_BIAS_RAW) * PACK_CURRENT_LSB_A;
            float volts = d[1] * PACK_VOLTAGE_LSB_V + PACK_VOLTAGE_OFFSET_V;
            g_pack.voltage_v   = volts;
            g_pack.current_raw = raw_cur;
            g_pack.current_a   = amps;
            g_pack.power_w     = volts * amps;
            g_pack.soc_raw     = d[4];
            g_pack.soc_pct     = d[4] * 0.4f - 0.8f;

            // Integrate power into session energy counters
            uint32_t now = millis();
            if (g_session_last_ms != 0) {
                uint32_t dt_ms = now - g_session_last_ms;
                float dt_s = dt_ms / 1000.0f;
                if (dt_s > 0 && dt_s < 5.0f) {   // sanity: skip bus-silent gaps
                    g_session_active_ms += dt_ms;
                    // power_w: positive = charging, negative = discharging
                    float wh = g_pack.power_w * dt_s / 3600.0f;
                    if (wh > 0) g_session_wh_charged += wh;
                    else        g_session_wh_drawn   += -wh;
                }
            }
            g_session_last_ms = now;

        } else if (pgn == PGN_F102) {
            if (allZero(d)) return;
            uint16_t max_mv = be16(d[0], d[1]);
            uint16_t min_mv = be16(d[2], d[3]);
            if (!max_mv || !min_mv) return;
            g_pack.cell_max_mv             = max_mv;
            g_pack.cell_min_mv             = min_mv;
            g_pack.cell_spread_mv          = max_mv - min_mv;
            g_pack.cell_max_n              = d[4];
            g_pack.cell_min_n              = d[5];
            g_pack.cell_spread_mv_reported = d[7];
            g_pack.v_estimate = 20.0f * (max_mv + min_mv) / 2.0f / 1000.0f;

        } else if (pgn == PGN_F104) {
            if (allZero(d) || d[0] == 0xFF || d[1] == 0xFF) return;
            g_pack.temp_max_c   = (int8_t)(d[0] - TEMP_OFFSET_C);
            g_pack.temp_min_c   = (int8_t)(d[1] - TEMP_OFFSET_C);
            g_pack.temp_max_n   = d[2];
            g_pack.temp_min_n   = d[3];
            g_pack.temp_spread_c = (int8_t)d[4];

        } else if (pgn == PGN_F106) {
            if (allZero(d)) return;
            g_bms_state.byte0          = d[0];
            g_bms_state.byte1          = d[1];
            g_bms_state.output_enable  = (d[0] & 0x01) != 0;
            g_bms_state.main_contactor = (d[0] & 0x04) != 0;
            g_bms_state.operating      = (d[0] & 0x40) != 0;
            g_bms_state.standby        = (d[0] & 0x80) != 0;
            g_bms_state.charging       = (d[1] & 0x08) != 0;
            g_bms_state.charger_present= (d[1] & 0x04) != 0;
            g_bms_state.drive_mode     = (d[1] & 0x20) != 0;
            g_bms_state.awake          = (d[1] & 0x40) != 0;
            g_bms_state.valid          = true;

        } else if (pgn == PGN_F107) {
            if (allZero(d)) return;
            g_bms_limit.discharge_a = be16(d[0], d[1]) * LIMIT_CURRENT_LSB_A;
            g_bms_limit.charge_a    = be16(d[2], d[3]) * LIMIT_CURRENT_LSB_A;
            g_bms_limit.mode  = d[4];
            g_bms_limit.byte5 = d[5];
            g_bms_limit.valid = true;

        } else if (pgn == PGN_F108) {
            if (allZero(d)) return;
            memcpy(g_bms_faults.bytes, d, 8);
            g_bms_faults.any_fault         = true;
            g_bms_faults.active_codes_mask = 0;
            for (int bi = 0; bi < 7; bi++) {
                for (int bit = 0; bit < 8; bit++) {
                    uint8_t code = FAULT_BYTES_0_TO_6[bi][bit];
                    if (code && ((d[bi] >> bit) & 1))
                        g_bms_faults.active_codes_mask |= (1ULL << (code - 100));
                }
            }
            for (int bit = 0; bit < 8; bit++) {
                uint8_t code = FAULT_BYTE7[bit];
                if (code && ((d[7] >> bit) & 1))
                    g_bms_faults.active_codes_mask |= (1ULL << (code - 100));
            }
        }

    } else if (src == SRC_VEHICLE && pgn == PGN_F100) {
        g_vc_state = d[0];

    } else if (src == SRC_MOTOR && pgn == PGN_FF21) {
        uint16_t rpm_raw  = le16(d[2], d[3]);
        int      rpm_mag  = (int)rpm_raw - RPM_BIAS;
        uint8_t  fnr      = d[7] & 0x0F;
        int8_t   dir      = (fnr == 0x4) ? 1 : (fnr == 0x8) ? -1 : 0;
        g_motor.rpm_magnitude      = (uint16_t)rpm_mag;
        g_motor.rpm_signed         = dir * rpm_mag;
        g_motor.direction          = dir;
        g_motor.range_gear         = ((d[7] >> 4) & 0x0F) + 1;
        g_motor.throttle_raw       = d[0];
        if (d[4]) g_motor.controller_temp_c = (int8_t)(d[4] - TEMP_OFFSET_C);
        if (d[5]) g_motor.motor_temp_c      = (int8_t)(d[5] - TEMP_OFFSET_C);
        g_motor.valid = true;

    } else if (src == SRC_DASH && pgn == PGN_FF21) {
        g_dash_alive = d[0];

    } else if (src == SRC_MOTOR && pgn == PGN_FECA) {
        uint32_t spn = d[2] | ((uint32_t)d[3] << 8)
                       | (((uint32_t)(d[4] >> 5) & 0x07) << 16);
        uint8_t fmi = d[4] & 0x1F;
        bool active = (spn || fmi);
        if (!d[0] && !d[1] && !active) return;   // healthy idle
        g_dm1.lamp_byte0 = d[0];
        g_dm1.lamp_byte1 = d[1];
        g_dm1.dtc_spn    = spn;
        g_dm1.dtc_fmi    = fmi;
        g_dm1.dtc_cm     = (d[5] >> 7) & 0x01;
        g_dm1.dtc_oc     = d[5] & 0x7F;
        g_dm1.valid      = true;

    } else if (src == SRC_CHARGER && pgn == PGN_FF50) {
        if (allZero(d)) return;
        g_charger.status         = d[0];
        g_charger.v_raw          = le16(d[1], d[2]);
        g_charger.i_raw          = le16(d[3], d[4]);  // d[4] is flags; kept as legacy raw
        g_charger.flags          = d[4];
        g_charger.output_disabled= (d[4] & 0x04) != 0;
        g_charger.line_ok        = (d[4] & 0x08) != 0;
        g_charger.no_line        = (d[4] & 0x10) != 0;
        if (d[0] == 0x03 && d[4] == 0x00) {
            g_charger.voltage_v = g_charger.v_raw * PACK_VOLTAGE_LSB_V + PACK_VOLTAGE_OFFSET_V;
            g_charger.current_a = d[3] * CHARGER_I_LSB_A;
        } else {
            g_charger.voltage_v = NAN;
            g_charger.current_a = NAN;
        }
        g_charger.valid = true;

    } else if (src == SRC_BMS_CHGR_IF && pgn == PGN_PROP_0600) {
        uint16_t v_set = be16(d[0], d[1]);
        uint16_t i_set = be16(d[2], d[3]);
        g_chgr_cmd.enable = d[4];
        // Idle frame: V=0, I=0, enable=1 — emit enable only, skip V/I zeros.
        if (v_set || i_set) {
            g_chgr_cmd.voltage_v = v_set * 0.1f;
            g_chgr_cmd.current_a = i_set * 0.1f;
            g_chgr_cmd.v_raw     = v_set;
            g_chgr_cmd.i_raw     = i_set;
        }
        g_chgr_cmd.valid = true;
    }
}

// ── JSON builder ──────────────────────────────────────────────────────────────

static void addFloat(JsonObject& obj, const char* key, float v, int decimals = 2) {
    if (!isnan(v)) {
        float factor = 1.0f;
        for (int i = 0; i < decimals; i++) factor *= 10.0f;
        obj[key] = roundf(v * factor) / factor;
    }
}

String buildJson() {
    JsonDocument doc;

    doc["uptime"] = millis() / 1000.0;

    // CAN bus health
    auto can = doc["can"].to<JsonObject>();
    if (!g_can_initialized) {
        can["state"] = "not_initialized";
    } else {
        twai_status_info_t si;
        if (twai_get_status_info(&si) == ESP_OK) {
            switch (si.state) {
                case TWAI_STATE_STOPPED:    can["state"] = "stopped";    break;
                case TWAI_STATE_RUNNING:    can["state"] = "running";    break;
                case TWAI_STATE_BUS_OFF:    can["state"] = "bus_off";    break;
                case TWAI_STATE_RECOVERING: can["state"] = "recovering"; break;
                default:                    can["state"] = "unknown";    break;
            }
            can["tec"]        = si.tx_error_counter;
            can["rec"]        = si.rx_error_counter;
            can["rx_missed"]  = si.rx_missed_count;
            can["bus_errors"] = si.bus_error_count;
        }
    }
    can["frames_rx"]      = g_frames_rx;
    can["frames_decoded"] = g_frames_decoded;
    if (g_frames_rx > 0)
        can["last_frame_age_s"] = (millis() - g_last_frame_ms) / 1000.0;

    // Pack
    auto pack = doc["pack"].to<JsonObject>();
    addFloat(pack, "voltage_v",    g_pack.voltage_v, 2);
    addFloat(pack, "current_a",    g_pack.current_a, 1);
    if (g_pack.current_raw >= 0)   pack["current_raw"] = g_pack.current_raw;
    addFloat(pack, "power_w",      g_pack.power_w,   1);
    if (g_pack.soc_raw)            pack["soc_raw"]  = g_pack.soc_raw;
    addFloat(pack, "soc_pct",      g_pack.soc_pct,   1);
    addFloat(pack, "v_estimate",   g_pack.v_estimate, 3);
    auto cells_obj = pack["cells"].to<JsonObject>();
    if (g_pack.cell_max_mv >= 0)   cells_obj["max_mv"]    = g_pack.cell_max_mv;
    if (g_pack.cell_min_mv >= 0)   cells_obj["min_mv"]    = g_pack.cell_min_mv;
    if (g_pack.cell_spread_mv >= 0)cells_obj["spread_mv"] = g_pack.cell_spread_mv;
    if (g_pack.cell_max_n)         cells_obj["max_n"]     = g_pack.cell_max_n;
    if (g_pack.cell_min_n)         cells_obj["min_n"]     = g_pack.cell_min_n;
    if (g_pack.cell_spread_mv_reported >= 0)
        cells_obj["spread_mv_reported"] = g_pack.cell_spread_mv_reported;
    auto temp = cells_obj["temp_summary"].to<JsonObject>();
    if (g_pack.temp_max_c != INT8_MIN) temp["max_c"]    = g_pack.temp_max_c;
    if (g_pack.temp_min_c != INT8_MIN) temp["min_c"]    = g_pack.temp_min_c;
    if (g_pack.temp_max_n)             temp["max_n"]    = g_pack.temp_max_n;
    if (g_pack.temp_min_n)             temp["min_n"]    = g_pack.temp_min_n;
    if (g_pack.temp_spread_c >= 0)     temp["spread_c"] = g_pack.temp_spread_c;

    // Session energy summary
    auto sess = doc["session"].to<JsonObject>();
    sess["wh_drawn"]   = roundf(g_session_wh_drawn   * 10.0f) / 10.0f;
    sess["wh_charged"] = roundf(g_session_wh_charged * 10.0f) / 10.0f;
    sess["wh_net"]     = roundf((g_session_wh_drawn - g_session_wh_charged) * 10.0f) / 10.0f;
    sess["wh_capacity"] = PACK_CAPACITY_WH;

    // Session-average net power (positive = net discharge, negative = net charge).
    // Uses *active* time (sum of valid dt's), so bus-silent gaps don't dilute it.
    float avg_power_w = NAN;
    float active_hours = g_session_active_ms / 3600000.0f;
    if (active_hours > 0.01f) {                            // ≥ ~36 s of data
        avg_power_w = (g_session_wh_drawn - g_session_wh_charged) / active_hours;
        sess["avg_power_w"] = roundf(avg_power_w * 10.0f) / 10.0f;
        sess["active_s"]    = g_session_active_ms / 1000;
    }

    if (!isnan(g_pack.soc_pct)) {
        float remaining = g_pack.soc_pct * PACK_CAPACITY_WH / 100.0f;
        sess["wh_remaining"] = roundf(remaining * 10.0f) / 10.0f;
        // ETAs use session-average power so they don't jump with instantaneous load
        if (!isnan(avg_power_w)) {
            if (avg_power_w > 50.0f) {
                sess["eta_to_zero_s"] = (uint32_t)(remaining / avg_power_w * 3600.0f);
            } else if (avg_power_w < -50.0f) {
                float headroom = PACK_CAPACITY_WH - remaining;
                if (headroom > 0)
                    sess["eta_to_full_s"] = (uint32_t)(headroom / -avg_power_w * 3600.0f);
            }
        }
    }

    // Per-cell arrays (20 voltages, 7 temperatures; null if not yet received)
    auto cells = cells_obj["voltages"].to<JsonArray>();
    for (int i = 0; i < NUM_CELLS; i++) {
        if (!isnan(g_cell_v[i]))
            cells.add(roundf(g_cell_v[i] * 1000.0f) / 1000.0f);
        else
            cells.add(nullptr);
    }
    auto temps = cells_obj["temp_readings"].to<JsonArray>();
    for (int i = 0; i < NUM_TEMPS; i++) {
        if (!isnan(g_temp_c[i]))
            temps.add((int)g_temp_c[i]);
        else
            temps.add(nullptr);
    }

    // BMS state
    if (g_bms_state.valid) {
        auto st = doc["bms"]["state"].to<JsonObject>();
        st["byte0"]          = g_bms_state.byte0;
        st["byte1"]          = g_bms_state.byte1;
        st["output_enable"]  = g_bms_state.output_enable  ? 1 : 0;
        st["main_contactor"] = g_bms_state.main_contactor ? 1 : 0;
        st["operating"]      = g_bms_state.operating      ? 1 : 0;
        st["standby"]        = g_bms_state.standby        ? 1 : 0;
        st["charging"]       = g_bms_state.charging       ? 1 : 0;
        st["charger_present"]= g_bms_state.charger_present? 1 : 0;
        st["drive_mode"]     = g_bms_state.drive_mode     ? 1 : 0;
        st["awake"]          = g_bms_state.awake          ? 1 : 0;
    }

    // BMS current limits
    if (g_bms_limit.valid) {
        auto lim = doc["bms"]["limit"].to<JsonObject>();
        addFloat(lim, "discharge_a", g_bms_limit.discharge_a, 2);
        addFloat(lim, "charge_a",    g_bms_limit.charge_a,    2);
        lim["mode"]  = g_bms_limit.mode;
        lim["byte5"] = g_bms_limit.byte5;
    }

    // Combined fault codes (BMS + Motor Controller)
    auto faults = doc["faults"].to<JsonObject>();
    auto bms_codes = faults["bms"].to<JsonArray>();
    if (g_bms_faults.any_fault) {
        for (int code = 100; code <= 145; code++) {
            if (g_bms_faults.active_codes_mask & (1ULL << (code - 100)))
                bms_codes.add(code);
        }
    }
    auto mc_codes = faults["mc"].to<JsonArray>();
    if (g_dm1.valid && g_dm1.dtc_spn != 0)
        mc_codes.add(g_dm1.dtc_spn);

    // Motor
    if (g_motor.valid) {
        auto mot = doc["motor"].to<JsonObject>();
        mot["rpm_signed"]    = g_motor.rpm_signed;
        mot["rpm_magnitude"] = g_motor.rpm_magnitude;
        mot["direction"]     = g_motor.direction;
        mot["range_gear"]    = g_motor.range_gear;
        mot["throttle_raw"]  = g_motor.throttle_raw;
        // Ground speed from RPM × range (Turf/Industrial tire calibration,
        // per Operator Manual p34; Agri tires would need different coeffs).
        if (g_motor.range_gear >= 1 && g_motor.range_gear <= 3) {
            static const float KMH_PER_RPM[3] = {
                5.7f / 2800.0f, 8.6f / 2800.0f, 17.0f / 2800.0f
            };
            float kmh = g_motor.rpm_magnitude * KMH_PER_RPM[g_motor.range_gear - 1];
            addFloat(mot, "speed_kmh", kmh, 2);
            addFloat(mot, "speed_mph", kmh * 0.6213712f, 2);
        }
        if (g_motor.controller_temp_c != INT8_MIN)
            mot["controller_temp_c"] = g_motor.controller_temp_c;
        if (g_motor.motor_temp_c != INT8_MIN)
            mot["motor_temp_c"] = g_motor.motor_temp_c;
    }

    // Charger
    if (g_charger.valid) {
        auto chg = doc["charger"].to<JsonObject>();
        chg["status"] = g_charger.status;
        chg["v_raw"]  = g_charger.v_raw;
        chg["i_raw"]  = g_charger.i_raw;
        chg["flags"]  = g_charger.flags;
        chg["output_disabled"] = g_charger.output_disabled ? 1 : 0;
        chg["line_ok"]         = g_charger.line_ok         ? 1 : 0;
        chg["no_line"]         = g_charger.no_line         ? 1 : 0;
        addFloat(chg, "voltage_v", g_charger.voltage_v, 2);
        addFloat(chg, "current_a", g_charger.current_a, 1);
    }

    // BMS→charger command
    if (g_chgr_cmd.valid) {
        auto cmd = doc["chgr_cmd"].to<JsonObject>();
        cmd["enable"] = g_chgr_cmd.enable;
        addFloat(cmd, "voltage_v", g_chgr_cmd.voltage_v, 1);
        addFloat(cmd, "current_a", g_chgr_cmd.current_a, 1);
        if (g_chgr_cmd.v_raw) cmd["v_raw"] = g_chgr_cmd.v_raw;
        if (g_chgr_cmd.i_raw) cmd["i_raw"] = g_chgr_cmd.i_raw;
    }

    // Vehicle controller
    if (g_vc_state != 0xFF)
        doc["vc"]["state"] = g_vc_state;

    // Dashboard
    if (g_dash_alive != 0xFF)
        doc["dash"]["alive"] = g_dash_alive;

    // DM1 (raw FMI/OC/CM and lamp bytes — fault code is also in faults.mc)
    if (g_dm1.valid) {
        auto dm1 = doc["dm1"].to<JsonObject>();
        dm1["lamp_byte0"] = g_dm1.lamp_byte0;
        dm1["lamp_byte1"] = g_dm1.lamp_byte1;
        dm1["dtc_spn"]    = g_dm1.dtc_spn;
        dm1["dtc_fmi"]    = g_dm1.dtc_fmi;
        dm1["dtc_cm"]     = g_dm1.dtc_cm;
        dm1["dtc_oc"]     = g_dm1.dtc_oc;
    }

    String out;
    serializeJsonPretty(doc, out);
    return out;
}

// ── HTTP handlers ─────────────────────────────────────────────────────────────

void handleJson() {
    server.send(200, "application/json", buildJson());
}

void handleRoot() {
    // HTML lives in src/dashboard.html; embedded via board_build.embed_txtfiles.
    // Length excludes the trailing null byte that embed_txtfiles appends.
    size_t len = dashboard_html_end - dashboard_html_start - 1;
    server.send_P(200, "text/html", (PGM_P)dashboard_html_start, len);
}

// ── SLCAN ─────────────────────────────────────────────────────────────────────
// Presents the CAN bus as an SLCAN device over USB CDC serial.
// python-can: interface='slcan', channel='/dev/cu.usbmodem...'

static char   slcan_buf[32];
static uint8_t slcan_len = 0;
static bool   slcan_open = false;

void slcanSendFrame(const twai_message_t& msg) {
    if (!slcan_open) return;
    char line[32];
    int n;
    if (msg.extd) {
        n = snprintf(line, sizeof(line), "T%08" PRIX32 "%u",
                     msg.identifier, msg.data_length_code);
    } else {
        n = snprintf(line, sizeof(line), "t%03" PRIX32 "%u",
                     msg.identifier & 0x7FF, msg.data_length_code);
    }
    for (int i = 0; i < msg.data_length_code; i++)
        n += snprintf(line + n, sizeof(line) - n, "%02X", msg.data[i]);
    line[n++] = '\r';
    Serial.write((uint8_t*)line, n);
}

void slcanHandleCommand(const char* cmd) {
    switch (cmd[0]) {
        case 'O': slcan_open = true;  Serial.write('\r'); break;
        case 'C': slcan_open = false; Serial.write('\r'); break;
        case 'S': Serial.write('\r'); break;   // speed — fixed at 250k
        case 'V': Serial.print("V1013\r"); break;
        case 'N': Serial.print("NA000\r"); break;
        case 'F': Serial.print("F00\r");   break;
        default:  Serial.write('\r'); break;
    }
}

void slcanPoll() {
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\r' || c == '\n') {
            if (slcan_len > 0) {
                slcan_buf[slcan_len] = '\0';
                slcanHandleCommand(slcan_buf);
                slcan_len = 0;
            }
        } else if (slcan_len < sizeof(slcan_buf) - 1) {
            slcan_buf[slcan_len++] = c;
        }
    }
}

// ── socketcand ────────────────────────────────────────────────────────────────
// Streams raw CAN frames over WiFi using the socketcand ASCII protocol.
// python-can: interface='socketcand', host='solectrac.local', port=28600,
//             channel='can0'  (channel name is accepted but ignored).
//
// Single client at a time: a new connection drops the previous one. All work
// is skipped when no client is in rawmode, so the cost when idle is one
// pointer check per CAN frame.

#define SOCKETCAND_PORT 28600

enum SocketcandState {
    SC_DISCONNECTED,
    SC_WAITING_OPEN,
    SC_WAITING_RAWMODE,
    SC_RAWMODE,
};

static WiFiServer socketcand_server(SOCKETCAND_PORT);
static WiFiClient socketcand_client;
static SocketcandState socketcand_state = SC_DISCONNECTED;
static char     socketcand_buf[64];
static uint8_t  socketcand_len = 0;

void socketcandSendFrame(const twai_message_t& msg) {
    if (socketcand_state != SC_RAWMODE) return;
    uint32_t now_ms = millis();
    uint32_t secs   = now_ms / 1000;
    uint32_t usecs  = (now_ms % 1000) * 1000;
    // EFF flag (bit 31) marks the ID as 29-bit extended — python-can expects this.
    uint32_t id_wire = msg.identifier | (msg.extd ? 0x80000000UL : 0);
    char line[80];
    int n = snprintf(line, sizeof(line), "< frame %lX %lu.%06lu ",
                     (unsigned long)id_wire,
                     (unsigned long)secs, (unsigned long)usecs);
    for (int i = 0; i < msg.data_length_code && n < (int)sizeof(line) - 4; i++)
        n += snprintf(line + n, sizeof(line) - n, "%02X", msg.data[i]);
    n += snprintf(line + n, sizeof(line) - n, " >");
    socketcand_client.write((const uint8_t*)line, n);
}

static void socketcandHandleCommand(const char* cmd) {
    if (socketcand_state == SC_WAITING_OPEN && strncmp(cmd, "< open ", 7) == 0) {
        socketcand_client.print("< ok >");
        socketcand_state = SC_WAITING_RAWMODE;
    } else if (socketcand_state == SC_WAITING_RAWMODE && strcmp(cmd, "< rawmode >") == 0) {
        socketcand_client.print("< ok >");
        socketcand_state = SC_RAWMODE;
    }
    // bcmmode/isotpmode/echo/statistics are intentionally unsupported.
}

void socketcandPoll() {
    WiFiClient new_client = socketcand_server.available();
    if (new_client) {
        if (socketcand_client) socketcand_client.stop();
        socketcand_client = new_client;
        socketcand_client.setNoDelay(true);
        socketcand_client.print("< hi >");
        socketcand_state = SC_WAITING_OPEN;
        socketcand_len   = 0;
    }
    if (socketcand_state != SC_DISCONNECTED && !socketcand_client.connected()) {
        socketcand_client.stop();
        socketcand_state = SC_DISCONNECTED;
        socketcand_len   = 0;
    }
    while (socketcand_client && socketcand_client.available()) {
        char c = socketcand_client.read();
        if (c == '<') {
            socketcand_len = 0;
            socketcand_buf[socketcand_len++] = c;
        } else if (socketcand_len > 0 && socketcand_len < sizeof(socketcand_buf) - 1) {
            socketcand_buf[socketcand_len++] = c;
            if (c == '>') {
                socketcand_buf[socketcand_len] = '\0';
                socketcandHandleCommand(socketcand_buf);
                socketcand_len = 0;
            }
        }
    }
}

// ── Setup & loop ──────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);

    ledInit();
    ledWrite(4, 4, 4);   // dim white the moment firmware starts running

    for (int i = 0; i < NUM_CELLS; i++) g_cell_v[i] = NAN;
    for (int i = 0; i < NUM_TEMPS; i++) g_temp_c[i] = NAN;

    // CAN at 250 kbit/s (J1939 standard). Default rx_queue_len is 5, which
    // overflows when server.handleClient() blocks the loop building JSON.
    // 128 gives ~0.5 s of buffer at full bus utilisation.
    twai_general_config_t can_cfg = TWAI_GENERAL_CONFIG_DEFAULT(
        CAN_TX_PIN, CAN_RX_PIN, TWAI_MODE_NORMAL);
    can_cfg.rx_queue_len = 128;
    can_cfg.tx_queue_len = 32;
    twai_timing_config_t  tim_cfg = TWAI_TIMING_CONFIG_250KBITS();
    twai_filter_config_t  flt_cfg = TWAI_FILTER_CONFIG_ACCEPT_ALL();
    esp_err_t err = twai_driver_install(&can_cfg, &tim_cfg, &flt_cfg);
    if (err == ESP_OK) {
        err = twai_start();
        if (err == ESP_OK) g_can_initialized = true;
    }

    // Soft-AP only: clients join the board's hotspot directly at 192.168.4.1.
    // Disable modem sleep so the radio stays hot — saves ~50–150 ms per request
    // at the cost of ~70 mA, fine on a tractor 12 V supply.
    WiFi.mode(WIFI_AP);
    g_ap_running = WiFi.softAP(AP_SSID, AP_PASS);
    WiFi.setSleep(WIFI_PS_NONE);

    // Wildcard DNS on the soft-AP: any hostname (solectrac.local, solectrac,
    // captive-portal probes, etc.) resolves to the board's AP IP. Needed
    // because phones generally don't do mDNS over an AP with no internet.
    if (g_ap_running) dns_server.start(53, "*", WiFi.softAPIP());

    MDNS.begin("solectrac");

    server.on("/",     handleRoot);
    server.on("/json", handleJson);
    server.begin();
    MDNS.addService("http", "tcp", 80);

    socketcand_server.begin();
    socketcand_server.setNoDelay(true);
    MDNS.addService("socketcand", "tcp", SOCKETCAND_PORT);
}

void loop() {
    twai_message_t msg;
    while (twai_receive(&msg, 0) == ESP_OK) {
        g_frames_rx++;
        g_last_frame_ms = millis();
        if (msg.extd)
            decodeCAN(msg.identifier, msg.data, msg.data_length_code);
        slcanSendFrame(msg);
        socketcandSendFrame(msg);
    }
    slcanPoll();
    socketcandPoll();
    dns_server.processNextRequest();
    server.handleClient();
    updateLed();
}
