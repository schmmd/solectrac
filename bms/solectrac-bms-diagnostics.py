#!/usr/bin/env python3
"""Web dashboard for the Solectrac UDAN BMS diagnostic CAN port.

A background thread polls the UDS DIDs documented in BMS.md over
0x740 (request) / 0x748 (response). The HTTP server on localhost serves a
single-page dashboard that re-renders the snapshot at ~1 Hz, organised into
five sections matching the iBMS UI tabs:

  Overview    identity + pack state + per-cell voltages + temps + extremes + alarms
  Charging    0x0900 / 0x0901 / 0x0902 (charger connection, V/A, lock, fault)
  BMU         0x1600 BMU rails, 0x1620 on-board temps, 0x0E00 HV detection,
              0x0E40 Hall/Shunt current
  CellHealth  0x0EA0/0x0EA1 balancing, 0x0ED0-0x0ED7 open-wire / short flags,
              0x2803 / 0x2804 cell extremum index
  Identity    0xA503 / 0xA505 / 0xA50D identity blocks + 0x0100 Batt config

The default UDS session is read-only — no SecAccess unlock is required for
any of the DIDs this tool reads.

Usage:
    ./solectrac-bms-diagnostics.py                          # canalystii ch0
    ./solectrac-bms-diagnostics.py --interface socketcand   # solectrac.local
    ./solectrac-bms-diagnostics.py --interface socketcand \\
        --host solectrac.local --socketcand-port 28600 --channel can0
    ./solectrac-bms-diagnostics.py --interface slcan \\
        --channel /dev/tty.usbmodem1101
    ./solectrac-bms-diagnostics.py --replay data/bms/bms-screenshots.asc
    ./solectrac-bms-diagnostics.py --replay data/bms/bms-screenshots.asc \\
        --replay-speed 5 --no-loop

Then open http://127.0.0.1:8000/ (or pass --open to launch the browser).
"""

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import can

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

    def __init__(
        self,
        bus: can.BusABC,
        req_id: int,
        resp_id: int,
        pad: int = 0x00,
        reader: Optional["can.BufferedReader"] = None,
        writer: Optional["can.Listener"] = None,
    ):
        self.bus = bus
        self.req_id = req_id
        self.resp_id = resp_id
        self.pad = pad
        # When ``reader`` is set, a Notifier owns ``bus.recv()`` so we must
        # consume from the BufferedReader instead — otherwise the Notifier
        # thread and our recv() race for incoming frames.
        self.reader = reader
        # When ``writer`` is set, also log our outgoing requests; most CAN
        # backends don't echo TX frames back through ``bus.recv()``.
        self.writer = writer

    def _pad(self, data: bytes) -> bytes:
        if len(data) >= 8:
            return data[:8]
        return data + bytes([self.pad]) * (8 - len(data))

    def _send_frame(self, data: bytes):
        msg = can.Message(
            arbitration_id=self.req_id,
            data=self._pad(data),
            is_extended_id=False,
            is_rx=False,
            timestamp=time.time(),
        )
        self.bus.send(msg)
        if self.writer is not None:
            self.writer.on_message_received(msg)

    def _recv_frame(self, deadline: float) -> can.Message:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsoTpTimeout("no response frame")
            timeout = min(remaining, FRAME_TIMEOUT)
            if self.reader is not None:
                msg = self.reader.get_message(timeout=timeout)
            else:
                msg = self.bus.recv(timeout=timeout)
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
    def __init__(
        self,
        bus: can.BusABC,
        desc: str,
        req_id: int = REQ_ID,
        resp_id: int = RESP_ID,
        reader: Optional["can.BufferedReader"] = None,
        writer: Optional["can.Listener"] = None,
        notifier: Optional["can.Notifier"] = None,
    ):
        self.bus = bus
        self._desc = desc
        self._reader = reader
        self._writer = writer
        self._notifier = notifier
        self.tp = IsoTp(bus, req_id, resp_id, reader=reader, writer=writer)

    def drain(self):
        if self._reader is not None:
            while self._reader.get_message(timeout=0.05) is not None:
                pass
        else:
            while self.bus.recv(timeout=0.05) is not None:
                pass

    def read_did(self, did: int) -> bytes:
        return read_did(self.tp, did)

    def describe(self) -> str:
        return self._desc

    def close(self):
        # Order matters: stop the Notifier first so no further frames are
        # dispatched to the writer, then flush the writer, then close the bus.
        if self._notifier is not None:
            self._notifier.stop()
        if self._writer is not None:
            self._writer.stop()
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

    # Comms stats
    polls_ok: int = 0
    polls_err: int = 0
    last_error: str = ""
    last_update_s: float = 0.0
    poll_durations_ms: deque = field(default_factory=lambda: deque(maxlen=10))

    # Transport label, set once at startup (used by the /state endpoint).
    transport_desc: str = ""

    def to_dict(self) -> dict:
        """JSON-safe snapshot — bytes → hex strings, deques → lists."""
        avg_ms = (sum(self.poll_durations_ms) / len(self.poll_durations_ms)) if self.poll_durations_ms else 0
        age_s = (time.time() - self.last_update_s) if self.last_update_s else None
        return {
            "identity": {
                "fw_version": self.fw_version,
                "hw_string": self.hw_string,
                "discovery": self.discovery,
                "ident_503": self.ident_503.hex(),
                "ident_505": self.ident_505.hex(),
                "ident_50d": self.ident_50d.hex(),
                "batt_config": self.batt_config.hex(),
            },
            "pack": {
                "soc_pct": self.soc_pct,
                "soh_pct": self.soh_pct,
                "pack_v": self.pack_v,
                "pack_a": self.pack_a,
                "state_extra": list(self.state_extra),
                "cell_count": self.cell_count,
                "cycle_count": self.cycle_count,
                "charge_ah": self.charge_ah,
                "discharge_ah": self.discharge_ah,
                "lifetime_counter": self.lifetime_counter,
                "session_uptime_s": self.session_uptime_s,
                "charge_time_s": self.charge_time_s,
                "discharge_time_s": self.discharge_time_s,
                "heartbeat": self.heartbeat,
            },
            "cells": {
                "cell_mv": list(self.cell_mv),
                "temp_c": list(self.temp_c),
                "max_cells": [list(t) for t in self.max_cells],
                "min_cells": [list(t) for t in self.min_cells],
                "max_temps": [list(t) for t in self.max_temps],
                "min_temps": [list(t) for t in self.min_temps],
            },
            "alarms_raw": self.alarms_raw.hex(),
            "charging": {
                "flags": self.charging_flags.hex(),
                "meas": self.charging_meas.hex(),
                "state": self.charging_state.hex(),
            },
            "bmu": {
                "power": self.bmu_power.hex(),
                "temps": self.bmu_temps.hex(),
                "hv_detect": self.hv_detect.hex(),
                "shunt_state": self.shunt_state.hex(),
            },
            "cell_health": {
                "balance_a": self.balance_a.hex(),
                "balance_b": self.balance_b.hex(),
                "open_wire": {f"0x{did:04X}": data.hex() for did, data in self.open_wire.items()},
                "cell_extremum": self.cell_extremum.hex(),
                "cell_index": self.cell_index.hex(),
            },
            "comms": {
                "transport": self.transport_desc,
                "polls_ok": self.polls_ok,
                "polls_err": self.polls_err,
                "last_error": self.last_error,
                "avg_poll_ms": avg_ms,
                "age_s": age_s,
            },
        }


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
# Polling loop.
# ---------------------------------------------------------------------------


