#!/usr/bin/env python3
"""
Decode J1939-style CAN logs from a small electric tractor BMS / charger.

Usage:
    python3 solectrac-analyze.py [-o OUTDIR] file1.asc [file2.blf ...]

Inputs are read via python-can's LogReader, so any format python-can
understands works: .asc (Vector ASCII), .blf, .log (canutils), .trc, and
python-can's own .csv format. (SavvyCAN's CSV export is *not* supported
because python-can doesn't read that dialect.)

Outputs (written into OUTDIR, default: current working directory):
    signals.csv   one tidy row per scalar measurement:
                  file, timestamp, frame_index, signal, value, unit
    frames.csv    one row per decoded frame (frames that produced >=1 signal):
                  frame_index, file, timestamp, can_id, pgn, source, len,
                  b0, b1, b2, b3, b4, b5, b6, b7
    decoders.csv  per-signal decode rule catalog:
                  signal, pgn, source, bytes, formula, unit, confidence, notes
    ids.csv       one row per unique CAN ID seen, with J1939 decode
    stdout        per-scenario summary

`frame_index` joins signals.csv -> frames.csv so any decoded value can be
traced back to its source bytes; decoders.csv documents the formula used
for each signal name. Together they let you re-derive any value by hand.

The long format is what pandas calls "tidy" data; pivot to a wide table
in one line:

    df = pd.read_csv("signals.csv")
    wide = df.pivot_table(index="timestamp", columns="signal", values="value")

Signal names use a `domain.name` (or `domain.NN.name`) convention:
    cell.NN.voltage_v          per-cell voltage (NN = 0-based BMS index)
    temp.NN.c                  per-channel module temp (NN = 0-based)
    pack.cell_max_mv           PGN F102 derived pack-wide stats
    pack.cell_min_mv
    pack.cell_spread_mv
    pack.cell_max_n            F102 byte 4: max-cell number, 1-based (BMS GUI numbering)
    pack.cell_min_n            F102 byte 5: min-cell number, 1-based (BMS GUI numbering)
    pack.flags                 F102 byte 7 (status/flag bits, raw)
    pack.v_estimate            20 * mean(min, max) / 1000
    pack.voltage_v             F100 byte 1: pack voltage, b * 0.1 + 76.8 V
    pack.current_raw           F100 bytes 2-3 (raw biased u16)
    pack.current_a             F100 signed pack current, A
    pack.temp_max_c            F104 byte 0: pack max module temp, b - 40
    pack.temp_min_c            F104 byte 1: pack min module temp, b - 40
    pack.temp_max_n            F104 byte 2: max-temp channel # (1-based)
    pack.temp_min_n            F104 byte 3: min-temp channel # (1-based)
    pack.temp_spread_c         F104 byte 4: max - min temp (°C)
    bms.state.byte0/1          F106 raw state bytes
    bms.state.charging         F106 byte 1 bit 3 (charger active)
    bms.state.charger_present  F106 byte 1 bit 2 (charger plugged in)
    bms.state.drive_mode       F106 byte 1 bit 5 (motor enabled)
    bms.state.contactors       F106 byte 1 bit 6 (vehicle awake)
    bms.limit.discharge_a      F107 bytes 0-1 BE * 0.01: max discharge current
    bms.limit.charge_a         F107 bytes 2-3 BE * 0.01: max charge current
    bms.limit.mode             F107 byte 4: 0=charging, 1=driving
    bms.limit.byte5            F107 byte 5: slowly varying counter (raw)
    bms.fault.byteN            F108 bytes 0..7 raw (only emitted when frame is non-zero;
                               corresponds to DBC FaultByteN_Raw signals)
    bms.fault.code_NNN         F108 byte 7: 1 when bit set; NNN per vendor table
                               (124, 140, 142, 143, 144, 145, 146; corresponds to
                               DBC Fault_<NNN>_<ShortName> bit signals)
    charger.status             FF50 byte 0
    charger.v_raw              FF50 bytes 1-2 LE (raw, always emitted)
    charger.voltage_v          FF50 charger output voltage, raw * 0.1 + 76.8 V
                               (only emitted while status == 0x03)
    charger.i_raw              FF50 bytes 3-4 LE (raw, always emitted)
    charger.current_a          FF50 current, A
                               (only emitted while status == 0x03)
    chgr_cmd.voltage_v         0600 bytes 0-1 BE * 0.1: BMS->charger V setpoint
                               (no +76.8 V offset; suppressed when idle)
    chgr_cmd.current_a         0600 bytes 2-3 BE * 0.1: BMS->charger I setpoint
                               (suppressed when idle)
    chgr_cmd.enable            0600 byte 4: 0=active command, 1=idle
    chgr_cmd.v_raw             0600 bytes 0-1 BE raw
    chgr_cmd.i_raw             0600 bytes 2-3 BE raw
    vc.state                   F100D0 byte 0 (raw heartbeat state)
    motor.rpm_signed           FF21CA RPM with directional sign
    motor.rpm_magnitude        FF21CA RPM unsigned
    motor.direction            +1 forward / 0 idle / -1 reverse
    motor.throttle_raw         FF21CA byte 0
    motor.controller_temp_c    FF21CA byte 4 (only emitted when nonzero)
    motor.motor_temp_c         FF21CA byte 5 (only emitted when nonzero)
    pack.soc_raw               F100F3 byte 4 (raw)
    pack.soc_pct               F100F3 byte 4 -> percent (b4 * 0.385 + 3.8)
    dm1.lamp.byte0/1           FECA bytes 0/1 raw (lamp & flash status, when nonzero)
    dm1.lamp.NAME_state        FECA byte 0 per-lamp 2-bit state (NAME in
                               {malfunction, red_stop, amber_warning, protect})
    dm1.lamp.NAME_flash        FECA byte 1 per-lamp 2-bit flash status
    dm1.dtc.spn                FECA SAE J1939-73 SPN (19-bit)
    dm1.dtc.fmi                FECA J1939-73 FMI (5-bit failure mode)
    dm1.dtc.cm                 FECA SPN Conversion Method bit
    dm1.dtc.oc                 FECA Occurrence Count (7-bit)

Decoder assumptions (verify against the BMS spec before trusting numerically):
  * Source 0xF3 is the BMS (broadcasts), 0xE5 is the external charger,
    0xCA is the motor / drive ECU, 0xD0 is the vehicle controller, 0xF4
    is the BMS again in its charger-interface role (sends only PGN
    0x000600 destination-addressed to 0xE5).
    Byte numbering below is 0-based throughout (matches data[N] indexing
    in code and the DECODERS table; NOTES.txt uses 1-based, so data[1] in
    code = "byte 2" in NOTES).
  * PGN 0xF113..0xF13C: 4 cell voltages per frame, big-endian uint16 mV.
        cell_index = (PGN - 0xF113) * 4 + slot
        Indexes >= NUM_CELLS (20) and 0xFFFF "not present" sentinels are
        suppressed.
  * PGN 0xF155..0xF15E: 8 module temperatures per frame, uint8 with the
    J1939-style +40 C offset (raw 0x35 = 13 C).
        temp_index = (PGN - 0xF155) * 8 + slot
        Indexes >= NUM_TEMPS (7) and 0xFF "not present" sentinels are
        suppressed.
  * PGN 0xF102: bytes 0-1 BE = max cell mV, bytes 2-3 BE = min cell mV,
                byte 4 = max-cell number (1-based BMS GUI numbering),
                byte 5 = min-cell number (1-based BMS GUI numbering),
                byte 7 = status/flag bits.
  * PGN 0xF100 byte 1 (data[1]) = pack voltage at the BMS terminals,
        encoded as 0.1 V/bit with a +76.8 V offset (V = b * 0.1 + 76.8).
        Confirmed by linear regression of byte 1 against 20 * mean cell mV
        across 24 captures spanning byte values 53..66 (82.0..83.4 V),
        residuals < 0.55 V, and cross-checked against the FF50 charger
        frame which uses an identical encoding.
  * PGN 0xF100 bytes 2-3 BE = signed pack current at 0.1 A/bit, biased so that
        raw 0x7D00 = 0 A (positive = drawing from pack, negative = charging).
        Cross-validated by the amp-*.asc dashboard-anchored set (1, 18, 35, 42,
        58 A): mean decoded current matches the displayed dashboard reading
        within ~1 A across the full range, including across the 0x7D->0x7E and
        0x7F->0x80 high-byte rollovers.
  * PGN 0xF108 byte 7 = dashboard-displayed BMS warning code bitmap. Each
        bit maps to a code in the vendor BMS error-code table (operator
        manual). bit 0=140, bit 1=124, bit 3=142, bit 4=143, bit 5=144,
        bit 7=146 (bits 2 and 6 didn't appear in either calibration
        capture; codes 141 and 145 from the manual are speculatively
        assigned). Bytes 0..6 carry additional fault info (bit-to-code
        mapping not yet established) and are surfaced as raw bytes for
        visibility.
  * PGN 0xFF50 from 0xE5: byte 0 = status (0x00=idle, 0x01/0x02=handshake
                          [transient], 0x03=active charging),
                          bytes 1-2 LE = charger output voltage at the pack
                          terminals, encoded identically to F100 byte 1:
                          raw * 0.1 + 76.8 V.
                          bytes 3-4 LE = charger output current in 0.1 A/bit
                          (no offset).
    Voltage and current scales were anchored against asc/charging-120V-90ish-
    to-100.asc (2863 active-charging frames; regression vs F100F3 gave R^2
    = 0.986 for V and R^2 = 0.999 for I). V/I bytes only carry meaningful
    values while status == 0x03; other states leave them at handshake / idle
    values.
  * PGN 0x000600 from 0xF4 to 0xE5 (charger): vendor-proprietary
        BMS->charger command frame. Reverse-engineered by correlating
        58,584 frames in charging-120V-90ish-to-100.asc against
        contemporaneous F100 (pack V/I/SoC), FF50 (charger V/I/status),
        and F107 (BMS current limits). Source 0xF4 sends only this PGN,
        and only to destination 0xE5 -- consistent with a dedicated SA
        for the BMS's charger-control role (likely the same physical
        BMS module that uses 0xF3 for broadcasts).
            bytes 0-1 BE u16 = voltage setpoint, 0.1 V/bit, no offset.
                               0x034E = 84.6 V (4.23 V/cell * 20 cells)
                               in every active-request frame.
            bytes 2-3 BE u16 = current setpoint, 0.1 A/bit, no offset.
                               Observed 3.0..39.0 A across the charge.
                               When the request <= the charger's delivery
                               capability (~14 A from a 120V/15A wall
                               outlet at 84 V), the charger tracks within
                               ~0.5 A. When the request exceeds capability
                               the charger saturates ~18 A regardless.
            byte 4           = enable: 0x00 = active command,
                               0x01 = idle / no-request (charger drops
                               to status 0x00 within a few frames).
            bytes 5-7        = padding 0xFF.
  * PGN 0xFECA from 0xCA: DM1 (Active Diagnostic Trouble Codes), per
        SAE J1939-73. Single-frame layout (multi-DTC BAM not observed):
            byte 0     = lamp status, 4 lamps x 2 bits each:
                           bits 7-6 MIL, 5-4 Red Stop,
                           3-2 Amber Warning, 1-0 Protect
            byte 1     = flash status, same lamp layout as byte 0
            bytes 2-5  = first DTC (4 bytes):
                           SPN  = b2 | (b3<<8) | ((b4>>5)&7)<<16
                           FMI  = b4 & 0x1F
                           CM   = (b5 >> 7) & 1
                           OC   = b5 & 0x7F
            bytes 6-7  = padding 0xFF for single-DTC frames
        All observed frames in our captures are the J1939 idle pattern
        (00 00 00 00 00 00 FF FF), which the decoder skips. Decoder is
        validated against the J1939-73 spec rather than against fault
        data; trust the lamp/state decode but treat any future SPN as
        TENTATIVE until cross-checked against vendor documentation.
  * PGN 0xFF21 from 0xCA: motor controller / drive ECU telemetry.
        byte 0     = throttle pedal position (raw, ~0..0x34)
        bytes 2-3  = motor RPM magnitude, little-endian uint16, biased by 0x0C80
                     (rpm = ((b3<<8)|b2) - 0x0C80; verified against a
                     0->2500 RPM acceleration trace). Always positive; sign of
                     motion comes from byte 7.
        byte 4     = main controller temperature, J1939 +40 C offset.
        byte 5     = motor temperature, J1939 +40 C offset.
        byte 7     = directional pedal state (foot-pedal selector):
                       0x10 = idle / neither pedal
                       0x14 = forward pedal pressed
                       0x18 = reverse pedal pressed
    Frame is suppressed entirely while charging (contactors open for traction).
"""

