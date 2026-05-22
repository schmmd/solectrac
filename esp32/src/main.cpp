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
#include <WebServer.h>
#include <ArduinoJson.h>
#include "driver/twai.h"

// ── Configuration ─────────────────────────────────────────────────────────────

#ifndef WIFI_SSID
#error "Set WIFI_SSID env var before building"
#endif
#ifndef WIFI_PASS
#error "Set WIFI_PASS env var before building"
#endif

#define CAN_TX_PIN GPIO_NUM_8
#define CAN_RX_PIN GPIO_NUM_14

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

// Session energy tracking (integrated power since boot)
uint32_t    g_session_last_ms   = 0;
uint32_t    g_session_active_ms = 0;   // sum of valid dt's — excludes bus-silent gaps
float       g_session_wh_drawn  = 0.0f;
float       g_session_wh_charged = 0.0f;

WebServer server(80);

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
    g_frames_rx++;
    g_last_frame_ms = millis();

    uint8_t d[8] = {};
    memcpy(d, raw, len < 8 ? len : 8);

    uint8_t  src = can_id & 0xFF;
    uint8_t  pf  = (can_id >> 16) & 0xFF;
    uint8_t  ps  = (can_id >> 8)  & 0xFF;
    uint16_t pgn = ((uint16_t)pf << 8) | (pf >= 0xF0 ? ps : 0);
    bool decoded = false;

    if (src == SRC_BMS || src == SRC_BMS_CHGR_IF || src == SRC_CHARGER ||
        src == SRC_VEHICLE || src == SRC_MOTOR || src == SRC_DASH)
        decoded = true;

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

    if (decoded) g_frames_decoded++;
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
    server.send(200, "text/html", R"HTML(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solectrac</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f0f2f5;color:#212121;font-family:system-ui,sans-serif;padding:10px;max-width:600px;margin:0 auto}
h3{font-size:.68em;text-transform:uppercase;letter-spacing:.1em;color:#757575;margin-bottom:8px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}
@media(max-width:400px){.g4{grid-template-columns:1fr 1fr}}
.card{background:#fff;border-radius:10px;padding:12px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.big{font-size:1.9em;font-weight:700;line-height:1.1}
.unit{font-size:.5em;color:#9e9e9e;font-weight:400}
.sub{font-size:.72em;color:#757575;margin-top:2px}
.kv{display:flex;justify-content:space-between;align-items:center;font-size:.82em;padding:2px 0}
.kv .k{color:#757575}
.gauges{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.gauge{display:block;width:100%}
.gauges h3{text-align:center}
.dir{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px}
.dirbtn{background:#fff;border-radius:10px;padding:16px 0;text-align:center;font-size:1.8em;font-weight:700;color:#bdbdbd;letter-spacing:.05em;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.dirbtn.on{background:#c8e6c9;color:#1b5e20}
.pills{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
.pill{padding:3px 10px;border-radius:20px;font-size:.72em;background:#eee;color:#9e9e9e}
.pill.on{background:#c8e6c9;color:#1b5e20}
.alert{background:#ffcdd2;color:#b71c1c;border-radius:8px;padding:10px 12px;margin-bottom:8px;font-size:.9em;font-weight:600}
#foot{text-align:center;font-size:.65em;color:#9e9e9e;margin-top:10px;padding-bottom:4px}
.ok{color:#2e7d32}.warn{color:#ef6c00}.bad{color:#c62828}
</style>
</head>
<body>
<div id="al"></div>
<div class="g4" id="kpi"></div>
<div class="card" style="margin-bottom:8px">
<div class="gauges">
<div>
<h3>Speed</h3>
<svg class="gauge" viewBox="0 0 200 120">
  <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e0e0e0" stroke-width="14" stroke-linecap="round"/>
  <path id="sprog" fill="none" stroke="#4caf50" stroke-width="14" stroke-linecap="round"/>
  <line id="sneed" x1="100" y1="100" x2="100" y2="30" stroke="#c62828" stroke-width="3" stroke-linecap="round"/>
  <circle cx="100" cy="100" r="5" fill="#c62828"/>
  <text x="20" y="116" text-anchor="middle" font-size="9" fill="#757575">0</text>
  <text x="180" y="116" text-anchor="middle" font-size="9" fill="#757575">11 mph</text>
  <text x="100" y="78" text-anchor="middle" font-size="26" font-weight="700" fill="#212121" id="sval">–</text>
  <text x="100" y="92" text-anchor="middle" font-size="9" fill="#9e9e9e" id="sunit">mph</text>
</svg>
</div>
<div>
<h3>Power</h3>
<svg class="gauge" viewBox="0 0 200 120">
  <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e0e0e0" stroke-width="14" stroke-linecap="round"/>
  <path id="pprog" fill="none" stroke="#4caf50" stroke-width="14" stroke-linecap="round"/>
  <line id="pneed" x1="100" y1="100" x2="100" y2="30" stroke="#c62828" stroke-width="3" stroke-linecap="round"/>
  <circle cx="100" cy="100" r="5" fill="#c62828"/>
  <text x="20" y="116" text-anchor="middle" font-size="9" fill="#757575">0</text>
  <text x="180" y="116" text-anchor="middle" font-size="9" fill="#757575">15 kW</text>
  <text x="100" y="78" text-anchor="middle" font-size="26" font-weight="700" fill="#212121" id="pval">–</text>
  <text x="100" y="92" text-anchor="middle" font-size="9" fill="#9e9e9e" id="punit">kW</text>
</svg>
</div>
</div>
</div>
<div class="dir" id="dir"></div>
<div class="card" style="margin-bottom:8px"><h3>Power Summary</h3><div id="psum"></div></div>
<div class="g2" id="mid"></div>
<div class="pills" id="flags"></div>
<div class="g2" id="bot"></div>
<div id="foot">Connecting…</div>
<script>
const f=(v,d=1)=>v==null?'–':v.toFixed(d);
const fi=v=>v==null?'–':(v>=0?'+':'')+v.toFixed(1);
function kv(k,v){return'<div class="kv"><span class="k">'+k+'</span><span>'+v+'</span></div>';}
var MC_FAULTS={
  12:'Controller Over Current',13:'Current Sensor Fault',
  15:'Controller Severe Undertemp',16:'Controller Severe Overtemp',
  17:'Severe B+ Undervoltage',18:'Severe B+ Overvoltage',
  22:'Controller Over Temp Cutback',23:'B+ Undervoltage Cutback',
  24:'B+ Overvoltage Cutback',25:'+5V Supply Failure',
  26:'Motor Temp Hot Cutback',29:'Motor Temp Sensor Fault',
  31:'Coil1 Driver Open/Short / Main Open/Short',
  32:'Coil2 Driver Open/Short / EM Brake Open/Short',
  36:'Encoder Fault / Sin/Cos Sensor Fault',37:'Motor Open',
  38:'Main Contactor Welded',39:'Main Contactor Did Not Close',
  41:'Throttle Wiper High',42:'Throttle Wiper Low',
  43:'Pot2 Wiper High',44:'Pot2 Wiper Low',
  45:'Pot Low Over Current',47:'HPD/Sequencing Fault',
  49:'Parameter Change Fault / PDO Timeout',
  71:'Stall Detected / Vehicle lock without applying hand brake',
  83:'Driver Supply',87:'Motor Characterization Fault',
  89:'Encoder Pulse Count Fault / Motor Type Fault',
  92:'EM Brake failed to set',99:'Parameter Mismatch'
};
var BMS_FAULTS={
  100:'SOC too high',101:'SOC too low',
  102:'Total voltage too high',103:'Total voltage too low',
  104:'Charge current fault',105:'Discharge current fault',
  106:'Battery temp too low',107:'Battery temp too high',
  108:'Battery under voltage',109:'Battery over voltage',
  110:'Battery temp unbalance',111:'Battery voltage unbalance',
  112:'Battery does not match',113:'Output pole temp too high',
  116:'Memory parameters fault',117:'Data memory fault',
  118:'Cell voltage detection fault',119:'Temperature detection fault',
  120:'Current detection fault',121:'Internal total voltage detection fault',
  122:'External total voltage detection fault',123:'Insulation monitoring fault',
  124:'Clock fault',125:'Internal CAN comm fault',
  126:'Serious insulation fault',127:'Slight insulation fault',
  140:'System fault level',142:'BMS fault — maintenance',
  143:'Battery fault — maintenance',144:'Battery system fault — maintenance',
  145:'Needs full charge/discharge cycle',146:'Maintenance mode status'
};
function fwh(wh){
  if(wh==null)return'–';
  if(Math.abs(wh)>=1000)return(wh/1000).toFixed(2)+' kWh';
  return Math.round(wh)+' Wh';
}
function feta(s){
  if(s==null||s<=0)return'–';
  if(s>360000)return'>100h';
  var h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h>0?h+'h '+m+'m':m+'m';
}
function update(d){
  const p=d.pack||{},m=d.motor||{},can=d.can||{};
  const pc=p.cells||{},pt=pc.temp_summary||{};
  const bst=(d.bms&&d.bms.state)||{};
  const fts=d.faults||{},chg=d.charger||{},cmd=d.chgr_cmd||{};
  const s=d.session||{};
  const al=[];
  (fts.bms||[]).forEach(function(c){
    al.push('BMS '+c+(BMS_FAULTS[c]?' ('+BMS_FAULTS[c]+')':''));
  });
  (fts.mc||[]).forEach(function(c){
    al.push('MC '+c+(MC_FAULTS[c]?' ('+MC_FAULTS[c]+')':''));
  });
  document.getElementById('al').innerHTML=al.map(function(a){return'<div class="alert">⚠ '+a+'</div>';}).join('');
  const soc=p.soc_pct,scls=soc==null?'':soc<20?'bad':soc<40?'warn':'ok';
  const cur=p.current_a,pwr=p.power_w;
  const curLbl=cur==null?'':cur<-0.5?'discharge':cur>0.5?'charge':'idle';
  document.getElementById('kpi').innerHTML=
    '<div class="card"><h3>SoC</h3><div class="big '+scls+'">'+f(soc,0)+'<span class="unit">%</span></div><div class="sub">'+(s.wh_remaining!=null&&s.wh_capacity?(s.wh_remaining/1000).toFixed(2)+' / '+fwh(s.wh_capacity):'')+'</div></div>'+
    '<div class="card"><h3>Voltage</h3><div class="big">'+f(p.voltage_v,1)+'<span class="unit">V</span></div><div class="sub">60 – 84 V range</div></div>'+
    '<div class="card"><h3>Current</h3><div class="big">'+fi(cur)+'<span class="unit">A</span></div><div class="sub">'+curLbl+'</div></div>'+
    '<div class="card"><h3>Power</h3><div class="big">'+(pwr!=null?(pwr/1000).toFixed(1):'–')+'<span class="unit">kW</span></div><div class="sub">'+(pwr!=null?Math.round(Math.abs(pwr)/150)+'% of 15 kW max':'')+'</div></div>';
  const netCharging=s.wh_net!=null&&s.wh_net<0;
  const etaLabel=netCharging?'ETA to 100%':'ETA to 0%';
  const etaVal=netCharging?s.eta_to_full_s:s.eta_to_zero_s;
  document.getElementById('psum').innerHTML=
    kv('Session draw',fwh(s.wh_drawn))+
    kv('Session charge',fwh(s.wh_charged))+
    kv('Session net',(s.wh_net!=null&&s.wh_net>0?'+':'')+fwh(s.wh_net))+
    kv(etaLabel,feta(etaVal));
  // Gauges (speed + power)
  function setGauge(val,max,progId,needId){
    var cx=100,cy=100,r=80;
    var pr=document.getElementById(progId),nd=document.getElementById(needId);
    if(val==null){
      pr.setAttribute('d','');
      nd.setAttribute('x2',cx);nd.setAttribute('y2',cy-(r-12));
      return;
    }
    var frac=Math.max(0,Math.min(val/max,1));
    var th=Math.PI*(1-frac);
    var ex=cx+r*Math.cos(th),ey=cy-r*Math.sin(th);
    var nr=r-12,nx=cx+nr*Math.cos(th),ny=cy-nr*Math.sin(th);
    pr.setAttribute('d',val>0.05?'M '+(cx-r)+' '+cy+' A '+r+' '+r+' 0 0 1 '+ex.toFixed(2)+' '+ey.toFixed(2):'');
    nd.setAttribute('x2',nx.toFixed(2));nd.setAttribute('y2',ny.toFixed(2));
  }
  setGauge(m.speed_mph,11,'sprog','sneed');
  document.getElementById('sval').textContent=m.speed_mph!=null?m.speed_mph.toFixed(1):'–';
  document.getElementById('sunit').textContent=m.speed_mph!=null?'mph ('+(m.speed_kmh!=null?m.speed_kmh.toFixed(1):'–')+' km/h)':'mph';
  var pkw=pwr!=null?Math.abs(pwr)/1000:null;
  setGauge(pkw,15,'pprog','pneed');
  document.getElementById('pval').textContent=pkw!=null?pkw.toFixed(1):'–';
  document.getElementById('punit').textContent=pkw!=null?'kW ('+Math.round(pkw/15*100)+'%)':'kW';
  document.getElementById('dir').innerHTML=
    [['F',1],['N',0],['R',-1]].map(function(x){
      return'<div class="dirbtn '+(m.direction===x[1]?'on':'')+'">'+x[0]+'</div>';
    }).join('');
  document.getElementById('mid').innerHTML=
    '<div class="card"><h3>Cell</h3>'+
    kv('Max',(pc.max_mv!=null?(pc.max_mv/1000).toFixed(3):'–')+' V (#'+(pc.max_n||'?')+')')+
    kv('Min',(pc.min_mv!=null?(pc.min_mv/1000).toFixed(3):'–')+' V (#'+(pc.min_n||'?')+')')+
    kv('Spread',pc.spread_mv!=null?(pc.spread_mv+' mV'+(pc.max_mv&&pc.min_mv?' ('+(pc.spread_mv/((pc.max_mv+pc.min_mv)/2)*100).toFixed(2)+'%)':'')):'–')+
    kv('Temp max',pt.max_c!=null?pt.max_c+'°C':'–')+
    kv('Temp min',pt.min_c!=null?pt.min_c+'°C':'–')+
    '</div>'+
    '<div class="card"><h3>Motor</h3>'+
    kv('RPM',m.rpm_magnitude!=null?m.rpm_magnitude:'–')+
    kv('Range','R'+(m.range_gear||'?'))+
    kv('MC Temp',m.controller_temp_c!=null?m.controller_temp_c+'°C':'–')+
    kv('Motor Temp',m.motor_temp_c!=null?m.motor_temp_c+'°C':'–')+
    '</div>';
  var flagDefs=[
    ['Awake',bst.awake],['Drive Mode',bst.drive_mode],
    ['Operating',bst.operating],['Standby',bst.standby],
    ['Charging',bst.charging],['Charger Present',bst.charger_present],
    ['Output Enabled',bst.output_enable],['Main Contactor',bst.main_contactor]
  ];
  document.getElementById('flags').innerHTML=flagDefs.map(function(x){
    return'<div class="pill '+(x[1]?'on':'')+'">'+x[0]+'</div>';
  }).join('');
  var bot='';
  if(d.charger){
    var acSt=chg.line_ok?'<span class="ok">OK</span>':chg.no_line?'<span class="bad">No AC</span>':'–';
    bot+='<div class="card" style="grid-column:1/-1"><h3>Charger</h3>'+
      kv('Output V',f(chg.voltage_v))+kv('Output A',f(chg.current_a))+
      kv('AC line',acSt)+kv('Cmd V / A',f(cmd.voltage_v)+' / '+f(cmd.current_a))+'</div>';
  }
  document.getElementById('bot').innerHTML=bot;
  var age=can.last_frame_age_s;
  var dot=can.state==='running'?'<span class="ok">●</span>':'<span class="bad">●</span>';
  document.getElementById('foot').innerHTML=
    dot+' '+can.state+' · '+(can.frames_rx||0)+' rx · age '+(age!=null?age.toFixed(1)+'s':'–')+' · up '+f(d.uptime/3600,1)+'h';
}
function refresh(){
  fetch('/json').then(function(r){return r.json();}).then(update)
    .catch(function(){document.getElementById('foot').textContent='Error — retrying…';});
}
refresh();
setInterval(refresh,1000);
</script>
</body>
</html>)HTML");
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
    int n = snprintf(line, sizeof(line), "T%08" PRIX32 "%u",
                     msg.identifier, msg.data_length_code);
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

// ── Setup & loop ──────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);

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

    WiFi.begin(WIFI_SSID, WIFI_PASS);
    while (WiFi.status() != WL_CONNECTED) delay(500);

    MDNS.begin("solectrac");

    server.on("/",     handleRoot);
    server.on("/json", handleJson);
    server.begin();
    MDNS.addService("http", "tcp", 80);
}

void loop() {
    twai_message_t msg;
    while (twai_receive(&msg, 0) == ESP_OK) {
        if (msg.extd) {
            decodeCAN(msg.identifier, msg.data, msg.data_length_code);
            slcanSendFrame(msg);
        }
    }
    slcanPoll();
    server.handleClient();
}