def _store_peak(attr, decoder):
    def _set(data: bytes, st: BmsState):
        setattr(st, attr, decoder(data))
    return _set


# (DID, decoder_callable(data, st)). One unified list — the background poller
# walks the whole list every cycle. Ordered roughly by importance so a slow
# bus still gets the headline values first.
ALL_POLLS = [
    # Pack state + counters
    (0x2800, decode_pack_state),
    (0x2801, decode_times),
    (0x2810, decode_energy),
    # Per-cell + temps
    (0x0101, decode_cells),
    (0x0102, decode_temps),
    # Peaks
    (0x2820, _store_peak("max_cells", decode_peak_v)),
    (0x2828, _store_peak("min_cells", decode_peak_v)),
    (0x2830, _store_peak("max_temps", decode_peak_t)),
    (0x2838, _store_peak("min_temps", decode_peak_t)),
    # Alarms
    (0x4000, _store("alarms_raw")),
    # Charging cluster
    (0x0900, _store("charging_flags")),
    (0x0901, _store("charging_meas")),
    (0x0902, _store("charging_state")),
    # BMU on-board rails
    (0x1600, _store("bmu_power")),
    (0x1620, _store("bmu_temps")),
    (0x0E00, _store("hv_detect")),
    (0x0E40, _store("shunt_state")),
    # Cell health
    (0x0EA0, _store("balance_a")),
    (0x0EA1, _store("balance_b")),
    (0x2803, _store("cell_extremum")),
    (0x2804, _store("cell_index")),
    *[(0x0ED0 + i, _store_open_wire(0x0ED0 + i)) for i in range(8)],
    # Extended identity / config
    (0xA503, _store("ident_503")),
    (0xA505, _store("ident_505")),
    (0xA50D, _store("ident_50d")),
    (0x0100, _store("batt_config")),
]


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


def poll_once(transport, st: BmsState, lock: threading.Lock):
    """Poll every DID in ALL_POLLS, updating ``st`` under ``lock``.

    Per-DID UdsError is non-fatal — leaves that field stale and continues.
    IsoTpError signals a transport-level problem and aborts the cycle so we
    don't hammer a wedged bus.
    """
    start = time.monotonic()
    any_ok = False

    for did, decoder in ALL_POLLS:
        try:
            data = transport.read_did(did)
        except UdsError as e:
            with lock:
                st.last_error = f"0x{did:04X}: {e}"
            continue
        except IsoTpError as e:
            with lock:
                st.polls_err += 1
                st.last_error = f"0x{did:04X}: {e}"
            return
        with lock:
            decoder(data, st)
            any_ok = True

    with lock:
        if any_ok:
            st.polls_ok += 1
            st.last_update_s = time.time()
            st.poll_durations_ms.append((time.monotonic() - start) * 1000)
        else:
            st.polls_err += 1


