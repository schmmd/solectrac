#!/usr/bin/env python3
"""Live TUI for the Solectrac UDAN BMS diagnostic CAN port.

Polls the UDS DIDs documented in BMS.md over 0x740 (request) / 0x748 (response)
and renders the data across five iBMS-style tabs.

  ← / →     cycle tabs              1..5  jump to tab
  q / Ctrl-C exit

Tabs:
  1 Overview   identity + pack state + cells + temps + extremes + alarms
  2 Charging   0x0900 / 0x0901 / 0x0902 (charger connection, V/A, lock, fault)
  3 BMU        0x1600 BMU rails, 0x1620 on-board temps, 0x0E00 HV detection,
               0x0E40 Hall/Shunt current
  4 CellHealth 0x0EA0/0x0EA1 balancing, 0x0ED0-0x0ED7 open-wire / short flags,
               0x2803 / 0x2804 cell extremum index
  5 Identity   0xA503 / 0xA505 / 0xA50D identity blocks + 0x0100 Batt config

The default UDS session is read-only — no SecAccess unlock is required for
any of the DIDs this tool reads.

Usage:
    util/solectrac-bms-diagnostics.py                          # canalystii ch0
    util/solectrac-bms-diagnostics.py --interface socketcand   # solectrac.local
    util/solectrac-bms-diagnostics.py --interface socketcand \\
        --host solectrac.local --port 28600 --channel can0
    util/solectrac-bms-diagnostics.py --interface slcan \\
        --channel /dev/tty.usbmodem1101
    util/solectrac-bms-diagnostics.py --replay data/bms-connection.asc
    util/solectrac-bms-diagnostics.py --replay data/bms-connection.asc \\
        --replay-speed 5 --no-loop
"""

import argparse
import os
import select
import sys
import termios
import time
import tty
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

import can
from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

BITRATE = 250_000
REQ_ID = 0x740
RESP_ID = 0x748

# Per-frame and end-to-end timeouts (seconds).
FRAME_TIMEOUT = 0.5
RESPONSE_TIMEOUT = 1.5


# ---------------------------------------------------------------------------
# ISO-TP transport (just enough to drive UDS ReadDataByIdentifier).
# ---------------------------------------------------------------------------


class IsoTpError(Exception):
    pass


class IsoTpTimeout(IsoTpError):
    pass


class IsoTp:
    """Single-target ISO-TP 15765-2 over classic CAN, 11-bit IDs, 8-byte frames."""

    def __init__(self, bus: can.BusABC, req_id: int, resp_id: int, pad: int = 0x00):
        self.bus = bus
        self.req_id = req_id
        self.resp_id = resp_id
        self.pad = pad

    def _pad(self, data: bytes) -> bytes:
        if len(data) >= 8:
            return data[:8]
        return data + bytes([self.pad]) * (8 - len(data))

    def _send_frame(self, data: bytes):
        msg = can.Message(
            arbitration_id=self.req_id,
            data=self._pad(data),
            is_extended_id=False,
        )
        self.bus.send(msg)

    def _recv_frame(self, deadline: float) -> can.Message:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsoTpTimeout("no response frame")
            msg = self.bus.recv(timeout=min(remaining, FRAME_TIMEOUT))
            if msg is None:
                continue
            if msg.is_extended_id or msg.arbitration_id != self.resp_id:
                continue
            return msg

    def send(self, payload: bytes):
        if len(payload) <= 7:
            self._send_frame(bytes([len(payload)]) + payload)
        else:
            # First frame + flow-control wait + consecutive frames.
            first = bytes([0x10 | ((len(payload) >> 8) & 0x0F), len(payload) & 0xFF]) + payload[:6]
            self._send_frame(first)
            # Wait for flow control from the BMS.
            deadline = time.monotonic() + RESPONSE_TIMEOUT
            fc = self._recv_frame(deadline)
            if (fc.data[0] >> 4) != 0x3 or (fc.data[0] & 0x0F) != 0:
                raise IsoTpError(f"unexpected FC frame: {fc.data.hex()}")
            seq = 1
            offset = 6
            while offset < len(payload):
                chunk = payload[offset : offset + 7]
                self._send_frame(bytes([0x20 | (seq & 0x0F)]) + chunk)
                seq += 1
                offset += 7

    def recv(self) -> bytes:
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        msg = self._recv_frame(deadline)
        pci = msg.data[0]
        pt = pci >> 4
        if pt == 0:
            n = pci & 0x0F
            return bytes(msg.data[1 : 1 + n])
        if pt == 1:
            total = ((pci & 0x0F) << 8) | msg.data[1]
            buf = bytearray(msg.data[2:8])
            # Send flow control: CTS, BS=0 (no limit), STmin=0 (as fast as possible).
            self._send_frame(bytes([0x30, 0x00, 0x00]))
            seq = 1
            while len(buf) < total:
                cf = self._recv_frame(deadline)
                if (cf.data[0] >> 4) != 0x2:
                    raise IsoTpError(f"expected CF, got {cf.data.hex()}")
                if (cf.data[0] & 0x0F) != (seq & 0x0F):
                    raise IsoTpError(
                        f"CF sequence mismatch: want {seq & 0x0F}, got {cf.data[0] & 0x0F}"
                    )
                buf.extend(cf.data[1:8])
                seq += 1
            return bytes(buf[:total])
        raise IsoTpError(f"unexpected PCI type {pt:#x}")


# ---------------------------------------------------------------------------
# ISO-TP reassembly for captures (same algorithm as util/uds_extract.py).
# ---------------------------------------------------------------------------


def iso_tp_assemble(frames):
    """frames: iterable of (ts, data_bytes). Yields (ts, full_payload_bytes)."""
    pending = None
    for ts, data in frames:
        if not data:
            continue
        pt = data[0] >> 4
        pl = data[0] & 0x0F
        if pt == 0:
            n = pl
            yield ts, bytes(data[1 : 1 + n])
            pending = None
        elif pt == 1:
            n = (pl << 8) | data[1]
            pending = {"ts": ts, "len": n, "buf": bytearray(data[2:8]), "seq": 1}
        elif pt == 2:
            if pending is None:
                continue
            if pl != (pending["seq"] & 0x0F):
                pending = None
                continue
            pending["buf"].extend(data[1:8])
            pending["seq"] += 1
            if len(pending["buf"]) >= pending["len"]:
                yield pending["ts"], bytes(pending["buf"][: pending["len"]])
                pending = None
        # Flow-control frames (pt=3) are ignored.


