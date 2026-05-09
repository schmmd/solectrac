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
python3 solectrac-analyze.py file1.csv [file2.csv ...]
```

The script writes its outputs alongside the first input file, prints a
per-file summary table (one row per input file), and prints a decoded
catalog of every unique CAN ID it saw.

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

All output CSVs include a `file` column tagging which input each row came
from, so you can pivot, filter, or join across captures easily.

| File               | Schema                                                                                        |
|--------------------|-----------------------------------------------------------------------------------------------|
| `cells.csv`        | `file, timestamp, cell_index, voltage_v` — per-cell voltage samples in volts.                 |
| `temps.csv`        | `file, timestamp, temp_index, temp_c` — per-channel module temperature in °C.                 |
| `cell_summary.csv` | `file, timestamp, max_mv, min_mv, spread_mv, byte5, byte6_min_idx, flags, pack_v_estimate`.   |
| `pack_current.csv` | `file, timestamp, voltage_raw_b2b3, byte4_raw, current_a_estimate` — magnitude in A.          |
| `charger.csv`      | `file, timestamp, status, v_raw, voltage_v_estimate, i_raw, current_a` — charger telemetry.   |
| `vc_status.csv`    | `file, timestamp, state_raw, state_name` — vehicle-controller heartbeat byte.                 |
| `ids.csv`          | `id, ext, count, priority, R, DP, PF, PS, SA, PGN, PDU, PS_role, name` — one row per unique ID.|

Every output file is regenerated from scratch on each run.

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
decoder produces rows in the corresponding output CSV. The list of
recognized PGNs and their source addresses lives in constants near the top
of `solectrac-analyze.py`:

* Cell-voltage PGN window: `PGN_CELL_FIRST` … `PGN_CELL_LAST`.
* Module-temperature PGN window: `PGN_TEMP_FIRST` … `PGN_TEMP_LAST`.
* BMS pack-status / cell-summary PGNs: `PGN_F100`, `PGN_F102`.
* Charger telemetry PGN: `PGN_FF50`.
* Source-address constants: `SRC_BMS`, `SRC_CHARGER`, `SRC_VEHICLE`.

All numerical scalings (mV/bit, A/bit, V/bit, °C offset) are also defined
as named constants so they can be revised in one place when a definitive
spec becomes available. Scalings that have **not** been confirmed against
vendor documentation are commented as "tentative" in the source.

## Extending

To add a new decoder:

1. Add any new PGN / source-address constants near the top of the file.
2. Add a new sink key to `OUTPUT_SCHEMAS` and `OUTPUT_FILES`.
3. Add a branch inside `decode_file()` that recognizes the frame and
   appends to the new sink.
4. Optionally add a human-readable name for the PGN to `PGN_NAMES` so it
   surfaces in `ids.csv`.

The `summarize()` and `write_outputs()` helpers handle the new sink
automatically as long as it's listed in `OUTPUT_SCHEMAS` and `OUTPUT_FILES`.

## License / disclaimer

This is a personal reverse-engineering exercise based on observed CAN
traffic, not on vendor documentation. PGN meanings beyond the SAE-standard
DM1 frame are inferred from data and may be wrong. Do not use this script
for any safety-relevant decision-making.
