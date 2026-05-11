#!/usr/bin/env python3
"""
solectrac-stream.py — Live (or replayed) BMS / charger TUI for the
Solectrac CAN bus.

Decodes the same J1939-style frames as solectrac-analyze.py, but
streams from a live CAN interface (or a python-can log file) and
displays a real-time dashboard:

    * Pack voltage estimate, current magnitude, DC and estimated AC power.
    * State-of-charge (SOC) estimate from min cell voltage (NMC OCV curve;
      load-sensitive — see SOC notes below).
    * Charger output V / A / power, status flag.
    * Per-cell voltages with min/max/spread (1-based BMS numbering).
    * Per-channel module temperatures (with the +40 C offset removed).
    * Vehicle-controller heartbeat state.
    * Live alerts (low/high cell, spread, temp, AC budget, stale BMS).

SOC estimate notes:
    * Voltage-only SOC is approximate. The OCV->SOC table assumes NMC
      chemistry (consistent with the ~4.1 V/cell range observed on this
      pack); LFP cells would need a different table.
    * Estimate is taken from the **lowest** cell voltage so it tracks the
      limiting cell (the one that will trip the BMS first), not the pack
      average.
    * Terminal voltage drops below OCV under load and rises above OCV
      while charging. The dashboard tags the estimate as "(loaded)" when
      |I| exceeds a small threshold so a depressed reading isn't taken
      as ground truth.

Data sources:
    --interface socketcan --channel can0    live SocketCAN bus
    --replay path/to/raw.log                python-can log file replay

Examples:
    # Live capture from SocketCAN
    solectrac-stream.py --interface socketcan --channel can0 --bitrate 250000

    # Replay an existing capture
    solectrac-stream.py --replay session.log

    # Live + raw logging + AC-budget alerts for a 120V/20A circuit
    solectrac-stream.py --interface socketcan --channel can0 \\
        --raw-log out.log --mains-v 120 --breaker-a 20

Requires:
    pip install python-can rich
"""

import argparse
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

try:
    import can
except ImportError:
    print("python-can is required: pip install python-can", file=sys.stderr)
    sys.exit(1)

try:
    from rich.console import Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("rich is required: pip install rich", file=sys.stderr)
    sys.exit(1)


# --- protocol constants (mirrored from solectrac-analyze.py) --------------

SRC_BMS = 0xF3
SRC_BMS_CHGR_IF = 0xF4   # BMS in its charger-interface role; SA only sends 0x000600
SRC_CHARGER = 0xE5
SRC_VEHICLE = 0xD0
SRC_MOTOR = 0xCA
SRC_DASH = 0x12          # dashboard / instrument-cluster heartbeat; only sends FF21

PGN_CELL_FIRST, PGN_CELL_LAST = 0xF113, 0xF13C
PGN_TEMP_FIRST, PGN_TEMP_LAST = 0xF155, 0xF15E
PGN_F100 = 0xF100
PGN_F102 = 0xF102
PGN_F104 = 0xF104
PGN_F106 = 0xF106
PGN_F107 = 0xF107
PGN_F108 = 0xF108
PGN_FF50 = 0xFF50
PGN_FF21 = 0xFF21
PGN_FECA = 0xFECA   # SAE J1939-73 DM1 (Active Diagnostic Trouble Codes)
PGN_PROP_0600 = 0x0600   # PDU1, src 0xF4 -> dest 0xE5: BMS charger setpoint

# DM1 lamp/flash 2-bit field decode tables (per J1939-73).
DM1_LAMP_NAMES = ("malfunction", "red_stop", "amber_warning", "protect")
DM1_LAMP_STATE = {0: "off", 1: "on", 2: "rsv", 3: "n/a"}
DM1_FLASH_STATE = {0: "-", 1: "1Hz", 2: "2Hz", 3: "n/a"}
# Standard J1939-73 Appendix A FMIs (most common; full list is 0..31).
DM1_FMI_NAMES = {
    0: "above max", 1: "below min", 2: "erratic",
    3: "shorted high", 4: "shorted low", 5: "open circuit",
    6: "shorted ground", 7: "wrong response", 8: "abnormal frequency",
    9: "abnormal update rate", 10: "abnormal change rate", 11: "unknown",
    12: "bad device", 13: "out of cal", 14: "special", 15: "info high (least)",
    16: "info high (mod)", 17: "info low (least)", 18: "info low (mod)",
    19: "data error", 20: "data drift high", 21: "data drift low",
    31: "condition exists",
}

# Vendor BMS error-code table from the CET / Farmtrac 25 G operator
# manual, p.44 ("Error Codes for Controller and Battery"). Authoritative
# source for the human-readable description of each numeric code shown on
# the dashboard's BMS error display.
BMS_FAULT_DESCRIPTIONS: dict = {
    100: "SOC is too high",
    101: "SOC is too low",
    102: "Total voltage is too high",
    103: "Total voltage is too low",
    104: "Charge current fault",
    105: "Discharge current fault",
    106: "Battery temperature is too low",
    107: "Battery temperature is too high",
    108: "Battery under voltage",
    109: "Battery over voltage",
    110: "Battery temperature unbalance",
    111: "Battery voltage unbalance",
    112: "The battery does not match",
    113: "Output pole temperature too high",
    116: "Memory parameter fault",
    117: "Data memory fault",
    118: "Cell voltage detection fault",
    119: "Temperature detection fault",
    120: "Current detection fault",
    121: "Internal total voltage detection fault",
    122: "External total voltage detection fault",
    123: "Insulation monitoring fault",
    124: "Pre-charging fault",
    125: "Internal CAN communication fault",
    126: "Serious insulation fault",
    127: "Slight insulation fault",
    140: "System fault: kvst",
    141: "BMS fault need maintenance",
    142: "BMS fault (manual omits 142)",  # tentative; bit-3 capture observation
    143: "Battery fault need maintenance",
    144: "Battery system fault needs maintenance",
    145: "Needs full charge/discharge maintenance",
    146: "Maintenance mode status",
}

# Motor controller fault codes from the same operator-manual table. Some
# numbers list two distinct conditions; both are kept because the manual
# gives no way to disambiguate them from the code alone.
MC_FAULT_DESCRIPTIONS: dict = {
    12: ["Controller Over Current"],
    13: ["Current Sensor Fault"],
    15: ["Controller Severe Undertemp"],
    16: ["Controller Severe Overtemp"],
    17: ["Severe B+ Undervoltage"],
    18: ["Severe B+ Overvoltage"],
    22: ["Controller Over Temp Cutback"],
    23: ["B+ Undervoltage Cutback"],
    24: ["B+ Overvoltage Cutback"],
    25: ["+5V Supply Failure"],
    26: ["Motor Temp Hot Cutback"],
    29: ["Motor Temp Sensor Fault"],
    31: ["Coil1 Driver Open/Short", "Main Open/Short"],
    32: ["Coil2 Driver Open/Short", "EM Brake Open/Short"],
    36: ["Encoder Fault", "Sin/Cos Sensor Fault"],
    37: ["Motor Open"],
    38: ["Main Contactor Welded"],
    39: ["Main Contactor Did Not Close"],
    41: ["Throttle Wiper High"],
    42: ["Throttle Wiper Low"],
    43: ["Pot2 Wiper High"],
    44: ["Pot2 Wiper Low"],
    45: ["Pot Low Over Current"],
    47: ["HPD/Sequencing Fault"],
    49: ["Parameter Change Fault", "PDO Timeout"],
    71: ["Stall Detected", "Vehicle lock without applying hand brake"],
    83: ["Driver Supply"],
    87: ["Motor Characterization Fault"],
    89: ["Encoder Pulse Count Fault", "Motor Type Fault"],
    92: ["EM Brake failed to set"],
    99: ["Parameter Mismatch"],
}


def describe_bms_code(code: int) -> str:
    """Return the operator-manual description for a BMS code, or 'unknown'."""
    return BMS_FAULT_DESCRIPTIONS.get(int(code), f"unknown code {int(code)}")


def describe_mc_code(code: int) -> str:
    """Return a slash-joined description for a motor-controller code."""
    entries = MC_FAULT_DESCRIPTIONS.get(int(code))
    if not entries:
        return f"unknown code {int(code)}"
    return " / ".join(entries)


# F108 byte 7: bit -> code, 1 bit per code with gaps. Mapping established
# by per-bit injection on 2026-05-10 (see solectrac-inject-f108.py and
# f108-byte7.csv): bit 0 = 140, bits 1,2 silent (dashboard shows nothing),
# bit 3 = 142, bit 4 = 143, bit 5 = 144, bit 6 = 144 (genuine duplicate
# of bit 5, re-verified), bit 7 = 145. Code 146 does NOT appear anywhere
# in F108; the operator's "146" in bms-124-140-142-143-144-146.asc was
# almost certainly a 145 transcription.
#
# Descriptions are looked up from BMS_FAULT_DESCRIPTIONS at render time
# so the operator-manual text remains the single source of truth.
BMS_FAULT_CODES_BYTE7: List[Tuple[int, int]] = [
    (0, 140),
    # bit 1: silent on dashboard
    # bit 2: silent on dashboard
    (3, 142),
    (4, 143),
    (5, 144),
    (6, 144),  # duplicate of bit 5 (re-verified by injection)
    (7, 145),
]

# F108 bytes 0..6: per-bit code table. Indexed by (byte, bit); None means
# the bit is silent on the dashboard. Layout uses MIXED encoding:
# bytes 0..3 are 2 bits per code (each pair of adjacent bits displays the
# same code), bytes 4..5 are 1 bit per code, byte 6 is fully silent. The
# manual's reserved codes (114, 115) take zero bits — bits 4..7 of byte 3
# are silent. Codes 120..123 live in byte 4 bits 4..7 (1 bit each).
#
# Confirmed end-to-end by injection sweep on 2026-05-10.
BMS_FAULT_CODES_BYTES_0_TO_6: dict = {
    0: (100, 100, 101, 101, 102, 102, 103, 103),
    1: (104, 104, 105, 105, 106, 106, 107, 107),
    2: (108, 108, 109, 109, 110, 110, 111, 111),
    3: (112, 112, 113, 113, None, None, None, None),  # 114, 115 reserved
    4: (116, 117, 118, 119, 120, 121, 122, 123),
    5: (124, 125, 126, 127, None, None, None, None),
    # byte 6: all 8 bits silent
}