# ---------------------------------------------------------------------------
# UDS ReadDataByIdentifier helper.
# ---------------------------------------------------------------------------


class UdsError(Exception):
    pass


NRC_NAMES = {
    0x10: "GeneralReject",
    0x11: "ServiceNotSupported",
    0x12: "SubFunctionNotSupported",
    0x13: "IncorrectMessageLengthOrInvalidFormat",
    0x22: "ConditionsNotCorrect",
    0x31: "RequestOutOfRange",
    0x33: "SecurityAccessDenied",
    0x35: "InvalidKey",
    0x36: "ExceededNumberOfAttempts",
    0x7E: "SubFunctionNotSupportedInActiveSession",
    0x7F: "ServiceNotSupportedInActiveSession",
}


def read_did(tp: IsoTp, did: int) -> bytes:
    tp.send(bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]))
    payload = tp.recv()
    if len(payload) >= 3 and payload[0] == 0x7F and payload[1] == 0x22:
        nrc = payload[2]
        raise UdsError(f"NRC 0x{nrc:02X} {NRC_NAMES.get(nrc, '?')} on DID 0x{did:04X}")
    if (
        len(payload) < 3
        or payload[0] != 0x62
        or payload[1] != (did >> 8) & 0xFF
        or payload[2] != did & 0xFF
    ):
        raise UdsError(f"malformed response for DID 0x{did:04X}: {payload.hex()}")
    return payload[3:]


# ---------------------------------------------------------------------------
# Transport: uniform read_did() / describe() interface for live and replay.
# ---------------------------------------------------------------------------


class LiveTransport:
    def __init__(self, bus: can.BusABC, desc: str, req_id: int = REQ_ID, resp_id: int = RESP_ID):
        self.bus = bus
        self._desc = desc
        self.tp = IsoTp(bus, req_id, resp_id)

    def drain(self):
        while self.bus.recv(timeout=0.05) is not None:
            pass

    def read_did(self, did: int) -> bytes:
        return read_did(self.tp, did)

    def describe(self) -> str:
        return self._desc

    def close(self):
        self.bus.shutdown()


class ReplayTransport:
    """Replays UDS responses extracted from a previously captured log.

    Time progresses with wall clock (scaled by ``speed``). Each ``read_did``
    returns the most recent response for that DID with capture-time
    ``ts <= now``. The transport never touches a CAN bus.
    """

    def __init__(
        self,
        path: str,
        req_id: int = REQ_ID,
        resp_id: int = RESP_ID,
        speed: float = 1.0,
        loop: bool = True,
    ):
        self.path = path
        self.speed = speed
        self.loop = loop
        self.responses, self.first_ts, self.last_ts = self._load(path, req_id, resp_id)
        if not self.responses:
            raise SystemExit(
                f"replay: no UDS responses extracted from {path!r} "
                f"(expected request id 0x{req_id:03X} / response id 0x{resp_id:03X})"
            )
        self.wall_start = time.monotonic()

    @staticmethod
    def _load(path: str, req_id: int, resp_id: int):
        try:
            reader = can.LogReader(path)
        except Exception as e:
            raise SystemExit(f"replay: failed to open {path!r}: {e}") from e
        req_frames = []
        resp_frames = []
        for msg in reader:
            if msg.is_extended_id:
                continue
            data = bytes(msg.data)
            if msg.arbitration_id == req_id:
                req_frames.append((msg.timestamp, data))
            elif msg.arbitration_id == resp_id:
                resp_frames.append((msg.timestamp, data))

        reqs = list(iso_tp_assemble(req_frames))
        resps = list(iso_tp_assemble(resp_frames))

        # Match each ReadDID request to the next response with ts >= req_ts.
        responses_by_did: dict = {}
        rj = 0
        for ts, req_payload in reqs:
            if len(req_payload) < 3 or req_payload[0] != 0x22:
                continue
            did = (req_payload[1] << 8) | req_payload[2]
            while rj < len(resps) and resps[rj][0] < ts:
                rj += 1
            if rj >= len(resps):
                break
            r_ts, r_payload = resps[rj]
            rj += 1
            if (
                len(r_payload) < 3
                or r_payload[0] != 0x62
                or ((r_payload[1] << 8) | r_payload[2]) != did
            ):
                continue
            responses_by_did.setdefault(did, []).append((r_ts, r_payload[3:]))

        if not responses_by_did:
            return {}, 0.0, 0.0
        first = min(lst[0][0] for lst in responses_by_did.values())
        last = max(lst[-1][0] for lst in responses_by_did.values())
        return responses_by_did, first, last

    @property
    def duration(self) -> float:
        return max(self.last_ts - self.first_ts, 0.001)

    @property
    def virtual_time(self) -> float:
        elapsed = (time.monotonic() - self.wall_start) * self.speed
        if self.loop:
            elapsed %= self.duration
        elif elapsed > self.duration:
            elapsed = self.duration
        return self.first_ts + elapsed

    def drain(self):
        pass

    def read_did(self, did: int) -> bytes:
        series = self.responses.get(did)
        if not series:
            raise UdsError(f"no captured response for DID 0x{did:04X}")
        t = self.virtual_time
        latest = None
        for ts, data in series:
            if ts <= t:
                latest = data
            else:
                break
        return latest if latest is not None else series[0][1]

    def describe(self) -> str:
        elapsed = self.virtual_time - self.first_ts
        flag = " loop" if self.loop else ""
        speed = "" if self.speed == 1.0 else f" ×{self.speed:g}"
        return (
            f"replay {os.path.basename(self.path)} "
            f"t={elapsed:5.1f}/{self.duration:.1f}s{speed}{flag} "
            f"({len(self.responses)} DIDs)"
        )

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Decoders for the DIDs we care about (see BMS.md).
# ---------------------------------------------------------------------------