import argparse
import csv
import sys
from pathlib import Path

try:
    import can
except ImportError:
    print("python-can is required: pip install python-can", file=sys.stderr)
    sys.exit(1)

# --- bus map -----------------------------------------------------------------
SRC_BMS = 0xF3
SRC_BMS_CHGR_IF = 0xF4   # BMS in its charger-interface role; only sender of
                         # PGN 0x000600 to 0xE5 (proprietary charger commands)
SRC_CHARGER = 0xE5
SRC_VEHICLE = 0xD0   # vehicle controller; broadcasts a minimal F100 heartbeat
SRC_MOTOR = 0xCA     # motor controller / drive ECU; FF21 telemetry, DM1 source

# Decoded names for the byte-1 state field of 18F100D0 (used only by stdout
# diagnostics; the numeric byte is what lands in signals.csv).
VC_STATE_NAMES = {
    0x00: "init",
    0x0C: "ready",
}

# Cell-voltage and temperature PGN windows (BMS broadcasts).
PGN_CELL_FIRST, PGN_CELL_LAST = 0xF113, 0xF13C
PGN_TEMP_FIRST, PGN_TEMP_LAST = 0xF155, 0xF15E

# Aggregate / status PGNs from BMS.
PGN_F100 = 0xF100   # pack status (bytes 2-3 BE = signed pack current)
PGN_F102 = 0xF102   # cell min/max summary
PGN_F104 = 0xF104   # temp min/max summary (symmetric with F102)
PGN_F106 = 0xF106   # BMS state / mode (bytes 0,1 = bitmap)
PGN_F107 = 0xF107   # BMS current limits (charge/discharge)
PGN_F108 = 0xF108   # BMS active fault bitmap (byte 7 = dashboard codes)

# Charger broadcast.
PGN_FF50 = 0xFF50   # charger telemetry (V, A)

# Motor controller broadcast.
PGN_FF21 = 0xFF21   # motor telemetry (RPM, throttle, drive-state, ctrl temp)

# Standard SAE J1939-73 diagnostic message (Active DTCs).
PGN_FECA = 0xFECA   # DM1 (Active Diagnostic Trouble Codes)

# Vendor proprietary BMS->charger command channel.
PGN_PROP_0600 = 0x0600   # PDU1, src 0xF4 -> dest 0xE5: charger setpoints
                         # (V/I requests + enable flag)