TEMP_OFFSET_C = 40


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32
PACK_CURRENT_LSB_A = 0.1
PACK_CURRENT_BIAS_RAW = 0x7D00  # F100F3 bytes 2-3 BE, biased so 0x7D00 = 0 A
PACK_VOLTAGE_LSB_V = 0.1        # F100F3 byte 1 and FF50E5 bytes 1-2 LE
PACK_VOLTAGE_OFFSET_V = 76.8    # V = raw * 0.1 + 76.8
CHARGER_V_LSB_V = PACK_VOLTAGE_LSB_V       # charger uses identical encoding
CHARGER_V_OFFSET_V = PACK_VOLTAGE_OFFSET_V
CHARGER_I_LSB_A = 0.1
RPM_BIAS = 0x0C80
LIMIT_CURRENT_LSB_A = 0.01     # F107 bytes 0-1 / 2-3 BE, 0.01 A/bit
# Throttle pedal scaling for FF21CA byte 0. Empirical survey across all
# 45,086 motor-telemetry frames: byte 0 ranges 0..0x69 (0..105) with an
# idle resting offset of ~3 (sensor noise floor with foot off pedal) and
# a controller dead-low around 14 (below this, motor RPM stays near 0;
# matches the Kelly TPS_dead_low concept from the hydraulic pump doc).
# The byte behaves like a J1939-style 0..100% throttle position with
# mild mechanical overshoot (rare excursions to 105). The previous
# constant of 52 (0x34) was the max seen in earlier captures only; it
# overstates pct by ~2x at full pedal.
THROTTLE_DEAD_LOW = 3          # idle resting offset (subtracted from raw)
THROTTLE_FULL_SCALE = 100      # byte 0 = direct percent; clamp at 100
# Pack ratings from the vendor BMS GUI (NOTES.txt): 300 Ah at 72.0 V
# nominal -> 21.6 kWh nominal energy. Used for the "% of pack" display
# only; not used for any decoding.
PACK_CAPACITY_AH = 300.0
PACK_NOMINAL_V = 72.0
PACK_CAPACITY_WH = PACK_CAPACITY_AH * PACK_NOMINAL_V    # 21,600 Wh

VC_STATE_NAMES = {0x00: "init", 0x0C: "ready"}

# Charger status byte (FF50CA byte 0). Values established empirically:
#   0x00 = idle (charger module powered, not charging)
#   0x01, 0x02 = transient handshake / pre-charge states (only seen briefly)
#   0x03 = actively delivering power
CHGR_STATUS_IDLE = 0x00
CHGR_STATUS_ACTIVE = 0x03
CHGR_HANDSHAKE_STATES = {0x01, 0x02}

# Pack topology from the vendor BMS GUI screenshot (see NOTES.txt).
NUM_CELLS = 20
NUM_TEMPS = 7

STALE_S = 2.0  # mark a channel stale if no update for this long

# OCV (open-circuit voltage) -> SOC table for typical NMC Li-ion at room
# temperature, in (mV-per-cell, percent). Composite curve; vendor-specific
# cells can drift from this by ~5-10% SOC. The lower knee is steeper than
# the upper, which is why the table is denser near the bottom.
NMC_OCV_TABLE: List[Tuple[int, float]] = [
    (3000,   0.0),
    (3300,   5.0),
    (3450,  10.0),
    (3530,  15.0),
    (3620,  20.0),
    (3690,  30.0),
    (3740,  40.0),
    (3800,  50.0),
    (3870,  60.0),
    (3930,  70.0),
    (4000,  80.0),
    (4080,  90.0),
    (4150,  95.0),
    (4200, 100.0),
]

# Treat the SOC reading as "loaded" (terminal voltage != OCV) when |I|
# exceeds this. At rest the estimate is the most trustworthy.
SOC_REST_CURRENT_A = 2.0

# Time-to-full estimator: retain (ts, soc%) samples for SOC_ETA_HISTORY_S,
# slope over the most recent SOC_ETA_WINDOW_S when SOC is rising in that
# window, and fall back to the slope across all retained history when
# the window lands entirely inside a plateau (the BMS publishes SOC in
# 0.385%/count steps that can hold for >1000 s in CV taper). Until the
# deque has seen SOC_ETA_STABLE_TRANSITIONS distinct values, the ETA is
# tagged "(rough)" because a one- or two-transition slope can be off by
# ~100%. Linear extrapolation is also optimistic near 100% SOC.
SOC_ETA_HISTORY_S = 7200.0          # retain up to 2 h of SOC samples
SOC_ETA_WINDOW_S = 1800.0           # preferred slope window (30 min)
SOC_ETA_MIN_SPAN_S = 30.0           # need at least this much data first
SOC_ETA_STABLE_TRANSITIONS = 3      # transitions before dropping "(rough)"

# Pack-power sparkline: keep the last POWER_HISTORY_S of (ts, W) samples
# and bucket them into POWER_SPARK_WIDTH columns at render time. F100F3
# arrives at ~10 Hz so 60 s gives ~600 samples; bucketing averages them.
POWER_HISTORY_S = 60.0
POWER_SPARK_WIDTH = 30


def soc_from_cell_mv(mv: float) -> float:
    """Linear-interpolated SOC % from a per-cell OCV using NMC_OCV_TABLE.

    Clamps below the table's first point to 0 % and above the last to
    100 %. This is voltage-only; load and temperature are not corrected.
    """
    table = NMC_OCV_TABLE
    if mv <= table[0][0]:
        return 0.0
    if mv >= table[-1][0]:
        return 100.0
    for (v0, s0), (v1, s1) in zip(table, table[1:]):
        if v0 <= mv <= v1:
            return s0 + (s1 - s0) * (mv - v0) / (v1 - v0)
    return 0.0


def parse_id(can_id: int) -> Tuple[int, int]:
    """Return (pgn, source) from a 29-bit J1939 ID."""
    src = can_id & 0xFF
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    pgn = (pf << 8) | (ps if pf >= 0xF0 else 0)
    return pgn, src


def be16(hi: int, lo: int) -> int:
    return (hi << 8) | lo


def le16(lo: int, hi: int) -> int:
    return (hi << 8) | lo


# --- state store ------------------------------------------------------------

@dataclass
class Channel:
    """A single decoded value with the time it was last updated."""
    value: Optional[float] = None
    ts: Optional[float] = None  # time.monotonic() of last update

    def update(self, value: float, now: float) -> None:
        self.value = value
        self.ts = now

    def clear(self) -> None:
        self.value = None
        self.ts = None

    def is_stale(self, now: float) -> bool:
        return self.ts is None or (now - self.ts) > STALE_S


