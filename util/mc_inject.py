#!/usr/bin/env python3
"""
Inject a modified FF21CA frame (SA 0xCA, the MC) to probe how the cluster
renders MC error codes.

Sniffs the real MC's current FF21CA payload, then transmits frames with one
byte changed at high rate for a bounded duration. Optionally logs the bus to
an ASC file (same format as the existing project captures) so you have a
matched recording of what the cluster saw during the injection window.

Default test: byte 7 = 0x2F (= 47, HPD/Sequencing per the operator manual).

USAGE
    python mc_inject.py
        # byte 7 = 0x2F for 5 s at 250 Hz (3x the MC's real rate)

    python mc_inject.py --byte 7 --value 0x01
        # bit 0 — bitmap hypothesis, should show lowest code (12) if true

    python mc_inject.py --byte 2 --value 0x2F
        # probe byte 2 instead

    python mc_inject.py --log captures/mc-inject-byte7-0x2F.asc
        # also record the bus during injection

    python mc_inject.py --dry-run
        # sniff baseline only; do not transmit

SAFETY
    Park brake on, neutral, wheels chocked, no one near the machine.
    Best with E-stop pressed (contactor open) so the MC can't act on
    contradictory frames it reads back from its own PGN. Watch the
    dashboard during injection. Duration is capped at 30 s.
"""

import argparse
import sys
import threading
import time
from collections import Counter

import can

CHANNEL = "/dev/tty.usbmodem101"
BITRATE = 250_000
DEFAULT_TARGET_ID = 0x0CFF21CA  # FF21CA from SA 0xCA, priority 3 (as MC broadcasts it)
SNIFF_TIMEOUT_S = 3.0
MAX_DURATION_S = 30.0


def sniff_baseline(bus, target_id):
    deadline = time.monotonic() + SNIFF_TIMEOUT_S
    seen = Counter()
    while time.monotonic() < deadline:
        msg = bus.recv(timeout=0.1)
        if msg is None:
            continue
        seen[msg.arbitration_id] += 1
        if (
            msg.arbitration_id == target_id
            and msg.is_extended_id
            and msg.dlc >= 8
        ):
            return bytearray(msg.data), seen
    return None, seen


def parse_hex_bytes(s):
    cleaned = s.replace(",", " ").replace("0x", "").split()
    if len(cleaned) == 1 and len(cleaned[0]) == 16:
        cleaned = [cleaned[0][i : i + 2] for i in range(0, 16, 2)]
    if len(cleaned) != 8:
        raise argparse.ArgumentTypeError(
            f"--bytes must be 8 hex bytes (got {len(cleaned)})"
        )
    return bytes(int(b, 16) for b in cleaned)


def reader_loop(bus, stop_event, counter, writer):
    while not stop_event.is_set():
        msg = bus.recv(timeout=0.1)
        if msg is None:
            continue
        counter[msg.arbitration_id] += 1
        if writer is not None:
            writer.on_message_received(msg)


def parse_int(s):
    return int(s, 0)


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--target-id", type=parse_int, default=DEFAULT_TARGET_ID,
                   help="CAN ID to transmit (default 0x0CFF21CA = FF21CA from MC)")
    p.add_argument("--byte", type=int, default=7,
                   help="byte index to modify in baseline (0-7), default 7")
    p.add_argument("--value", type=parse_int, default=0x2F,
                   help="new value 0..255 (default 0x2F)")
    p.add_argument("--bytes", type=parse_hex_bytes, default=None,
                   help="full 8-byte payload as hex (skips baseline sniff). "
                        "e.g. '04 FF 2F 00 1F 01 FF FF'")
    p.add_argument("--rate", type=float, default=250.0,
                   help="injection rate Hz (default 250)")
    p.add_argument("--duration", type=float, default=5.0,
                   help="injection duration seconds (default 5, max 30)")
    p.add_argument("--log", type=str, default=None,
                   help="optional ASC log path")
    p.add_argument("--dry-run", action="store_true",
                   help="sniff and print baseline, do not inject")
    args = p.parse_args()

    if not 0 <= args.byte <= 7:
        sys.exit("byte must be in 0..7")
    if not 0 <= args.value <= 0xFF:
        sys.exit("value must be in 0..255")
    if args.duration > MAX_DURATION_S:
        sys.exit(f"--duration capped at {MAX_DURATION_S}s")

    target_id = args.target_id
    print(f"Opening slcan on {CHANNEL} at {BITRATE} bps...", file=sys.stderr)
    with can.Bus(interface="slcan", channel=CHANNEL, bitrate=BITRATE) as bus:
        if args.bytes is not None:
            data = bytearray(args.bytes)
            print(
                f"  target  : {target_id:08X}",
                f"  payload : {data.hex(' ')} (explicit, no baseline sniff)",
                sep="\n",
                file=sys.stderr,
            )
        else:
            print(
                f"Sniffing for {target_id:08X} (timeout {SNIFF_TIMEOUT_S}s)...",
                file=sys.stderr,
            )
            baseline, seen = sniff_baseline(bus, target_id)
            if baseline is None:
                print(
                    f"No {target_id:08X} in {SNIFF_TIMEOUT_S}s. "
                    f"Saw {sum(seen.values())} frames across {len(seen)} IDs:",
                    file=sys.stderr,
                )
                for arb_id, n in sorted(seen.items()):
                    print(f"  {arb_id:08X}  {n}", file=sys.stderr)
                if not seen:
                    print(
                        "  (bus silent — adapter, port, bitrate, or another "
                        "process holds the device)",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"  (bus is alive but {target_id:08X} is silent — "
                        "either it's never broadcast, or the source is in a "
                        "state that suppresses it. Use --bytes to inject anyway.)",
                        file=sys.stderr,
                    )
                sys.exit(1)

            print(f"  baseline: {baseline.hex(' ')}", file=sys.stderr)
            data = bytearray(baseline)
            data[args.byte] = args.value
            print(
                f"  modified: {data.hex(' ')}  "
                f"(byte {args.byte} = 0x{args.value:02X} = {args.value})",
                file=sys.stderr,
            )

        if args.dry_run:
            print("Dry run — not injecting.", file=sys.stderr)
            return

        writer = can.ASCWriter(args.log) if args.log else None
        stop = threading.Event()
        counter = Counter()
        rd = threading.Thread(
            target=reader_loop, args=(bus, stop, counter, writer), daemon=True
        )
        rd.start()

        msg = can.Message(
            arbitration_id=target_id, data=bytes(data), is_extended_id=True
        )
        period = 1.0 / args.rate
        sent = 0
        end = time.monotonic() + args.duration

        print(
            f"Injecting at ~{args.rate:.0f} Hz for {args.duration}s. "
            "Watch the dashboard. Ctrl+C to stop.",
            file=sys.stderr,
        )
        try:
            next_t = time.monotonic()
            while time.monotonic() < end:
                bus.send(msg)
                sent += 1
                next_t += period
                slack = next_t - time.monotonic()
                if slack > 0:
                    time.sleep(slack)
        except KeyboardInterrupt:
            print("\nStopped by user.", file=sys.stderr)
        finally:
            stop.set()
            rd.join(timeout=1.0)
            if writer is not None:
                writer.stop()

        print(f"\nSent {sent} frames.", file=sys.stderr)
        if counter:
            print("RX summary during injection (top 15 IDs):", file=sys.stderr)
            for arb_id, n in counter.most_common(15):
                print(f"  {arb_id:08X}  {n}", file=sys.stderr)
        if args.log:
            print(f"Log written to {args.log}", file=sys.stderr)


if __name__ == "__main__":
    main()