def poller_thread(transport, st: BmsState, lock: threading.Lock,
                  period: float, stop_event: threading.Event):
    """Background polling loop. Exits when ``stop_event`` is set."""
    try:
        transport.drain()
        with lock:
            pass  # let any startup race settle
        read_identity(transport, st)
        next_t = time.monotonic()
        while not stop_event.is_set():
            poll_once(transport, st, lock)
            next_t += period
            slack = next_t - time.monotonic()
            if slack > 0:
                stop_event.wait(slack)
            else:
                next_t = time.monotonic()
    except Exception as e:
        with lock:
            st.last_error = f"poller crashed: {e!r}"


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
            host=args.socketcand_host,
            port=args.socketcand_port,
        )
        desc = f"socketcand {args.socketcand_host}:{args.socketcand_port}/{args.channel}"
    elif args.interface == "slcan":
        bus = can.Bus(interface="slcan", channel=args.channel, bitrate=BITRATE)
        desc = f"slcan {args.channel} @ {BITRATE // 1000} kbit/s"
    else:
        raise SystemExit(f"unknown interface: {args.interface}")
    return LiveTransport(bus, desc)


# ---------------------------------------------------------------------------
# HTTP server.
# ---------------------------------------------------------------------------


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Solectrac BMS Diagnostics</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161a21;
    --panel2: #1d222b;
    --border: #2a313d;
    --fg: #e6e9ef;
    --dim: #8a93a3;
    --accent: #7dd3fc;
    --ok: #4ade80;
    --warn: #facc15;
    --err: #f87171;
    --max: #f87171;
    --min: #60a5fa;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--fg);
    font: 13px/1.45 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  nav.topbar {
    position: sticky; top: 0; z-index: 10;
    background: #11141a; border-bottom: 1px solid var(--border);
    padding: 10px 20px; display: flex; align-items: center; gap: 24px; flex-wrap: wrap;
  }
  nav.topbar .title { font-weight: 700; color: var(--accent); }
  nav.topbar a {
    color: var(--fg); text-decoration: none; padding: 4px 10px;
    border-radius: 4px; border: 1px solid transparent;
  }
  nav.topbar a:hover { background: var(--panel2); border-color: var(--border); }
  nav.topbar .conn { margin-left: auto; font-size: 12px; color: var(--dim); }
  nav.topbar .conn.ok { color: var(--ok); }
  nav.topbar .conn.err { color: var(--err); }
  main { padding: 20px; max-width: 1400px; margin: 0 auto; }
  /* Offset anchor jumps so the section heading isn't hidden under the sticky topbar. */
  section { margin-bottom: 28px; scroll-margin-top: 60px; }
  section h2 {
    margin: 0 0 12px; padding-bottom: 6px; font-size: 14px; letter-spacing: 1px;
    text-transform: uppercase; color: var(--accent);
    border-bottom: 1px solid var(--border);
  }
  .grid { display: grid; gap: 12px; }
  .cols-3 { grid-template-columns: repeat(3, 1fr); }
  .cols-4 { grid-template-columns: repeat(4, 1fr); }
  .cols-2 { grid-template-columns: 1fr 1fr; }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 14px;
  }
  .panel h3 {
    margin: 0 0 10px; font-size: 12px; color: var(--dim);
    letter-spacing: 0.5px; text-transform: uppercase; font-weight: 600;
  }
  .bignum { font-size: 32px; line-height: 1; font-weight: 600; }
  .bignum .unit { font-size: 14px; color: var(--dim); margin-left: 4px; font-weight: 400; }
  .sub { color: var(--dim); font-size: 11px; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 4px 8px; }
  th { color: var(--dim); font-weight: 500; font-size: 11px; text-transform: uppercase; }
  tr + tr td { border-top: 1px solid var(--border); }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .tag { color: var(--dim); font-size: 11px; }
  .tent { color: var(--warn); font-size: 10px; margin-left: 6px; letter-spacing: 0.5px; }
  .hex { font-family: inherit; color: var(--dim); word-break: break-all; font-size: 12px; }
  .cells {
    display: grid; gap: 6px;
    grid-template-columns: repeat(5, 1fr);
  }
  .cell {
    background: var(--panel2); border: 1px solid var(--border);
    border-radius: 4px; padding: 6px 8px;
    display: grid; grid-template-columns: 28px 1fr; gap: 6px; align-items: center;
  }
  .cell.max { border-color: var(--max); }
  .cell.min { border-color: var(--min); }
  .cell .n { color: var(--dim); font-size: 11px; }
  .cell .mv { font-variant-numeric: tabular-nums; }
  .cell .bar {
    grid-column: 1 / -1; height: 4px; background: #222b38;
    border-radius: 2px; overflow: hidden;
  }
  .cell .bar > div {
    height: 100%; background: var(--accent);
  }
  .cell.max .bar > div { background: var(--max); }
  .cell.min .bar > div { background: var(--min); }
  .temps { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
  .temps .t {
    background: var(--panel2); border: 1px solid var(--border);
    border-radius: 4px; padding: 8px; text-align: center;
  }
  .temps .t .n { color: var(--dim); font-size: 11px; }
  .temps .t .v { font-size: 18px; font-variant-numeric: tabular-nums; }
  .alarm-ok { color: var(--ok); font-weight: 600; }
  .alarm-bad { color: var(--err); font-weight: 600; }
  .alarm-list { display: grid; gap: 4px; margin-top: 8px; }
  .alarm-byte {
    padding: 4px 8px; background: rgba(248, 113, 113, 0.1);
    border-left: 3px solid var(--err); border-radius: 3px;
  }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; background: var(--panel2); border: 1px solid var(--border);
  }
  .badge.ok { color: var(--ok); border-color: rgba(74, 222, 128, 0.3); }
  .badge.warn { color: var(--warn); border-color: rgba(250, 204, 21, 0.3); }
  .badge.err { color: var(--err); border-color: rgba(248, 113, 113, 0.3); }
  details { margin-top: 8px; }
  summary { color: var(--dim); cursor: pointer; font-size: 11px; }
  summary:hover { color: var(--fg); }