@dataclass
class State:
    # pack-level
    # pack_v_terminal: from F100F3 byte 1 (the BMS-published terminal
    # voltage; load-sensitive, anchored against 24-capture regression per
    # NOTES). Authoritative when present.
    # pack_v_est: from F102 cell-min/max mean (20 * (max+min)/2). Cheap
    # cross-check / fallback before any F100 frame has been seen.
    pack_v_terminal: Channel = field(default_factory=Channel)
    pack_v_est: Channel = field(default_factory=Channel)
    pack_i_a: Channel = field(default_factory=Channel)
    # Derived: instantaneous pack power (V*I, signed). + draw / - charge.
    # Session-cumulative energy in Wh, integrated trapezoidally between
    # successive F100F3 frames; gated against gaps > 5 s (likely bus
    # dropouts) so we don't smear arbitrary power across the gap. The
    # last_energy_ts tracks the timestamp / power of the previous F100F3
    # frame for the trapezoidal step.
    pack_power_w: Channel = field(default_factory=Channel)
    # Recent (ts, pack_power_w) samples for the sparkline. Pruned to
    # POWER_HISTORY_S so the panel always shows the last minute.
    power_history: Deque[Tuple[float, float]] = field(default_factory=deque)
    energy_wh_drawn: float = 0.0
    energy_wh_charged: float = 0.0
    last_energy_ts: Optional[float] = None
    last_energy_p: Optional[float] = None
    # charger
    chgr_v: Channel = field(default_factory=Channel)
    chgr_i: Channel = field(default_factory=Channel)
    chgr_status: Channel = field(default_factory=Channel)
    # vehicle controller
    vc_state_raw: Channel = field(default_factory=Channel)
    # motor controller (FF21CA)
    motor_rpm: Channel = field(default_factory=Channel)        # signed (dir * |rpm|)
    motor_rpm_mag: Channel = field(default_factory=Channel)    # |rpm| magnitude
    motor_throttle: Channel = field(default_factory=Channel)
    motor_direction: Channel = field(default_factory=Channel)  # -1 R / 0 N / +1 F (byte 7 low nibble)
    motor_range_gear: Channel = field(default_factory=Channel) # 1..3 (byte 7 high nibble)
    # FF21CA bytes 4 and 5 are both J1939 +40 C-offset temps; byte 4 is
    # the main controller (consistently warmer, ramps from cold-start) and
    # byte 5 is the motor housing.
    controller_temp_c: Channel = field(default_factory=Channel)
    motor_temp_c: Channel = field(default_factory=Channel)
    # F102 cell summary
    max_cell_mv: Channel = field(default_factory=Channel)
    min_cell_mv: Channel = field(default_factory=Channel)
    spread_mv: Channel = field(default_factory=Channel)
    max_cell_n: Channel = field(default_factory=Channel)  # 1-based per BMS
    min_cell_n: Channel = field(default_factory=Channel)  # 1-based per BMS
    # F104 temp summary (symmetric with F102)
    temp_max_c: Channel = field(default_factory=Channel)
    temp_min_c: Channel = field(default_factory=Channel)
    temp_spread_c: Channel = field(default_factory=Channel)
    temp_max_n: Channel = field(default_factory=Channel)  # 1-based per BMS
    temp_min_n: Channel = field(default_factory=Channel)  # 1-based per BMS
    # F106 BMS state bitmap
    bms_state_byte0: Channel = field(default_factory=Channel)
    bms_state_byte1: Channel = field(default_factory=Channel)
    bms_output_enable: Channel = field(default_factory=Channel)    # b0 bit 0
    bms_main_contactor: Channel = field(default_factory=Channel)   # b0 bit 2
    bms_operating: Channel = field(default_factory=Channel)        # b0 bit 6
    bms_standby: Channel = field(default_factory=Channel)          # b0 bit 7
    bms_charging: Channel = field(default_factory=Channel)         # b1 bit 3
    bms_charger_present: Channel = field(default_factory=Channel)  # b1 bit 2
    bms_drive_mode: Channel = field(default_factory=Channel)       # b1 bit 5
    bms_contactors: Channel = field(default_factory=Channel)       # b1 bit 6
    # F107 BMS current limits
    limit_discharge_a: Channel = field(default_factory=Channel)
    limit_charge_a: Channel = field(default_factory=Channel)
    limit_mode: Channel = field(default_factory=Channel)           # 0 chg / 1 drv
    # Voltage-derived SOC (from min cell mV via NMC OCV table).
    soc_pct: Channel = field(default_factory=Channel)
    # BMS-published SOC (F100F3 byte 4). More authoritative than the
    # voltage-only estimate when present; preferred in render_pack.
    bms_soc_pct: Channel = field(default_factory=Channel)
    # Recent (ts, bms_soc%) samples used to estimate time-to-full during
    # charging. Pruned to SOC_ETA_HISTORY_S; the estimator prefers slope
    # over the SOC_ETA_WINDOW_S window and falls back to the full
    # retained history when the window lands inside a SOC plateau.
    soc_history: Deque[Tuple[float, float]] = field(default_factory=deque)
    # F108 fault bitmap bytes. Bytes 0..6 carry vendor codes 100..127 at
    # 2 bits per code (4 codes per byte; code = 100 + 4*byte + pair_index
    # over bit pairs (0,1)(2,3)(4,5)(6,7)); see active_bms_faults. Byte 7
    # is the system/maintenance code bitmap (1 bit per code, decoded
    # against BMS_FAULT_CODES_BYTE7). All 8 bytes are tracked so the TUI
    # can show raw bitmap state alongside decoded codes.
    fault_bytes: List[Channel] = field(
        default_factory=lambda: [Channel() for _ in range(8)]
    )
    # DM1 (J1939-73 Active DTCs) from motor ECU 0xCA. We track raw
    # lamp/flash bytes and the most recent active DTC fields. Cleared
    # back to None when an idle frame (00 00 00 00 00 00 FF FF) arrives,
    # so the panel reflects "presently inactive" instead of "last fault
    # ever observed".
    dm1_lamp_byte: Channel = field(default_factory=Channel)
    dm1_flash_byte: Channel = field(default_factory=Channel)
    dm1_spn: Channel = field(default_factory=Channel)
    dm1_fmi: Channel = field(default_factory=Channel)
    dm1_cm: Channel = field(default_factory=Channel)
    dm1_oc: Channel = field(default_factory=Channel)
    # 1806E5F4: BMS-to-charger command. The BMS-side address 0xF4
    # publishes voltage and current setpoints (and an enable flag) to
    # the charger at 0xE5. Idle frames clear voltage/current to None
    # while keeping enable visible, so the panel mirrors charger state
    # rather than freezing on the last active setpoint.
    chgr_cmd_v_v: Channel = field(default_factory=Channel)
    chgr_cmd_i_a: Channel = field(default_factory=Channel)
    chgr_cmd_enable: Channel = field(default_factory=Channel)
    # 0x18FF2112: dashboard / instrument-cluster heartbeat at 10 Hz.
    # byte 0 = alive flag (0 during ~700 ms boot, 1 thereafter); other
    # bytes always zero. Useful as a liveness check: if this Channel
    # goes stale the dashboard ECU has likely dropped off the bus.
    dash_alive: Channel = field(default_factory=Channel)
    # per-cell / per-temp arrays (indexed 0-based; display is 1-based)
    cells: List[Channel] = field(
        default_factory=lambda: [Channel() for _ in range(NUM_CELLS)]
    )
    temps: List[Channel] = field(
        default_factory=lambda: [Channel() for _ in range(NUM_TEMPS)]
    )
    # session counters
    frames: int = 0
    decoded: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.monotonic)


# --- helpers ----------------------------------------------------------------

def primary_pack_v(state: State) -> Channel:
    """Return the more authoritative pack-voltage Channel: the F100F3
    BMS-published terminal voltage when present, else the F102-derived
    cell-mean estimate. Both are kept in state so a fallback exists
    before the first F100 frame arrives.
    """
    if state.pack_v_terminal.value is not None:
        return state.pack_v_terminal
    return state.pack_v_est


# --- decoder ----------------------------------------------------------------