# DM1 lamp-status enum per J1939-73 (2 bits per lamp, same encoding for byte 0
# "lamp on/off" and byte 1 "flash status"):
#   0b00 = off / no flash
#   0b01 = on  / slow flash (1 Hz)
#   0b10 = reserved / fast flash (2 Hz)
#   0b11 = not available
DM1_LAMP_NAMES = ("malfunction", "red_stop", "amber_warning", "protect")
DM1_LAMP_STATE = {0: "off", 1: "on", 2: "reserved", 3: "n/a"}
DM1_FLASH_STATE = {0: "no_flash", 1: "slow_1hz", 2: "fast_2hz", 3: "n/a"}

TEMP_OFFSET_C = 40

# Pack topology from the vendor BMS GUI screenshot (see NOTES.txt). Cell /
# temp PGN ranges have room for many more channels but only the first
# NUM_CELLS / NUM_TEMPS slots are real on this pack; the rest are padding
# / "not present" sentinels.
NUM_CELLS = 20
NUM_TEMPS = 7

# F108 byte 7: dashboard-displayed BMS warning code bitmap. Bit -> (code,
# description). Cross-validated against two operator-confirmed captures
# in asc/bms-error-codes/:
#   * bms-124-140-142-143-144-146.asc (codes 124,140,142,143,144,146):
#     byte 7 = 0xBB = bits {0,1,3,4,5,7}
#   * bms-fullcharge-102-109-140.asc (codes 102,109,140):
#     byte 7 = 0x01 = bit {0}
# The only code shared by both captures is 140, and the only byte-7 bit
# shared by both is bit 0 -> bit 0 = 140 (not 124 as previously assumed).
# That fixes 140 to bit 0 and forces 124 to bit 1; the remaining four set
# bits (3,4,5,7) cover the four remaining capture-A codes (142,143,144,
# 146) in numeric order. Bits 2 and 6 weren't lit in either capture, so
# the codes assigned to them are *speculative*; codes 141 and 145 from
# the manual are the most plausible candidates.
BMS_FAULT_CODES_BYTE7 = [
    (0, 140, "System fault: kvst"),
    (1, 124, "Pre-charging fault"),
    (2, 141, "BMS fault need maintenance"),       # speculative
    (3, 142, "BMS fault (manual omits 142)"),     # tentative; manual lists 141
    (4, 143, "Battery fault need maintenance"),
    (5, 144, "Battery system fault needs maintenance"),
    (6, 145, "Full charge/discharge cycle needed"),  # speculative
    (7, 146, "Maintenance mode status"),
]

PACK_CURRENT_LSB_A = 0.1                  # F100F3 bytes 3-4 BE, 0.1 A/bit
PACK_CURRENT_BIAS_RAW = 0x7D00            # raw value at 0 A (positive = discharge)
PACK_VOLTAGE_LSB_V = 0.1                  # F100F3 byte 2 and FF50E5 bytes 2-3 LE
PACK_VOLTAGE_OFFSET_V = 76.8              # same encoding shared by BMS and charger
CHARGER_V_LSB_V = PACK_VOLTAGE_LSB_V      # charger reports the same encoding
CHARGER_V_OFFSET_V = PACK_VOLTAGE_OFFSET_V
CHARGER_I_LSB_A = 0.1
RPM_BIAS = 0x0C80                         # FF21CA bytes 2-3 LE zero-RPM offset
LIMIT_CURRENT_LSB_A = 0.01                # F107F3 bytes 0-1 / 2-3 BE, 0.01 A/bit


def c_to_f(c):
    """Celsius -> Fahrenheit (used only by the stdout summary)."""
    return None if c is None else round(c * 9 / 5 + 32, 1)


# --- helpers -----------------------------------------------------------------

def parse_id(can_id: int):
    """Return (priority, pgn, source) from a 29-bit J1939 ID."""
    src = can_id & 0xFF
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    pgn = (pf << 8) | (ps if pf >= 0xF0 else 0)   # PDU2 vs PDU1
    priority = (can_id >> 26) & 0x7
    return priority, pgn, src


# Known PGN descriptions (SAE-defined plus what we've identified locally).
PGN_NAMES = {
    0x00EB00: "TP.DT (Transport Protocol Data Transfer)",
    0x00EC00: "TP.CM (Transport Protocol Connection Mgmt)",
    0x00EE00: "Address Claimed",
    0x00EF00: "Proprietary A",
    0x00FECA: "DM1 (Active Diagnostic Trouble Codes)",
    0x00FECB: "DM2 (Previously Active DTCs)",
    # Solectrac BMS broadcasts (vendor-defined within the J1939 envelope):
    0x00F100: "BMS pack status (incl. signed pack current)",
    0x00F102: "BMS cell min/max summary",
    0x00F104: "BMS temp min/max summary",
    0x00F106: "BMS state (charger-dependent)",
    0x00F107: "BMS current/voltage limits",
    0x00F108: "BMS active fault bitmap",
    0x00FF50: "Charger telemetry (V, A)",
    0x00FF21: "Motor telemetry (RPM, throttle, state)",
    0x000600: "BMS->Charger command (V/I setpoint, enable)",
}


def describe_pgn(pgn: int) -> str:
    if pgn in PGN_NAMES:
        return PGN_NAMES[pgn]
    if PGN_CELL_FIRST <= pgn <= PGN_CELL_LAST:
        slot0 = (pgn - PGN_CELL_FIRST) * 4
        return f"BMS cell voltages {slot0}-{slot0 + 3}"
    if PGN_TEMP_FIRST <= pgn <= PGN_TEMP_LAST:
        slot0 = (pgn - PGN_TEMP_FIRST) * 8
        return f"BMS module temps {slot0}-{slot0 + 7}"
    if 0xFF00 <= pgn <= 0xFFFF:
        return "Proprietary B"
    if 0xF000 <= pgn <= 0xFEFF:
        return "Broadcast (vendor / unassigned)"
    return ""


def decode_can_id(can_id: int, is_extended: bool) -> dict:
    """Decode a CAN ID (11- or 29-bit) into J1939 fields."""
    if not is_extended:
        return {
            "id": f"{can_id:03X}",
            "ext": False,
            "priority": "",
            "r": "",
            "dp": "",
            "pf": "",
            "ps": "",
            "sa": "",
            "pgn": "",
            "pdu": "",
            "ps_role": "",
            "name": "non-J1939 (11-bit)",
        }
    priority = (can_id >> 26) & 0x7
    r = (can_id >> 25) & 0x1
    dp = (can_id >> 24) & 0x1
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    sa = can_id & 0xFF
    pdu2 = pf >= 0xF0
    pgn = (dp << 16) | (pf << 8) | (ps if pdu2 else 0)
    return {
        "id": f"{can_id:08X}",
        "ext": True,
        "priority": priority,
        "r": r,
        "dp": dp,
        "pf": f"{pf:02X}",
        "ps": f"{ps:02X}",
        "sa": f"{sa:02X}",
        "pgn": f"{pgn:06X}",
        "pdu": "PDU2" if pdu2 else "PDU1",
        "ps_role": "GE" if pdu2 else "DA",
        "name": describe_pgn(pgn),
    }


def data_bytes(msg_data) -> list:
    """Return 8 ints, padding with 0 for short payloads."""
    out = list(msg_data)
    while len(out) < 8:
        out.append(0)
    return out[:8]


def be16(hi, lo):
    return (hi << 8) | lo


def le16(lo, hi):
    return (hi << 8) | lo


# --- per-frame decoders ------------------------------------------------------