@dataclass
class BmsState:
    # Identity (one-shot)
    fw_version: Optional[str] = None
    hw_string: Optional[str] = None
    discovery: Optional[int] = None

    # Pack state (DID 0x2800)
    soc_pct: Optional[float] = None
    soh_pct: Optional[float] = None
    pack_v: Optional[float] = None
    pack_a: Optional[float] = None
    state_extra: tuple = ()

    # Counters
    lifetime_counter: Optional[int] = None
    session_uptime_s: Optional[int] = None
    charge_time_s: Optional[int] = None
    discharge_time_s: Optional[int] = None
    heartbeat: Optional[int] = None

    # Energy (DID 0x2810)
    cell_count: Optional[int] = None
    cycle_count: Optional[int] = None
    charge_ah: Optional[float] = None
    discharge_ah: Optional[float] = None

    # Cells (DID 0x0101) and temps (DID 0x0102)
    cell_mv: list = field(default_factory=list)
    temp_c: list = field(default_factory=list)

    # Peak data (DID cluster 0x2820/2828/2830/2838)
    # Each entry: (value, subsys_0based, idx_0based)
    max_cells: list = field(default_factory=list)
    min_cells: list = field(default_factory=list)
    max_temps: list = field(default_factory=list)
    min_temps: list = field(default_factory=list)

    # Alarms (DID 0x4000)
    alarms_raw: bytes = b""

    # Charging cluster (0x0900 / 0x0901 / 0x0902)
    charging_flags: bytes = b""    # 7B: enum/flag block (conn, S2, lock)
    charging_meas: bytes = b""     # 14B: V/A measurements, trailing CC sentinels
    charging_state: bytes = b""    # 14B: fault / state machine

    # BMU on-board rails (BMS tab)
    bmu_power: bytes = b""         # 0x1600 — BMU power-supply rail (~12.75 V)
    bmu_temps: bytes = b""         # 0x1620 — on-board NTC temps
    hv_detect: bytes = b""         # 0x0E00 — pack-V × 10 twice + state
    shunt_state: bytes = b""       # 0x0E40 — Hall / Shunt current

    # Cell health (Cell info tab)
    balance_a: bytes = b""         # 0x0EA0 — balancing
    balance_b: bytes = b""         # 0x0EA1 — balancing
    open_wire: dict = field(default_factory=dict)   # 0x0ED0..0x0ED7
    cell_extremum: bytes = b""     # 0x2803
    cell_index: bytes = b""        # 0x2804

    # Extended identity (Identity tab)
    ident_503: bytes = b""
    ident_505: bytes = b""
    ident_50d: bytes = b""
    batt_config: bytes = b""       # 0x0100 — Batt config thresholds

    # UI
    current_view: int = 0
    view_names: tuple = ("Overview", "Charging", "BMU", "CellHealth", "Identity")
    view_msg: str = ""

    # Comms stats
    polls_ok: int = 0
    polls_err: int = 0
    last_error: str = ""
    last_update_s: float = 0.0
    poll_durations_ms: deque = field(default_factory=lambda: deque(maxlen=10))


def _be_u16(data: bytes, off: int) -> int:
    return (data[off] << 8) | data[off + 1]


def _be_i16(data: bytes, off: int) -> int:
    v = _be_u16(data, off)
    return v - 0x10000 if v & 0x8000 else v


def _be_u32(data: bytes, off: int) -> int:
    return (data[off] << 24) | (data[off + 1] << 16) | (data[off + 2] << 8) | data[off + 3]


def decode_pack_state(data: bytes, st: BmsState):
    if len(data) < 12:
        return
    st.soc_pct = _be_u16(data, 0) / 10.0
    st.soh_pct = _be_u16(data, 2) / 10.0
    st.pack_v = _be_u16(data, 4) / 10.0
    st.pack_a = _be_i16(data, 6) / 10.0  # TENTATIVE: signed pack current per BMS.md
    st.state_extra = (_be_u16(data, 8), _be_u16(data, 10))


def decode_times(data: bytes, st: BmsState):
    if len(data) < 16:
        return
    st.lifetime_counter = _be_u32(data, 0)
    st.session_uptime_s = _be_u32(data, 4)
    st.charge_time_s = _be_u32(data, 8)
    st.discharge_time_s = _be_u32(data, 12)
    st.heartbeat = data[3]


def decode_energy(data: bytes, st: BmsState):
    if len(data) < 20:
        return
    st.cell_count = _be_u16(data, 0)
    st.cycle_count = _be_u16(data, 2)
    st.charge_ah = _be_u32(data, 12) * 0.01
    st.discharge_ah = _be_u32(data, 16) * 0.01


def decode_cells(data: bytes, st: BmsState):
    n = len(data) // 2
    st.cell_mv = [_be_u16(data, i * 2) for i in range(n)]


def decode_temps(data: bytes, st: BmsState):
    # BMS.md: "°C = raw − 40" (TENTATIVE offset)
    st.temp_c = [b - 40 for b in data]


def decode_peak_v(data: bytes) -> list:
    # 4 × (u16 BE mV, u8 subsys, u8 cell_idx)
    out = []
    for i in range(0, len(data), 4):
        if i + 4 > len(data):
            break
        out.append((_be_u16(data, i), data[i + 2], data[i + 3]))
    return out


def decode_peak_t(data: bytes) -> list:
    # 4 × (u8 raw, u8 subsys, u8 probe_idx)
    out = []
    for i in range(0, len(data), 3):
        if i + 3 > len(data):
            break
        out.append((data[i] - 40, data[i + 1], data[i + 2]))
    return out


# --- Raw-store decoders (panels parse the bytes at render time) ------------


def _store(attr):
    def _set(data: bytes, st: BmsState):
        setattr(st, attr, data)
    return _set


def _store_open_wire(did: int):
    def _set(data: bytes, st: BmsState):
        st.open_wire[did] = data
    return _set


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def fmt_dur(s: Optional[int]) -> str:
    if s is None:
        return "—"
    h, r = divmod(int(s), 3600)
    m, sec = divmod(r, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h:02d}:{m:02d}:{sec:02d}"
    return f"{h:02d}:{m:02d}:{sec:02d}"