</style>
</head>
<body>
<nav class="topbar">
  <span class="title">Solectrac BMS</span>
  <a href="#overview">Overview</a>
  <a href="#charging">Charging</a>
  <a href="#bmu">BMU</a>
  <a href="#cellhealth">Cell health</a>
  <a href="#identity">Identity</a>
  <span class="conn" id="conn">connecting…</span>
</nav>
<main>
  <section id="overview">
    <h2>Overview</h2>
    <div class="grid cols-4" id="ov-bignums"></div>
    <div class="grid cols-2" style="margin-top:12px;">
      <div class="panel">
        <h3>Counters</h3>
        <table id="ov-counters"></table>
      </div>
      <div class="panel">
        <h3>Alarms (0x4000)</h3>
        <div id="ov-alarms"></div>
      </div>
    </div>
    <div class="panel" style="margin-top:12px;">
      <h3>Cells (mV)</h3>
      <div class="cells" id="ov-cells"></div>
      <div class="sub" id="ov-cells-summary"></div>
    </div>
    <div class="panel" style="margin-top:12px;">
      <h3>NTC temperatures (°C, raw − 40)</h3>
      <div class="temps" id="ov-temps"></div>
    </div>
    <div class="grid cols-2" style="margin-top:12px;">
      <div class="panel">
        <h3>Top-4 cells (0x2820 max / 0x2828 min)</h3>
        <table id="ov-peak-v"></table>
      </div>
      <div class="panel">
        <h3>Top-4 NTCs (0x2830 max / 0x2838 min)</h3>
        <table id="ov-peak-t"></table>
      </div>
    </div>
  </section>

  <section id="charging">
    <h2>Charging cluster <span class="tent">TENTATIVE layout</span></h2>
    <div class="grid cols-3">
      <div class="panel">
        <h3>0x0900 flags</h3>
        <div id="ch-flags"></div>
      </div>
      <div class="panel">
        <h3>0x0901 measurements</h3>
        <div id="ch-meas"></div>
      </div>
      <div class="panel">
        <h3>0x0902 fault / state</h3>
        <div id="ch-state"></div>
      </div>
    </div>
  </section>

  <section id="bmu">
    <h2>BMU on-board rails</h2>
    <div class="grid cols-4">
      <div class="panel">
        <h3>BMU power (0x1600)</h3>
        <div id="bmu-power"></div>
      </div>
      <div class="panel">
        <h3>HV detection (0x0E00)</h3>
        <div id="bmu-hv"></div>
      </div>
      <div class="panel">
        <h3>Shunt / Hall (0x0E40) <span class="tent">TENT.</span></h3>
        <div id="bmu-shunt"></div>
      </div>
      <div class="panel">
        <h3>On-board temps (0x1620)</h3>
        <div id="bmu-temps"></div>
      </div>
    </div>
  </section>

  <section id="cellhealth">
    <h2>Cell health</h2>
    <div class="grid cols-2">
      <div class="panel">
        <h3>Balancing (0x0EA0 / 0x0EA1)</h3>
        <div id="ch-balance"></div>
      </div>
      <div class="panel">
        <h3>Open-wire / short flags (0x0ED0–0x0ED7)</h3>
        <table id="ch-openwire"></table>
      </div>
    </div>
    <div class="panel" style="margin-top:12px;">
      <h3>Cell extremum (0x2803 / 0x2804)</h3>
      <table id="ch-extremum"></table>
    </div>
  </section>

  <section id="identity">
    <h2>Identity &amp; Batt config</h2>
    <div class="grid cols-2">
      <div class="panel">
        <h3>Identity</h3>
        <table id="id-basic"></table>
      </div>
      <div class="panel">
        <h3>Batt config (0x0100) <span class="tent">TENTATIVE</span></h3>
        <table id="id-battconfig"></table>
      </div>
    </div>
    <div class="panel" style="margin-top:12px;">
      <h3>Identity/status blocks (UNKNOWN layouts)</h3>
      <table id="id-extra"></table>
    </div>
    <div class="panel" style="margin-top:12px;">
      <h3>Comms</h3>
      <table id="id-comms"></table>
    </div>
  </section>
</main>

<script>
"use strict";

const $ = (id) => document.getElementById(id);
const dash = '—';

