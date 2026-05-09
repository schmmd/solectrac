#!/usr/bin/env python3
"""
Decode J1939-style CAN logs from a small electric tractor BMS / charger.

Usage:
    python3 solectrac-analyze.py file1.asc [file2.blf ...]

Inputs are read via python-can's LogReader, so any format python-can
understands works: .asc (Vector ASCII), .blf, .log (canutils), .trc, and
python-can's own .csv format. (SavvyCAN's CSV export is *not* supported
because python-can doesn't read that dialect.)

Outputs (written next to the inputs):
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
    pack.byte5
    pack.byte6_min_idx
    pack.flags
    pack.v_estimate            20 * mean(min, max) / 1000
    pack.voltage_proxy_b2      F100 byte 2 (raw)
    pack.current_raw           F100 bytes 3-4 (raw biased u16)
    pack.current_a             F100 signed pack current, A
    charger.status             FF50 byte 1
    charger.v_raw              FF50 bytes 2-3 LE (raw)
    charger.voltage_v          FF50 voltage estimate (1/3 V/bit, tentative)
    charger.i_raw              FF50 bytes 4-5 LE (raw)
    charger.current_a          FF50 current, A
    vc.state                   F100D0 byte 0 (raw heartbeat state)
    motor.rpm_signed           FF21CA RPM with directional sign
    motor.rpm_magnitude        FF21CA RPM unsigned
    motor.direction            +1 forward / 0 idle / -1 reverse
    motor.throttle_raw         FF21CA byte 0
    motor.controller_temp_c    FF21CA byte 5 (only emitted when nonzero)

Decoder assumptions (verify against the BMS spec before trusting numerically):
  * Source 0xF3 is the BMS, 0xE5 is the external charger, 0xF4 is a vehicle
    controller.
  * PGN 0xF113..0xF13C: 4 cell voltages per frame, big-endian uint16 mV.
        cell_index = (PGN - 0xF113) * 4 + slot
  * PGN 0xF155..0xF15E: 8 module temperatures per frame, uint8 with the
    J1939-style +40 C offset (raw 0x35 = 13 C).
        temp_index = (PGN - 0xF155) * 8 + slot
  * PGN 0xF102: bytes 1-2 BE = max cell mV, bytes 3-4 BE = min cell mV,
                bytes 5-6 = max/min cell index, byte 8 = spread/flags.
  * PGN 0xF100 bytes 3-4 BE = signed pack current at 0.1 A/bit, biased so that
        raw 0x7D00 = 0 A (positive = drawing from pack, negative = charging).
        Cross-validated by the amp-*.asc dashboard-anchored set (1, 18, 35, 42,
        58 A): mean decoded current matches the displayed dashboard reading
        within ~1 A across the full range, including across the 0x7D->0x7E and
        0x7F->0x80 high-byte rollovers.
  * PGN 0xFF50 from 0xE5: byte 1 = status (0x02 = active),
                          bytes 2-3 LE = charger output voltage (raw),
                          bytes 4-5 LE = charger output current in 0.1 A/bit.
    Voltage scale is uncertain pending a full-SOC capture; both raw and a
    tentative 1/3 V/bit estimate are emitted.
  * PGN 0xFF21 from 0xCA: motor controller / drive ECU telemetry.
        byte 0     = throttle pedal position (raw, ~0..0x34)
        bytes 2-3  = motor RPM magnitude, little-endian uint16, biased by 0x0C80
                     (rpm = ((b3<<8)|b2) - 0x0C80; verified against a
                     0->2500 RPM acceleration trace). Always positive; sign of
                     motion comes from byte 7.
        byte 5     = controller temperature with the J1939 +40 C offset.
        byte 7     = directional pedal state (foot-pedal selector):
                       0x10 = idle / neither pedal
                       0x14 = forward pedal pressed
                       0x18 = reverse pedal pressed
    Frame is suppressed entirely while charging (contactors open for traction).
"""

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
PGN_F100 = 0xF100   # pack status (bytes 3-4 BE = signed pack current)
PGN_F102 = 0xF102   # cell min/max summary