def fmt_opt(v, fmt="{}", dash="—"):
    return dash if v is None else fmt.format(v)


def panel_identity(st: BmsState, transport) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(style="dim", justify="right")
    t.add_column()
    t.add_row("Bus", transport.describe())
    t.add_row("Request / Response", f"0x{REQ_ID:03X} / 0x{RESP_ID:03X} @ {BITRATE // 1000} kbit/s")
    t.add_row("Firmware (0xF195)", st.fw_version or "—")
    t.add_row("Hardware (0xA50F)", st.hw_string or "—")
    t.add_row("Discovery (0xA500)", "—" if st.discovery is None else f"0x{st.discovery:02X}")
    return Panel(t, title="Identity", border_style="cyan")


def panel_pack(st: BmsState) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column(style="bold")
    t.add_row("SOC", fmt_opt(st.soc_pct, "{:.1f} %"))
    t.add_row("SOH", fmt_opt(st.soh_pct, "{:.1f} %"))
    t.add_row("Pack voltage", fmt_opt(st.pack_v, "{:.1f} V"))
    t.add_row("Pack current", fmt_opt(st.pack_a, "{:+.1f} A"))
    t.add_row("Cycles", fmt_opt(st.cycle_count))
    t.add_row("Cells", fmt_opt(st.cell_count))
    t.add_row("Charged", fmt_opt(st.charge_ah, "{:.2f} Ah"))
    t.add_row("Discharged", fmt_opt(st.discharge_ah, "{:.2f} Ah"))
    t.add_row("Session uptime", fmt_dur(st.session_uptime_s))
    t.add_row("Charge time (Σ)", fmt_dur(st.charge_time_s))
    t.add_row("Discharge time (Σ)", fmt_dur(st.discharge_time_s))
    t.add_row("Heartbeat byte", fmt_opt(st.heartbeat, "0x{:02X}"))
    return Panel(t, title="Pack state", border_style="green")


def panel_cells(st: BmsState) -> Panel:
    if not st.cell_mv:
        return Panel(Align.center(Text("(no cell data yet)", style="dim")),
                     title="Cells (mV)", border_style="magenta")
    cells = st.cell_mv
    vmin = min(cells)
    vmax = max(cells)
    vavg = sum(cells) / len(cells)
    spread = vmax - vmin
    cols = 5
    t = Table.grid(padding=(0, 1))
    for _ in range(cols):
        t.add_column(justify="right")
    for row_start in range(0, len(cells), cols):
        row = []
        for i in range(row_start, min(row_start + cols, len(cells))):
            mv = cells[i]
            if mv == vmax and vmax != vmin:
                style = "bold red"
            elif mv == vmin and vmax != vmin:
                style = "bold blue"
            else:
                style = ""
            row.append(Text(f"#{i + 1:02d} {mv:4d}", style=style))
        t.add_row(*row)
    summary = Text(
        f"min {vmin}  max {vmax}  Δ {spread} mV  avg {vavg:.1f} mV",
        style="dim",
    )
    return Panel(Group(t, summary), title=f"Cells ({len(cells)} × mV)", border_style="magenta")


def panel_temps(st: BmsState) -> Panel:
    if not st.temp_c:
        return Panel(Align.center(Text("(no temp data yet)", style="dim")),
                     title="Temperatures (°C)", border_style="yellow")
    t = Table.grid(padding=(0, 2))
    for _ in range(len(st.temp_c)):
        t.add_column(justify="right")
    headers = [Text(f"P{i + 1}", style="dim") for i in range(len(st.temp_c))]
    values = [Text(f"{v:+d}", style="bold") for v in st.temp_c]
    t.add_row(*headers)
    t.add_row(*values)
    tmin = min(st.temp_c)
    tmax = max(st.temp_c)
    summary = Text(f"min {tmin}°C  max {tmax}°C  Δ {tmax - tmin}°C", style="dim")
    return Panel(Group(t, summary), title="Temperatures (°C, raw−40)", border_style="yellow")


def _peak_row(tbl: Table, label: str, entries: list, unit: str):
    if not entries:
        tbl.add_row(label, "—", "—", "—", "—")
        return
    cells = []
    for v, sub, idx in entries:
        cells.append(f"{v}{unit} (s{sub}/i{idx})")
    while len(cells) < 4:
        cells.append("—")
    tbl.add_row(label, *cells[:4])


def panel_extremes(st: BmsState) -> Panel:
    t = Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True)
    t.add_column("Rank", style="dim")
    for n in range(1, 5):
        t.add_column(f"#{n}")
    _peak_row(t, "max V", st.max_cells, "mV")
    _peak_row(t, "min V", st.min_cells, "mV")
    _peak_row(t, "max T", st.max_temps, "°C")
    _peak_row(t, "min T", st.min_temps, "°C")
    return Panel(t, title="Extremes (0x2820/0x2828/0x2830/0x2838)", border_style="blue")


# Per BMS.md: 31 bytes, idle sentinel 0xFF at fixed positions {11, 12, 21, 24-30}.
ALARM_SENTINEL_POSITIONS = {11, 12, 21, 24, 25, 26, 27, 28, 29, 30}


def panel_alarms(st: BmsState) -> Panel:
    if not st.alarms_raw:
        return Panel(Align.center(Text("(no alarm data yet)", style="dim")),
                     title="Alarms (0x4000)", border_style="red")
    active = []
    for i, b in enumerate(st.alarms_raw):
        if b == 0x00:
            continue
        if b == 0xFF and i in ALARM_SENTINEL_POSITIONS:
            continue
        active.append((i, b))
    if not active:
        body = Text("OK — no faults active", style="green")
    else:
        body_lines = []
        for i, b in active:
            sev = {0x01: "L1", 0x02: "L2", 0x03: "L3"}.get(b, f"0x{b:02X}")
            body_lines.append(f"byte[{i:02d}] = {sev}")
        body = Text("\n".join(body_lines), style="bold red")
    return Panel(body, title="Alarms (0x4000)", border_style="red")