function fmtNum(v, decimals = 1, unit = '') {
  if (v == null || isNaN(v)) return dash;
  const s = Number(v).toFixed(decimals);
  return unit ? `${s} ${unit}` : s;
}
function fmtInt(v) { return v == null ? dash : v.toString(); }
function fmtDur(s) {
  if (s == null) return dash;
  s = Math.floor(s);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad = (n) => n.toString().padStart(2, '0');
  return d > 0 ? `${d}d ${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(h)}:${pad(m)}:${pad(s)}`;
}
function hexSpaced(hex, max = 32) {
  if (!hex) return dash;
  const trimmed = hex.length > max * 2 ? hex.slice(0, max * 2) + '...' : hex;
  return trimmed.match(/.{1,2}/g).join(' ');
}
function beU16(hex, off) {
  if (!hex || hex.length < (off + 2) * 2) return null;
  return parseInt(hex.slice(off * 2, off * 2 + 4), 16);
}
function beI16(hex, off) {
  const v = beU16(hex, off);
  if (v == null) return null;
  return v & 0x8000 ? v - 0x10000 : v;
}
function bigNum(label, value, unit, sub) {
  return `<div class="panel">
    <h3>${label}</h3>
    <div class="bignum">${value}<span class="unit">${unit || ''}</span></div>
    ${sub ? `<div class="sub">${sub}</div>` : ''}
  </div>`;
}
function row(label, value) {
  return `<tr><td class="tag">${label}</td><td class="num">${value}</td></tr>`;
}

// ---- Render functions ---------------------------------------------------

function renderOverview(s) {
  const p = s.pack;
  $('ov-bignums').innerHTML = [
    bigNum('SOC', fmtNum(p.soc_pct, 1), '%'),
    bigNum('SOH', fmtNum(p.soh_pct, 1), '%'),
    bigNum('Pack voltage', fmtNum(p.pack_v, 1), 'V'),
    bigNum('Pack current', fmtNum(p.pack_a, 1), 'A',
           '<span class="tent">TENTATIVE</span>'),
  ].join('');

  $('ov-counters').innerHTML = [
    row('Cells', fmtInt(p.cell_count)),
    row('Cycles', fmtInt(p.cycle_count)),
    row('Charged', fmtNum(p.charge_ah, 2, 'Ah')),
    row('Discharged', fmtNum(p.discharge_ah, 2, 'Ah')),
    row('Session uptime', fmtDur(p.session_uptime_s)),
    row('Charge time (Σ)', fmtDur(p.charge_time_s)),
    row('Discharge time (Σ)', fmtDur(p.discharge_time_s)),
    row('Heartbeat', p.heartbeat == null ? dash : '0x' + p.heartbeat.toString(16).padStart(2, '0').toUpperCase()),
  ].join('');

  renderCells(s.cells.cell_mv);
  renderTemps(s.cells.temp_c);
  renderPeakV(s.cells);
  renderPeakT(s.cells);
  renderAlarms(s.alarms_raw);
}

function renderCells(cells) {
  if (!cells || !cells.length) {
    $('ov-cells').innerHTML = `<div class="sub">${dash}</div>`;
    $('ov-cells-summary').textContent = '';
    return;
  }
  const vmin = Math.min(...cells);
  const vmax = Math.max(...cells);
  const span = Math.max(1, vmax - vmin);
  const html = cells.map((mv, i) => {
    const cls = mv === vmax && vmax !== vmin ? 'max'
              : mv === vmin && vmax !== vmin ? 'min' : '';
    const pct = ((mv - vmin) / span) * 100;
    return `<div class="cell ${cls}">
      <span class="n">#${(i + 1).toString().padStart(2, '0')}</span>
      <span class="mv">${mv} mV</span>
      <div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>
    </div>`;
  }).join('');
  $('ov-cells').innerHTML = html;
  const avg = (cells.reduce((a, b) => a + b, 0) / cells.length).toFixed(1);
  $('ov-cells-summary').textContent =
    `min ${vmin}  max ${vmax}  Δ ${vmax - vmin} mV  avg ${avg} mV`;
}

function renderTemps(temps) {
  if (!temps || !temps.length) {
    $('ov-temps').innerHTML = `<div class="sub">${dash}</div>`;
    return;
  }
  $('ov-temps').innerHTML = temps.map((t, i) =>
    `<div class="t"><div class="n">NTC ${i + 1}</div><div class="v">${t}°</div></div>`
  ).join('');
}

function peakRow(label, entries, unit) {
  if (!entries || !entries.length) {
    return `<tr><td class="tag">${label}</td><td colspan="4">${dash}</td></tr>`;
  }
  const cells = entries.slice(0, 4).map(([v, sub, idx]) =>
    `<td class="num">${v}${unit} <span class="tag">s${sub}/#${idx + 1}</span></td>`
  );
  while (cells.length < 4) cells.push('<td class="num">—</td>');
  return `<tr><td class="tag">${label}</td>${cells.join('')}</tr>`;
}