def decode_file(path: Path, scenario: str, rows: list, frames: list,
                counts: dict, id_counts: dict):
    """Stream one log via python-can; append decoded rows + frames in-place."""
    sc = counts.setdefault(scenario, {
        "total": 0, "cells": 0, "temps": 0, "f100": 0, "f102": 0,
        "f108": 0, "charger": 0, "vc": 0, "motor": 0,
        "skipped_zero": 0, "extended_false": 0,
    })

    reader = can.LogReader(str(path))
    try:
        for msg in reader:
            sc["total"] += 1
            can_id = msg.arbitration_id
            is_ext = bool(msg.is_extended_id)

            # Track every unique (id, ext) pair we see.
            id_key = (can_id, is_ext)
            id_counts[id_key] = id_counts.get(id_key, 0) + 1

            if not is_ext:
                # 11-bit IDs aren't J1939; leave them out of the BMS decode.
                sc["extended_false"] += 1
                continue

            _, pgn, src = parse_id(can_id)
            ts = msg.timestamp                # seconds, float
            data = data_bytes(msg.data)

            # Each decoder branch builds a list of (signal, value, unit)
            # emissions. We commit a frames.csv entry plus the signals
            # together at the end if anything was produced, so every signal
            # row carries the frame_index of its source frame.
            emissions = []

            if src == SRC_BMS:
                if PGN_CELL_FIRST <= pgn <= PGN_CELL_LAST:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    base = (pgn - PGN_CELL_FIRST) * 4
                    for slot in range(4):
                        idx = base + slot
                        # The cell PGN range covers up to 168 channels but
                        # this pack has only NUM_CELLS real cells; suppress
                        # the rest along with 0 (empty) and 0xFFFF
                        # ("not present" sentinel).
                        if idx >= NUM_CELLS:
                            continue
                        mv = be16(data[2 * slot], data[2 * slot + 1])
                        if mv == 0 or mv == 0xFFFF:
                            continue
                        emissions.append(
                            (f"cell.{idx:02d}.voltage_v",
                             mv / 1000.0, "v"))
                    sc["cells"] += 1

                elif PGN_TEMP_FIRST <= pgn <= PGN_TEMP_LAST:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    base = (pgn - PGN_TEMP_FIRST) * 8
                    for slot, b in enumerate(data):
                        idx = base + slot
                        # 80-channel range, only NUM_TEMPS real probes.
                        # 0xFF is the "not present" sentinel.
                        if idx >= NUM_TEMPS:
                            continue
                        if b == 0 or b == 0xFF:
                            continue
                        emissions.append(
                            (f"temp.{idx:02d}.c",
                             b - TEMP_OFFSET_C, "c"))
                    sc["temps"] += 1

                elif pgn == PGN_F100:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    raw = be16(data[2], data[3])  # bytes 2-3 BE, biased u16
                    amps = (raw - PACK_CURRENT_BIAS_RAW) * PACK_CURRENT_LSB_A
                    volts = data[1] * PACK_VOLTAGE_LSB_V + PACK_VOLTAGE_OFFSET_V
                    emissions.append(("pack.voltage_v", round(volts, 2), "v"))
                    emissions.append(("pack.current_raw", raw, ""))
                    emissions.append(("pack.current_a", round(amps, 1), "a"))
                    # byte 4: BMS-published State-of-Charge. Identified by
                    # cross-capture comparison: byte 4 is constant within
                    # short captures (e.g. 250 across 489 frames in
                    # accellerate-decelerate.asc despite voltage moving
                    # 100 mV); saturates at 250 in soc-100-idle.asc; spans
                    # 224..250 in charging-120V-90ish-to-100.asc whose
                    # filename indicates a 90%->100% charge. Linear fit
                    # through (224, 90%) and (250, 100%) gives the slope
                    # and offset below; dynamic range is small so the
                    # slope is loose, and the field is marked tentative
                    # until a deeper-discharge capture confirms it.
                    soc_pct = round(data[4] * 0.385 + 3.8, 1)
                    emissions.append(("pack.soc_raw", data[4], ""))
                    emissions.append(("pack.soc_pct", soc_pct, "%"))
                    sc["f100"] += 1

                elif pgn == PGN_F102:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    max_mv = be16(data[0], data[1])
                    min_mv = be16(data[2], data[3])
                    if max_mv == 0 or min_mv == 0:
                        continue
                    pack_v = round(20 * (max_mv + min_mv) / 2.0 / 1000.0, 3)
                    emissions.append(("pack.cell_max_mv", max_mv, "mv"))
                    emissions.append(("pack.cell_min_mv", min_mv, "mv"))
                    emissions.append(
                        ("pack.cell_spread_mv", max_mv - min_mv, "mv"))
                    emissions.append(("pack.cell_max_n", data[4], ""))
                    emissions.append(("pack.cell_min_n", data[5], ""))
                    emissions.append(("pack.flags", data[7], ""))
                    emissions.append(("pack.v_estimate", pack_v, "v"))
                    sc["f102"] += 1

                elif pgn == PGN_F104:
                    # Symmetric with F102 (cell min/max summary) but for
                    # module temperatures. Layout verified by cross-
                    # referencing every capture's F104 payload against
                    # the per-channel temp.NN.c values decoded from
                    # F155..F15E in the same capture: byte 0 = max temp
                    # in °C with the J1939 +40 offset, byte 1 = min
                    # temp same encoding, byte 2 = max-temp channel
                    # number (1-based), byte 3 = min-temp channel
                    # number (1-based), byte 4 = b0 - b1 (spread, °C);
                    # bytes 5-7 are 0xFF (J1939 unused).
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    if data[0] == 0xFF or data[1] == 0xFF:
                        continue
                    emissions.append(
                        ("pack.temp_max_c", data[0] - TEMP_OFFSET_C, "c"))
                    emissions.append(
                        ("pack.temp_min_c", data[1] - TEMP_OFFSET_C, "c"))
                    emissions.append(("pack.temp_max_n", data[2], ""))
                    emissions.append(("pack.temp_min_n", data[3], ""))
                    emissions.append(("pack.temp_spread_c", data[4], "c"))
                    sc.setdefault("f104", 0)
                    sc["f104"] += 1

                elif pgn == PGN_F106:
                    # BMS state byte pair. Across all 20 captures we see
                    # only a small set of (byte 0, byte 1) combinations:
                    #   (0x45, 0xE0) = drive ready (every capture with
                    #                  motor activity)
                    #   (0x45, 0xCC) = active charging (dominant in
                    #                  charging-120V-90ish-to-100.asc)
                    #   (0x80, 0xC4) = charger plugged in, idle
                    #   (0x45, 0xC4) / (0x84, 0xC4) / (0x85, 0xC4) /
                    #   (0x00, 0x80) = transient handshake states
                    # byte 1 has the clearest bit semantics:
                    #   bit 7 (0x80) = BMS alive (always set when data
                    #                  is published)
                    #   bit 6 (0x40) = vehicle/contactors awake
                    #   bit 5 (0x20) = drive mode (set only with motor)
                    #   bit 3 (0x08) = charging active (set only with
                    #                  charger status 0x03)
                    #   bit 2 (0x04) = charger present
                    # bits in byte 0 are less clear; emit raw for now.
                    # byte 2 is constant 0xFC; bytes 3-7 are 0xFF
                    # (J1939 unused).
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    emissions.append(("bms.state.byte0", data[0], ""))
                    emissions.append(("bms.state.byte1", data[1], ""))
                    b1 = data[1]
                    emissions.append(
                        ("bms.state.charging", 1 if b1 & 0x08 else 0, ""))
                    emissions.append(
                        ("bms.state.charger_present",
                         1 if b1 & 0x04 else 0, ""))
                    emissions.append(
                        ("bms.state.drive_mode",
                         1 if b1 & 0x20 else 0, ""))
                    emissions.append(
                        ("bms.state.contactors",
                         1 if b1 & 0x40 else 0, ""))
                    sc.setdefault("f106", 0)
                    sc["f106"] += 1

                elif pgn == PGN_F107:
                    # BMS current limits, two BE u16 fields at 0.01 A/bit:
                    #   bytes 0-1 = max discharge current
                    #   bytes 2-3 = max charge current
                    # In every drive capture bytes 0-1 = 0x38A4 = 145.0 A
                    # and bytes 2-3 = 0x2710 = 100.0 A; in every charging
                    # capture both fall to 0x2710 = 100.0 A. The pack
                    # spec lists 200 A peak / 100 A continuous, so the
                    # 145 A figure is the BMS-published derated peak and
                    # 100 A is the continuous limit.
                    # byte 4 = mode flag (0x00 charging, 0x01 driving),
                    # byte 5 = slowly varying counter / status (~0x71..
                    # 0x77 in driving captures, 0x00 in charging); not
                    # yet fully decoded.
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    i_dis_max = be16(data[0], data[1]) * LIMIT_CURRENT_LSB_A
                    i_chg_max = be16(data[2], data[3]) * LIMIT_CURRENT_LSB_A
                    emissions.append(
                        ("bms.limit.discharge_a", round(i_dis_max, 2), "a"))
                    emissions.append(
                        ("bms.limit.charge_a", round(i_chg_max, 2), "a"))
                    emissions.append(("bms.limit.mode", data[4], ""))
                    emissions.append(("bms.limit.byte5", data[5], ""))
                    sc.setdefault("f107", 0)
                    sc["f107"] += 1

                elif pgn == PGN_F108:
                    # All zeros = healthy idle baseline. Byte 7 is the
                    # dashboard-displayed BMS warning code bitmap (decoded
                    # against the vendor table). Bytes 0..6 carry
                    # additional fault info; bit-to-code mapping isn't
                    # established yet, so they're surfaced as raw values
                    # for visibility.
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    for i, b in enumerate(data):
                        if b != 0:
                            emissions.append(
                                (f"bms.fault.byte{i}", b, ""))
                    b7 = data[7]
                    for bit, code, _desc in BMS_FAULT_CODES_BYTE7:
                        if (b7 >> bit) & 1:
                            emissions.append(
                                (f"bms.fault.code_{code}", 1, ""))
                    sc["f108"] += 1

            elif src == SRC_VEHICLE and pgn == PGN_F100:
                emissions.append(("vc.state", data[0], ""))
                sc["vc"] += 1

            elif src == SRC_MOTOR and pgn == PGN_FF21:
                # bytes 2-3 little-endian, biased by 0x0C80, give RPM magnitude.
                rpm_mag = ((data[3] << 8) | data[2]) - RPM_BIAS
                throttle_raw = data[0]
                # byte 7 selects which directional pedal is pressed.
                pedal = data[7]
                if pedal == 0x14:
                    direction = 1            # forward pedal
                elif pedal == 0x18:
                    direction = -1           # reverse pedal
                else:
                    direction = 0            # idle / neither
                rpm_signed = direction * rpm_mag
                emissions.append(("motor.rpm_signed", rpm_signed, "rpm"))
                emissions.append(("motor.rpm_magnitude", rpm_mag, "rpm"))
                emissions.append(("motor.direction", direction, ""))
                emissions.append(("motor.throttle_raw", throttle_raw, ""))
                # bytes 4 and 5 are both J1939 +40 C-offset temperatures.
                # The operator manual lists two separate gauges on the dash:
                # "Main Controller Temperature" and "Motor Temperature".
                # Across all captures byte 4 is consistently a few degrees
                # hotter than byte 5 and ramps up from cold-start in
                # ignition-without-charger-inserted.asc (40->59 raw =
                # 0->19 C while byte 5 stays at 13 C); inverter electronics
                # (controller) typically run hotter than the motor housing,
                # so byte 4 = controller, byte 5 = motor. Raw 0 means
                # "not present" and is suppressed.
                if data[4]:
                    emissions.append(
                        ("motor.controller_temp_c",
                         data[4] - TEMP_OFFSET_C, "c"))
                if data[5]:
                    emissions.append(
                        ("motor.motor_temp_c",
                         data[5] - TEMP_OFFSET_C, "c"))
                sc["motor"] += 1

            elif src == SRC_MOTOR and pgn == PGN_FECA:
                # DM1 (Active Diagnostic Trouble Codes) per SAE J1939-73.
                #   data[0] = lamp status: 4 lamps x 2 bits each
                #             bits 7-6 = MIL (Malfunction Indicator Lamp)
                #             bits 5-4 = Red Stop
                #             bits 3-2 = Amber Warning
                #             bits 1-0 = Protect
                #   data[1] = flash status, same layout as data[0]
                #   data[2..5] = first DTC (4 bytes, layout below)
                #   data[6..7] = padding (0xFF) for single-DTC frame
                # DTC layout (CM=0, the modern SAE convention):
                #   data[2]      = SPN bits  0..7
                #   data[3]      = SPN bits  8..15
                #   data[4]      = SPN bits 16..18 (high 3 bits) | FMI (low 5)
                #   data[5]      = CM (bit 7) | OC (low 7 bits)
                # Multi-DTC DM1 messages use J1939 transport-protocol BAM
                # (PGN 0xECFF / 0xEBFF). None observed in our captures, so
                # this decoder handles only single-frame DM1.
                #
                # Healthy idle convention: 00 00 00 00 00 00 FF FF (all
                # lamps off, no DTC, padding 0xFF). Suppressed to keep the
                # CSV compact; only nonzero / interesting frames emit rows.
                lamp_byte = data[0]
                flash_byte = data[1]
                spn = (data[2]
                       | (data[3] << 8)
                       | (((data[4] >> 5) & 0x07) << 16))
                fmi = data[4] & 0x1F
                cm = (data[5] >> 7) & 0x01
                oc = data[5] & 0x7F
                dtc_active = (spn != 0) or (fmi != 0)
                if lamp_byte == 0 and flash_byte == 0 and not dtc_active:
                    sc["skipped_zero"] += 1
                    continue
                if lamp_byte:
                    emissions.append(("dm1.lamp.byte0", lamp_byte, ""))
                    for i, name in enumerate(DM1_LAMP_NAMES):
                        # i=0 -> bits 7-6, i=1 -> bits 5-4, etc.
                        shift = 6 - 2 * i
                        v = (lamp_byte >> shift) & 0x03
                        if v:
                            emissions.append(
                                (f"dm1.lamp.{name}_state", v, ""))
                if flash_byte:
                    emissions.append(("dm1.lamp.byte1", flash_byte, ""))
                    for i, name in enumerate(DM1_LAMP_NAMES):
                        shift = 6 - 2 * i
                        v = (flash_byte >> shift) & 0x03
                        if v:
                            emissions.append(
                                (f"dm1.lamp.{name}_flash", v, ""))
                if dtc_active:
                    emissions.append(("dm1.dtc.spn", spn, ""))
                    emissions.append(("dm1.dtc.fmi", fmi, ""))
                    emissions.append(("dm1.dtc.cm", cm, ""))
                    emissions.append(("dm1.dtc.oc", oc, ""))
                sc.setdefault("dm1", 0)
                sc["dm1"] += 1

            elif src == SRC_CHARGER and pgn == PGN_FF50:
                if all(b == 0 for b in data):
                    sc["skipped_zero"] += 1
                    continue
                status = data[0]
                v_raw = le16(data[1], data[2])
                i_raw = le16(data[3], data[4])
                emissions.append(("charger.status", status, ""))
                emissions.append(("charger.v_raw", v_raw, ""))
                emissions.append(("charger.i_raw", i_raw, ""))
                # The voltage/current bytes only carry meaningful values
                # while status == 0x03 (actively charging). In other
                # states the bytes hold handshake/leftover values that
                # decode to nonsense (e.g. byte 4 = 0x08 in idle ->
                # 204.8 A). Keep status/v_raw/i_raw unconditional so the
                # raw bytes remain visible, but only emit the engineering
                # values while charging.
                if status == 0x03:
                    emissions.append(
                        ("charger.voltage_v",
                         round(v_raw * CHARGER_V_LSB_V + CHARGER_V_OFFSET_V, 2),
                         "v"))
                    emissions.append(
                        ("charger.current_a",
                         round(i_raw * CHARGER_I_LSB_A, 1), "a"))
                sc["charger"] += 1

            elif src == SRC_BMS_CHGR_IF and pgn == PGN_PROP_0600:
                # BMS->Charger command frame (vendor proprietary PGN
                # 0x000600, src 0xF4 -> dest 0xE5). Reverse-engineered
                # by correlating contemporaneous F100 (pack V/I/SoC),
                # FF50 (charger V/I/status), and F107 (BMS limits)
                # across charging-120V-90ish-to-100.asc:
                #   bytes 0-1 BE = voltage setpoint, 0.1 V/bit, no offset
                #     (always 0x034E = 84.6 V during active requests --
                #      4.23 V/cell * 20 cells, the NMC max charge V).
                #   bytes 2-3 BE = current setpoint, 0.1 A/bit, no offset
                #     (3.0..39.0 A across the charge; charger faithfully
                #      tracks the request when within its delivery
                #      capability and saturates near its power-limit
                #      otherwise).
                #   byte 4 = enable flag: 0x00 = command power output,
                #            0x01 = idle / no request (charger drops to
                #            status 0x00 within a few frames).
                #   bytes 5-7 = padding 0xFF.
                # Idle-only frames (00 00 00 00 01 FF FF FF) are
                # suppressed to keep the CSV compact, the same way
                # FF50 idle is handled.
                v_set_raw = be16(data[0], data[1])
                i_set_raw = be16(data[2], data[3])
                enable = data[4]
                if (v_set_raw == 0 and i_set_raw == 0
                        and enable in (0, 1) and all(b == 0xFF
                                                     for b in data[5:])):
                    if enable == 1:
                        # Idle / no-request frame -- emit just the
                        # enable flag so analyses can find idle periods,
                        # but skip the V/I zeros to avoid noise.
                        emissions.append(
                            ("chgr_cmd.enable", enable, ""))
                        sc.setdefault("chgr_cmd", 0)
                        sc["chgr_cmd"] += 1
                    else:
                        # all-zero with enable=0 hasn't been observed;
                        # treat as malformed and skip.
                        sc["skipped_zero"] += 1
                    continue
                emissions.append(
                    ("chgr_cmd.voltage_v",
                     round(v_set_raw * 0.1, 1), "v"))
                emissions.append(
                    ("chgr_cmd.current_a",
                     round(i_set_raw * 0.1, 1), "a"))
                emissions.append(("chgr_cmd.enable", enable, ""))
                emissions.append(("chgr_cmd.v_raw", v_set_raw, ""))
                emissions.append(("chgr_cmd.i_raw", i_set_raw, ""))
                sc.setdefault("chgr_cmd", 0)
                sc["chgr_cmd"] += 1

            if emissions:
                frame_index = len(frames)
                frames.append((
                    frame_index, scenario, ts,
                    f"{can_id:08X}", f"{pgn:04X}", f"{src:02X}",
                    len(msg.data),
                    *(f"{b:02X}" for b in data),
                ))
                for signal, value, unit in emissions:
                    rows.append(
                        (scenario, ts, frame_index, signal, value, unit))
    finally:
        if hasattr(reader, "stop"):
            try:
                reader.stop()
            except Exception:
                pass