def panel_comms(st: BmsState) -> Panel:
    avg_ms = (sum(st.poll_durations_ms) / len(st.poll_durations_ms)) if st.poll_durations_ms else 0
    age = (time.monotonic() - st.last_update_s) if st.last_update_s else 0
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    t.add_row("Polls OK", str(st.polls_ok))
    t.add_row("Polls err", str(st.polls_err))
    t.add_row("Avg poll", f"{avg_ms:.0f} ms")
    t.add_row("Last update", f"{age:.1f} s ago" if st.last_update_s else "—")
    if st.last_error:
        t.add_row("Last error", Text(st.last_error, style="red"))
    return Panel(t, title="Comms", border_style="white")


def _hex_or_dash(data: bytes, width: int = 24) -> str:
    if not data:
        return "—"
    return data[:width].hex(" ")


def _kv(label: str, value, style: str = "bold") -> tuple:
    if value is None or value == "":
        return (Text(label, style="dim"), Text("—", style="dim"))
    return (Text(label, style="dim"), Text(str(value), style=style))


def panel_charging(st: BmsState) -> Panel:
    """0x0900 (7B) flags, 0x0901 (14B) measurements, 0x0902 (14B) fault state.

    Field layout is TENTATIVE per BMS.md — all observed captures had the
    charger disconnected. We show the decoded best-guess plus the raw bytes
    so anything we get wrong is still visible.
    """
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()

    # 0x0900 flags: enum/flag block (charger conn, S2, lock)
    if st.charging_flags:
        f = st.charging_flags
        conn = "connected" if (len(f) > 0 and f[0]) else "not connected"
        s2 = "active" if (len(f) > 2 and f[2]) else "inactive"
        t.add_row(*_kv("Charger conn (0x0900[0])", conn))
        t.add_row(*_kv("S2 (0x0900[2]) TENT.", s2))
        t.add_row(*_kv("0x0900 raw", _hex_or_dash(f)))
    else:
        t.add_row(*_kv("0x0900", None))

    # 0x0901: 14B measurements; trailing FFFFFFFF = CC Res + CC2 Res sentinels
    if st.charging_meas and len(st.charging_meas) >= 14:
        m = st.charging_meas
        v1 = _be_u16(m, 0) / 100.0   # TENT: charger V × 100
        v2 = _be_u16(m, 4) / 100.0
        cc_res_sent = m[10:14] == b"\xff\xff\xff\xff"
        t.add_row(*_kv("0x0901[0:2] V×100 TENT.", f"{v1:.2f}"))
        t.add_row(*_kv("0x0901[4:6] V×100 TENT.", f"{v2:.2f}"))
        t.add_row(*_kv("CC/CC2 Res", "sentinel (disconnected)" if cc_res_sent else m[10:14].hex(" ")))
        t.add_row(*_kv("0x0901 raw", _hex_or_dash(m)))
    else:
        t.add_row(*_kv("0x0901", None))

    # 0x0902: 14B fault / state machine
    if st.charging_state:
        s = st.charging_state
        nonzero = any(b for b in s)
        t.add_row(*_kv("0x0902 fault/state", "all zero (idle)" if not nonzero else "non-zero — fault?"))
        t.add_row(*_kv("0x0902 raw", _hex_or_dash(s)))
    else:
        t.add_row(*_kv("0x0902", None))

    return Panel(t, title="Charging cluster (0x0900/01/02) — TENTATIVE layout",
                 border_style="green")


def panel_bmu_rails(st: BmsState) -> Panel:
    """0x1600 BMU power-supply rail (~12.75 V per BMS.md).

    Sample from screenshots: 22B `31 f8 00 00 ...`. 0x31F8 = 12792 → /1000 = 12.792 V,
    matching the iBMS-displayed 12.75 V order of magnitude.
    """
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    if st.bmu_power and len(st.bmu_power) >= 2:
        bmu_v = _be_u16(st.bmu_power, 0) / 1000.0
        t.add_row(*_kv("BMU rail (0x1600[0:2] /1000)", f"{bmu_v:.3f} V"))
        t.add_row(*_kv("0x1600 raw", _hex_or_dash(st.bmu_power, 22)))
    else:
        t.add_row(*_kv("0x1600", None))
    return Panel(t, title="BMU power (0x1600)", border_style="blue")


def panel_bmu_temps(st: BmsState) -> Panel:
    """0x1620 on-board NTC temperatures — BMS.md says raw values with same
    raw−40 °C convention as the cell temps."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    if st.bmu_temps:
        # Decode every non-zero byte as °C = raw − 40
        cells = []
        for i, b in enumerate(st.bmu_temps):
            if b == 0:
                cells.append(("—", "off"))
            else:
                cells.append((f"#{i}", f"{b - 40:+d} °C"))
        for lbl, val in cells:
            t.add_row(*_kv(f"NTC {lbl}", val))
        t.add_row(*_kv("0x1620 raw", _hex_or_dash(st.bmu_temps)))
    else:
        t.add_row(*_kv("0x1620", None))
    return Panel(t, title="On-board temps (0x1620)", border_style="yellow")


def panel_hv_detect(st: BmsState) -> Panel:
    """0x0E00 — BMS.md: 'pack-V × 10 twice + state'."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    if st.hv_detect and len(st.hv_detect) >= 4:
        d = st.hv_detect
        v1 = _be_u16(d, 0) / 10.0
        v2 = _be_u16(d, 2) / 10.0
        t.add_row(*_kv("HV1 (0x0E00[0:2] /10)", f"{v1:.1f} V"))
        t.add_row(*_kv("HV2 (0x0E00[2:4] /10)", f"{v2:.1f} V"))
        if len(d) > 4:
            t.add_row(*_kv("Trailing state", _hex_or_dash(d[4:])))
    else:
        t.add_row(*_kv("0x0E00", None))
    return Panel(t, title="HV detection (0x0E00)", border_style="magenta")


