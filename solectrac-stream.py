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
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

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
SRC_CHARGER = 0xE5
SRC_VEHICLE = 0xD0
SRC_MOTOR = 0xCA

PGN_CELL_FIRST, PGN_CELL_LAST = 0xF113, 0xF13C
PGN_TEMP_FIRST, PGN_TEMP_LAST = 0xF155, 0xF15E
PGN_F100 = 0xF100
PGN_F102 = 0xF102
PGN_F108 = 0xF108
PGN_FF50 = 0xFF50
PGN_FF21 = 0xFF21

# F108 byte 7 carries a bitmap of dashboard-displayed BMS warning codes,
# packed in numeric order from the vendor BMS error-code table (operator
# manual). Each entry is (bit, code, description). Decoded from
# asc/bms-error-codes/bms-124-140-142-143-144-146.asc vs idle-no-bms.asc:
# byte 7 = 0xBB in the fault capture (= bits 0,1,3,4,5,7) maps exactly to
# the operator-confirmed codes {124, 140, 142, 143, 144, 146}. Bit 2 maps
# to code 141, which is reserved (not in the manual) and is omitted here.
BMS_FAULT_CODES_BYTE7: List[Tuple[int, int, str]] = [
    (0, 124, "Clock fault"),
    (1, 140, "System fault level"),
    (3, 142, "BMS fault need maintenance"),
    (4, 143, "Battery fault need maintenance"),
    (5, 144, "Battery system fault needs maintenance"),
    (6, 145, "Full charge/discharge cycle needed"),
    (7, 146, "Maintenance mode status"),
]

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
    motor_direction: Channel = field(default_factory=Channel)  # -1 rev / 0 idle / +1 fwd
    motor_temp_c: Channel = field(default_factory=Channel)
    # F102 cell summary
    max_cell_mv: Channel = field(default_factory=Channel)
    min_cell_mv: Channel = field(default_factory=Channel)
    spread_mv: Channel = field(default_factory=Channel)
    max_cell_n: Channel = field(default_factory=Channel)  # 1-based per BMS
    min_cell_n: Channel = field(default_factory=Channel)  # 1-based per BMS
    # Voltage-derived SOC (from min cell mV via NMC OCV table).
    soc_pct: Channel = field(default_factory=Channel)
    # F108 fault bitmap bytes. Byte 7 is decoded (BMS warning code
    # bitmap from the operator manual). Bytes 0 and 2 are known to
    # carry fault info too (seen nonzero in
    # asc/bms-error-codes/bms-fullcharge-102-109-140.asc) but the
    # bit-to-code mapping isn't established yet, and codes 100-123
    # aren't in the BMS manual table at all — so bytes 0/2 may host
    # non-BMS dashboard codes (MC / charger / VC). All 8 bytes are
    # tracked so undecoded fault info is at least visible.
    fault_bytes: List[Channel] = field(
        default_factory=lambda: [Channel() for _ in range(8)]
    )
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
            state.decoded += 1

        elif pgn == PGN_F108:
            # All zeros in healthy idle. Byte 7 = dashboard-displayed
            # warning code bitmap (decoded). Other bytes carry fault
            # info too (bytes 0 and 2 seen nonzero with non-BMS-table
            # codes 102/109 displayed) but aren't decoded yet.
            for i in range(8):
                state.fault_bytes[i].update(data[i], now)
            state.decoded += 1

        elif pgn == PGN_F102:
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

    elif src == SRC_VEHICLE and pgn == PGN_F100:
        state.vc_state_raw.update(data[0], now)
        state.decoded += 1

    elif src == SRC_MOTOR and pgn == PGN_FF21:
        # bytes 2-3 little-endian, biased by 0x0C80, give RPM magnitude.
        rpm_mag = ((data[3] << 8) | data[2]) - RPM_BIAS
        # byte 7 = directional pedal selector; only three literal values
        # have been observed (NOTES.txt). Match exactly to avoid
        # accepting unobserved values as forward/reverse.
        pedal = data[7]
        if pedal == 0x14:
            direction = 1            # forward pedal
        elif pedal == 0x18:
            direction = -1           # reverse pedal
        else:
            direction = 0            # idle / neither (0x10) or unknown
        state.motor_rpm_mag.update(rpm_mag, now)
        state.motor_rpm.update(direction * rpm_mag, now)
        state.motor_direction.update(direction, now)
        state.motor_throttle.update(data[0], now)
        if data[5]:
            state.motor_temp_c.update(data[5] - TEMP_OFFSET_C, now)
        state.decoded += 1

    elif src == SRC_CHARGER and pgn == PGN_FF50:
        if all(b == 0 for b in data):
            return
        state.chgr_status.update(data[0], now)
        v_raw = le16(data[1], data[2])
        i_raw = le16(data[3], data[4])
        state.chgr_v.update(v_raw * CHARGER_V_LSB_V + CHARGER_V_OFFSET_V, now)
        state.chgr_i.update(i_raw * CHARGER_I_LSB_A, now)
        state.decoded += 1