# Charger broadcast.
PGN_FF50 = 0xFF50   # charger telemetry (V, A)

# Motor controller broadcast.
PGN_FF21 = 0xFF21   # motor telemetry (RPM, throttle, drive-state, ctrl temp)

TEMP_OFFSET_C = 40

PACK_CURRENT_LSB_A = 0.1                  # F100F3 bytes 3-4 BE, 0.1 A/bit
PACK_CURRENT_BIAS_RAW = 0x7D00            # raw value at 0 A (positive = discharge)
CHARGER_V_LSB_V = 1.0 / 3.0               # tentative; revisit with full-SOC data
CHARGER_I_LSB_A = 0.1
RPM_BIAS = 0x0C80                         # FF21CA bytes 2-3 LE zero-RPM offset


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
    0x00F108: "BMS aux status",
    0x00FF50: "Charger telemetry (V, A)",
    0x00FF21: "Motor telemetry (RPM, throttle, state)",
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
        "charger": 0, "vc": 0, "motor": 0,
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
                        mv = be16(data[2 * slot], data[2 * slot + 1])
                        if mv == 0:
                            continue
                        emissions.append(
                            (f"cell.{base + slot:02d}.voltage_v",
                             mv / 1000.0, "v"))
                    sc["cells"] += 1

                elif PGN_TEMP_FIRST <= pgn <= PGN_TEMP_LAST:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    base = (pgn - PGN_TEMP_FIRST) * 8
                    for slot, b in enumerate(data):
                        if b == 0:
                            continue
                        emissions.append(
                            (f"temp.{base + slot:02d}.c",
                             b - TEMP_OFFSET_C, "c"))
                    sc["temps"] += 1

                elif pgn == PGN_F100:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    raw = be16(data[2], data[3])  # bytes 3-4 BE, biased u16
                    amps = (raw - PACK_CURRENT_BIAS_RAW) * PACK_CURRENT_LSB_A
                    emissions.append(("pack.voltage_proxy_b2", data[1], ""))
                    emissions.append(("pack.current_raw", raw, ""))
                    emissions.append(("pack.current_a", round(amps, 1), "a"))
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
                    emissions.append(("pack.byte5", data[4], ""))
                    emissions.append(("pack.byte6_min_idx", data[5], ""))
                    emissions.append(("pack.flags", data[7], ""))
                    emissions.append(("pack.v_estimate", pack_v, "v"))
                    sc["f102"] += 1

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
                # byte 5 is +40 C-offset; 0 means "not present" in this frame.
                if data[5]:
                    emissions.append(
                        ("motor.controller_temp_c",
                         data[5] - TEMP_OFFSET_C, "c"))
                sc["motor"] += 1

            elif src == SRC_CHARGER and pgn == PGN_FF50:
                if all(b == 0 for b in data):
                    sc["skipped_zero"] += 1
                    continue
                v_raw = le16(data[1], data[2])
                i_raw = le16(data[3], data[4])
                emissions.append(("charger.status", data[0], ""))
                emissions.append(("charger.v_raw", v_raw, ""))
                emissions.append(
                    ("charger.voltage_v",
                     round(v_raw * CHARGER_V_LSB_V, 2), "v"))
                emissions.append(("charger.i_raw", i_raw, ""))
                emissions.append(
                    ("charger.current_a",
                     round(i_raw * CHARGER_I_LSB_A, 1), "a"))
                sc["charger"] += 1

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
     "NN = (PGN-0xF113)*4 + slot; zero-mV slots suppressed"),
    ("temp.NN.c", "F155..F15E", "F3", "slot 0..7",
     "u8 - 40", "c", "verified",
     "NN = (PGN-0xF155)*8 + slot; J1939 +40C offset; zero bytes suppressed"),
    ("pack.cell_max_mv", "F102", "F3", "0-1", "BE u16",
     "mv", "verified", ""),
    ("pack.cell_min_mv", "F102", "F3", "2-3", "BE u16",
     "mv", "verified", ""),
    ("pack.cell_spread_mv", "F102", "F3", "0-3", "max - min",
     "mv", "verified", ""),
    ("pack.byte5", "F102", "F3", "4", "u8 (raw)",
     "", "unknown", "semantics not yet identified"),
    ("pack.byte6_min_idx", "F102", "F3", "5", "u8 (raw)",
     "", "tentative", "appears to encode min-cell index"),
    ("pack.flags", "F102", "F3", "7", "u8 (raw)",
     "", "unknown", ""),
    ("pack.v_estimate", "F102", "F3", "0-3", "20 * (max+min)/2 / 1000",
     "v", "verified", "assumes 20-cell pack"),
    ("pack.voltage_proxy_b2", "F100", "F3", "1", "u8 (raw)",
     "", "tentative", "proxy; semantics unconfirmed"),
    ("pack.current_raw", "F100", "F3", "2-3", "BE u16 (biased)",
     "", "verified", "subtract 0x7D00 for signed amps"),
    ("pack.current_a", "F100", "F3", "2-3", "(BE u16 - 0x7D00) * 0.1",
     "a", "verified",
     "+draw / -charge; cross-validated against amp-*.asc dashboard captures"),
    ("charger.status", "FF50", "E5", "0", "u8 (raw)",
     "", "verified", "0x02 = active"),
    ("charger.v_raw", "FF50", "E5", "1-2", "LE u16",
     "", "verified", ""),
    ("charger.voltage_v", "FF50", "E5", "1-2", "LE u16 * (1/3)",
     "v", "tentative", "scale needs full-SOC verification"),
    ("charger.i_raw", "FF50", "E5", "3-4", "LE u16",
     "", "verified", ""),
    ("charger.current_a", "FF50", "E5", "3-4", "LE u16 * 0.1",
     "a", "verified", ""),
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
    ("motor.controller_temp_c", "FF21", "CA", "5", "u8 - 40",
     "c", "tentative", "0 = not present in this frame; suppressed"),
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
    """Emit the per-unique-ID J1939 decode table as ids.csv and stdout."""
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

    print()
    print(f"{'ID':<10} {'count':>6} {'P':>1} {'DP':>2} "
          f"{'PF':>2} {'PS':>2} {'SA':>2} {'PGN':>6} {'PDU':<4} {'name'}")
    for d, n in decoded:
        print(f"{d['id']:<10} {n:>6} "
              f"{d['priority']!s:>1} {d['dp']!s:>2} "
              f"{d['pf']:>2} {d['ps']:>2} {d['sa']:>2} "
              f"{d['pgn']:>6} {d['pdu']:<4} {d['name']}")