function renderPeakV(c) {
  const head = `<thead><tr><th></th><th>#1</th><th>#2</th><th>#3</th><th>#4</th></tr></thead>`;
  $('ov-peak-v').innerHTML = head + '<tbody>' +
    peakRow('max V', c.max_cells, 'mV') +
    peakRow('min V', c.min_cells, 'mV') + '</tbody>';
}
function renderPeakT(c) {
  const head = `<thead><tr><th></th><th>#1</th><th>#2</th><th>#3</th><th>#4</th></tr></thead>`;
  $('ov-peak-t').innerHTML = head + '<tbody>' +
    peakRow('max T', c.max_temps, '°') +
    peakRow('min T', c.min_temps, '°') + '</tbody>';
}

// Per BMS.md: 31 B, sentinel 0xFF at fixed positions {11,12,21,24-30}.
const ALARM_SENTINELS = new Set([11, 12, 21, 24, 25, 26, 27, 28, 29, 30]);
function renderAlarms(hex) {
  const root = $('ov-alarms');
  if (!hex) { root.innerHTML = `<div class="sub">${dash}</div>`; return; }
  const bytes = [];
  for (let i = 0; i < hex.length; i += 2) bytes.push(parseInt(hex.slice(i, i + 2), 16));
  const faults = [];
  for (let i = 0; i < bytes.length; i++) {
    const b = bytes[i];
    if (b === 0) continue;
    if (b === 0xFF && ALARM_SENTINELS.has(i)) continue;
    const sev = b === 1 ? 'L1' : b === 2 ? 'L2' : b === 3 ? 'L3' : `0x${b.toString(16).padStart(2, '0').toUpperCase()}`;
    faults.push(`<div class="alarm-byte">byte[${i.toString().padStart(2, '0')}] = ${sev}</div>`);
  }
  if (!faults.length) {
    root.innerHTML = `<div class="alarm-ok">✓ OK — no faults active</div>`;
  } else {
    root.innerHTML = `<div class="alarm-bad">${faults.length} fault(s) active</div>
      <div class="alarm-list">${faults.join('')}</div>`;
  }
}

function renderCharging(c) {
  // 0x0900 flags
  const f = c.flags;
  let flagsHtml;
  if (!f) flagsHtml = `<div class="sub">${dash}</div>`;
  else {
    const b0 = parseInt(f.slice(0, 2), 16);
    const b2 = f.length >= 6 ? parseInt(f.slice(4, 6), 16) : 0;
    const conn = b0 ? '<span class="badge ok">connected</span>' : '<span class="badge">not connected</span>';
    const s2 = b2 ? '<span class="badge ok">active</span>' : '<span class="badge">inactive</span>';
    flagsHtml = `<table>${row('Charger conn (byte 0)', conn)}${row('S2 (byte 2)', s2)}</table>
      <div class="hex" style="margin-top:8px;">${hexSpaced(f)}</div>`;
  }
  $('ch-flags').innerHTML = flagsHtml;

  // 0x0901 measurements
  const m = c.meas;
  let measHtml;
  if (!m || m.length < 28) measHtml = `<div class="sub">${dash}</div>`;
  else {
    const v1 = beU16(m, 0) / 100;
    const v2 = beU16(m, 4) / 100;
    const ccSentinel = m.slice(20, 28).toUpperCase() === 'FFFFFFFF';
    measHtml = `<table>
      ${row('V@0 (÷100) <span class="tent">TENT.</span>', fmtNum(v1, 2, 'V'))}
      ${row('V@4 (÷100) <span class="tent">TENT.</span>', fmtNum(v2, 2, 'V'))}
      ${row('CC / CC2 Resistance', ccSentinel
          ? '<span class="badge">sentinel (disconnected)</span>'
          : `<span class="hex">${m.slice(20, 28).match(/.{2}/g).join(' ')}</span>`)}
    </table>
    <div class="hex" style="margin-top:8px;">${hexSpaced(m)}</div>`;
  }
  $('ch-meas').innerHTML = measHtml;

  // 0x0902 fault / state
  const st = c.state;
  let stateHtml;
  if (!st) stateHtml = `<div class="sub">${dash}</div>`;
  else {
    const nonZero = /[1-9a-f]/i.test(st);
    const badge = nonZero
      ? '<span class="badge err">non-zero — investigate</span>'
      : '<span class="badge ok">all zero (idle)</span>';
    stateHtml = `<div>${badge}</div>
      <div class="hex" style="margin-top:8px;">${hexSpaced(st)}</div>`;
  }
  $('ch-state').innerHTML = stateHtml;
}