# --- BMS faults -------------------------------------------------------------

def active_bms_faults(state: State) -> List[Tuple[int, str]]:
    """Return [(code_number, description), ...] for currently set bits in
    F108 byte 7 (the dashboard-visible BMS warning code bitmap).

    Byte 5's semantics aren't decoded yet, so it isn't surfaced here as a
    code; render_faults shows its raw value separately for visibility.
    """
    b7 = state.fault_bytes[7].value
    if b7 is None:
        return []
    out: List[Tuple[int, str]] = []
    for bit, code, desc in BMS_FAULT_CODES_BYTE7:
        if (int(b7) >> bit) & 1:
            out.append((code, desc))
    return out


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
        codes = ", ".join(str(c) for c, _ in faults)
        alerts.append(("WARN", f"BMS reports active fault codes: {codes}"))

    return alerts


# --- TUI rendering ----------------------------------------------------------

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

    soc = state.soc_pct.value
    if soc is not None:
        bar_w = 20
        filled = int(round(soc * bar_w / 100))
        bar = Text("█" * filled + "░" * (bar_w - filled))
        if soc < 15:
            soc_style = "bold red"
        elif soc < 30:
            soc_style = "yellow"
        else:
            soc_style = "green"
        # Tag the reading when the pack is under significant load: terminal
        # voltage is depressed (discharging) or elevated (charging) and the
        # voltage-only SOC won't match the true coulomb-counted state.
        if pi is not None and abs(pi) > SOC_REST_CURRENT_A:
            tag = " (loaded)" if pi > 0 else " (charging)"
        else:
            tag = ""
        if state.soc_pct.is_stale(now):
            tag += " stale"
        soc_text = Text.assemble(
            bar,
            Text(f"  {soc:>3.0f}%", style=soc_style),
            Text(tag, style="dim"),
        )
        t.add_row("SOC", soc_text)

    return Panel(t, title="Pack", border_style="green")


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
    return Panel(t, title="Charger", border_style="green")