def decode(msg: "can.Message", state: State, now: float) -> None:
    """Update state from a single CAN frame."""
    state.frames += 1
    if not getattr(msg, "is_extended_id", True):
        return
    try:
        pgn, src = parse_id(msg.arbitration_id)
    except Exception:
        state.errors += 1
        return

    data = list(msg.data) + [0] * max(0, 8 - len(msg.data))

    if src == SRC_BMS:
        if PGN_CELL_FIRST <= pgn <= PGN_CELL_LAST:
            if all(b == 0 for b in data):
                return
            base = (pgn - PGN_CELL_FIRST) * 4
            for slot in range(4):
                idx = base + slot
                if idx >= NUM_CELLS:
                    continue
                mv = be16(data[2 * slot], data[2 * slot + 1])
                if mv == 0 or mv == 0xFFFF:
                    continue
                state.cells[idx].update(mv, now)
            state.decoded += 1

        elif PGN_TEMP_FIRST <= pgn <= PGN_TEMP_LAST:
            if all(b == 0 for b in data):
                return
            base = (pgn - PGN_TEMP_FIRST) * 8
            for slot, b in enumerate(data):
                idx = base + slot
                if idx >= NUM_TEMPS:
                    continue
                if b == 0 or b == 0xFF:
                    continue
                state.temps[idx].update(b - TEMP_OFFSET_C, now)
            state.decoded += 1

        elif pgn == PGN_F100:
            if all(b == 0 for b in data):
                return
            # byte 1 = pack terminal voltage (b * 0.1 + 76.8 V).
            state.pack_v_terminal.update(
                data[1] * PACK_VOLTAGE_LSB_V + PACK_VOLTAGE_OFFSET_V, now
            )
            # bytes 2-3 BE = signed pack current (biased u16, 0.1 A/bit).
            raw_i = be16(data[2], data[3])
            state.pack_i_a.update(
                (raw_i - PACK_CURRENT_BIAS_RAW) * PACK_CURRENT_LSB_A, now
            )
            # Derived: instantaneous pack power (V * I, signed; + draw /
            # - charge). Integrate trapezoidally over the gap to the
            # previous F100F3 frame for session Wh totals; gap > 5 s is
            # treated as a bus dropout and skipped (we don't know what
            # was happening in between). F100F3 publishes at ~10 Hz so
            # the typical dt is ~100 ms.
            volts = state.pack_v_terminal.value
            amps = state.pack_i_a.value
            if volts is not None and amps is not None:
                power = volts * amps
                state.pack_power_w.update(power, now)
                state.power_history.append((now, power))
                p_cutoff = now - POWER_HISTORY_S
                while (state.power_history
                       and state.power_history[0][0] < p_cutoff):
                    state.power_history.popleft()
                if (state.last_energy_ts is not None
                        and state.last_energy_p is not None):
                    dt = now - state.last_energy_ts
                    if 0.0 < dt <= 5.0:
                        p0, p1 = state.last_energy_p, power
                        avg_pos = (max(p0, 0.0) + max(p1, 0.0)) / 2.0
                        avg_neg = (min(p0, 0.0) + min(p1, 0.0)) / 2.0
                        state.energy_wh_drawn += avg_pos * dt / 3600.0
                        state.energy_wh_charged += -avg_neg * dt / 3600.0
                state.last_energy_ts = now
                state.last_energy_p = power
            # byte 4 = BMS-published State-of-Charge. Linear fit through
            # (224, 90%) and (250, 100%) from charging-120V-90ish-to-100.asc;
            # saturates at 250 in soc-100-idle.asc. Slope is loose pending
            # a deeper-discharge capture.
            state.bms_soc_pct.update(data[4] * 0.385 + 3.8, now)
            # Sliding window of SOC samples for the time-to-full ETA.
            state.soc_history.append((now, state.bms_soc_pct.value))
            cutoff = now - SOC_ETA_HISTORY_S
            while (state.soc_history
                   and state.soc_history[0][0] < cutoff):
                state.soc_history.popleft()
            state.decoded += 1

        elif pgn == PGN_F108:
            # All zeros in healthy idle. Bytes 0..6 carry vendor codes
            # 100..127 (2 bits per code). Byte 7 carries the system /
            # maintenance code bitmap (1 bit per code). See
            # active_bms_faults for the full decode.
            for i in range(8):
                state.fault_bytes[i].update(data[i], now)
            state.decoded += 1

        elif pgn == PGN_F102:
            # F102 layout (corpus survey, 36,950 active frames):
            #   bytes 0-1 BE = max cell mV
            #   bytes 2-3 BE = min cell mV
            #   byte 4       = max-cell channel (1-based, per BMS GUI)
            #   byte 5       = min-cell channel (1-based, per BMS GUI)
            #   byte 6       = 0x00 padding (constant across corpus)
            #   byte 7       = cell spread mV (max - min, 1 mV/bit; matches
            #                  the computed (max-min) in 36,950/36,950
            #                  corpus frames -- previously called 'flags')
            if all(b == 0 for b in data):
                return
            max_mv = be16(data[0], data[1])
            min_mv = be16(data[2], data[3])
            if max_mv == 0 or min_mv == 0:
                return
            state.max_cell_mv.update(max_mv, now)
            state.min_cell_mv.update(min_mv, now)
            state.spread_mv.update(max_mv - min_mv, now)
            state.max_cell_n.update(data[4], now)
            state.min_cell_n.update(data[5], now)
            state.pack_v_est.update(
                NUM_CELLS * (max_mv + min_mv) / 2.0 / 1000.0, now
            )
            # SOC tracks the limiting (lowest) cell; conservative.
            state.soc_pct.update(soc_from_cell_mv(min_mv), now)
            state.decoded += 1

        elif pgn == PGN_F104:
            # Symmetric with F102 but for module temperatures. Layout
            # cross-validated against per-channel temp.NN.c values from
            # F155..F15E in every capture (see analyze.py for detail).
            if all(b == 0 for b in data) or data[0] == 0xFF:
                return
            state.temp_max_c.update(data[0] - TEMP_OFFSET_C, now)
            state.temp_min_c.update(data[1] - TEMP_OFFSET_C, now)
            state.temp_max_n.update(data[2], now)
            state.temp_min_n.update(data[3], now)
            state.temp_spread_c.update(data[4], now)
            state.decoded += 1

        elif pgn == PGN_F106:
            # BMS state. Across 36,955 frames in 30 captures only six
            # (b0, b1) clusters appear (see analyze.py F106 block for
            # full survey). Byte 0 is a top-level mode bitfield:
            #   bit 0 = output_enable (drive/charge command active)
            #   bit 2 = main_contactor closed
            #   bit 6 = operating (power flowing)
            #   bit 7 = standby (charger present, no main current)
            # bits 6 and 7 are perfectly mutually exclusive across the
            # corpus (operating vs standby).
            # Byte 1 carries the secondary bitmap (charging / charger
            # present / drive mode / contactors).
            if all(b == 0 for b in data):
                return
            state.bms_state_byte0.update(data[0], now)
            state.bms_state_byte1.update(data[1], now)
            b0 = data[0]
            state.bms_output_enable.update(1 if b0 & 0x01 else 0, now)
            state.bms_main_contactor.update(1 if b0 & 0x04 else 0, now)
            state.bms_operating.update(1 if b0 & 0x40 else 0, now)
            state.bms_standby.update(1 if b0 & 0x80 else 0, now)
            b1 = data[1]
            state.bms_charging.update(1 if b1 & 0x08 else 0, now)
            state.bms_charger_present.update(1 if b1 & 0x04 else 0, now)
            state.bms_drive_mode.update(1 if b1 & 0x20 else 0, now)
            state.bms_contactors.update(1 if b1 & 0x40 else 0, now)
            state.decoded += 1

        elif pgn == PGN_F107:
            # BMS current limits, two BE u16 fields at 0.01 A/bit.
            # Drive captures: 145.0 A discharge / 100.0 A charge.
            # Charging captures: 100.0 A / 100.0 A.
            # Byte 4 = mode flag (0=charging, 1=driving).
            # Byte 5 = coarse-quantized pack-voltage echo. Across 5,326
            # driving frames it tracks F100F3 V_pack at R^2=0.97 with
            # V_pack ~= b5 * 0.2212 + 57.01 (~0.22 V/bit step); 0x00
            # while charging; rare transients 0x4D/0x6B/0xA7 in init/
            # teardown windows. Not surfaced separately here -- the
            # full-precision pack voltage is already on state.pack_v.
            if all(b == 0 for b in data):
                return
            i_dis = be16(data[0], data[1]) * LIMIT_CURRENT_LSB_A
            i_chg = be16(data[2], data[3]) * LIMIT_CURRENT_LSB_A
            state.limit_discharge_a.update(i_dis, now)
            state.limit_charge_a.update(i_chg, now)
            state.limit_mode.update(data[4], now)
            state.decoded += 1

    elif src == SRC_VEHICLE and pgn == PGN_F100:
        # Minimal vehicle-controller heartbeat. Across 22,338 frames in
        # 30 captures, byte 0 only ever takes 0x00 (init/transition,
        # 19 frames) or 0x0C (ready, 22,319 frames); bytes 1..7 are
        # always 0xFF (J1939 "not available" sentinel). The 0x00 burst
        # leads BMS F106 mode transitions by ~0.5-1 s.
        state.vc_state_raw.update(data[0], now)
        state.decoded += 1

    elif src == SRC_MOTOR and pgn == PGN_FF21:
        # Layout (across 45,086 frames in 30 captures):
        #   byte 0 = throttle pedal (raw, ~0..0x69)
        #   byte 1 = always 0x00 (reserved padding)
        #   bytes 2-3 = RPM magnitude (LE u16, biased)
        #   byte 4 = controller temp (J1939 +40 C offset, 0=absent)
        #   byte 5 = motor temp     (J1939 +40 C offset, 0=absent)
        #   byte 6 = always 0x00 (reserved padding)
        #   byte 7 = packed (range_gear << 4) | direction
        #            high nibble 0x0/0x1/0x2 = Range 1/2/3
        #            low  nibble 0x0/0x4/0x8 = N / F / R
        # bytes 2-3 little-endian, biased by 0x0C80, give RPM magnitude.
        rpm_mag = ((data[3] << 8) | data[2]) - RPM_BIAS
        # Low nibble of byte 7 = F/N/R lever; verified by drive-r-n-f.asc
        # walking R->N->F (byte 7: 0x28->0x20->0x24). High nibble = range
        # gear; verified by range-1-2-3.asc walking 1->2->3.
        fnr = data[7] & 0x0F
        if fnr == 0x4:
            direction = 1            # forward
        elif fnr == 0x8:
            direction = -1           # reverse
        else:
            direction = 0            # neutral
        range_gear = ((data[7] >> 4) & 0x0F) + 1
        state.motor_rpm_mag.update(rpm_mag, now)
        state.motor_rpm.update(direction * rpm_mag, now)
        state.motor_direction.update(direction, now)
        state.motor_range_gear.update(range_gear, now)
        state.motor_throttle.update(data[0], now)
        # bytes 4 and 5 are both J1939 +40 C-offset temperatures; byte 4
        # is the main controller and byte 5 is the motor (per the
        # cold-start ramp observed in ignition-without-charger-inserted.asc:
        # byte 4 climbs 0->19 C while byte 5 stays at 13 C). Raw 0 means
        # "not present" and is suppressed.
        if data[4]:
            state.controller_temp_c.update(data[4] - TEMP_OFFSET_C, now)
        if data[5]:
            state.motor_temp_c.update(data[5] - TEMP_OFFSET_C, now)
        state.decoded += 1

    elif src == SRC_DASH and pgn == PGN_FF21:
        # Dashboard heartbeat from SA 0x12 (same PGN as motor telemetry,
        # different sender). Byte 0 is a 0=booting / 1=alive flag broadcast
        # at 10 Hz; bytes 1..7 are always zero.
        state.dash_alive.update(data[0], now)
        state.decoded += 1

    elif src == SRC_MOTOR and pgn == PGN_FECA:
        # SAE J1939-73 DM1: Active Diagnostic Trouble Codes from the
        # motor ECU. Single-frame layout (no multi-frame BAM observed):
        #   data[0]   lamp status   (4 lamps x 2 bits)
        #   data[1]   flash status  (same lamp layout)
        #   data[2-5] first DTC: SPN (19), FMI (5), CM (1), OC (7)
        #   data[6-7] padding 0xFF
        # Idle/healthy convention: 00 00 00 00 00 00 FF FF (every frame
        # in our captures so far). Clear the channels on idle so the
        # panel shows "no active fault" rather than the most recent
        # historical fault.
        lamp_byte = data[0]
        flash_byte = data[1]
        spn = (data[2]
               | (data[3] << 8)
               | (((data[4] >> 5) & 0x07) << 16))
        fmi = data[4] & 0x1F
        cm = (data[5] >> 7) & 0x01
        oc = data[5] & 0x7F
        if lamp_byte == 0 and flash_byte == 0 and spn == 0 and fmi == 0:
            for ch in (state.dm1_lamp_byte, state.dm1_flash_byte,
                       state.dm1_spn, state.dm1_fmi,
                       state.dm1_cm, state.dm1_oc):
                ch.clear()
        else:
            state.dm1_lamp_byte.update(lamp_byte, now)
            state.dm1_flash_byte.update(flash_byte, now)
            if spn != 0 or fmi != 0:
                state.dm1_spn.update(spn, now)
                state.dm1_fmi.update(fmi, now)
                state.dm1_cm.update(cm, now)
                state.dm1_oc.update(oc, now)
            else:
                state.dm1_spn.clear()
                state.dm1_fmi.clear()
                state.dm1_cm.clear()
                state.dm1_oc.clear()
        state.decoded += 1

    elif src == SRC_CHARGER and pgn == PGN_FF50:
        # FF50E5 (charger telemetry, 3,108 frames across 5 of 30 captures).
        # Layout established by full-corpus survey:
        #   byte 0   = status (0x00 idle, 0x01/0x02 handshake, 0x03 active)
        #   bytes 1-2 LE = pack/output voltage (0.1 V/bit, +0 V offset)
        #   byte 3   = output current (0.1 A/bit)
        #   byte 4   = status-flags bitmap (NOT current high byte!):
        #                bit 2 = output disabled
        #                bit 3 = AC line OK
        #                bit 4 = AC line not detected
        #   bytes 5-7 = 0x00 padding (constant across all observed frames)
        # Voltage/current are only physically meaningful while
        # status == 0x03 AND flags == 0x00. Idle / handshake / faulted
        # frames carry leftover values that decode to nonsense (e.g.
        # byte 3 = 0x08 in idle would decode to 0.8 A even though no
        # current flows). Only emit V/I in the clean active state; clear
        # them otherwise so the TUI shows '---' instead of garbage.
        if all(b == 0 for b in data):
            return
        status = data[0]
        flags = data[4]
        state.chgr_status.update(status, now)
        if status == CHGR_STATUS_ACTIVE and flags == 0x00:
            v_raw = le16(data[1], data[2])
            state.chgr_v.update(v_raw * CHARGER_V_LSB_V + CHARGER_V_OFFSET_V, now)
            state.chgr_i.update(data[3] * CHARGER_I_LSB_A, now)
        else:
            state.chgr_v.clear()
            state.chgr_i.clear()
        state.decoded += 1

    elif src == SRC_BMS_CHGR_IF and pgn == PGN_PROP_0600:
        # BMS->Charger command. Bytes 0-1 BE = V setpoint (0.1 V/bit, no
        # offset, always 84.6 V during active requests), bytes 2-3 BE =
        # I setpoint (0.1 A/bit), byte 4 = enable (0 = active, 1 = idle),
        # bytes 5-7 padding 0xFF. Idle frame is 00 00 00 00 01 FF FF FF;
        # when seen, clear V/I so the panel doesn't freeze on the last
        # active setpoint.
        v_set_raw = be16(data[0], data[1])
        i_set_raw = be16(data[2], data[3])
        enable = data[4]
        idle_pattern = (v_set_raw == 0 and i_set_raw == 0
                        and enable in (0, 1)
                        and all(b == 0xFF for b in data[5:]))
        state.chgr_cmd_enable.update(enable, now)
        if idle_pattern:
            state.chgr_cmd_v_v.clear()
            state.chgr_cmd_i_a.clear()
        else:
            state.chgr_cmd_v_v.update(round(v_set_raw * 0.1, 1), now)
            state.chgr_cmd_i_a.update(round(i_set_raw * 0.1, 1), now)
        state.decoded += 1