function renderBmu(b) {
  // 0x1600 BMU rail
  if (b.power && b.power.length >= 4) {
    const mv = beU16(b.power, 0);
    $('bmu-power').innerHTML = `<div class="bignum">${(mv / 1000).toFixed(3)}<span class="unit">V</span></div>
      <div class="sub">raw u16 [0:2] ÷ 1000 — expected ~12.75 V per BMS.md</div>
      <div class="hex" style="margin-top:8px;">${hexSpaced(b.power)}</div>`;
  } else {
    $('bmu-power').innerHTML = `<div class="sub">${dash}</div>`;
  }
  // 0x0E00 HV detection
  if (b.hv_detect && b.hv_detect.length >= 8) {
    const hv1 = (beU16(b.hv_detect, 0) / 10).toFixed(1);
    const hv2 = (beU16(b.hv_detect, 2) / 10).toFixed(1);
    $('bmu-hv').innerHTML = `<table>${row('HV1', hv1 + ' V')}${row('HV2', hv2 + ' V')}</table>
      <div class="hex" style="margin-top:8px;">${hexSpaced(b.hv_detect)}</div>`;
  } else {
    $('bmu-hv').innerHTML = `<div class="sub">${dash}</div>`;
  }
  // 0x0E40 shunt
  if (b.shunt_state && b.shunt_state.length >= 4) {
    const hall = (beI16(b.shunt_state, 0) / 100).toFixed(2);
    $('bmu-shunt').innerHTML = `<div class="bignum">${hall >= 0 ? '+' : ''}${hall}<span class="unit">A</span></div>
      <div class="sub">i16 ÷ 100 — TENTATIVE scale</div>
      <div class="hex" style="margin-top:8px;">${hexSpaced(b.shunt_state)}</div>`;
  } else {
    $('bmu-shunt').innerHTML = `<div class="sub">${dash}</div>`;
  }
  // 0x1620 on-board temps
  if (b.temps) {
    const bytes = [];
    for (let i = 0; i < b.temps.length; i += 2) bytes.push(parseInt(b.temps.slice(i, i + 2), 16));
    const rows = bytes.map((v, i) => {
      const t = v === 0 ? 'off' : `${v - 40}°C`;
      return row(`NTC #${i}`, t);
    }).join('');
    $('bmu-temps').innerHTML = `<table>${rows}</table>`;
  } else {
    $('bmu-temps').innerHTML = `<div class="sub">${dash}</div>`;
  }
}

function renderCellHealth(c) {
  // Balancing
  const balHtml = ['balance_a', 'balance_b'].map((k, i) => {
    const did = 0xEA0 + i;
    const raw = c[k];
    if (!raw) return row(`0x0${did.toString(16).toUpperCase()}`, dash);
    const allFF = /^([fF]{2})+$/.test(raw);
    const status = allFF
      ? '<span class="badge ok">no balancing (all 0xFF)</span>'
      : '<span class="badge warn">active</span>';
    return row(`0x0${did.toString(16).toUpperCase()}`, status) +
      `<tr><td></td><td><span class="hex">${hexSpaced(raw)}</span></td></tr>`;
  }).join('');
  $('ch-balance').innerHTML = `<table>${balHtml}</table>`;

  // Open-wire table
  const ow = c.open_wire || {};
  const keys = Object.keys(ow).sort();
  if (!keys.length) {
    $('ch-openwire').innerHTML = `<tbody><tr><td class="sub">${dash}</td></tr></tbody>`;
  } else {
    const rows = keys.map(k => {
      const raw = ow[k];
      const allFF = /^([fF]{2})+$/.test(raw);
      const tag = allFF ? '<span class="badge ok">idle</span>' : '<span class="badge warn">non-FF</span>';
      return `<tr><td class="tag">${k}</td><td>${tag}</td><td class="hex">${hexSpaced(raw)}</td></tr>`;
    }).join('');
    $('ch-openwire').innerHTML = `<thead><tr><th>DID</th><th>State</th><th>Raw</th></tr></thead><tbody>${rows}</tbody>`;
  }

  // Cell extremum
  $('ch-extremum').innerHTML = `<tbody>
    ${row('0x2803', `<span class="hex">${hexSpaced(c.cell_extremum)}</span>`)}
    ${row('0x2804', `<span class="hex">${hexSpaced(c.cell_index)}</span>`)}
  </tbody>`;
}

function renderIdentity(id) {
  $('id-basic').innerHTML = `<tbody>
    ${row('Firmware (0xF195)', id.fw_version || dash)}
    ${row('Hardware (0xA50F)', id.hw_string || dash)}
    ${row('Discovery (0xA500)', id.discovery == null ? dash : '0x' + id.discovery.toString(16).padStart(2, '0').toUpperCase())}
  </tbody>`;

  // Batt config decoded
  const bc = id.batt_config;
  if (!bc || bc.length < 42) {
    $('id-battconfig').innerHTML = `<tbody><tr><td class="sub">${dash}</td></tr></tbody>`;
  } else {
    const chem = '0x' + bc.slice(0, 2).toUpperCase();
    const cap = (beU16(bc, 1) / 10).toFixed(1) + ' Ah';
    const curr = (beU16(bc, 3) / 10).toFixed(1) + ' A';
    const volt = (beU16(bc, 5) / 10).toFixed(1) + ' V';
    const f7 = (beU16(bc, 7) / 10).toFixed(1);
    const f9 = (beU16(bc, 9) / 10).toFixed(1);
    const series = beU16(bc, 11);
    const par = beU16(bc, 13);
    const ntc = beU16(bc, 15);
    const f17 = (beU16(bc, 17) / 10).toFixed(1);
    const isoc = (beU16(bc, 19) / 10).toFixed(1) + ' %';
    $('id-battconfig').innerHTML = `<tbody>
      ${row('Chemistry enum (byte 0)', chem)}
      ${row('Rated capacity', cap)}
      ${row('Rated current', curr)}
      ${row('Rated voltage', volt)}
      ${row('Field@7 <span class="tent">TENT.</span>', f7)}
      ${row('Field@9 <span class="tent">TENT.</span>', f9)}
      ${row('Series count', series)}
      ${row('Parallel <span class="tent">TENT.</span>', par)}
      ${row('NTC count', ntc)}
      ${row('Field@17 <span class="tent">TENT.</span>', f17)}
      ${row('Initial SOC <span class="tent">TENT.</span>', isoc)}
    </tbody>`;
  }

  // Extra identity raw blocks
  $('id-extra').innerHTML = `<thead><tr><th>DID</th><th>Length</th><th>Raw</th></tr></thead>
    <tbody>
      ${['ident_503', 'ident_505', 'ident_50d'].map((k, i) => {
        const did = ['0xA503', '0xA505', '0xA50D'][i];
        const raw = id[k] || '';
        return `<tr><td class="tag">${did}</td><td class="num">${raw.length / 2} B</td><td class="hex">${hexSpaced(raw)}</td></tr>`;
      }).join('')}
    </tbody>`;
}

