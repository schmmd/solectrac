#!/usr/bin/env python3
"""
Decode J1939-style CAN logs from a small electric tractor BMS / charger.

Usage:
    python3 parse_solectrac_can.py file1.csv [file2.csv ...]

Each input row is tagged with its source filename, so the output CSVs
combine all captures into a single tidy long-format dataset that's easy
to filter / pivot.

Outputs (written next to the inputs):
    cells.csv          per-cell voltage samples
    temps.csv          per-channel module temperatures (degC, +40 offset removed)
    cell_summary.csv   max/min cell mV from PGN F102 (and inferred pack voltage)
    pack_current.csv   pack current magnitude inferred from F100 byte 4
    charger.csv        external charger telemetry (PGN FF50, source 0xE5)
    stdout             per-scenario summary

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
  * PGN 0xF100 byte 4 ~ |pack current| in 0.1 A/bit (sign unknown; tentative).
  * PGN 0xFF50 from 0xE5: byte 1 = status (0x02 = active),
                          bytes 2-3 LE = charger output voltage (raw),
                          bytes 4-5 LE = charger output current in 0.1 A/bit.
    Voltage scale is uncertain pending a full-SOC capture; stored as raw and
    a tentative 1/3 V/bit estimate.
"""

import csv
import sys
from pathlib import Path

# --- bus map -----------------------------------------------------------------
SRC_BMS = 0xF3
SRC_CHARGER = 0xE5
SRC_VEHICLE = 0xD0   # vehicle controller; broadcasts a minimal F100 heartbeat

# Decoded names for the byte-1 state field of 18F100D0.
VC_STATE_NAMES = {
    0x00: "init",
    0x0C: "ready",
}

# Cell-voltage and temperature PGN windows (BMS broadcasts).
PGN_CELL_FIRST, PGN_CELL_LAST = 0xF113, 0xF13C
PGN_TEMP_FIRST, PGN_TEMP_LAST = 0xF155, 0xF15E

# Aggregate / status PGNs from BMS.
PGN_F100 = 0xF100   # pack status (byte 4 ~ |current|)
PGN_F102 = 0xF102   # cell min/max summary
# PGN 0xF104, 0xF106, 0xF107 are also broadcast but not yet decoded numerically.

# Charger broadcast.
PGN_FF50 = 0xFF50   # charger telemetry (V, A)

TEMP_OFFSET_C = 40
PACK_CURRENT_LSB_A = 0.1                  # tentative scaling for F100 byte 4
CHARGER_V_LSB_V = 1.0 / 3.0               # tentative; revisit with full-SOC data
CHARGER_I_LSB_A = 0.1


# --- helpers -----------------------------------------------------------------

def parse_id(id_hex: str):
    """Return (priority, pgn, source) from a 29-bit J1939 ID in hex."""
    can_id = int(id_hex, 16)
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
    0x00F100: "BMS pack status (incl. |I|)",
    0x00F102: "BMS cell min/max summary",
    0x00F104: "BMS temp min/max summary",
    0x00F106: "BMS state (charger-dependent)",
    0x00F107: "BMS current/voltage limits",
    0x00F108: "BMS aux status",
    0x00FF50: "Charger telemetry (V, A)",
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


def describe_id(id_hex: str) -> dict:
    """Decode a CAN ID (11- or 29-bit) into J1939 fields."""
    can_id = int(id_hex, 16)
    is_29bit = can_id > 0x7FF or len(id_hex.strip()) > 3
    if not is_29bit:
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


def data_bytes(row):
    """Return 8 ints, padding with 0 for short or missing fields."""
    out = []
    for i in range(1, 9):
        v = row.get(f"D{i}") or "00"
        try:
            out.append(int(v, 16))
        except ValueError:
            out.append(0)
    return out


def be16(hi, lo):
    return (hi << 8) | lo


def le16(lo, hi):
    return (hi << 8) | lo


# --- per-frame decoders ------------------------------------------------------