# --- writers / summary -------------------------------------------------------

SIGNALS_HEADER = ["file", "timestamp", "frame_index", "signal", "value", "unit"]
FRAMES_HEADER = ["frame_index", "file", "timestamp",
                 "can_id", "pgn", "source", "len",
                 "b0", "b1", "b2", "b3", "b4", "b5", "b6", "b7"]
DECODERS_HEADER = ["signal", "pgn", "source", "bytes", "formula",
                   "unit", "confidence", "notes"]

# Per-signal decode rule catalog, written verbatim to decoders.csv.
# `bytes` is described relative to data byte 0 (i.e., J1939 SPN byte index 0,
# which corresponds to "byte 1" in some vendor-spec conventions).
DECODERS = [
    ("cell.NN.voltage_v", "F113..F13C", "F3", "2*slot, 2*slot+1 (slot 0..3)",
     "BE u16 / 1000", "v", "verified",
     "NN = (PGN-0xF113)*4 + slot; capped at NUM_CELLS=20; "
     "0-mV and 0xFFFF (not-present) sentinels suppressed"),
    ("temp.NN.c", "F155..F15E", "F3", "slot 0..7",
     "u8 - 40", "c", "verified",
     "NN = (PGN-0xF155)*8 + slot; capped at NUM_TEMPS=7; J1939 +40C offset; "
     "0 and 0xFF (not-present) sentinels suppressed"),
    ("pack.cell_max_mv", "F102", "F3", "0-1", "BE u16",
     "mv", "verified", ""),
    ("pack.cell_min_mv", "F102", "F3", "2-3", "BE u16",
     "mv", "verified", ""),
    ("pack.cell_spread_mv", "F102", "F3", "0-3", "max - min",
     "mv", "verified", ""),
    ("pack.cell_max_n", "F102", "F3", "4", "u8 (raw)",
     "", "verified",
     "max-cell number, 1-based (BMS GUI numbering); subtract 1 for 0-based cell_index"),
    ("pack.cell_min_n", "F102", "F3", "5", "u8 (raw)",
     "", "verified",
     "min-cell number, 1-based (BMS GUI numbering); subtract 1 for 0-based cell_index"),
    ("pack.flags", "F102", "F3", "7", "u8 (raw)",
     "", "tentative", "status/flag bits per NOTES; bit-level decode unknown"),
    ("pack.v_estimate", "F102", "F3", "0-3", "20 * (max+min)/2 / 1000",
     "v", "verified", "assumes 20-cell pack"),
    ("pack.temp_max_c", "F104", "F3", "0", "u8 - 40",
     "c", "verified",
     "max module temp; cross-validated against per-channel temp.NN.c "
     "decoded from F155..F15E in every capture"),
    ("pack.temp_min_c", "F104", "F3", "1", "u8 - 40",
     "c", "verified", "min module temp; same cross-validation as temp_max_c"),
    ("pack.temp_max_n", "F104", "F3", "2", "u8 (raw)",
     "", "verified",
     "max-temp channel number, 1-based BMS GUI numbering; "
     "subtract 1 for 0-based temp_index"),
    ("pack.temp_min_n", "F104", "F3", "3", "u8 (raw)",
     "", "verified",
     "min-temp channel number, 1-based BMS GUI numbering; "
     "subtract 1 for 0-based temp_index"),
    ("pack.temp_spread_c", "F104", "F3", "4", "u8",
     "c", "verified", "= byte 0 - byte 1 in every observed capture"),
    ("pack.voltage_v", "F100", "F3", "1", "u8 * 0.1 + 76.8",
     "v", "verified",
     "anchored by 24-capture regression vs 20*mean(cell mV); confirmed by FF50"),
    ("pack.current_raw", "F100", "F3", "2-3", "BE u16 (biased)",
     "", "verified", "subtract 0x7D00 for signed amps"),
    ("pack.current_a", "F100", "F3", "2-3", "(BE u16 - 0x7D00) * 0.1",
     "a", "verified",
     "+draw / -charge; cross-validated against amp-*.asc dashboard captures"),
    ("pack.soc_raw", "F100", "F3", "4", "u8 (raw)",
     "", "tentative",
     "BMS-published SoC raw byte; saturates at 250 in soc-100-idle.asc"),
    ("pack.soc_pct", "F100", "F3", "4", "u8 * 0.385 + 3.8",
     "%", "tentative",
     "linear fit through (224, 90%) and (250, 100%) from "
     "charging-120V-90ish-to-100.asc; slope loose pending deeper-discharge data"),
    ("bms.state.byte0", "F106", "F3", "0", "u8 (raw)",
     "", "tentative",
     "BMS top-level state byte; observed values 0x00, 0x44, 0x45, 0x80, "
     "0x84, 0x85 across 20 captures; bit semantics not fully established"),
    ("bms.state.byte1", "F106", "F3", "1", "u8 (raw)",
     "", "verified",
     "BMS state bitmap; bits decoded into bms.state.charging / "
     "charger_present / drive_mode / contactors below"),
    ("bms.state.charging", "F106", "F3", "1 (bit 3)", "(b1 >> 3) & 1",
     "", "verified",
     "set only in charging-120V-90ish-to-100.asc while charger status=0x03"),
    ("bms.state.charger_present", "F106", "F3", "1 (bit 2)", "(b1 >> 2) & 1",
     "", "verified",
     "set whenever charger is plugged in (charging or idle); "
     "matches charger.status presence"),
    ("bms.state.drive_mode", "F106", "F3", "1 (bit 5)", "(b1 >> 5) & 1",
     "", "verified",
     "set only in captures with motor (FF21CA) traffic; clear during charging"),
    ("bms.state.contactors", "F106", "F3", "1 (bit 6)", "(b1 >> 6) & 1",
     "", "tentative",
     "set whenever the vehicle is awake (drive or charge); "
     "likely tracks contactor / pre-charge complete"),
    ("bms.limit.discharge_a", "F107", "F3", "0-1", "BE u16 * 0.01",
     "a", "verified",
     "max discharge current; 145.0 A in every drive capture, "
     "100.0 A during charging; matches 200 A peak / 100 A continuous spec"),
    ("bms.limit.charge_a", "F107", "F3", "2-3", "BE u16 * 0.01",
     "a", "verified",
     "max charge current; 100.0 A in every observed capture"),
    ("bms.limit.mode", "F107", "F3", "4", "u8 (raw)",
     "", "verified",
     "0x00 in charging captures, 0x01 in drive captures"),
    ("bms.limit.byte5", "F107", "F3", "5", "u8 (raw)",
     "", "tentative",
     "slowly varying counter / status (0x71..0x77 in drive captures, "
     "0x00 in charging); semantics unknown"),
    ("bms.fault.byteN", "F108", "F3", "0..7", "u8 (raw, when nonzero)",
     "", "verified",
     "byte 7 = dashboard warning code bitmap; bytes 0..6 carry additional "
     "fault info, bit-to-code mapping not yet established"),
    ("bms.fault.code_NNN", "F108", "F3", "7", "(byte7 >> bit) & 1",
     "", "verified",
     "NNN per vendor BMS error-code table; bit 0=140, bit 1=124, bit 3=142, "
     "bit 4=143, bit 5=144, bit 7=146 (bits 2 and 6 speculative as codes "
     "141 and 145); emitted as 1 only when bit set"),
    ("charger.status", "FF50", "E5", "0", "u8 (raw)",
     "", "verified",
     "0x00=idle, 0x01/0x02=handshake (transient), 0x03=active"),
    ("charger.v_raw", "FF50", "E5", "1-2", "LE u16",
     "", "verified",
     "raw bytes always emitted (handshake/idle constants visible)"),
    ("charger.voltage_v", "FF50", "E5", "1-2", "LE u16 * 0.1 + 76.8",
     "v", "verified",
     "same encoding as F100 byte 1; emitted only while status==0x03 "
     "(R^2=0.986 vs F100F3 across 2863 active-charging frames)"),
    ("charger.i_raw", "FF50", "E5", "3-4", "LE u16",
     "", "verified",
     "raw bytes always emitted; in idle byte 4=0x08 -> i_raw=2048"),
    ("charger.current_a", "FF50", "E5", "3-4", "LE u16 * 0.1",
     "a", "verified",
     "emitted only while status==0x03 (in idle the raw bytes would "
     "decode to a spurious 204.8 A)"),
    ("chgr_cmd.voltage_v", "0600", "F4", "0-1", "BE u16 * 0.1",
     "v", "verified",
     "BMS-commanded charger voltage setpoint; always 84.6 V during "
     "active requests (20s NMC * 4.23 V/cell); no +76.8 V offset "
     "(unlike F100/FF50). Suppressed during idle frames."),
    ("chgr_cmd.current_a", "0600", "F4", "2-3", "BE u16 * 0.1",
     "a", "verified",
     "BMS-commanded charger current setpoint; 3.0..39.0 A observed "
     "across the 90%->100% charge in charging-120V-90ish-to-100.asc; "
     "charger.current_a tracks within ~0.5 A when request <= charger "
     "delivery capability, saturates ~18 A on a 120V/15A wall outlet "
     "when request exceeds it. Suppressed during idle frames."),
    ("chgr_cmd.enable", "0600", "F4", "4", "u8 (raw)",
     "", "verified",
     "0x00 = active charging command, 0x01 = idle / no-request "
     "(charger.status drops to 0x00 within a few frames)"),
    ("chgr_cmd.v_raw", "0600", "F4", "0-1", "BE u16",
     "", "verified",
     "raw setpoint, emitted alongside the engineering value for parity "
     "with charger.v_raw / pack.current_raw"),
    ("chgr_cmd.i_raw", "0600", "F4", "2-3", "BE u16",
     "", "verified", ""),
    ("vc.state", "F100", "D0", "0", "u8 (raw)",
     "", "verified", "0x00=init, 0x0C=ready"),
    ("motor.rpm_signed", "FF21", "CA", "2-3, 7",
     "(LE u16 - 0x0C80) * direction(b7)", "rpm", "verified", ""),
    ("motor.rpm_magnitude", "FF21", "CA", "2-3", "LE u16 - 0x0C80",
     "rpm", "verified", "verified against 0->2500 RPM acceleration trace"),
    ("motor.direction", "FF21", "CA", "7", "0x14->+1, 0x18->-1, else 0",
     "", "verified", "directional pedal selector"),
    ("motor.throttle_raw", "FF21", "CA", "0", "u8 (raw)",
     "", "verified", "~0..0x34"),
    ("motor.controller_temp_c", "FF21", "CA", "4", "u8 - 40",
     "c", "tentative",
     "main controller temp; consistently warmer than byte 5 and ramps up "
     "from cold-start; 0 = not present and suppressed"),
    ("motor.motor_temp_c", "FF21", "CA", "5", "u8 - 40",
     "c", "tentative",
     "motor temp; cooler/steadier than byte 4; 0 = not present and suppressed"),
    ("dm1.lamp.byte0", "FECA", "CA", "0", "u8 (raw, when nonzero)",
     "", "verified",
     "SAE J1939-73 DM1 lamp-status byte; per-lamp decode below; "
     "every observed frame in current captures = 0x00 (no faults active)"),
    ("dm1.lamp.byte1", "FECA", "CA", "1", "u8 (raw, when nonzero)",
     "", "verified",
     "SAE J1939-73 DM1 lamp-flash-status byte; per-lamp decode below"),
    ("dm1.lamp.NAME_state", "FECA", "CA", "0 (2 bits)", "(b0 >> shift) & 3",
     "", "verified",
     "NAME in {malfunction, red_stop, amber_warning, protect}; "
     "shift = 6,4,2,0 respectively; values 0=off, 1=on, 2=reserved, 3=n/a; "
     "emitted only when nonzero"),
    ("dm1.lamp.NAME_flash", "FECA", "CA", "1 (2 bits)", "(b1 >> shift) & 3",
     "", "verified",
     "same NAME / shift mapping as _state; values 0=no_flash, 1=slow_1Hz, "
     "2=fast_2Hz, 3=n/a"),
    ("dm1.dtc.spn", "FECA", "CA", "2-4",
     "b2 | (b3<<8) | ((b4>>5)&7)<<16", "", "verified",
     "SAE J1939-73 SPN (Suspect Parameter Number, 19 bits); CM=0 layout; "
     "emitted only when SPN!=0 or FMI!=0 (no active DTCs in any observed capture)"),
    ("dm1.dtc.fmi", "FECA", "CA", "4 (low 5 bits)", "b4 & 0x1F",
     "", "verified",
     "Failure Mode Indicator (5 bits); SAE J1939-73 Appendix A enumerates "
     "the 32 standard FMIs (0=above-range high, 1=below-range low, etc.)"),
    ("dm1.dtc.cm", "FECA", "CA", "5 (bit 7)", "(b5 >> 7) & 1",
     "", "verified",
     "SPN Conversion Method bit; 0 = modern (this decoder), 1 = legacy "
     "(re-decode SPN/FMI if observed nonzero)"),
    ("dm1.dtc.oc", "FECA", "CA", "5 (low 7 bits)", "b5 & 0x7F",
     "", "verified",
     "Occurrence Count: number of times this DTC has been activated since "
     "the last clear (saturates at 126; 127 = not available)"),
]