# --- BMS faults -------------------------------------------------------------

def active_bms_faults(state: State) -> List[Tuple[int, str]]:
    """Return [(code_number, description), ...] for currently active codes
    in F108. Bytes 0..6 are decoded per BMS_FAULT_CODES_BYTES_0_TO_6
    (mixed 2-bit/1-bit encoding by byte); byte 7 is decoded per
    BMS_FAULT_CODES_BYTE7 (1 bit per code with gaps; bits 5 and 6 both
    = 144).

    Descriptions come from the operator-manual BMS_FAULT_DESCRIPTIONS
    table.
    """
    active: set = set()
    for byte_idx, codes in BMS_FAULT_CODES_BYTES_0_TO_6.items():
        b = state.fault_bytes[byte_idx].value
        if b is None:
            continue
        b = int(b)
        for bit_idx, code in enumerate(codes):
            if code is None:
                continue
            if (b >> bit_idx) & 1:
                active.add(code)
    b7 = state.fault_bytes[7].value
    if b7 is not None:
        b7 = int(b7)
        for bit, code in BMS_FAULT_CODES_BYTE7:
            if (b7 >> bit) & 1:
                active.add(code)
    return [(code, describe_bms_code(code)) for code in sorted(active)]


# --- alerts -----------------------------------------------------------------

def evaluate_alerts(state: State, mains_v: float, breaker_a: float,
                    efficiency: float, now: float) -> List[Tuple[str, str]]:
    alerts: List[Tuple[str, str]] = []

    for i, c in enumerate(state.cells):
        if c.value is None:
            continue
        mv = c.value
        if mv < 3000:
            alerts.append(("CRIT", f"cell #{i + 1} below 3.00 V "
                                   f"({mv / 1000:.3f} V)"))
        elif mv < 3300:
            alerts.append(("WARN", f"cell #{i + 1} below 3.30 V "
                                   f"({mv / 1000:.3f} V)"))
        if mv > 4200:
            alerts.append(("CRIT", f"cell #{i + 1} above 4.20 V "
                                   f"({mv / 1000:.3f} V)"))

    if state.spread_mv.value is not None and state.spread_mv.value > 100:
        alerts.append(("WARN", f"cell spread {int(state.spread_mv.value)} mV "
                               f"> 100 mV"))

    for i, t in enumerate(state.temps):
        if t.value is None:
            continue
        if t.value > 55:
            alerts.append(("CRIT",
                           f"T{i} = {t.value} °C ({c_to_f(t.value):.0f} °F)"
                           f" > 55 °C"))

    temp_vals = [t.value for t in state.temps if t.value is not None]
    if len(temp_vals) >= 2:
        delta = max(temp_vals) - min(temp_vals)
        if delta > 10:
            alerts.append(("WARN",
                           f"temp delta {delta} °C ({delta * 9 / 5:.0f} °F)"
                           f" > 10 °C"))

    # AC-supply budget (only meaningful while actively charging, status 0x03;
    # handshake states 0x01/0x02 are too transient to draw breaker power).
    chgr_active = (state.chgr_status.value == CHGR_STATUS_ACTIVE
                   and not state.chgr_status.is_stale(now))
    pack_v = primary_pack_v(state)
    if (chgr_active and pack_v.value
            and state.pack_i_a.value is not None
            and state.pack_i_a.value < 0):
        dc_w = pack_v.value * -state.pack_i_a.value
        ac_w = dc_w / max(efficiency, 0.01)
        ac_a = ac_w / max(mains_v, 1.0)
        if ac_a > 0.8 * breaker_a:
            alerts.append(("WARN",
                           f"est AC draw {ac_a:.1f} A > 80% of "
                           f"{breaker_a:.0f} A breaker"))

    # Stale BMS heartbeat while VC says we're awake.
    if (state.frames > 100
            and state.vc_state_raw.value == 0x0C
            and state.pack_i_a.is_stale(now)):
        alerts.append(("CRIT", "no F100 frame from BMS in > 2 s"))

    # Active BMS warning codes (F108 byte 7).
    faults = active_bms_faults(state)
    if faults:
        items = ", ".join(f"{c} ({d})" for c, d in faults)
        alerts.append(("WARN", f"BMS reports active fault codes: {items}"))

    return alerts


# --- TUI rendering ----------------------------------------------------------

# Sparkline glyphs all grow from the bottom; sign is communicated by
# colour (red = drawing, green = charging) since unicode block elements
# don't have a clean symmetric set that grows above and below a baseline.
_SPARK_LEVELS = " ▁▂▃▄▅▆▇█"   # 0 = baseline tick, 8 = max magnitude


def power_sparkline(state: State, width: int = POWER_SPARK_WIDTH) -> Text:
    """Return a coloured unicode sparkline of recent pack power.

    Buckets samples into `width` columns by timestamp. Bar height encodes
    |power| scaled to the window's peak; colour encodes sign (red =
    drawing, green = charging). Empty buckets render as a dim tick.
    """
    samples = list(state.power_history)
    if not samples:
        return Text("─" * width, style="dim")

    t0 = samples[0][0]
    t1 = samples[-1][0]
    span = max(t1 - t0, 1e-6)
    buckets: List[List[float]] = [[] for _ in range(width)]
    for ts, p in samples:
        idx = int((ts - t0) / span * width)
        if idx >= width:
            idx = width - 1
        buckets[idx].append(p)

    means = [sum(b) / len(b) if b else None for b in buckets]
    abs_max = max((abs(m) for m in means if m is not None), default=0.0)
    if abs_max < 1.0:
        abs_max = 1.0  # avoid divide-by-zero / amplifying noise

    out = Text()
    last: Optional[float] = None
    for m in means:
        if m is None:
            # Carry the previous bucket value across short gaps so the
            # line doesn't look chopped up; render a dim tick when there
            # is no prior sample either.
            if last is None:
                out.append("─", style="dim")
                continue
            m = last
        last = m
        level = min(8, int(round(abs(m) / abs_max * 8)))
        ch = _SPARK_LEVELS[level]
        if level == 0:
            out.append(ch, style="dim")
        elif m >= 0:
            out.append(ch, style="red")
        else:
            out.append(ch, style="green")
    return out


# F106 flag display order. Tuples of (channel attribute, short label,
# style-when-active). Mode-style flags (operating/standby/charging/drive)
# get bold so they read as "what the BMS is currently doing"; the rest
# get plain green for "yes, this is on". Ordered to keep mutually
# exclusive primary-mode pills (op / stby / chg) adjacent.
_BMS_FLAGS: List[Tuple[str, str, str]] = [
    ("bms_main_contactor", "MC",     "bold green"),
    ("bms_contactors",     "ctct",   "green"),
    ("bms_output_enable",  "out",    "green"),
    ("bms_operating",      "OPER",   "bold green"),
    ("bms_standby",        "STBY",   "bold cyan"),
    ("bms_charging",       "CHG",    "bold green"),
    ("bms_drive_mode",     "DRIVE",  "bold green"),
    ("bms_charger_present", "chgr",  "green"),
]


def bms_flags_pills(state: State) -> Text:
    """Compact pill row for the eight F106 BMS state flags.

    Each pill is the abbreviated flag name; bright when the flag is set,
    dim when clear. Pills are separated by a thin '·' so the row reads
    as one line of state at a glance.
    """
    out = Text()
    sep = Text(" ")  # single space; pill colours give the visual break
    for i, (attr, label, style) in enumerate(_BMS_FLAGS):
        if i > 0:
            out.append_text(sep)
        ch: Channel = getattr(state, attr)
        v = ch.value
        if v is None:
            out.append(label, style="dim")
        elif int(v):
            out.append(label, style=style)
        else:
            out.append(label, style="dim")
    return out


def fmt(c: Channel, fmt_spec: str = "{:.2f}", unit: str = "",
        now: Optional[float] = None) -> Text:
    if c.value is None:
        return Text("---", style="dim")
    text = fmt_spec.format(c.value)
    if unit:
        text += f" {unit}"
    if now is not None and c.is_stale(now):
        return Text(text, style="yellow dim")
    return Text(text)