def panel_shunt(st: BmsState) -> Panel:
    """0x0E40 — BMS.md: 'Shunt state (Hall current sensing)'."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    if st.shunt_state and len(st.shunt_state) >= 2:
        d = st.shunt_state
        # Signed Hall current — TENTATIVE scale
        hall = _be_i16(d, 0) / 100.0
        t.add_row(*_kv("Hall (0x0E40[0:2] /100 signed) TENT.", f"{hall:+.2f} A"))
        t.add_row(*_kv("0x0E40 raw", _hex_or_dash(d)))
    else:
        t.add_row(*_kv("0x0E40", None))
    return Panel(t, title="Shunt / Hall (0x0E40) — TENTATIVE",
                 border_style="blue")


def panel_balancing(st: BmsState) -> Panel:
    """0x0EA0 / 0x0EA1 — balancing flags per BMS.md."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    for did, raw in ((0x0EA0, st.balance_a), (0x0EA1, st.balance_b)):
        if not raw:
            t.add_row(*_kv(f"0x{did:04X}", None))
            continue
        all_ff = all(b == 0xFF for b in raw)
        bal = "no balancing active (all 0xFF)" if all_ff else "active — see raw"
        t.add_row(*_kv(f"0x{did:04X}", bal))
        t.add_row(*_kv(f"0x{did:04X} raw", _hex_or_dash(raw)))
    return Panel(t, title="Balancing (0x0EA0/0x0EA1)", border_style="cyan")


def panel_open_wire(st: BmsState) -> Panel:
    """0x0ED0..0x0ED7 — open-wire / short flags per BMS.md."""
    t = Table.grid(padding=(0, 1))
    t.add_column(style="dim", justify="right")
    t.add_column()
    if not st.open_wire:
        t.add_row(*_kv("0x0ED0–0x0ED7", None))
    else:
        for did in sorted(st.open_wire):
            raw = st.open_wire[did]
            all_ff = raw and all(b == 0xFF for b in raw)
            mark = " (idle)" if all_ff else ""
            t.add_row(*_kv(f"0x{did:04X}{mark}", _hex_or_dash(raw, 8)))
    return Panel(t, title="Open-wire / short (0x0ED0–0x0ED7)", border_style="red")


def panel_cell_extremum(st: BmsState) -> Panel:
    """0x2803 / 0x2804 — cell extremum + index per BMS.md."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    t.add_row(*_kv("0x2803", _hex_or_dash(st.cell_extremum)))
    t.add_row(*_kv("0x2804", _hex_or_dash(st.cell_index)))
    return Panel(t, title="Cell extremum (0x2803 / 0x2804)", border_style="magenta")


def panel_batt_config(st: BmsState) -> Panel:
    """0x0100 — Batt config thresholds. ~35 B per BMS.md ('constant config').

    Cross-referenced against Screenshot 2 'System config' panel
    (Batt. type / Rated capacity 300 Ah / series 20 / NTC count 7 / etc.):

      byte 0    : chemistry/type enum (0x06 on this pack — probably NCM)
      BE u16 1  : rated capacity × 10 = 3000 → 300.0 Ah ✓
      BE u16 3  : rated current × 10  = 5000 → 500.0 A  ✓
      BE u16 5  : rated voltage × 10  = 720  → 72.0 V   ✓
      BE u16 7  : ? (1500)  — possibly max charge current × 10
      BE u16 9  : ? (300)   — possibly max discharge current × 10
      BE u16 11 : series count = 20  ✓
      BE u16 13 : parallel ? = 20
      BE u16 15 : NTC count = 7  ✓
      BE u16 17 : ? (500)
      BE u16 19 : ? (1000) — probably initial SOC × 10 = 100.0 %

    Marked TENTATIVE — derived from a single-pack capture cross-checked
    against iBMS UI text, not a vendor spec.
    """
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column(style="bold")
    if not st.batt_config or len(st.batt_config) < 21:
        t.add_row(*_kv("0x0100", None))
        return Panel(t, title="Batt config (0x0100) — TENTATIVE",
                     border_style="green")
    d = st.batt_config
    t.add_row(*_kv("Chemistry enum (byte 0)", f"0x{d[0]:02X}"))
    t.add_row(*_kv("Rated capacity", f"{_be_u16(d, 1) / 10:.1f} Ah"))
    t.add_row(*_kv("Rated current",  f"{_be_u16(d, 3) / 10:.1f} A"))
    t.add_row(*_kv("Rated voltage",  f"{_be_u16(d, 5) / 10:.1f} V"))
    t.add_row(*_kv("Field@7 TENT.",  f"{_be_u16(d, 7) / 10:.1f}"))
    t.add_row(*_kv("Field@9 TENT.",  f"{_be_u16(d, 9) / 10:.1f}"))
    t.add_row(*_kv("Series count",   _be_u16(d, 11)))
    t.add_row(*_kv("Parallel TENT.", _be_u16(d, 13)))
    t.add_row(*_kv("NTC count",      _be_u16(d, 15)))
    t.add_row(*_kv("Field@17 TENT.", f"{_be_u16(d, 17) / 10:.1f}"))
    t.add_row(*_kv("Initial SOC TENT.", f"{_be_u16(d, 19) / 10:.1f} %"))
    t.add_row(*_kv("Raw (first 28B)", _hex_or_dash(d, 28)))
    return Panel(t, title="Batt config (0x0100) — TENTATIVE layout",
                 border_style="green")


def panel_extra_identity(st: BmsState) -> Panel:
    """0xA503 / 0xA505 / 0xA50D — identity/status blocks, UNKNOWN layouts."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    for did, raw in ((0xA503, st.ident_503), (0xA505, st.ident_505), (0xA50D, st.ident_50d)):
        t.add_row(*_kv(f"0x{did:04X} ({len(raw)}B)", _hex_or_dash(raw, 24)))
    return Panel(t, title="Identity/status blocks (UNKNOWN layouts)",
                 border_style="cyan")


def panel_tab_bar(st: BmsState) -> Panel:
    chips = []
    for i, name in enumerate(st.view_names):
        marker = f"[{i + 1}] {name}"
        if i == st.current_view:
            chips.append(Text(f" {marker} ", style="reverse bold"))
        else:
            chips.append(Text(f" {marker} ", style="dim"))
        chips.append(Text("  "))
    line1 = Text.assemble(*chips)
    line2 = Text(
        "← / →  cycle    1..5 jump    q quit"
        + (f"    │  {st.view_msg}" if st.view_msg else ""),
        style="dim",
    )
    return Panel(Group(line1, line2), border_style="white")


