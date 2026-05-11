#!/usr/bin/env python3
"""
solectrac-inject-f108.py — Spoof BMS F108 fault-bitmap frames onto the
bus to probe the dashboard error-code mapping.

Background
----------
The real BMS broadcasts F108 (arbitration ID 0x18F108F3) at ~10 Hz.
Byte 7 of the payload is the dashboard-displayed warning bitmap and
its bit-to-code mapping is already known (see NOTES.txt → "F108F3"
section). Bytes 0..6 carry additional fault info whose mapping is
open: bms-fullcharge-102-109-140.asc shows byte 0 = 0x10 and byte 2
= 0x04 set with codes 102, 109, 140 cycling on the dashboard, but
the per-bit identities are unknown.

This script transmits chosen F108 payloads at a higher rate (default
50 Hz, 5x the real BMS) so the dashboard's most-recent-wins behaviour
surfaces our value. Set one bit at a time, read the code off the
screen, walk the 56 unmapped bits.

Validating the race is being won
--------------------------------
Before probing unknown bits, send an all-zero payload:

    solectrac-inject-f108.py ... --bytes 00:00:00:00:00:00:00:00

Code 124 (Clock fault, byte 7 bit 0) is normally always on. If the
dashboard drops it while you're injecting zeros, the spoof is
winning. If 124 stays lit, raise --rate or pull the BMS CAN
connector and try again.

Modes
-----
--bytes XX:XX:XX:XX:XX:XX:XX:XX    explicit 8-byte payload
--bit BYTE,BIT                     set exactly one bit, rest zero
--sweep                            walk every bit in --sweep-bytes,
                                   holding each for --hold-s seconds
                                   and (unless --no-prompt) recording
                                   the displayed code

Examples
--------
    # Sanity check: should make code 124 disappear while running.
    solectrac-inject-f108.py --interface socketcan --channel can0 \\
        --bitrate 250000 --bytes 00:00:00:00:00:00:00:00

    # Walk bytes 0..6 bit by bit, 8s per bit, log to CSV.
    solectrac-inject-f108.py --interface socketcan --channel can0 \\
        --bitrate 250000 --sweep --hold-s 8 --out f108-mapping.csv

WARNING: this transmits onto a live vehicle bus. Use with the tractor
parked, neutral, parking brake set. Don't drive while injecting.
"""

import argparse
import csv
import sys
import time
from typing import List, Optional, Tuple

try:
    import can
except ImportError:
    print("python-can is required: pip install python-can", file=sys.stderr)
    sys.exit(1)


F108_ARB_ID = 0x18F108F3   # priority 6, PGN 0xF108, SA 0xF3 (BMS)


def parse_payload(s: str) -> bytes:
    parts = [p for p in s.replace(",", ":").replace(" ", ":").split(":") if p]
    if len(parts) != 8:
        raise argparse.ArgumentTypeError(
            f"--bytes needs 8 hex bytes, got {len(parts)}: {s!r}")
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"--bytes parse error: {e}")


