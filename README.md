# solectrac

Reverse-engineered J1939 CAN-bus tooling for a Solectrac electric tractor:

* `solectrac-analyze.py` — offline batch decoder. Reads CAN logs and writes
  long-format CSVs suitable for spreadsheets, pandas, or plotting.
* `solectrac-stream.py` — live TUI dashboard. Decodes the same frames in
  real time or replayed log file.

## solectrac-analyze.py

```sh
python3 solectrac-analyze.py [-o OUTDIR] file1.asc [file2.blf ...]
```

Inputs are read via `python-can`'s `LogReader`, so any format `python-can`
understands works: `.asc` (Vector ASCII), `.blf`, `.log` (canutils), `.trc`,
and python-can's own `.csv` format.

Outputs are written to the current working directory by default, or to
`OUTDIR` if `-o` / `--output-dir` is given (created if it doesn't exist).
A per-file summary table is printed to stdout.

### Output files

Four CSVs, all regenerated from scratch on each run. Together they let you
trace any decoded value back to its source bytes and the formula that
produced it:

* `signals.csv` — what we decoded (one row per scalar)
* `frames.csv` — what was on the bus (one row per consumed frame, joined to
  signals via `frame_index`)
* `decoders.csv` — how we decoded it (per-signal formula catalog)
* `ids.csv` — every unique CAN ID seen, with J1939 breakdown (metadata, not a
  timeseries, so it has its own schema)

To re-derive a value by hand: pick a row from `signals.csv`, look up its
`frame_index` in `frames.csv` to get the raw bytes, then look up its
`signal` in `decoders.csv` for the formula.

#### `signals.csv` — tidy long-format measurements

```
file, timestamp, frame_index, signal, value, unit
```

In [tidy / long format][tidy]. Pivot to wide from any consumer:

```python
import pandas as pd
df = pd.read_csv("signals.csv")
wide = df.pivot_table(index="timestamp", columns="signal", values="value")
```

Signal names use a `domain.name` (or `domain.NN.name`) convention —
`cell.NN.voltage_v`, `pack.voltage_v`, `motor.rpm_signed`, `bms.fault.code_NNN`,
`dm1.dtc.spn`, etc. The complete list with formulas, byte positions, and
confidence levels lives in `decoders.csv`; the module docstring at the top of
`solectrac-analyze.py` documents each signal in detail.

[tidy]: https://vita.had.co.nz/papers/tidy-data.pdf

#### `frames.csv` — raw frame log

```
frame_index, file, timestamp, can_id, pgn, source, len, b0, b1, b2, b3, b4, b5, b6, b7
```

`can_id` is 8-hex (e.g. `18F100F3`), `pgn` is 4-hex, `source` is 2-hex,
each `bN` is the data byte at position N as 2-hex. One row per frame that
produced at least one signal.

#### `decoders.csv` — per-signal decode rule catalog

```
signal, pgn, source, bytes, formula, unit, confidence, notes
```

One row per signal name (parametric signals like `cell.NN.voltage_v` use
`NN` as a placeholder). `bytes` references positions within `frames.csv`'s
`b0..b7` columns. `confidence` is `verified`, `tentative`, or `unknown`.

#### `ids.csv` — per-unique-CAN-ID J1939 decode

```
id, ext, count, priority, R, DP, PF, PS, SA, PGN, PDU, PS_role, name
```

One row per distinct CAN ID seen, with the J1939 field breakdown described
[below](#j1939-id-reference).

## solectrac-stream.py

Live (or replayed) BMS / charger / motor dashboard. Decodes the same
J1939 frames as `solectrac-analyze.py`.

![solectrac-stream TUI](screenshot.svg)

```sh
# Live capture using slcan
solectrac-stream.py --interface slcan --channel /dev/cu.usbmodem101 --bitrate 250000

# Replay an existing capture
solectrac-stream.py --replay session.log
```

Displays pack voltage / current / DC and estimated AC power, SOC estimate
(NMC OCV curve, taken from the lowest cell), charger output, per-cell
voltages with min/max/spread, module temperatures, vehicle-controller
heartbeat, and live alerts (low/high cell, spread, temp, AC budget,
stale BMS).

Requires `python-can` and `rich` (`pip install -r requirements.txt`).

## Disclaimer

This is a personal reverse-engineering exercise based on observed CAN
traffic, not on vendor documentation. PGN meanings beyond the SAE-standard
DM1 frame are inferred from data and may be wrong.