def _layout_overview(st: BmsState, transport) -> Layout:
    body = Layout()
    body.split_column(
        Layout(name="top", size=9),
        Layout(name="middle"),
        Layout(name="bottom", size=9),
    )
    body["top"].split_row(
        Layout(panel_identity(st, transport), name="ident", ratio=2),
        Layout(panel_comms(st), name="comms", ratio=1),
    )
    body["middle"].split_row(
        Layout(panel_pack(st), name="pack", ratio=1),
        Layout(panel_cells(st), name="cells", ratio=2),
    )
    body["bottom"].split_row(
        Layout(panel_temps(st), name="temps", ratio=1),
        Layout(panel_extremes(st), name="ext", ratio=2),
        Layout(panel_alarms(st), name="alarms", ratio=1),
    )
    return body


def _layout_charging(st: BmsState, transport) -> Layout:
    body = Layout()
    body.split_row(
        Layout(panel_charging(st), name="charging", ratio=2),
        Layout(panel_comms(st), name="comms", ratio=1),
    )
    return body


def _layout_bmu(st: BmsState, transport) -> Layout:
    body = Layout()
    body.split_column(
        Layout(name="top"),
        Layout(name="bottom"),
    )
    body["top"].split_row(
        Layout(panel_bmu_rails(st), name="rails"),
        Layout(panel_bmu_temps(st), name="temps"),
    )
    body["bottom"].split_row(
        Layout(panel_hv_detect(st), name="hv"),
        Layout(panel_shunt(st), name="shunt"),
        Layout(panel_comms(st), name="comms"),
    )
    return body


def _layout_cellhealth(st: BmsState, transport) -> Layout:
    body = Layout()
    body.split_column(
        Layout(name="top"),
        Layout(name="bottom", size=9),
    )
    body["top"].split_row(
        Layout(panel_balancing(st), name="bal"),
        Layout(panel_open_wire(st), name="open"),
    )
    body["bottom"].split_row(
        Layout(panel_cell_extremum(st), name="ext", ratio=2),
        Layout(panel_comms(st), name="comms", ratio=1),
    )
    return body


def _layout_identity(st: BmsState, transport) -> Layout:
    body = Layout()
    body.split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=1),
    )
    body["left"].split_column(
        Layout(panel_identity(st, transport), name="ident"),
        Layout(panel_extra_identity(st), name="extra"),
    )
    body["right"].split_column(
        Layout(panel_batt_config(st), name="config"),
        Layout(panel_comms(st), name="comms", size=9),
    )
    return body


VIEW_LAYOUTS = (
    _layout_overview,
    _layout_charging,
    _layout_bmu,
    _layout_cellhealth,
    _layout_identity,
)


def build_layout(st: BmsState, transport) -> Layout:
    root = Layout()
    root.split_column(
        Layout(panel_tab_bar(st), name="tabs", size=4),
        Layout(name="body"),
    )
    view = max(0, min(st.current_view, len(VIEW_LAYOUTS) - 1))
    root["body"].update(VIEW_LAYOUTS[view](st, transport))
    return root


# ---------------------------------------------------------------------------
# Polling loop.
# ---------------------------------------------------------------------------


BASELINE_POLLS = [
    (0x2800, decode_pack_state),
    (0x2801, decode_times),
    (0x2810, decode_energy),
    (0x0101, decode_cells),
    (0x0102, decode_temps),
]

PEAK_POLLS = [
    (0x2820, "max_cells", decode_peak_v),
    (0x2828, "min_cells", decode_peak_v),
    (0x2830, "max_temps", decode_peak_t),
    (0x2838, "min_temps", decode_peak_t),
]


# Each view's per-poll list: (did, decoder_callable_taking(data, st)).
VIEW_POLLS = {
    0: (  # Overview
        [(d, dec) for d, dec in BASELINE_POLLS]
        + [(0x4000, _store("alarms_raw"))]
    ),
    1: [  # Charging
        (0x0900, _store("charging_flags")),
        (0x0901, _store("charging_meas")),
        (0x0902, _store("charging_state")),
    ],
    2: [  # BMU
        (0x1600, _store("bmu_power")),
        (0x1620, _store("bmu_temps")),
        (0x0E00, _store("hv_detect")),
        (0x0E40, _store("shunt_state")),
    ],
    3: [  # Cell health
        (0x0EA0, _store("balance_a")),
        (0x0EA1, _store("balance_b")),
        (0x2803, _store("cell_extremum")),
        (0x2804, _store("cell_index")),
    ] + [(0x0ED0 + i, _store_open_wire(0x0ED0 + i)) for i in range(8)],
    4: [  # Identity
        (0xA503, _store("ident_503")),
        (0xA505, _store("ident_505")),
        (0xA50D, _store("ident_50d")),
        (0x0100, _store("batt_config")),
    ],
}


PEAK_POLLS_BY_VIEW = {0: PEAK_POLLS}


def read_identity(transport, st: BmsState):
    try:
        st.fw_version = transport.read_did(0xF195).decode("ascii", errors="replace").rstrip("\x00")
    except (IsoTpError, UdsError) as e:
        st.last_error = f"0xF195: {e}"
    try:
        st.hw_string = transport.read_did(0xA50F).decode("ascii", errors="replace").rstrip("\x00")
    except (IsoTpError, UdsError) as e:
        st.last_error = f"0xA50F: {e}"
    try:
        d = transport.read_did(0xA500)
        st.discovery = d[0] if d else None
    except (IsoTpError, UdsError) as e:
        st.last_error = f"0xA500: {e}"