def parse_bit(s: str) -> Tuple[int, int]:
    try:
        b_str, n_str = s.split(",")
        byte_idx, bit_idx = int(b_str), int(n_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--bit needs BYTE,BIT (e.g. '0,3'); got {s!r}")
    if not 0 <= byte_idx <= 7:
        raise argparse.ArgumentTypeError(
            f"--bit byte must be 0..7, got {byte_idx}")
    if not 0 <= bit_idx <= 7:
        raise argparse.ArgumentTypeError(
            f"--bit bit must be 0..7, got {bit_idx}")
    return byte_idx, bit_idx


def parse_byte_list(s: str) -> List[int]:
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        v = int(part)
        if not 0 <= v <= 7:
            raise argparse.ArgumentTypeError(
                f"--sweep-bytes index out of range: {v}")
        out.append(v)
    if not out:
        raise argparse.ArgumentTypeError("--sweep-bytes is empty")
    return out


def single_bit_payload(byte_idx: int, bit_idx: int) -> bytes:
    p = bytearray(8)
    p[byte_idx] = 1 << bit_idx
    return bytes(p)


def transmit_for(bus: "can.BusABC", arb_id: int, payload: bytes,
                 duration_s: float, rate_hz: float) -> int:
    """Spam `payload` at `rate_hz` for `duration_s` (inf = until Ctrl-C)."""
    period = 1.0 / rate_hz
    msg = can.Message(arbitration_id=arb_id, data=payload,
                      is_extended_id=True)
    start = time.monotonic()
    deadline = start + duration_s
    next_send = start
    n = 0
    while time.monotonic() < deadline:
        bus.send(msg)
        n += 1
        next_send += period
        sleep = next_send - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_send = time.monotonic()
    return n


def main() -> int:
    p = argparse.ArgumentParser(
        description="Inject F108 fault-bitmap frames to probe dashboard "
                    "error-code mapping.")
    p.add_argument("--interface", required=True,
                   help="python-can interface (e.g. socketcan, slcan, pcan)")
    p.add_argument("--channel", required=True,
                   help="bus channel (e.g. can0)")
    p.add_argument("--bitrate", type=int, default=None,
                   help="bus bitrate (e.g. 250000)")
    p.add_argument("--id", dest="arb_id", type=lambda x: int(x, 0),
                   default=F108_ARB_ID,
                   help=f"arbitration ID (default 0x{F108_ARB_ID:08X})")
    p.add_argument("--rate", type=float, default=50.0,
                   help="transmission rate in Hz (default 50; real BMS ≈ 10)")
    p.add_argument("--hold-s", type=float, default=None,
                   help="seconds per step. Default: 8.0 with --sweep, "
                        "run until Ctrl-C otherwise.")

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bytes", dest="bytes_payload", type=parse_payload,
                      help="explicit 8-byte payload, e.g. "
                           "'00:00:00:00:00:00:00:01'")
    mode.add_argument("--bit", type=parse_bit,
                      help="set BYTE,BIT (e.g. '0,3'); rest zero")
    mode.add_argument("--sweep", action="store_true",
                      help="walk each bit in --sweep-bytes one at a time")

    p.add_argument("--sweep-bytes", type=parse_byte_list,
                   default=[0, 1, 2, 3, 4, 5, 6],
                   help="comma-separated byte indices to sweep "
                        "(default 0,1,2,3,4,5,6; byte 7 already mapped)")
    p.add_argument("--out",
                   help="CSV path to write sweep results "
                        "(step, payload, observed_code)")
    p.add_argument("--no-prompt", action="store_true",
                   help="don't prompt for the observed code between steps")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan without opening the bus")

    args = p.parse_args()

    steps: List[Tuple[str, bytes]]
    if args.bytes_payload is not None:
        steps = [("explicit", args.bytes_payload)]
    elif args.bit is not None:
        b_idx, n_idx = args.bit
        steps = [(f"byte{b_idx}.bit{n_idx}", single_bit_payload(b_idx, n_idx))]
    else:
        steps = [(f"byte{b}.bit{n}", single_bit_payload(b, n))
                 for b in args.sweep_bytes for n in range(8)]

    if args.hold_s is None:
        args.hold_s = 8.0 if args.sweep else float("inf")

    hold_str = "∞ (Ctrl-C to stop)" if args.hold_s == float("inf") \
        else f"{args.hold_s:.1f} s"
    print(f"target ID  : 0x{args.arb_id:08X}")
    print(f"rate       : {args.rate:.1f} Hz "
          f"(period {1000/args.rate:.1f} ms)")
    print(f"steps      : {len(steps)} × {hold_str}")
    if args.dry_run:
        for label, payload in steps:
            print(f"  {label}: {payload.hex(' ').upper()}")
        return 0

    kwargs = {}
    if args.bitrate is not None:
        kwargs["bitrate"] = args.bitrate
    bus = can.Bus(interface=args.interface, channel=args.channel, **kwargs)

    results: List[Tuple[str, str, str]] = []
    interrupted = False
    try:
        for i, (label, payload) in enumerate(steps, 1):
            hexp = payload.hex(" ").upper()
            print(f"[{i}/{len(steps)}] {label} -> {hexp}  ({hold_str})",
                  flush=True)
            sent = transmit_for(bus, args.arb_id, payload,
                                args.hold_s, args.rate)
            print(f"          sent {sent} frames", flush=True)
            if args.no_prompt or args.hold_s == float("inf"):
                continue
            try:
                obs = input("          observed code "
                            "(blank = none, q = quit): ").strip()
            except EOFError:
                obs = ""
            if obs.lower() == "q":
                break
            results.append((label, hexp, obs))
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted; stopping injection.", file=sys.stderr)
    finally:
        bus.shutdown()

    if args.out and results:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["step", "payload", "observed_code"])
            w.writerows(results)
        print(f"wrote {len(results)} rows to {args.out}")

    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