# ids.csv has its own writer because it's per-ID metadata, not timeseries.
IDS_SCHEMA = ["id", "ext", "count", "priority", "R", "DP",
              "PF", "PS", "SA", "PGN", "PDU", "PS_role", "name"]


def write_signals(rows: list, out_dir: Path):
    path = out_dir / "signals.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SIGNALS_HEADER)
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def write_frames(frames: list, out_dir: Path):
    path = out_dir / "frames.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FRAMES_HEADER)
        w.writerows(frames)
    print(f"wrote {path} ({len(frames)} frames)")


def write_decoders(out_dir: Path):
    path = out_dir / "decoders.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(DECODERS_HEADER)
        w.writerows(DECODERS)
    print(f"wrote {path} ({len(DECODERS)} decoders)")


def write_ids(id_counts: dict, out_dir: Path):
    """Emit the per-unique-ID J1939 decode table to ids.csv."""
    path = out_dir / "ids.csv"
    decoded = []
    for (can_id, is_ext), n in id_counts.items():
        d = decode_can_id(can_id, is_ext)
        decoded.append((d, n))
    # Sort: 29-bit before 11-bit, then by numeric ID value.
    decoded.sort(key=lambda dn: (not dn[0]["ext"], int(dn[0]["id"], 16)))

    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(IDS_SCHEMA)
        for d, n in decoded:
            w.writerow([d["id"], d["ext"], n, d["priority"], d["r"], d["dp"],
                        d["pf"], d["ps"], d["sa"], d["pgn"], d["pdu"],
                        d["ps_role"], d["name"]])
    print(f"wrote {path} ({len(decoded)} unique IDs)")