def render_header(state: State, now: float) -> Panel:
    uptime = now - state.started_at
    h = int(uptime // 3600)
    m = int((uptime % 3600) // 60)
    s = int(uptime % 60)
    rate = state.frames / uptime if uptime > 0 else 0.0
    line = (f"Up: {h:02d}:{m:02d}:{s:02d}    "
            f"Frames: {state.frames:,}    "
            f"Decoded: {state.decoded:,}    "
            f"Rate: {rate:.0f} fps    "
            f"Errors: {state.errors}")
    return Panel(Text(line), title="Solectrac BMS — LIVE",
                 border_style="cyan")


def render_pack(state: State, mains_v: float, efficiency: float,
                now: float) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="left")
    t.add_column(justify="left")

    pack_v_ch = primary_pack_v(state)
    t.add_row("voltage", fmt(pack_v_ch, "{:.2f}", "V", now))

    pi = state.pack_i_a.value
    chgr_active = (state.chgr_status.value == CHGR_STATUS_ACTIVE
                   and not state.chgr_status.is_stale(now))
    if pi is None:
        i_text = Text("---", style="dim")
    else:
        # F100F3 bytes 2-3 BE biased; sign comes from the field itself
        # (positive = drawing from pack, negative = charging into pack).
        if pi > 0.05:
            i_text = Text(f"+{pi:.1f} A (drawing)", style="red")
        elif pi < -0.05:
            i_text = Text(f"{pi:.1f} A (charging)", style="green")
        else:
            i_text = Text(f"{pi:.1f} A")
    t.add_row("current", i_text)

    # F107 BMS current limit headroom. Pick the relevant limit from the
    # sign of pack current: discharge limit when drawing, charge limit
    # when charging. ideas.txt: "you're at 18 A of a 200 A budget".
    if pi is not None:
        if pi >= 0 and state.limit_discharge_a.value is not None:
            limit_ch = state.limit_discharge_a
            kind = "discharge"
            used = pi
        elif pi < 0 and state.limit_charge_a.value is not None:
            limit_ch = state.limit_charge_a
            kind = "charge"
            used = -pi
        else:
            limit_ch = None
            kind = None
            used = 0.0
        limit = limit_ch.value if limit_ch is not None else None
        if limit is not None and limit > 0:
            frac = max(0.0, min(1.0, used / limit))
            bar_w = 14
            filled = int(round(frac * bar_w))
            bar = Text("█" * filled + "░" * (bar_w - filled))
            if frac >= 0.9:
                style = "bold red"
            elif frac >= 0.7:
                style = "yellow"
            else:
                style = None
            tag = " (stale)" if limit_ch.is_stale(now) else ""
            short = "dis" if kind == "discharge" else "chg"
            t.add_row(
                "limit",
                Text.assemble(
                    bar,
                    Text(f"  {used:>5.1f}/{limit:.0f}A {short}"
                         f"  {frac * 100:>3.0f}%{tag}",
                         style=style),
                ),
            )

    if pack_v_ch.value is not None and pi is not None:
        # Pack convention: positive current = discharging the pack.
        # Power into the pack (charging) is negative; power out is positive.
        dc_w = pack_v_ch.value * pi
        t.add_row("power", Text(f"{dc_w:+.0f} W"))
        if chgr_active and pi < 0:
            ac_w = -dc_w / max(efficiency, 0.01)
            ac_a = ac_w / max(mains_v, 1.0)
            t.add_row("AC est",
                      Text(f"~{ac_w:.0f} W  ({ac_a:.1f} A @ {mains_v:.0f} V, "
                           f"{efficiency * 100:.0f}% eff)"))

    # 60 s sparkline of pack power. Glyphs above the baseline (red) =
    # drawing, below (green) = charging. Empty until the first F100F3.
    if state.power_history:
        spark = power_sparkline(state)
        t.add_row(
            "trend",
            Text.assemble(
                spark,
                Text(f"  last {int(POWER_HISTORY_S)}s", style="dim"),
            ),
        )

    # F106 BMS state flags. Eight booleans decoded from byte 0/1; we
    # render them as pills so the operator can see at a glance whether
    # the contactor is closed, what mode the BMS is in, and whether the
    # charger is present. Bright = set, dim = clear, "?" = no F106 yet.
    if state.bms_state_byte0.value is not None:
        flags_row = bms_flags_pills(state)
        t.add_row("flags", flags_row)

    # Session-cumulative energy. Integrated across F100F3 frames since
    # stream start. Net is positive when the session has drawn more
    # than it charged.
    wh_out = state.energy_wh_drawn
    wh_in = state.energy_wh_charged
    if wh_out > 0.5 or wh_in > 0.5:
        t.add_row("", "")
        t.add_row("session draw", Text(f"{wh_out:.0f} Wh", style="red"))
        t.add_row("session charge", Text(f"{wh_in:.0f} Wh", style="green"))
        net = wh_out - wh_in
        net_style = "red" if net > 0 else "green"
        t.add_row("session net",
                  Text(f"{net:+.0f} Wh  "
                       f"({net / PACK_CAPACITY_WH * 100:+.1f}% of pack)",
                       style=net_style))

    # Tag the reading when the pack is under significant load: terminal
    # voltage is depressed (discharging) or elevated (charging) and the
    # voltage-only SOC won't match the true coulomb-counted state.
    # Applied to both rows; the BMS row is also tagged but not for the
    # same reason -- the BMS does its own coulomb counting -- it just
    # gives the user a hint that the two readings may diverge under load.
    if pi is not None and abs(pi) > SOC_REST_CURRENT_A:
        load_tag = " (loaded)" if pi > 0 else " (charging)"
    else:
        load_tag = ""

    def _soc_row(label: str, ch: Channel) -> Optional[Text]:
        if ch.value is None:
            return None
        soc = ch.value
        bar_w = 20
        filled = int(round(soc * bar_w / 100))
        bar = Text("█" * filled + "░" * (bar_w - filled))
        if soc < 15:
            soc_style = "bold red"
        elif soc < 30:
            soc_style = "yellow"
        else:
            soc_style = "green"
        tag = load_tag + (" stale" if ch.is_stale(now) else "")
        return Text.assemble(
            bar,
            Text(f"  {soc:>3.0f}%", style=soc_style),
            Text(tag, style="dim"),
        )

    bms_row = _soc_row("BMS", state.bms_soc_pct)
    if bms_row is not None:
        t.add_row("SOC (BMS)", bms_row)
    ocv_row = _soc_row("OCV", state.soc_pct)
    if ocv_row is not None:
        t.add_row("SOC (OCV)", ocv_row)

    # Runtime-to-empty estimate. Symmetric with the charger panel's
    # ETA-to-100%: same soc_history slope, opposite sign. Only shown
    # when the pack is actually being drawn from -- a parked tractor
    # with zero load would otherwise show a "(rough)" never-ending ETA.
    if (state.bms_soc_pct.value is not None
            and pi is not None and pi > SOC_REST_CURRENT_A):
        eta = estimate_drain_eta_s(state)
        if eta is None:
            t.add_row("ETA to 0%", Text("estimating...", style="dim"))
        elif count_soc_transitions(state) < SOC_ETA_STABLE_TRANSITIONS:
            t.add_row("ETA to 0%",
                      Text(f"{format_eta(eta)} (rough)", style="yellow"))
        else:
            t.add_row("ETA to 0%", Text(format_eta(eta)))

    return Panel(t, title="Pack", border_style="green")


def count_soc_transitions(state: State) -> int:
    """Number of times the BMS SOC value changed in the retained history.

    Used to gate the "(rough)" tag on the ETA: with only one or two
    transitions a single quantization step dominates the slope, so the
    estimate can be off by ~100%.
    """
    n = 0
    last: Optional[float] = None
    for _ts, sc in state.soc_history:
        if last is not None and sc != last:
            n += 1
        last = sc
    return n


def _estimate_soc_eta_s(state: State, target: float,
                        rising: bool) -> Optional[float]:
    """Generic SOC slope-extrapolation ETA used by the charge-to-100% and
    drain-to-0% estimators.

    Prefers the slope across the most recent SOC_ETA_WINDOW_S so the ETA
    tracks the current phase. Falls back to the full retained history
    when the window lands entirely inside a SOC plateau (the BMS holds
    each 0.385%/count step for >1000 s in CV taper). Returns None when
    SOC isn't moving in the requested direction.
    """
    samples = state.soc_history
    if len(samples) < 2:
        return None
    t1, s1 = samples[-1]
    if rising and s1 >= target:
        return 0.0
    if (not rising) and s1 <= target:
        return 0.0

    def _slope_eta(t0: float, s0: float) -> Optional[float]:
        span = t1 - t0
        if span < SOC_ETA_MIN_SPAN_S:
            return None
        rate = (s1 - s0) / span  # %/s, signed
        if rising and rate <= 0:
            return None
        if (not rising) and rate >= 0:
            return None
        return (target - s1) / rate

    cutoff = t1 - SOC_ETA_WINDOW_S
    for ts, sc in samples:
        if ts >= cutoff:
            eta = _slope_eta(ts, sc)
            if eta is not None:
                return eta
            break
    return _slope_eta(samples[0][0], samples[0][1])


def estimate_charge_eta_s(state: State) -> Optional[float]:
    """Seconds until BMS SOC reaches 100%, or None if not rising.

    Linear extrapolation is optimistic in the last ~10% because charge
    current tapers in CV.
    """
    return _estimate_soc_eta_s(state, target=100.0, rising=True)


def estimate_drain_eta_s(state: State) -> Optional[float]:
    """Seconds until BMS SOC reaches 0%, or None if not falling.

    Linear extrapolation; real packs hit a BMS cutoff above 0% and the
    cutback regions distort the slope, so this is "remaining at current
    pace" rather than a hard runtime.
    """
    return _estimate_soc_eta_s(state, target=0.0, rising=False)


def format_eta(secs: float) -> str:
    if secs <= 0:
        return "complete"
    if secs < 60:
        return "<1 min"
    hours = int(secs // 3600)
    minutes = int((secs % 3600) // 60)
    if hours > 0:
        return f"~{hours}h {minutes:02d}m"
    return f"~{minutes} min"


def render_charger(state: State, now: float) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="left")
    t.add_column(justify="left")

    cs = state.chgr_status.value
    stale = state.chgr_status.is_stale(now)
    if cs is None:
        st = Text("---", style="dim")
    elif stale:
        st = Text(f"stale  (last 0x{int(cs):02X})", style="yellow dim")
    elif cs == CHGR_STATUS_IDLE:
        st = Text("idle")
    elif cs in CHGR_HANDSHAKE_STATES:
        st = Text(f"handshake (status=0x{int(cs):02X})", style="yellow")
    elif cs == CHGR_STATUS_ACTIVE:
        st = Text("CHARGING (status=0x03)", style="bold green")
    else:
        st = Text(f"unknown (status=0x{int(cs):02X})", style="magenta")
    t.add_row("State", st)
    t.add_row("voltage", fmt(state.chgr_v, "{:.1f}", "V", now))
    t.add_row("current", fmt(state.chgr_i, "{:.1f}", "A", now))
    if state.chgr_v.value is not None and state.chgr_i.value is not None:
        t.add_row("power",
                  Text(f"{state.chgr_v.value * state.chgr_i.value:.0f} W"))

    # BMS->Charger setpoints from 1806E5F4. Show V/I requested by the
    # BMS alongside what the charger reports delivering, and surface the
    # enable flag (0=active request, 1=idle).
    en = state.chgr_cmd_enable.value
    if (state.chgr_cmd_v_v.value is not None
            or state.chgr_cmd_i_a.value is not None
            or en is not None):
        t.add_row("", "")
        if en is None:
            en_text = Text("---", style="dim")
        elif int(en) == 0:
            en_text = Text("active", style="green")
        elif int(en) == 1:
            en_text = Text("idle")
        else:
            en_text = Text(f"0x{int(en):02X}", style="magenta")
        t.add_row("BMS request", en_text)
        t.add_row("  V setpoint", fmt(state.chgr_cmd_v_v, "{:.1f}", "V", now))
        t.add_row("  I setpoint", fmt(state.chgr_cmd_i_a, "{:.1f}", "A", now))

    # Time-to-full estimate, shown only while actively charging. Based
    # on the slope of recent BMS SOC samples; CV taper near full will
    # make the linear extrapolation read low in the last ~10%.
    if cs == CHGR_STATUS_ACTIVE and not stale:
        t.add_row("", "")
        soc_now = state.bms_soc_pct.value
        if soc_now is not None and soc_now >= 99.5:
            t.add_row("ETA to 100%", Text("complete", style="green"))
        else:
            eta = estimate_charge_eta_s(state)
            if eta is None:
                t.add_row("ETA to 100%",
                          Text("estimating...", style="dim"))
            elif count_soc_transitions(state) < SOC_ETA_STABLE_TRANSITIONS:
                t.add_row("ETA to 100%",
                          Text(f"{format_eta(eta)} (rough)",
                               style="yellow"))
            else:
                t.add_row("ETA to 100%", Text(format_eta(eta)))

    return Panel(t, title="Charger", border_style="green")


def render_cells(state: State, now: float) -> Panel:
    cells = state.cells
    vals = [c.value for c in cells if c.value is not None]
    if not vals:
        return Panel(Text("(no cell data yet)", style="dim"),
                     title="Cell voltages", border_style="blue")

    lo, hi = min(vals), max(vals)
    span = max(1, hi - lo)
    bar_w = 6
    cols = 4  # cells per row (row-major: 5 rows × 4 cols for 20 cells)

    t = Table.grid(padding=(0, 1))
    for _ in range(cols):
        t.add_column(justify="right")
        t.add_column()
        t.add_column(justify="right")

    row: list = []
    for i, c in enumerate(cells):
        n = i + 1  # BMS-style 1-based display
        if c.value is None:
            bar = Text("·" * bar_w, style="dim")
            mv_text = Text("---", style="dim")
        else:
            frac = (c.value - lo) / span
            filled = int(round(frac * bar_w))
            bar = Text("█" * filled + "░" * (bar_w - filled))
            if c.value == hi:
                style = "bold green"
            elif c.value == lo:
                style = "bold red"
            elif c.is_stale(now):
                style = "yellow dim"
            else:
                style = None
            mv_text = Text(f"{int(c.value)} mV", style=style)
        row.extend([f"#{n:>2}", bar, mv_text])
        if len(row) == cols * 3:
            t.add_row(*row)
            row = []
    if row:
        # pad final row so add_row gets the right column count
        while len(row) < cols * 3:
            row.extend(["", Text(""), ""])
        t.add_row(*row)

    summary = Text()
    if state.max_cell_n.value is not None:
        summary.append(
            f"  BMS reports max #{int(state.max_cell_n.value)}, "
            f"min #{int(state.min_cell_n.value)}, "
            f"spread {int(state.spread_mv.value or 0)} mV"
        )

    return Panel(Group(t, summary),
                 title=f"Cell voltages  ({lo}–{hi} mV)",
                 border_style="blue")


def render_temps(state: State, now: float) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right")
    t.add_column(justify="left")
    for i, ch in enumerate(state.temps):
        label = Text(f"T{i+1}", style="dim")
        if ch.value is None:
            val = Text("---", style="dim")
        else:
            style = "yellow dim" if ch.is_stale(now) else None
            val = Text(f"{int(ch.value)}°C ({int(c_to_f(ch.value))}°F)",
                       style=style)
        t.add_row(label, val)

    vals = [c.value for c in state.temps if c.value is not None]
    if vals:
        lo, hi = min(vals), max(vals)
        delta = hi - lo
        sub = Text(f"Δ {delta}°C  {lo}–{hi}°C", style="dim")
    else:
        sub = Text("")
    return Panel(Group(t, sub), title="Temps", border_style="blue")


def render_motor(state: State, now: float) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="left")
    t.add_column(justify="left")

    rpm_mag = state.motor_rpm_mag.value
    if rpm_mag is None:
        rpm_text = Text("---", style="dim")
    else:
        mag = abs(int(rpm_mag))
        style = ("bold red" if mag > 2800
                else "green" if mag > 100
                else None)
        di = state.motor_direction.value
        sign = "-" if di == -1 else " "
        rpm_text = Text(f"{sign}{mag:>5d}", style=style)
        if state.motor_rpm_mag.is_stale(now):
            rpm_text = Text(f"{sign}{mag:>5d}  (stale)", style="yellow dim")
    t.add_row("RPM", rpm_text)

    thr = state.motor_throttle.value
    if thr is None:
        t.add_row("throttle", Text("---", style="dim"))
    else:
        # Byte 0 is treated as a 0..100% percent field; subtract a 3-unit
        # idle dead-low (resting sensor offset) and clamp at 100%. Range
        # 0..0x69 (105) seen across the full corpus; the rare overshoot
        # past 100 saturates rather than exceeding the bar.
        pct = max(0, min(THROTTLE_FULL_SCALE,
                         int(round(thr)) - THROTTLE_DEAD_LOW))
        bar_w = 20
        filled = int(round(pct * bar_w / 100))
        bar = Text("█" * filled + "░" * (bar_w - filled))
        t.add_row("throttle",
                  Text.assemble(bar, Text(f"  {pct:>3d}%  (raw {int(thr)})")))

    di = state.motor_direction.value
    if di is None:
        di_text = Text("---", style="dim")
    elif di == 1:
        di_text = Text("FORWARD", style="bold green")
    elif di == -1:
        di_text = Text("REVERSE", style="bold yellow")
    else:
        di_text = Text("NEUTRAL", style="dim")
    t.add_row("F/N/R", di_text)

    rg = state.motor_range_gear.value
    if rg is None:
        rg_text = Text("---", style="dim")
    else:
        rg_text = Text(f"R{int(rg)}")
    t.add_row("range", rg_text)

    def _temp_text(ch: Channel) -> Text:
        if ch.value is None:
            return Text("---", style="dim")
        text = f"{ch.value:.0f} °C ({c_to_f(ch.value):.0f} °F)"
        if ch.is_stale(now):
            return Text(text, style="yellow dim")
        return Text(text)

    t.add_row("ctrl temp", _temp_text(state.controller_temp_c))
    t.add_row("motor temp", _temp_text(state.motor_temp_c))

    return Panel(t, title="Motor controller", border_style="magenta")


def render_vc(state: State, now: float) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="left")
    t.add_column(justify="left")
    raw = state.vc_state_raw.value
    if raw is None:
        s = Text("---", style="dim")
    else:
        name = VC_STATE_NAMES.get(int(raw), "unknown")
        s = Text(f"{name}  (0x{int(raw):02X})")
    t.add_row("Heartbeat", s)
    if state.vc_state_raw.ts is not None:
        ago = now - state.vc_state_raw.ts
        t.add_row("Last F100D0", Text(f"{ago:.1f} s ago"))
    return Panel(t, title="Vehicle controller", border_style="magenta")