def poll_once(transport, st: BmsState):
    """Poll the DIDs needed by the currently-active view.

    Per-DID UdsError is non-fatal (other panels still get refreshed). An
    IsoTpError indicates a transport problem and aborts the rest of the cycle.
    """
    start = time.monotonic()
    any_ok = False

    polls = VIEW_POLLS.get(st.current_view, [])
    for did, decoder in polls:
        try:
            decoder(transport.read_did(did), st)
            any_ok = True
        except UdsError as e:
            st.last_error = f"0x{did:04X}: {e}"
        except IsoTpError as e:
            st.polls_err += 1
            st.last_error = f"0x{did:04X}: {e}"
            return

    for did, attr, decoder in PEAK_POLLS_BY_VIEW.get(st.current_view, []):
        try:
            setattr(st, attr, decoder(transport.read_did(did)))
            any_ok = True
        except UdsError as e:
            st.last_error = f"0x{did:04X}: {e}"
        except IsoTpError as e:
            st.polls_err += 1
            st.last_error = f"0x{did:04X}: {e}"
            return

    if any_ok:
        st.polls_ok += 1
        st.last_update_s = time.monotonic()
        st.poll_durations_ms.append((st.last_update_s - start) * 1000)
    elif polls:
        st.polls_err += 1


# ---------------------------------------------------------------------------
# Keyboard handling: cbreak + non-blocking stdin reads.
# ---------------------------------------------------------------------------


@contextmanager
def cbreak_stdin():
    """Put stdin into cbreak (non-canonical, no echo). Restore on exit.

    If stdin isn't a TTY (e.g. piped), just yield without changing modes.
    """
    if not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_key_nonblocking() -> Optional[str]:
    """Return one key event, or None if nothing is pending. Handles arrows."""
    if not sys.stdin.isatty():
        return None
    r, _, _ = select.select([sys.stdin], [], [], 0)
    if not r:
        return None
    ch = sys.stdin.read(1)
    if ch != "\x1b":
        return ch
    # Escape — could be a bare ESC or the start of a CSI sequence.
    r, _, _ = select.select([sys.stdin], [], [], 0.05)
    if not r:
        return "\x1b"
    rest = sys.stdin.read(2)
    if rest == "[A":
        return "UP"
    if rest == "[B":
        return "DOWN"
    if rest == "[C":
        return "RIGHT"
    if rest == "[D":
        return "LEFT"
    return "\x1b" + rest


def handle_key(key: str, st: BmsState) -> bool:
    """Apply a key to BmsState. Returns False if the user wants to quit."""
    if key in ("q", "Q", "\x03", "\x04"):  # q, Ctrl-C, Ctrl-D
        return False
    if key == "LEFT":
        st.current_view = (st.current_view - 1) % len(st.view_names)
        st.view_msg = f"switched to {st.view_names[st.current_view]}"
    elif key == "RIGHT":
        st.current_view = (st.current_view + 1) % len(st.view_names)
        st.view_msg = f"switched to {st.view_names[st.current_view]}"
    elif key in ("1", "2", "3", "4", "5"):
        idx = int(key) - 1
        if 0 <= idx < len(st.view_names):
            st.current_view = idx
            st.view_msg = f"switched to {st.view_names[idx]}"
    return True


# ---------------------------------------------------------------------------
# CLI / main.
# ---------------------------------------------------------------------------


def open_transport(args):
    if args.replay:
        return ReplayTransport(
            args.replay, speed=args.replay_speed, loop=not args.no_loop
        )
    if args.interface == "canalystii":
        bus = can.Bus(interface="canalystii", channel=args.channel_index, bitrate=BITRATE)
        desc = f"canalystii ch{args.channel_index} @ {BITRATE // 1000} kbit/s"
    elif args.interface == "socketcand":
        bus = can.Bus(
            interface="socketcand",
            channel=args.channel,
            host=args.host,
            port=args.port,
        )
        desc = f"socketcand {args.host}:{args.port}/{args.channel}"
    elif args.interface == "slcan":
        bus = can.Bus(interface="slcan", channel=args.channel, bitrate=BITRATE)
        desc = f"slcan {args.channel} @ {BITRATE // 1000} kbit/s"
    else:
        raise SystemExit(f"unknown interface: {args.interface}")
    return LiveTransport(bus, desc)


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--interface",
        choices=("canalystii", "socketcand", "slcan"),
        default="canalystii",
        help="CAN backend (default: canalystii)",
    )
    p.add_argument("--channel-index", type=int, default=0,
                   help="canalystii channel index (default: 0)")
    p.add_argument("--host", default="solectrac.local",
                   help="socketcand host (default: solectrac.local)")
    p.add_argument("--port", type=int, default=28600,
                   help="socketcand port (default: 28600)")
    p.add_argument("--channel", default="can0",
                   help="socketcand channel name or slcan serial device "
                        "(default: can0 — for slcan use e.g. /dev/tty.usbmodem1101)")
    p.add_argument("--rate", type=float, default=1.0,
                   help="polling / refresh rate in Hz (default: 1.0)")
    p.add_argument("--replay", metavar="FILE",
                   help="replay UDS responses from a captured CAN log "
                        "(.asc, .blf, .log, .trc, ...) instead of opening a bus")
    p.add_argument("--replay-speed", type=float, default=1.0,
                   help="replay time scale (default: 1.0 = real time)")
    p.add_argument("--no-loop", action="store_true",
                   help="stop at end of replay capture instead of looping")
    args = p.parse_args()

    transport = open_transport(args)
    st = BmsState()

    period = 1.0 / args.rate
    running = True
    try:
        with cbreak_stdin(), Live(
            build_layout(st, transport), refresh_per_second=8, screen=True
        ) as live:
            transport.drain()
            read_identity(transport, st)
            live.update(build_layout(st, transport))
            next_t = time.monotonic()
            while running:
                # Drain any pending key events first so view-changes feel snappy.
                while True:
                    key = read_key_nonblocking()
                    if key is None:
                        break
                    if not handle_key(key, st):
                        running = False
                        break
                if not running:
                    break

                poll_once(transport, st)
                live.update(build_layout(st, transport))

                next_t += period
                # Sleep in small slices so key presses are picked up promptly
                # without hammering the bus.
                while running:
                    slack = next_t - time.monotonic()
                    if slack <= 0:
                        break
                    chunk = min(slack, 0.05)
                    time.sleep(chunk)
                    key = read_key_nonblocking()
                    if key is not None:
                        if not handle_key(key, st):
                            running = False
                            break
                        live.update(build_layout(st, transport))
                if not running:
                    break
                if next_t < time.monotonic() - period:
                    next_t = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()
    print("Stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
