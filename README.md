# solectrac-analyze

A small, dependency-free Python 3 script that decodes J1939 CAN-bus logs
exported from a Solectrac electric tractor's diagnostic port and writes
tidy long-format CSVs suitable for spreadsheets, pandas, or plotting tools.

## Requirements

* Python 3 (standard library only — no `pip install` needed).
* CAN-bus log files exported as CSV in SavvyCAN / GVRET format
  (header `Time Stamp,ID,Extended,Dir,Bus,LEN,D1,D2,D3,D4,D5,D6,D7,D8`).

## Usage

```sh
python3 solectrac-analyze.py [-o OUTDIR] file1.csv [file2.csv ...]
```

The script writes its outputs into the current working directory by
default, or into `OUTDIR` if `-o` / `--output-dir` is given (the
directory is created if it doesn't exist). It prints a per-file summary
table (one row per input file) and a decoded catalog of every unique
CAN ID it saw.

## Input format

Each input row represents one CAN frame. Required columns:

| Column      | Meaning                                                   |
|-------------|-----------------------------------------------------------|
| `Time Stamp`| Logger timestamp (treated opaquely; not assumed monotonic).|
| `ID`        | CAN identifier as hex (11-bit or 29-bit).                  |
| `Extended`  | `true` for 29-bit IDs, `false` for 11-bit.                 |
| `Dir`       | `Tx` / `Rx` (informational only).                          |
| `Bus`       | Logger bus index (informational only).                     |
| `LEN`       | Payload length in bytes (0–8).                             |
| `D1`..`D8`  | Payload bytes as hex; missing or short fields treated as 0.|

## Output files

The script writes four CSVs alongside the first input. All are
regenerated from scratch on each run. Together they let you trace any
decoded value back to its source bytes and the formula that produced it:

* `signals.csv` — what we decoded (one row per scalar)
* `frames.csv` — what was on the bus (one row per consumed frame, joined
  to signals via `frame_index`)
* `decoders.csv` — how we decoded it (per-signal formula catalog)
* `ids.csv` — every unique CAN ID seen, with J1939 breakdown

### `signals.csv` — tidy long-format measurements

One row per scalar measurement, in [tidy / long format][tidy]:

```
file, timestamp, frame_index, signal, value, unit
```

`file` tags which input each row came from; `frame_index` joins to
`frames.csv`. Pivot to a wide table from any consumer:

```python
import pandas as pd
df = pd.read_csv("signals.csv")
wide = df.pivot_table(index="timestamp", columns="signal", values="value")
```

Signal names use a `domain.name` (or `domain.NN.name`) convention:

| Signal                        | Meaning                                              |
|-------------------------------|------------------------------------------------------|
| `cell.NN.voltage_v`           | Per-cell voltage in volts (NN = 0-based BMS index).  |
| `temp.NN.c`                   | Per-channel module temperature in °C (+40 removed).  |
| `pack.cell_max_mv`            | F102 max-cell voltage, mV.                           |
| `pack.cell_min_mv`            | F102 min-cell voltage, mV.                           |
| `pack.cell_spread_mv`         | F102 max - min, mV.                                  |
| `pack.byte5`                  | F102 byte 5 (raw).                                   |
| `pack.byte6_min_idx`          | F102 byte 6 (raw; appears to encode min-cell index). |
| `pack.flags`                  | F102 byte 8 (raw flag/status field).                 |
| `pack.v_estimate`             | 20 × mean(min, max) / 1000, V.                       |
| `pack.voltage_proxy_b2`       | F100 byte 2 (raw).                                   |
| `pack.current_raw`            | F100 bytes 3-4 BE (raw biased u16).                  |
| `pack.current_a`              | F100 signed pack current, A (+draw / -charge).       |
| `charger.status`              | FF50 byte 1.                                         |
| `charger.v_raw`               | FF50 bytes 2-3 LE (raw).                             |
| `charger.voltage_v`           | FF50 voltage estimate, V (1/3 V/bit, tentative).     |
| `charger.i_raw`               | FF50 bytes 4-5 LE (raw).                             |
| `charger.current_a`           | FF50 current, A.                                     |
| `vc.state`                    | F100D0 byte 0 (raw heartbeat state).                 |
| `motor.rpm_signed`            | FF21CA RPM with directional sign.                    |
| `motor.rpm_magnitude`         | FF21CA RPM unsigned.                                 |
| `motor.direction`             | +1 forward / 0 idle / -1 reverse.                    |
| `motor.throttle_raw`          | FF21CA byte 0 (raw throttle).                        |
| `motor.controller_temp_c`     | FF21CA byte 5 (only emitted when nonzero).           |

[tidy]: https://vita.had.co.nz/papers/tidy-data.pdf

### `frames.csv` — raw frame log

One row per frame that produced at least one signal:

```
frame_index, file, timestamp, can_id, pgn, source, len, b0, b1, b2, b3, b4, b5, b6, b7
```

`can_id` is 8-hex (e.g. `18F100F3`), `pgn` is 4-hex, `source` is 2-hex,
each `bN` is the data byte at position N as 2-hex. Join to
`signals.csv` on `frame_index` to see exactly which bytes any decoded
value came from.

### `decoders.csv` — per-signal decode rule catalog

```
signal, pgn, source, bytes, formula, unit, confidence, notes
```

One row per signal name (parametric signals like `cell.NN.voltage_v` use
`NN` as a placeholder). `bytes` references positions within `frames.csv`'s
`b0..b7` columns. `confidence` is `verified`, `tentative`, or `unknown`.

Together with `frames.csv`, this lets you re-derive any value by hand:

1. Pick a row from `signals.csv` (note its `frame_index` and `signal`).
2. Look up that frame's bytes in `frames.csv`.
3. Look up the signal's formula in `decoders.csv`.
4. Apply the formula to the bytes; the result should equal `value`.

### `ids.csv` — per-unique-CAN-ID J1939 decode

```
id, ext, count, priority, R, DP, PF, PS, SA, PGN, PDU, PS_role, name
```

Companion catalog (one row per distinct CAN ID seen) with the J1939
breakdown described below. This is metadata, not a timeseries, so it
keeps its own schema.

## J1939 ID reference

Each 29-bit J1939 identifier breaks down as:

| Bits   | Field                       | Notes                                                                 |
|--------|-----------------------------|-----------------------------------------------------------------------|
| 28..26 | Priority (P)                | 0 = highest, 7 = lowest. Priority 6 is typical for periodic broadcasts.|
| 25     | Reserved (R) / EDP          | Always 0 in classic J1939.                                            |
| 24     | Data Page (DP)              | Selects between page 0 (default) and page 1.                          |
| 23..16 | PDU Format (PF)             | PF < 0xF0 → PDU1 (destination-specific). PF ≥ 0xF0 → PDU2 (broadcast).|
| 15..8  | PDU Specific (PS)           | Destination Address (DA) for PDU1, or Group Extension (GE) for PDU2.  |
| 7..0   | Source Address (SA)         | The transmitter's J1939 address.                                      |

The Parameter Group Number (PGN) is reconstructed as:

* PDU1: `PGN = (DP << 16) | (PF << 8)` (DA is **not** part of the PGN).
* PDU2: `PGN = (DP << 16) | (PF << 8) | PS`.

The script's `decode_can_id()` helper performs this decoding for every unique
ID it sees, populating the `ids.csv` catalog.

## Decoders

The parser has decoders for a fixed set of PGN/source combinations. Each
decoder calls `emit(rows, scenario, ts, signal, value, unit)` once per
scalar it produces. The list of recognized PGNs and their source
addresses lives in constants near the top of `solectrac-analyze.py`:

* Cell-voltage PGN window: `PGN_CELL_FIRST` … `PGN_CELL_LAST`.
* Module-temperature PGN window: `PGN_TEMP_FIRST` … `PGN_TEMP_LAST`.
* BMS pack-status / cell-summary PGNs: `PGN_F100`, `PGN_F102`.
* Charger telemetry PGN: `PGN_FF50`.
* Motor controller PGN: `PGN_FF21`.
* Source-address constants: `SRC_BMS`, `SRC_CHARGER`, `SRC_VEHICLE`,
  `SRC_MOTOR`.

All numerical scalings (mV/bit, A/bit, V/bit, °C offset) are also defined
as named constants so they can be revised in one place when a definitive
spec becomes available. Scalings that have **not** been confirmed against
vendor documentation are commented as "tentative" in the source.

## Extending

To add a new decoder:

1. Add any new PGN / source-address constants near the top of the file.
2. Add a branch inside `decode_file()` that recognizes the frame and
   appends one or more `(signal, value, unit)` tuples to `emissions`.
   The surrounding loop commits a `frames.csv` row and the matching
   `signals.csv` rows together with a shared `frame_index`.
3. Add a row per new signal name to the `DECODERS` catalog so the
   formula and confidence land in `decoders.csv`.
4. Optionally add a human-readable name for the PGN to `PGN_NAMES` so it
   surfaces in `ids.csv`.
5. If you want the new signal in the stdout summary, add a
   `values_for(rows, scenario, "your.signal")` block to `summarize()`.

## License / disclaimer

This is a personal reverse-engineering exercise based on observed CAN
traffic, not on vendor documentation. PGN meanings beyond the SAE-standard
DM1 frame are inferred from data and may be wrong. Do not use this script
for any safety-relevant decision-making.