def decode_file(path: Path, scenario: str, sinks: dict, counts: dict,
                id_counts: dict):
    """Stream one CSV; append decoded rows to the sinks dict in-place."""
    sc = counts.setdefault(scenario, {
        "total": 0, "cells": 0, "temps": 0, "f100": 0, "f102": 0,
        "charger": 0, "vc": 0, "skipped_zero": 0, "extended_false": 0,
    })

    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            sc["total"] += 1
            id_hex = (row.get("ID") or "").strip()
            if id_hex:
                id_counts[id_hex] = id_counts.get(id_hex, 0) + 1
            ext = (row.get("Extended") or "").strip().lower()
            if ext == "false":
                # 11-bit IDs aren't J1939; leave them out of the BMS decode.
                sc["extended_false"] += 1
                continue
            try:
                _, pgn, src = parse_id(row["ID"])
            except (KeyError, ValueError):
                continue

            ts = row.get("Time Stamp", "")
            data = data_bytes(row)

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
                        sinks["cells"].append(
                            (scenario, ts, base + slot, mv / 1000.0)
                        )
                    sc["cells"] += 1

                elif PGN_TEMP_FIRST <= pgn <= PGN_TEMP_LAST:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    base = (pgn - PGN_TEMP_FIRST) * 8
                    for slot, b in enumerate(data):
                        if b == 0:
                            continue
                        sinks["temps"].append(
                            (scenario, ts, base + slot, b - TEMP_OFFSET_C)
                        )
                    sc["temps"] += 1

                elif pgn == PGN_F100:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    raw = data[3]                       # byte 4 (0-indexed: 3)
                    sinks["pack_current"].append(
                        (scenario, ts,
                         f"{data[1]:02X}{data[2]:02X}",  # voltage raw bytes 2-3
                         raw,
                         round(raw * PACK_CURRENT_LSB_A, 1))
                    )
                    sc["f100"] += 1

                elif pgn == PGN_F102:
                    if all(b == 0 for b in data):
                        sc["skipped_zero"] += 1
                        continue
                    max_mv = be16(data[0], data[1])
                    min_mv = be16(data[2], data[3])
                    if max_mv == 0 or min_mv == 0:
                        continue
                    sinks["cell_summary"].append((
                        scenario, ts,
                        max_mv, min_mv, max_mv - min_mv,
                        data[4], data[5], data[7],
                        round(20 * (max_mv + min_mv) / 2.0 / 1000.0, 3),
                    ))
                    sc["f102"] += 1

            elif src == SRC_VEHICLE and pgn == PGN_F100:
                state = data[0]
                sinks["vc_status"].append((
                    scenario, ts,
                    f"{state:02X}",
                    VC_STATE_NAMES.get(state, "unknown"),
                ))
                sc["vc"] += 1

            elif src == SRC_CHARGER and pgn == PGN_FF50:
                if all(b == 0 for b in data):
                    sc["skipped_zero"] += 1
                    continue
                v_raw = le16(data[1], data[2])
                i_raw = le16(data[3], data[4])
                sinks["charger"].append((
                    scenario, ts,
                    data[0],                                     # status byte
                    v_raw, round(v_raw * CHARGER_V_LSB_V, 2),
                    i_raw, round(i_raw * CHARGER_I_LSB_A, 1),
                ))
                sc["charger"] += 1


# --- writers / summary -------------------------------------------------------

OUTPUT_SCHEMAS = {
    "cells":        ["file", "timestamp", "cell_index", "voltage_v"],
    "temps":        ["file", "timestamp", "temp_index", "temp_c"],
    "cell_summary": ["file", "timestamp",
                     "max_mv", "min_mv", "spread_mv",
                     "byte5", "byte6_min_idx", "flags",
                     "pack_v_estimate"],
    "pack_current": ["file", "timestamp",
                     "voltage_raw_b2b3", "byte4_raw", "current_a_estimate"],
    "charger":      ["file", "timestamp", "status",
                     "v_raw", "voltage_v_estimate",
                     "i_raw", "current_a"],
    "vc_status":    ["file", "timestamp", "state_raw", "state_name"],
}