def values_for(rows: list, scenario: str, signal: str):
    """Pull all values for one scenario+signal pair."""
    return [r[4] for r in rows if r[0] == scenario and r[3] == signal]


def summarize(counts: dict, rows: list):
    print()
    print(f"{'file':<28} {'frames':>7} {'cells':>6} {'temps':>6} "
          f"{'F100':>5} {'F102':>5} {'F108':>5} {'chgr':>5} {'vc':>5} "
          f"{'motor':>6}")
    for scenario, sc in counts.items():
        print(f"{scenario:<28} {sc['total']:>7} {sc['cells']:>6} "
              f"{sc['temps']:>6} {sc['f100']:>5} {sc['f102']:>5} "
              f"{sc['f108']:>5} {sc['charger']:>5} {sc['vc']:>5} "
              f"{sc['motor']:>6}")

    print("\nsummary:")
    for scenario in counts:
        print(f"\n  {scenario}")
        maxs = values_for(rows, scenario, "pack.cell_max_mv")
        mins = values_for(rows, scenario, "pack.cell_min_mv")
        spreads = values_for(rows, scenario, "pack.cell_spread_mv")
        est = values_for(rows, scenario, "pack.v_estimate")
        if maxs:
            print(f"    cell max  : {min(maxs)}..{max(maxs)} mV")
            print(f"    cell min  : {min(mins)}..{max(mins)} mV")
            print(f"    spread    : {min(spreads)}..{max(spreads)} mV")
            print(f"    pack est  : {min(est):.2f}..{max(est):.2f} V "
                  f"(20 cells * mean cell mV)")
        amps = values_for(rows, scenario, "pack.current_a")
        if amps:
            print(f"    I (F100)  : {min(amps):+.1f}..{max(amps):+.1f} A "
                  f"(0.1 A/bit, +draw / -charge)")
        active_codes = sorted({
            int(r[3].rsplit("_", 1)[1])
            for r in rows
            if r[0] == scenario and r[3].startswith("bms.fault.code_")
        })
        if active_codes:
            print(f"    BMS codes : {', '.join(str(c) for c in active_codes)} "
                  f"(union over capture; F108 byte 7)")
        chgr_v = values_for(rows, scenario, "charger.voltage_v")
        chgr_i = values_for(rows, scenario, "charger.current_a")
        if chgr_v:
            print(f"    chgr V    : {min(chgr_v):.1f}..{max(chgr_v):.1f} V "
                  f"(0.1 V/bit + 76.8 V; meaningful only while status=0x03)")
            print(f"    chgr I    : {min(chgr_i):.1f}..{max(chgr_i):.1f} A")
        # Per-channel module temps share the temp.NN.c naming.
        temps_c = [r[4] for r in rows
                   if r[0] == scenario
                   and r[3].startswith("temp.")
                   and r[3].endswith(".c")]
        if temps_c:
            t_min, t_max = min(temps_c), max(temps_c)
            print(f"    temps     : {t_min}..{t_max} C  "
                  f"({c_to_f(t_min)}..{c_to_f(t_max)} F)")
        rpms_signed = values_for(rows, scenario, "motor.rpm_signed")
        rpms_mag = values_for(rows, scenario, "motor.rpm_magnitude")
        dirs = values_for(rows, scenario, "motor.direction")
        thr = values_for(rows, scenario, "motor.throttle_raw")
        if rpms_signed:
            n_fwd = sum(1 for d in dirs if d == 1)
            n_rev = sum(1 for d in dirs if d == -1)
            n_idle = sum(1 for d in dirs if d == 0)
            print(f"    motor RPM : {min(rpms_signed)}..{max(rpms_signed)} (signed)")
            print(f"    |RPM|     : {min(rpms_mag)}..{max(rpms_mag)}")
            print(f"    throttle  : {min(thr)}..{max(thr)} (raw)")
            print(f"    pedal     : fwd={n_fwd}  rev={n_rev}  idle={n_idle}")


# --- main --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Decode J1939-style CAN logs from a Solectrac tractor.",
        epilog="supported formats: any python-can LogReader format "
               "(.asc, .blf, .log, .trc, python-can .csv)",
    )
    parser.add_argument("inputs", nargs="+", metavar="FILE",
                        help="CAN log file(s) to decode")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd(),
                        metavar="DIR",
                        help="directory to write output CSVs into "
                             "(default: current working directory)")
    args = parser.parse_args()

    inputs = [Path(p) for p in args.inputs]
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    frames = []
    counts = {}
    id_counts = {}

    for path in inputs:
        print(f"reading {path.name}")
        decode_file(path, path.name, rows, frames, counts, id_counts)

    write_signals(rows, out_dir)
    write_frames(frames, out_dir)
    write_decoders(out_dir)
    write_ids(id_counts, out_dir)
    summarize(counts, rows)


if __name__ == "__main__":
    main()