def render_faults(state: State, now: float) -> Panel:
    """Display BMS fault info from F108 plus DM1 (motor-ECU Active
    DTCs) from PGN FECA. Byte 7 of F108 has a decoded bit-to-code
    mapping (vendor BMS error-code table); other bytes are shown as
    raw values when nonzero so undecoded fault data is at least
    visible. DM1 is summarised in a compact section underneath.
    """
    bytes_seen = any(c.value is not None for c in state.fault_bytes)
    faults = active_bms_faults(state) if bytes_seen else []

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right")
    t.add_column(justify="left")

    if not bytes_seen:
        t.add_row(Text("F108", style="dim"),
                  Text("(no F108 frame seen yet)", style="dim"))
        nonzero_other = []
        raw = Text("", style="dim")
    else:
        # Stale if the most recent F108 byte update is older than STALE_S.
        stamps = [c.ts for c in state.fault_bytes if c.ts is not None]
        stale = (not stamps) or ((now - max(stamps)) > STALE_S)

        vals = [int(c.value) if c.value is not None else 0
                for c in state.fault_bytes]
        nonzero_other = [(i, v) for i, v in enumerate(vals)
                         if v != 0 and i != 7]

        if faults:
            for code, desc in faults:
                t.add_row(Text(f"{code}", style="bold red"), Text(desc))
        else:
            t.add_row(Text("byte 7", style="dim"),
                      Text("no codes from byte-7 group", style="green"))

        if nonzero_other:
            # Bytes 0..6 (excluding 7) with bit-position breakdown. Useful
            # while the bit-to-code mapping for these bytes is still open.
            for i, v in nonzero_other:
                bits = ", ".join(str(b) for b in range(8) if (v >> b) & 1)
                t.add_row(
                    Text(f"byte {i}", style="yellow"),
                    Text(f"0x{v:02X}  bits {{{bits}}}  (undecoded)",
                         style="yellow"),
                )

        raw_style = "yellow dim" if stale else "dim"
        raw_hex = " ".join(f"{v:02X}" for v in vals)
        raw = Text(
            f"raw  {raw_hex}" + ("  (stale)" if stale else ""),
            style=raw_style,
        )

    # DM1 section: always rendered. When healthy it's a single
    # "no active DTCs" line; when active, lamp + SPN/FMI rows.
    dm1 = _render_dm1(state, now)

    dm1_active = (state.dm1_lamp_byte.value or
                  state.dm1_flash_byte.value or
                  state.dm1_spn.value or state.dm1_fmi.value)
    if faults or nonzero_other or dm1_active:
        border = "red" if (faults or dm1_active) else "yellow"
    elif bytes_seen:
        border = "green"
    else:
        border = "blue"

    return Panel(Group(t, raw, dm1), title="Faults & DTCs",
                 border_style=border)