def render_cells(state: State, now: float) -> Panel:
    cells = state.cells
    vals = [c.value for c in cells if c.value is not None]
    if not vals:
        return Panel(Text("(no cell data yet)", style="dim"),
                     title="Cell voltages", border_style="blue")

    lo, hi = min(vals), max(vals)
    span = max(1, hi - lo)
    bar_w = 24

    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right")
    t.add_column()
    t.add_column(justify="right")

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
        t.add_row(f"#{n:>2}", bar, mv_text)

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
    t = Table.grid(padding=(0, 2))
    for _ in range(NUM_TEMPS):
        t.add_column(justify="center")
    t.add_row(*[Text(f"T{i}", style="dim") for i in range(NUM_TEMPS)])
    cells_row = []
    for ch in state.temps:
        if ch.value is None:
            cells_row.append(Text("---", style="dim"))
        else:
            style = "yellow dim" if ch.is_stale(now) else None
            cells_row.append(Text(
                f"{int(ch.value)}°C ({int(c_to_f(ch.value))}°F)",
                style=style))
    t.add_row(*cells_row)

    vals = [c.value for c in state.temps if c.value is not None]
    if vals:
        lo, hi = min(vals), max(vals)
        delta = hi - lo
        sub = Text(f"  Δ {delta} °C ({delta * 9 / 5:.0f} °F)    "
                   f"range {lo}–{hi} °C "
                   f"({c_to_f(lo):.0f}–{c_to_f(hi):.0f} °F)",
                   style="dim")
    else:
        sub = Text("")
    return Panel(Group(t, sub), title="Temperatures", border_style="blue")


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
        # Approximate full-scale 0x34 = 52 raw seen in captures.
        pct = int(round(thr * 100 / 52))
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
        di_text = Text("idle", style="dim")
    t.add_row("direction", di_text)

    mt = state.motor_temp_c.value
    if mt is None:
        mt_text = Text("---", style="dim")
    else:
        text = f"{mt:.0f} °C ({c_to_f(mt):.0f} °F)"
        if state.motor_temp_c.is_stale(now):
            mt_text = Text(text, style="yellow dim")
        else:
            mt_text = Text(text)
    t.add_row("ctrl temp", mt_text)

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
    """Display BMS fault info from F108. Byte 7 has a decoded bit-to-code
    mapping (vendor BMS error-code table); other bytes are shown as raw
    values when nonzero so undecoded fault data is at least visible.
    """
    bytes_seen = any(c.value is not None for c in state.fault_bytes)
    if not bytes_seen:
        return Panel(Text("(no F108 frame seen yet)", style="dim"),
                     title="BMS faults", border_style="blue")

    # Stale if the most recent F108 byte update is older than STALE_S.
    stamps = [c.ts for c in state.fault_bytes if c.ts is not None]
    stale = (not stamps) or ((now - max(stamps)) > STALE_S)

    vals = [int(c.value) if c.value is not None else 0
            for c in state.fault_bytes]
    nonzero_other = [(i, v) for i, v in enumerate(vals)
                     if v != 0 and i != 7]

    faults = active_bms_faults(state)

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right")
    t.add_column(justify="left")

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

    if faults or nonzero_other:
        border = "red" if faults else "yellow"
    else:
        border = "green"

    return Panel(Group(t, raw), title="BMS faults (F108)",
                 border_style=border)


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
        Layout(name="row1", size=9),
        Layout(name="cells", size=NUM_CELLS + 4),
        Layout(name="row3", size=5),
        Layout(name="row4", size=8),
        Layout(name="faults", size=13),
        Layout(name="alerts", size=8),
    )
    layout["header"].update(render_header(state, now))
    layout["row1"].split_row(
        Layout(render_pack(state, args.mains_v, args.efficiency, now)),
        Layout(render_charger(state, now)),
    )
    layout["cells"].update(render_cells(state, now))
    layout["row3"].update(render_temps(state, now))
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
    p.add_argument("--replay-rate", type=float, default=0.0,
                   help="seconds to sleep between replayed frames "
                        "(0 = as fast as possible)")
    p.add_argument("--realtime", action="store_true",
                   help="for --replay, pace frames using their original "
                        "timestamps so playback runs at recorded speed")
    p.add_argument("--replay-speed", type=float, default=1.0,
                   help="multiplier for --realtime replay "
                        "(2.0 = 2x faster, 0.5 = half speed)")
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
                speed = args.replay_speed if args.replay_speed > 0 else 1.0
                for msg in source:
                    if stop_evt.is_set():
                        break
                    if raw_logger is not None:
                        raw_logger(msg)
                    if args.realtime and getattr(msg, "timestamp", None):
                        if first_msg_ts is None:
                            first_msg_ts = msg.timestamp
                            replay_start = time.monotonic()
                        else:
                            elapsed_log = (msg.timestamp - first_msg_ts) / speed
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
                    if args.replay_rate > 0:
                        time.sleep(args.replay_rate)
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