# ids.csv has its own writer because the column set is naturally different.
IDS_SCHEMA = ["id", "ext", "count", "priority", "R", "DP",
              "PF", "PS", "SA", "PGN", "PDU", "PS_role", "name"]

OUTPUT_FILES = {
    "cells":        "cells.csv",
    "temps":        "temps.csv",
    "cell_summary": "cell_summary.csv",
    "pack_current": "pack_current.csv",
    "charger":      "charger.csv",
    "vc_status":    "vc_status.csv",
}


def write_outputs(sinks: dict, out_dir: Path):
    for key, rows in sinks.items():
        path = out_dir / OUTPUT_FILES[key]
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(OUTPUT_SCHEMAS[key])
            w.writerows(rows)
        print(f"wrote {path} ({len(rows)} rows)")


def write_ids(id_counts: dict, out_dir: Path):
    """Emit the per-unique-ID J1939 decode table as ids.csv and stdout."""
    path = out_dir / "ids.csv"
    decoded = []
    for id_hex, n in id_counts.items():
        d = describe_id(id_hex)
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


def summarize(counts: dict, sinks: dict):
    print()
    print(f"{'file':<28} {'frames':>7} {'cells':>6} {'temps':>6} "
          f"{'F100':>5} {'F102':>5} {'chgr':>5} {'vc':>5}")
    for scenario, sc in counts.items():
        print(f"{scenario:<28} {sc['total']:>7} {sc['cells']:>6} "
              f"{sc['temps']:>6} {sc['f100']:>5} {sc['f102']:>5} "
              f"{sc['charger']:>5} {sc['vc']:>5}")

    print("\nsummary:")
    for scenario in counts:
        cs = [r for r in sinks["cell_summary"] if r[0] == scenario]
        pc = [r for r in sinks["pack_current"] if r[0] == scenario]
        ch = [r for r in sinks["charger"] if r[0] == scenario]
        ts = [r for r in sinks["temps"] if r[0] == scenario]
        print(f"\n  {scenario}")
        if cs:
            maxs = [r[2] for r in cs]
            mins = [r[3] for r in cs]
            spreads = [r[4] for r in cs]
            print(f"    cell max  : {min(maxs)}..{max(maxs)} mV")
            print(f"    cell min  : {min(mins)}..{max(mins)} mV")
            print(f"    spread    : {min(spreads)}..{max(spreads)} mV")
            est = [r[8] for r in cs]
            print(f"    pack est  : {min(est):.2f}..{max(est):.2f} V "
                  f"(20 cells * mean cell mV)")
        if pc:
            amps = [r[4] for r in pc]
            print(f"    |I| (F100): {min(amps):.1f}..{max(amps):.1f} A "
                  f"(0.1 A/bit, tentative)")
        if ch:
            v_est = [r[4] for r in ch]
            i = [r[6] for r in ch]
            print(f"    chgr V    : {min(v_est):.1f}..{max(v_est):.1f} V "
                  f"(1/3 V/bit, tentative)")
            print(f"    chgr I    : {min(i):.1f}..{max(i):.1f} A")
        if ts:
            ts_c = [r[3] for r in ts]
            print(f"    temps     : {min(ts_c)}..{max(ts_c)} C")


# --- main --------------------------------------------------------------------

def main():
    if len(sys.argv) <= 1:
        print(f"usage: {sys.argv[0]} file1.csv [file2.csv ...]",
              file=sys.stderr)
        sys.exit(2)
    inputs = [Path(a) for a in sys.argv[1:]]
    out_dir = inputs[0].parent

    sinks = {key: [] for key in OUTPUT_SCHEMAS}
    counts = {}
    id_counts = {}

    for path in inputs:
        print(f"reading {path.name}")
        decode_file(path, path.name, sinks, counts, id_counts)

    write_outputs(sinks, out_dir)
    write_ids(id_counts, out_dir)
    summarize(counts, sinks)


if __name__ == "__main__":
    main()