def _render_dm1(state: State, now: float):
    """Compact DM1 (motor-ECU Active DTC) summary, embedded in the
    Faults & DTCs panel. Shows a single 'no active DTCs' line when
    healthy, and lamp/SPN/FMI breakdowns when a fault is active."""
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right")
    t.add_column(justify="left")

    lamp = state.dm1_lamp_byte
    flash = state.dm1_flash_byte
    spn = state.dm1_spn
    fmi = state.dm1_fmi
    cm = state.dm1_cm
    oc = state.dm1_oc

    # If we've never seen a DM1 frame, lamp.ts is None and value is None.
    # If the last seen frame was idle, the decoder cleared all channels so
    # ts goes back to None. Either way we render a single status line.
    has_lamp = lamp.value is not None or flash.value is not None
    has_dtc = spn.value is not None or fmi.value is not None

    if not has_lamp and not has_dtc:
        # No active fault. Distinguish "never seen" from "actively idle"
        # using state.frames as a proxy -- after any traffic at all on
        # the bus, the motor ECU's 1 Hz DM1 broadcast will have arrived.
        msg = Text("DM1 (motor ECU): no active DTCs",
                   style="green")
        t.add_row("", msg)
        return t

    if has_lamp:
        lb = int(lamp.value or 0)
        fb = int(flash.value or 0)
        for i, name in enumerate(DM1_LAMP_NAMES):
            shift = 6 - 2 * i
            s = (lb >> shift) & 0x03
            f = (fb >> shift) & 0x03
            if s == 0 and f == 0:
                continue
            state_txt = DM1_LAMP_STATE.get(s, "?")
            flash_txt = DM1_FLASH_STATE.get(f, "?")
            style = "bold red" if s == 1 else "yellow"
            t.add_row(
                Text(f"DM1 {name}", style=style),
                Text(f"{state_txt}  flash={flash_txt}", style=style),
            )

    if has_dtc:
        spn_v = int(spn.value or 0)
        fmi_v = int(fmi.value or 0)
        oc_v = int(oc.value or 0)
        cm_v = int(cm.value or 0)
        fmi_name = DM1_FMI_NAMES.get(fmi_v, "?")
        t.add_row(
            Text("DM1 DTC", style="bold red"),
            Text(
                f"SPN={spn_v}  FMI={fmi_v} ({fmi_name})  "
                f"OC={oc_v}  CM={cm_v}",
                style="bold red",
            ),
        )

    return t


def render_alerts(alerts: List[Tuple[str, str]]) -> Panel:
    if not alerts:
        return Panel(Text("(none)", style="green"),
                     title="Alerts", border_style="green")
    t = Table.grid(padding=(0, 2))
    t.add_column()
    t.add_column()
    for sev, msg in alerts:
        style = "bold red" if sev == "CRIT" else "yellow"
        t.add_row(Text(sev, style=style), Text(msg))
    border = "red" if any(s == "CRIT" for s, _ in alerts) else "yellow"
    return Panel(t, title="Alerts", border_style=border)


def build_layout(state: State, args, now: float) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="row1", size=17),
        Layout(name="cells", size=11),
        Layout(name="row4", size=8),
        Layout(name="faults", size=15),
        Layout(name="alerts", size=8),
    )
    layout["header"].update(render_header(state, now))
    layout["row1"].split_row(
        Layout(render_pack(state, args.mains_v, args.efficiency, now)),
        Layout(render_charger(state, now)),
    )
    layout["cells"].split_row(
        Layout(render_cells(state, now), ratio=1),
        Layout(render_temps(state, now), ratio=1),
    )
    layout["row4"].split_row(
        Layout(render_motor(state, now)),
        Layout(render_vc(state, now)),
    )
    layout["faults"].update(render_faults(state, now))
    alerts = evaluate_alerts(state, args.mains_v, args.breaker_a,
                             args.efficiency, now)
    layout["alerts"].update(render_alerts(alerts))
    return layout


# --- frame source -----------------------------------------------------------

def open_source(args):
    """Return either a python-can Bus (live) or LogReader (replay)."""
    if args.replay:
        return can.LogReader(args.replay), "replay"
    kwargs = {}
    if args.bitrate is not None:
        kwargs["bitrate"] = args.bitrate
    return can.Bus(interface=args.interface, channel=args.channel,
                   **kwargs), "live"


# --- main -------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Live BMS / charger TUI for the Solectrac CAN bus.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--interface",
                     help="python-can interface (e.g. socketcan, slcan, pcan)")
    src.add_argument("--replay",
                     help="replay a python-can log file (.log/.asc/.blf)")
    p.add_argument("--channel",
                   help="bus channel for live capture (e.g. can0)")
    p.add_argument("--bitrate", type=int, default=None,
                   help="bus bitrate for live capture (e.g. 250000)")
    p.add_argument("--raw-log",
                   help="write a python-can log of all received frames")
    p.add_argument("--mains-v", type=float, default=120.0,
                   help="AC supply voltage for AC-draw estimate (default 120)")
    p.add_argument("--breaker-a", type=float, default=20.0,
                   help="AC breaker rating for alerting (default 20)")
    p.add_argument("--efficiency", type=float, default=0.85,
                   help="assumed AC->DC charger efficiency (default 0.85)")
    p.add_argument("--refresh-hz", type=float, default=5.0,
                   help="TUI refresh rate (default 5)")
    p.add_argument("--timescale", type=float, default=1.0,
                   help="for --replay, multiplier on realtime playback "
                        "(1.0 = recorded speed, 2.0 = 2x faster, "
                        "0.5 = half speed)")
    p.add_argument("--no-tui", action="store_true",
                   help="headless mode (just log, no display)")
    args = p.parse_args()

    if args.interface and not args.channel and args.interface != "virtual":
        p.error("--channel is required with --interface")

    state = State()
    source, mode = open_source(args)

    raw_logger: Optional["can.Listener"] = None
    if args.raw_log:
        raw_logger = can.Logger(args.raw_log)

    stop_evt = threading.Event()

    def reader_loop():
        try:
            if mode == "live":
                while not stop_evt.is_set():
                    msg = source.recv(timeout=0.1)
                    if msg is None:
                        continue
                    if raw_logger is not None:
                        raw_logger(msg)
                    decode(msg, state, time.monotonic())
            else:
                first_msg_ts: Optional[float] = None
                replay_start: Optional[float] = None
                timescale = args.timescale if args.timescale > 0 else 1.0
                for msg in source:
                    if stop_evt.is_set():
                        break
                    if raw_logger is not None:
                        raw_logger(msg)
                    if getattr(msg, "timestamp", None):
                        if first_msg_ts is None:
                            first_msg_ts = msg.timestamp
                            replay_start = time.monotonic()
                        else:
                            elapsed_log = (msg.timestamp - first_msg_ts) / timescale
                            target = replay_start + elapsed_log
                            delay = target - time.monotonic()
                            if delay > 0:
                                # break sleep into chunks so stop_evt is responsive
                                end = time.monotonic() + delay
                                while not stop_evt.is_set():
                                    remaining = end - time.monotonic()
                                    if remaining <= 0:
                                        break
                                    time.sleep(min(0.1, remaining))
                    decode(msg, state, time.monotonic())
                # signal that replay finished
                stop_evt.set()
        except Exception as e:
            state.errors += 1
            sys.stderr.write(f"reader error: {e}\n")

    reader = threading.Thread(target=reader_loop, daemon=True)
    reader.start()

    try:
        if args.no_tui:
            while reader.is_alive() and not stop_evt.is_set():
                reader.join(timeout=1.0)
        else:
            with Live(build_layout(state, args, time.monotonic()),
                      refresh_per_second=args.refresh_hz,
                      screen=True) as live:
                tick = 1.0 / max(args.refresh_hz, 1.0)
                while reader.is_alive() and not stop_evt.is_set():
                    live.update(build_layout(state, args, time.monotonic()))
                    time.sleep(tick)
                # Keep the final frame visible briefly when replay ends.
                if args.replay:
                    live.update(build_layout(state, args, time.monotonic()))
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        try:
            if hasattr(source, "shutdown"):
                source.shutdown()
        except Exception:
            pass
        if raw_logger is not None:
            try:
                raw_logger.stop()
            except Exception:
                pass
        reader.join(timeout=2.0)

    return 0


if __name__ == "__main__":
    sys.exit(main())