function renderComms(c) {
  $('id-comms').innerHTML = `<tbody>
    ${row('Transport', c.transport || dash)}
    ${row('Polls OK', c.polls_ok)}
    ${row('Polls error', c.polls_err)}
    ${row('Avg poll time', fmtNum(c.avg_poll_ms, 0, 'ms'))}
    ${row('Data age', c.age_s == null ? dash : fmtNum(c.age_s, 1, 's'))}
    ${row('Last error', c.last_error || '<span class="badge ok">none</span>')}
  </tbody>`;
}

// ---- Polling ------------------------------------------------------------

let lastOk = 0;
async function refresh() {
  try {
    const r = await fetch('/state', { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    renderOverview(s);
    renderCharging(s.charging);
    renderBmu(s.bmu);
    renderCellHealth(s.cell_health);
    renderIdentity(s.identity);
    renderComms(s.comms);
    lastOk = Date.now();
    $('conn').textContent = `connected · ${s.comms.transport}`;
    $('conn').className = 'conn ok';
  } catch (e) {
    $('conn').textContent = `error: ${e.message}`;
    $('conn').className = 'conn err';
  }
}

setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>
"""


class StateServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with shared BmsState + lock attached."""

    def __init__(self, addr, handler, state: BmsState, lock: threading.Lock):
        super().__init__(addr, handler)
        self.state = state
        self.lock = lock


class StateHandler(BaseHTTPRequestHandler):
    server: StateServer  # type hint for IDEs

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_bytes(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/state":
            with self.server.lock:
                body = json.dumps(self.server.state.to_dict()).encode("utf-8")
            self._serve_bytes(body, "application/json", no_cache=True)
        else:
            self.send_error(404, f"not found: {path}")

    def _serve_bytes(self, body: bytes, content_type: str, no_cache: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        # Suppress the default per-request access log — too noisy for a 1 Hz poller.
        pass


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # CAN backend
    p.add_argument(
        "--interface",
        choices=("canalystii", "socketcand", "slcan"),
        default="canalystii",
        help="CAN backend (default: canalystii)",
    )
    p.add_argument("--channel-index", type=int, default=0,
                   help="canalystii channel index (default: 0)")
    p.add_argument("--socketcand-host", default="solectrac.local",
                   help="socketcand host (default: solectrac.local)")
    p.add_argument("--socketcand-port", type=int, default=28600,
                   help="socketcand port (default: 28600)")
    p.add_argument("--channel", default="can0",
                   help="socketcand channel name or slcan serial device "
                        "(default: can0 — for slcan use e.g. /dev/tty.usbmodem1101)")
    p.add_argument("--rate", type=float, default=1.0,
                   help="polling rate in Hz (default: 1.0)")
    # Replay
    p.add_argument("--replay", metavar="FILE",
                   help="replay UDS responses from a captured CAN log "
                        "(.asc, .blf, .log, .trc, ...) instead of opening a bus")
    p.add_argument("--replay-speed", type=float, default=1.0,
                   help="replay time scale (default: 1.0 = real time)")
    p.add_argument("--no-loop", action="store_true",
                   help="stop at end of replay capture instead of looping")
    # HTTP server
    p.add_argument("--bind", default="127.0.0.1",
                   help="HTTP bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000,
                   help="HTTP port (default: 8000)")
    p.add_argument("--open", action="store_true",
                   help="open the dashboard URL in your browser after startup")
    args = p.parse_args()

    transport = open_transport(args)
    state = BmsState()
    state.transport_desc = transport.describe()
    lock = threading.Lock()
    stop_event = threading.Event()

    period = 1.0 / args.rate
    poller = threading.Thread(
        target=poller_thread,
        args=(transport, state, lock, period, stop_event),
        name="bms-poller",
        daemon=True,
    )
    poller.start()

    server = StateServer((args.bind, args.port), StateHandler, state, lock)
    url = f"http://{args.bind}:{args.port}/"
    print(f"Serving Solectrac BMS dashboard at {url}", file=sys.stderr)
    print(f"Transport: {state.transport_desc}", file=sys.stderr)
    print("Ctrl-C to stop.", file=sys.stderr)
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
    finally:
        stop_event.set()
        server.server_close()
        poller.join(timeout=2.0)
        transport.close()


if __name__ == "__main__":
    main()