def values_for(rows: list, scenario: str, signal: str):
    """Pull all values for one scenario+signal pair."""
    return [r[4] for r in rows if r[0] == scenario and r[3] == signal]


def summarize(counts: dict, rows: list):
    print()
    print(f"{'file':<28} {'frames':>7} {'cells':>6} {'temps':>6} "
          f"{'F100':>5} {'F102':>5} {'chgr':>5} {'vc':>5} {'motor':>6}")
    for scenario, sc in counts.items():
        print(f"{scenario:<28} {sc['total']:>7} {sc['cells']:>6} "
              f"{sc['temps']:>6} {sc['f100']:>5} {sc['f102']:>5} "
              f"{sc['charger']:>5} {sc['vc']:>5} {sc['motor']:>6}")

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
        chgr_v = values_for(rows, scenario, "charger.voltage_v")
        chgr_i = values_for(rows, scenario, "charger.current_a")
        if chgr_v:
            print(f"    chgr V    : {min(chgr_v):.1f}..{max(chgr_v):.1f} V "
                  f"(1/3 V/bit, tentative)")
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
    if len(sys.argv) <= 1:
        print(f"usage: {sys.argv[0]} file1.asc [file2.blf ...]",
              file=sys.stderr)
        print("supported formats: any python-can LogReader format "
              "(.asc, .blf, .log, .trc, python-can .csv)", file=sys.stderr)
        sys.exit(2)
    inputs = [Path(a) for a in sys.argv[1:]]
    out_dir = inputs[0].parent

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
