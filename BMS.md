# Solectrac BMS Diagnostic Port

Reference for the diagnostic CAN port on the UDAN BMS shipped in the
Solectrac e25 tractor (India 72V 300Ah variant). Covers wire protocol,
session lifecycle, and the Data Identifier (DID) map. Reverse-engineering
provenance and unresolved investigations are in the Appendix.

Confidence markers used throughout: **CONFIRMED**, **TENTATIVE**, **UNKNOWN**.

---

## Pack identity

| Field                              | Value                                 |
|------------------------------------|---------------------------------------|
| Variant                            | India 72V 300Ah, original (`印度系列72V300Ah原版`) |
| UI / firmware project              | `C121.082.001.01`                     |
| Hardware build string (DID 0xA50F) | `A650_C121.074.001.01_T1.0.2`         |
| Firmware version (DID 0xF195)      | `3.0.4.4`                             |
| BMS family                         | UDS-capable (P700 / U600 / X700 class) |
| MCU (per UDAN symbol table)        | NXP S32K142 / S32K314 (ARM Cortex-M)  |
| Flash                              | GD25Q64 (SPI NOR, 64 Mbit) + W25N01G (NAND, 1 Gbit) |

The `C121.*` project numbers diverge between UI (`C121.082`) and hardware
string (`C121.074`); TENTATIVE: firmware-project vs hardware-revision tag.

## Pack structure (CONFIRMED)

- Chemistry: NCM
- Configuration: 20S × 1 subsystem
- Rated capacity: 300 Ah; rated current: 500 A
- Nominal pack voltage: 72 V (≈ 78.5 V at high SOC)
- Temperature probes: 7 per subsystem
- HV rails: B+, HV1 (Main+), HV2, HV3 active; HV4 / HV5 not used
- Contactors: HSS1 (Main+), HSS2–HSS5, LSS1 (only HSS1 closed when idle)
- "Calibrating" Running mode — TENTATIVE: this BMS's normal idle state,
  not a special diagnostic mode

---

## Wire protocol

### CAN parameters

| Direction               | 11-bit ID | Notes                                |
|-------------------------|-----------|--------------------------------------|
| Tester → BMS (UDS req)  | `0x740`   | Only request ID this BMS responds to |
| BMS → Tester (UDS resp) | `0x748`   |                                      |

ISO-TP (ISO 15765-2) over CAN, 11-bit standard IDs. Bitrate: not measured
directly, but the bus is shared with the OBD-II side at 250 kbit/s
(see `DOCUMENTATION.md`).

### UDS services in use

| SID    | Service                 | Observed use                                  |
|--------|-------------------------|-----------------------------------------------|
| `0x10` | DiagnosticSessionControl | Enter extended session (`10 03`) before unlock |
| `0x22` | ReadDataByIdentifier    | All live-data and identity reads              |
| `0x27` | SecurityAccess          | Level 1 unlock (`27 01` seed, `27 02` key)    |
| `0x31` | RoutineControl          | Six `0xF009`–`0xF011` routines, trigger UNKNOWN |
| `0x34` / `0x36` / `0x37` | RequestDownload / TransferData / TransferExit | Bootstrap 1528 B write to `0x00003A00`, purpose UNKNOWN |

DID notation: `0xXXXX` everywhere. Wire format is the standard UDS form:

```
Request:  03 22 02 09          PCI=3, SID=22, DID=0x0209
Response: 05 62 02 09 00 00    PCI=5, SID|0x40=62, DID echoed, data
```

Responses longer than 7 bytes use ISO-TP first/consecutive framing.

### Session lifecycle

Default session is read-only for live data. Extended session + SecAccess L1
are required before any write or routine call is honored.

```
1.  02 10 03                                 → 06 50 03 00 32 00 C8 00    DSC: enter extended session (P2 = 50 ms, P2* = 200 ms)
2.  02 27 01                                 → 06 67 01 <4-byte seed>     SecAccess L1: request seed
3.  06 27 02 <4-byte key>                    → 02 67 02                   SecAccess L1: send key
```

The L1 key algorithm is custom (Go source `uds_udan_key_calculator_YJC.go`
inside `app/iBMSUpper.exe`). Cryptanalysis on 9 captured pairs has ruled
out all simple/linear forms — see "SecAccess L1" in the reverse-engineering notes appendix.

### Connection bootstrap

The iBMS PC tool performs a fixed 11-step sequence on every connection
(elapsed ~0.5 s, before any UI polling starts):

| Step | Request                              | Purpose                                       |
|------|--------------------------------------|-----------------------------------------------|
| 1    | `22 A5 00`                           | Discovery probe (1-byte response, `01`)       |
| 2    | `22 01 06`                           | UNKNOWN (2-byte response)                     |
| 3    | `22 F1 95`                           | Firmware version (one-shot only)              |
| 4    | `22 A5 0F`                           | Hardware/build string                         |
| 5    | `22 28 00`                           | System state snapshot                         |
| 6    | `10 03`                              | Enter extended session                        |
| 7    | `27 01` / `27 02`                    | SecAccess L1 unlock                           |
| 8    | `34 00 24 00 00 3A 00 05 F8`         | RequestDownload: 1528 B to `0x00003A00`       |
| 9    | `36 01` / `36 02` / `36 03`          | TransferData (3 × ~516 B)                     |
| 10   | `37`                                 | TransferExit                                  |
| 11   | `22 A5 03`, `22 A5 05`, `22 A5 0D`   | Additional identity / status reads            |

Steps 8–10 are the open mystery — see "Bootstrap RequestDownload" in the reverse-engineering notes appendix.

---

## Data Identifiers

Organized by data category. UDAN message IDs (`0xNN`) refer to the
iBMS-internal symbol table that names CSV exports; see "Message-ID symbol
table" in the iBMS-software-notes appendix.

### Identity (one-shot, or polled in baseline)

| DID    | Type             | Sample value                              | Confidence |
|--------|------------------|-------------------------------------------|------------|
| `0xF195` | ASCII string   | `"3.0.4.4"` (FW version)                  | CONFIRMED  |
| `0xA50F` | ASCII string   | `"A650_C121.074.001.01_T1.0.2"`           | CONFIRMED  |
| `0xA500` | 1-byte flag    | `0x01` (discovery / liveness)             | TENTATIVE  |
| `0xA503`, `0xA505`, `0xA50D` | varying (4–19 B) | Identity / status block | UNKNOWN |

### Per-cell live data

| DID    | UDAN tag           | Format                                       | Confidence |
|--------|--------------------|----------------------------------------------|------------|
| `0x0101` | `0x08` Voltages  | 20 × BE u16, mV                              | CONFIRMED  |
| `0x0102` | `0x09` Temperatures | 7 × u8, `°C = raw − 40` (offset TENTATIVE) | CONFIRMED  |

Sample `0x0101` payload (76.8 % SOC, idle):
`3925 3925 3926 3926 3925 3924 3928 3927 3925 3925 3925 3923 3928 3926 3929 3929 3925 3927 3930 3929` mV.

Sample `0x0102` payload: `41 41 41 41 40 41 41` → ~22 °C across 7 probes.

### Pack-level state — DID `0x2800` (UDAN `0x93`)

12 data bytes. Six BE uint16 fields; three identified by live-UI match:

| Offset | BE u16   | Field                          | Sample          |
|--------|----------|--------------------------------|-----------------|
| 0      | `0x0312` | Real SOC × 10                  | 78.6 %          |
| 2      | `0x03E8` | SOH × 10                       | 100.0 %         |
| 4      | `0x0311` | HV1 / Pack voltage × 10        | 78.5 V          |
| 6      | `0xFFED` | TENTATIVE: signed pack current | ≈ −0.2 A (idle) |
| 8      | small    | UNKNOWN counter / flag         | 5–7             |
| 10     | varies   | UNKNOWN                        | ~`0x33xx`       |

### Peak data — DID cluster `0x2820`/`0x2828`/`0x2830`/`0x2838` (UDAN `0x06`)

Each DID carries the top-4 extremes for one quantity. The iBMS UI and CSV
export only the #1 entry per column; the BMS internally tracks four.

| DID      | Tuple format                                                       | Sorted | Quantity                |
|----------|--------------------------------------------------------------------|--------|-------------------------|
| `0x2820` | 4 × (u16 BE voltage_mV, u8 subsys_0based, u8 cell_idx_0based)      | DESC   | Top-4 **max** cell V    |
| `0x2828` | same                                                               | ASC    | Top-4 **min** cell V    |
| `0x2830` | 4 × (u8 temp_raw, u8 subsys_0based, u8 probe_idx_0based)           | DESC   | Top-4 **max** probe T   |
| `0x2838` | same                                                               | ASC    | Top-4 **min** probe T   |

Cross-checks against `0x0101`: top-1 entries match the cell-array max
(3930 mV at cell 18) and min (3923 mV at cell 11) exactly. Subsys byte is
0-based internally (CSV reports 1-based).

### Counters

#### DID `0x2801` — (Dis)charged time (UDAN `0x95`)

16 data bytes = 4 × BE uint32, all in seconds:

| Offset | Sample      | Field                                                      |
|--------|-------------|------------------------------------------------------------|
| 0      | 832,857,443 | TENTATIVE: lifetime counter (ms or epoch-like); ticks 1/s  |
| 4      | 1,329       | Session uptime; zero at boot, ticks 1/s                    |
| 8      | 3,873,795   | Accumulated charge time (1076 h)                           |
| 12     | 576,772     | Accumulated discharge / usage time (160 h)                 |

**Heartbeat byte:** byte 3 of the payload (low byte of the offset-0 u32)
increments by 1 every ~1 s — this is the byte exported as the `Heartbeat`
column in the iBMS System-state CSV.

#### DID `0x2810` — (Dis)charged energy (UDAN `0x89`)

20 data bytes:

| Offset | Width | Field                                                |
|--------|-------|------------------------------------------------------|
| 0–1    | u16   | Cell count (= 20)                                    |
| 2–3    | u16   | Cycle count (= 7)                                    |
| 4–7    | u32   | UNKNOWN (TENTATIVE: avg cell × scale)                |
| 8–11   | u32   | UNKNOWN (instantaneous quantity)                     |
| 12–15  | u32   | Accumulated charge capacity × 0.01 Ah                |
| 16–19  | u32   | Accumulated discharge capacity × 0.01 Ah             |

### Alarms — DID `0x4000` (UDAN `0x87`)

31 data bytes, one severity-level enum per byte:

- `0x00` — No Fault
- `0x01` / `0x02` / `0x03` — TENTATIVE: Lvl 1 / 2 / 3 Alarm
- `0xFF` — fault category not implemented on this BMS variant

Constant idle payload (10 sentinels at fixed positions `{11, 12, 21, 24–30}`):

```
00 00 00 00 00 00 00 00 00 00 00 ff ff 00 00 00 00 00 00 00 00 ff 00 00 ff ff ff ff ff ff ff
```

The CSV export has ~73 fault columns; only ~21 are wired on this pack.
Per-byte-to-column mapping needs an active fault to pin down.

### Charging — DID cluster `0x0900` + `0x0901` + `0x0902` (UDAN `0x94`, TENTATIVE)

Three DIDs polled in parallel during the Charge-info / BMS tab. Combined
35 data bytes covers the 16 non-time CSV columns. Field layout is
TENTATIVE — every observation so far has the charger disconnected.

| DID      | Data (B) | Sample                                          | Interpretation                                  |
|----------|----------|-------------------------------------------------|-------------------------------------------------|
| `0x0900` | 7        | `01 00 01 00 00 00 00`                          | Enum/flag block (Charger conn., S2, Lock state) |
| `0x0901` | 14       | `33 0c 00 00 33 0c 00 00 00 00 ff ff ff ff`     | Measurements; trailing `FF FF FF FF` = CC Res + CC2 Res sentinels |
| `0x0902` | 14       | `00 00 00 00 00 00 00 00 00 00 00 00 00 00`     | All-zero in idle; likely fault / state machine  |

### X700 IoT subsystem — DIDs `0xA501`, `0xA502`, `0xA506`, `0xA507`, `0xA50E`

The BMS contains a built-in cellular telemetry subsystem ("X700"). Visible
in the iBMS UI but unprovisioned on the shipped Solectrac unit. Schema
exposed (all fields empty in observed unit):

```
HWID, FWVersion, HWVersion, DeviceName, Host, Port,
APN UserName, APN Password, MQTT UserName, MQTT Password
```

UDAN message IDs `0x98` (WiFi info) and `0x9D` (WiFi / DTU) likely cover
this data; explicit DID-to-field mapping UNKNOWN.

### Calibration tables — `0x30xx` / `0x40xx` (~80 DIDs)

Triggered by the SOC tab "Read" button: one-shot dump of ~80 DIDs in the
`0x3010`, `0x3030`–`0x3093`, `0x30A0`–`0x30E6`, `0x3140`–`0x3153`,
`0x4011`/`0x4012`, `0x4019`/`0x401A` ranges (plus `0x0E11`, `0x0E61`).
Almost all responses are 35 B (32 B data after the `62 XX XX` header).

Populate the iBMS Cap.config / SOC calib.config / HighSoc / LowSoc threshold
tables. Numerically-paired DIDs (e.g. `0x3030`/`0x3031`) TENTATIVE:
charge-vs-discharge or high-vs-low of the same parameter.

### `0x28xx` address-space map

The `0x28xx` range is segmented by purpose, not a single state block:

| DID                | Content                                          |
|--------------------|--------------------------------------------------|
| `0x2800`           | System state                                     |
| `0x2801`           | (Dis)charged time                                |
| `0x2810`           | (Dis)charged energy                              |
| `0x2820` / `0x2828`| Peak data — max-V / min-V                        |
| `0x2830` / `0x2838`| Peak data — max-T / min-T                        |
| `0x2832` / `0x283A`| Empty on this pack — TENTATIVE: subsystem-2 slots |
| `0x2850`           | UNKNOWN 2-byte block                             |
| `0x2803`, `0x2804` | Cell-level extremum / index (Cell info tab)      |

### DIDs observed but not yet identified

These are polled by the iBMS but not yet mapped to a known UDAN message:

| Range                                                  | Notes |
|--------------------------------------------------------|-------|
| `0x0100`, `0x0103`–`0x0105`                            | `0x0100` is constant config (thresholds); others mostly empty |
| `0x0200`–`0x020B` (mixed)                              | `0x0202` is a fixed cell-index table; `0x0205` is a probe-channel map |
| `0x0620`, `0x0621`, `0x0648`                           | Mostly-empty sub-block, UNKNOWN |
| `0x0641`–`0x0647`                                      | Per-channel 1-byte values (7 total), UNKNOWN |
| `0x0E00`                                               | HV detection / Hlss state — contains pack-V × 10 twice |
| `0x0E21`, `0x0F50`, `0x0F60`                           | UNKNOWN small values |
| `0x0E40`                                               | Shunt state (Hall current sensing) |
| `0x0E70`–`0x0E72`, `0x0EF0`, `0x0F10`, `0x0F30`        | Signal detection / on-board rails |
| `0x0EA0`, `0x0EA1`, `0x0ED0`–`0x0ED7`                  | Cell info tab — balancing / open-wire / short flags |
| `0x0960`, `0x0961`, `0x0905`, `0x0962`                 | UNKNOWN |
| `0x1600`, `0x1620`                                     | `0x1600` = BMU power-supply rail (~12.75 V); `0x1620` = on-board temps |

---

## Polling patterns

| Phase                         | Frequency | DIDs                                                                  |
|-------------------------------|-----------|-----------------------------------------------------------------------|
| Bootstrap (~0.5 s)            | one-shot  | See §"Connection bootstrap" steps 1–11                                |
| Baseline (continuous)         | ~1 Hz     | ~30 DIDs covering identity + per-cell + pack state + peak data + counters + alarms |
| Cell info tab (additive)      | ~1 Hz     | `0x0EAx`, `0x0EDx`, `0x2803`/`0x2804`, `0x096x`                       |
| BMS tab (additive)            | ~1 Hz     | `0x0900`–`0x0902`, `0x0E00`/`0x0E40`/`0x0E7x`/`0x0Exx`/`0x0Fxx`, `0x1600`/`0x1620`, `0xA50x` (X700), `0x064x` |
| SOC tab Read                  | one-shot  | ~80 calibration DIDs in `0x30xx` / `0x40xx`                           |
| Late-session routine burst    | ~1 Hz, transient | `31 01 F0 09`–`31 01 F0 11`, plus `0x0905`/`0x0962`/`0x064E`/`0x067x` — trigger UNKNOWN |

The session is kept open by continuous baseline polling; explicit `0x3E`
TesterPresent is referenced in the iBMS binary's symbol table but not
observed on the wire during baseline polling.

---

## Writes

Not yet observed on the wire. The iBMS UI exposes Sync / Import / Export /
Read / Write buttons on the SOC calibration tab; **Write** is gated by
SecAccess L1 unlock. Specific write services are TENTATIVE pending an
observed write transaction.

The internal protobuf message types in `app/iBMSUpper.exe`
(`MosForceControlMessage`, `WorkModeControlMessage`,
`ChgForceControl`/`Time`, `U600ElecLockForceContrl`, `U600HLSSForceContrl`)
indicate write/control surfaces are available; see "Protobuf message types"
in the iBMS-software-notes appendix.

---

# Appendix

## iBMS software notes

Facts about the UDAN iBMS PC Utility itself: what's in the binary, what
adapters it supports, what its UI looks like, what it does on connect,
and what file formats it produces. Reference material, not active
investigation.

### iBMS PC Utility — software provenance

The protocol map above was derived from a Solectrac-specific install of
the iBMS PC Utility (UDAN's vendor tool) plus traces taken while the tool
was running.

#### Installer

- File: `docs/iBMSUpper-setup-x86(v3.1.7).exe`
- Type: Inno Setup installer (PE32, 36 MB), Company: UDAN
- Product: iBMS PC Utility v3.1.6 (build 2020-03-14)

#### Application

- Language: Go 1.15.15 → Windows PE32, UPX-compressed
- UI: embedded web app, served on localhost via HTTP
  - JS bundles `static/js/app.e064497ec6be8a62ce23.js`, `vendor.ee7bb8e9289003d7cac7.js` + 9 numbered chunks
- Serialization: Protocol Buffers (protobuf) for internal types
- Connection types (Go iface `Connection`):
  - `ConnectionCan` — UDS/CanTp transport (Solectrac uses this)
  - `ConnectionUart` — Modbus over UART-over-CAN (F700 family — see "Sibling BMS family" below)
  - `ConnectionDemo` — simulation

#### Embedded Go source paths

```
D:/golang/gopath/src/iBMSUpper/uds_read_data.go
D:/golang/gopath/src/iBMSUpper/uds_read_data_A7.go
D:/golang/gopath/src/iBMSUpper/uds_read_data_dataflash_gd25q64.go
D:/golang/gopath/src/iBMSUpper/uds_read_data_dataflash_w25n01g.go
D:/golang/gopath/src/iBMSUpper/uds_save_data_P7.go
D:/golang/gopath/src/iBMSUpper/uds_save_data_U6.go
D:/golang/gopath/src/iBMSUpper/uds_udan_key_calculator_YJC.go
```

#### Supported CAN adapters (kerneldll.ini)

47 ZLG (周立功) adapters across USBCAN / CANDTU / CANWIFI / PCI families,
plus Peak PCAN (separate driver) and CAN232 serial. CANFD support via
`zpcfd_x86.dll` / `usbcanfd.dll`.

#### Message-ID symbol table

The iBMS binary names internal data records by byte ID. These appear in
CSV export filenames (e.g. `Voltages 0x08.csv`); they are *not* CAN
arbitration IDs.

| ID    | Name                            | Mapped to DID(s) |
|-------|---------------------------------|------------------|
| 0x06  | Peak data                       | `0x2820`/`0x2828`/`0x2830`/`0x2838` |
| 0x08  | Voltages                        | `0x0101`         |
| 0x09  | Temperatures                    | `0x0102`         |
| 0x0A  | Heat and Pole Temperatures      | not on this pack |
| 0x0B  | Heat Pole MOS Temperatures      | not on this pack |
| 0x79  | Balancing state                 | UNKNOWN          |
| 0x80  | Device list                     | —                |
| 0x81  | Device info                     | —                |
| 0x82  | Device list (alt)               | —                |
| 0x83  | System state                    | alt name for `0x93` |
| 0x84  | DTU info                        | —                |
| 0x85  | Charging                        | alt name for `0x94` |
| 0x86  | Balancing state                 | UNKNOWN          |
| 0x87  | Alarm state                     | `0x4000`         |
| 0x88  | (Dis)charged energy             | alt name for `0x89` |
| 0x89  | (Dis)charged energy (alt)       | `0x2810`         |
| 0x91  | List of supported commands      | —                |
| 0x92  | Device info (alt)               | —                |
| 0x93  | System state (alt)              | `0x2800`         |
| 0x94  | Charging (alt)                  | `0x0900`+`0x0901`+`0x0902` TENTATIVE |
| 0x95  | (Dis)charged time               | `0x2801`         |
| 0x96  | DTU info                        | —                |
| 0x97  | Enable/disable data             | UNKNOWN          |
| 0x98  | WiFi info                       | X700 subsystem (UNKNOWN DID) |
| 0x99  | Charging state / ChgState       | UNKNOWN          |
| 0x9A  | Voltages (alt)                  | UNKNOWN          |
| 0x9B  | Peak data (alt)                 | UNKNOWN          |
| 0x9D  | WiFi / DTU                      | X700 subsystem (UNKNOWN DID) |
| 0x9F  | System state                    | —                |
| 0xB6  | System state                    | —                |
| 0xBB  | DTU / "Enter programming session" | —              |
| 0xBE  | Temperature disabled data       | —                |
| 0xC0  | Host diagnostic data            | —                |

#### Protobuf message types

Found in the Go binary:

- `ChargeMessage` — charge request V/A, connect state, fault flags
- `DiagnosisMessage` — alarm count, diagnosis info
- `ExtremumMessage` — max/min cell V/T, SOC parameters
- `TotalPackageMessage` — wraps the above + Remote + CloudConfig
- `MosForceControlMessage` — force MOS index switch state
- `WorkModeControlMessage` — system lock/unlock, reset
- `RemoteControlMessage` — control type + cell-balance state envelope
- `CloudServiceConfigMessage` — cloud config

#### Remote / force-control surfaces

The Go binary exposes:

- `F700WriteForceContrl` (F700 only)
- `P700MOSForceContrl` — force MOS control
- `U600ElecLockForceContrl` — electric-lock control
- `U600HLSSForceContrl` — HLSS contactor control
- `ChgForceControl` / `ChgForceControlTime`

These imply UDS write or routine services exist on the BMS for
force-set, but the wire-level mapping is UNKNOWN until an observed write.

#### Product models listed in the binary

```
F700 / F702 / F715–F723 / F728–F753 / F780–F788
E700 / E720 / E721 / E730 / E750–E753
P700 (parallel BMS), U600, X700
```

### Sibling BMS family — F700 (Modbus over UART-over-CAN)

The iBMS tool also supports an entirely different protocol family used
by F700-class BMSs: **Modbus RTU framed over UART-over-CAN**, not UDS.
The Solectrac BMS is *not* F700 — it ignores the F700 probe — but this
is documented here because the iBMS tool always probes both families on
connect.

- Modbus client lib: `gitlab.udantech.com/wenjun.ye/go-modbus`
- CAN framing: `gitlab.udantech.com/xqp/can.(*RawClient)`, ≤ 8 B Modbus
  payload per CAN frame; transports over TCP to a CANDTU / CANWIFI adapter

#### F700 test mode (`F700TestModeSwitch`)

Writes Modbus holding register `0x0E11` using FC `0x10` (Write Multiple
Registers), slave address `0x01`:

| Direction      | Modbus RTU bytes                                  |
|----------------|---------------------------------------------------|
| Test mode ON   | `01 10 0E 11 00 01 02 00 01 8B 11`                |
| Test mode OFF  | `01 10 0E 11 00 01 02 00 00 4A D1`                |

Field breakdown: `[slave=01] [FC=10] [reg-hi=0E] [reg-lo=11] [qty-hi=00]
[qty-lo=01] [byte-count=02] [data-2-bytes] [CRC-lo] [CRC-hi]`

Split into CAN frames of ≤ 8 B each:

- Frame 1 (both): `01 10 0E 11 00 01 02 00`
- Frame 2 (ON):   `01 8B 11`
- Frame 2 (OFF):  `00 4A D1`

Related registers in the same template (purpose UNKNOWN): `0x0EDC`,
`0x0E13`. Related Go functions: `F700SwitchRunState` (reg `0x0E10`),
`F700SwitchProtocol`. Modbus slave address is configurable via
`ReinitSlaveAddress`. CAN arbitration ID is runtime-configured (not
hardcoded).

### Connection discovery sweep

The iBMS tool does not know in advance which protocol family or which CAN
ID the BMS uses. On connect, it broadcasts probe frames on both families,
sweeping multiple candidate IDs, until something responds. The cycle
repeats every ~2.7 s with no back-off.

#### UDS probe

Sent on each candidate UDS request ID. Observed sweep IDs: `0x740`,
`0x7D0`, `0x36E`.

| Field        | Value                          |
|--------------|--------------------------------|
| Frame bytes  | `03 22 A5 00 00 00 00 00`      |
| UDS request  | `0x22` ReadDID, DID `0xA500`   |

Solectrac BMS responds only on `0x740`/`0x748`.

A separate UDS-shaped frame `01 22 0F 1A 02 08 0F` is also seen on
`0x34E` each cycle. Purpose UNKNOWN.

#### F700 probe

Two CAN frames ~100 ms apart on the configured Modbus-over-CAN ID
(observed: `0x750`):

| # | Bytes                       | Meaning                                                   |
|---|-----------------------------|-----------------------------------------------------------|
| 1 | `AA AA 55 01`               | TENTATIVE: sync/handshake preamble (classic `AA AA 55`)   |
| 2 | `01 03 0B 36 00 0A 27 E7`   | Modbus FC `0x03` Read Holding Registers from `0x0B36`, qty 10, CRC `0x27E7` |

Solectrac BMS does not respond on `0x750`.

The probes contain no target addressing — the tool discriminates by
which ID + framing first gets an answer.

### iBMS UI tab structure

The iBMS PC Utility presents five top-level tabs (with sub-navigation
where present). Pairing tab transitions against trace polling-burst
boundaries was the primary technique for mapping DIDs to data — see
"DID-mapping methodology" in the reverse-engineering notes below.

| Top tab          | Right sub-nav                                                                                                                    | Driving DIDs              |
|------------------|----------------------------------------------------------------------------------------------------------------------------------|---------------------------|
| System overview  | —                                                                                                                                | Baseline DIDs only        |
| Cell info        | —                                                                                                                                | Baseline + `0x0EAx` / `0x0EDx` / `0x2803-4` / `0x096x` |
| Charge info      | —                                                                                                                                | Baseline + `0x09xx` cluster |
| BMS              | Hlss state · HV detection · Hall state · Shunt state · Signal detection · On-board volt · On-board temp · BMU info · X700        | Baseline + `0x09xx`/`0x0E*xx`/`0x0F*xx`/`0x16xx`/`0xA50x`/`0x064x` (all polled in parallel) |
| SOC              | Cap. config · SOC calib. config · HighSoc · LowSoc                                                                               | Baseline + one-shot `0x30xx` / `0x40xx` dump on "Read" |

The SOC tab also exposes Sync / Import / Export / Read / Write buttons.

### Historical CSV exports

The iBMS tool offers to pull historical state from the BMS's onboard
logger and save it as CSVs. Filename pattern: `<timestamp>_<UDAN-name
0xNN>.csv`. There is also an aggregate Excel file named after the pack
serial: `<pack-serial>_<timestamp>.xlsx`.

The UDAN message ID in the filename is the iBMS-internal record-type
tag (see "Message-ID symbol table" above), *not* a CAN ID. CSV row
timestamps come from the BMS RTC, which is not set — don't use these
timestamps to correlate against trace data.

CSV file inventory observed:

```
(Dis)charged energy 0x89.csv      — maps to DID 0x2810
(Dis)charged time 0x95.csv        — maps to DID 0x2801
Alarm state 0x87.csv              — maps to DID 0x4000
Charging 0x94.csv                 — maps to DID 0x0900+0x0901+0x0902 (TENTATIVE)
Peak data 0x06.csv                — maps to DID cluster 0x2820/0x2828/0x2830/0x2838
System state 0x93.csv             — maps to DID 0x2800
Temperatures 0x09.csv             — maps to DID 0x0102
Voltages 0x08.csv                 — maps to DID 0x0101
```

---

## Reverse-engineering notes

Active investigations: methodology, captured data, unresolved findings,
and open questions. Anything here is subject to change as more captures
arrive.

### DID-mapping methodology

DID-to-data assignments were derived by aligning trace polling-burst
boundaries against screenshots of the iBMS UI taken at known wall-clock
times. Procedure:

1. Note the active iBMS UI tab (and sub-nav) at each screenshot timestamp.
2. Segment the trace into windows where the set of polled DIDs is stable.
3. Intersect each window's DIDs with the "Driving DIDs" expected for the
   active tab (see "iBMS UI tab structure" above).
4. For each candidate DID, compare its response payload to the matching
   CSV's column count + scale and to live-UI values; promote to
   CONFIRMED if both check out.

CSV row *values* are not usable for correlation (BMS RTC is unset). CSV
*schema* (column names, widths, units) is the reliable cross-reference.

### SecAccess L1 — captured pairs and cryptanalysis

The Level-1 unlock key is computed by `uds_udan_key_calculator_YJC.go`
inside `app/iBMSUpper.exe`. Captured (seed, key) pairs:

| Seed (hex)    | Key (hex)     | Source capture          |
|---------------|---------------|-------------------------|
| `0D 4A F9 74` | `38 20 62 9F` | `bms-connection.asc`    |
| `66 80 20 47` | `92 0F 02 BA` | `bms-connection-2.asc`  |
| `9C 43 69 8E` | `9A 00 4F 4E` | `bms-connection-3.asc`  |
| `2A 4B 8D D2` | `3B 87 BE E1` | `bms-connection-4.asc`  |
| `2A 64 C4 19` | `16 37 31 4D` | `bms-connection-5.asc`  |
| `09 E6 16 7A` | `17 26 91 AD` | `bms-connection-6.asc`  |
| `9F 9C 21 C7` | `E1 42 86 06` | `bms-connection-7.asc`  |
| `DD F5 53 1B` | `13 1F 61 69` | `bms-connection-8.asc`  |
| `F8 FD 0C 44` | `B1 3E 9A 09` | earlier capture         |

Seeds are non-deterministic per session; each listed key was accepted
(positive `02 67 02` response).

**Ruled out** (via `util/crack_bms_seedkey.py` / `crack_bms_seedkey2.py`):

- `key = seed XOR C`, `seed ± C`, `seed · C mod 2³²` for any 32-bit `C`
- `key = ROL(seed, r) XOR C` for all 32 rotations + two-stage rotate/xor
- `key = bitrev(seed) XOR C`, `byteswap(seed) XOR C`, `~seed XOR C`
- Per-nibble S-box (contradicts in nibble 0)
- LFSR shift-and-XOR with common CRC polynomials (CRC32, CRC16-CCITT,
  CRC16-Modbus, etc.) over 8–64 rounds
- **Any GF(2)-linear function of the seed**: the differences
  `Δkᵢ = kᵢ⊕k₀` are not linear in `Δsᵢ = sᵢ⊕s₀`, so the algorithm
  contains a genuinely nonlinear step (carry-propagating add,
  multiplication, or LUT).

CONSEQUENCE: brute force on additional captured pairs alone is unlikely
to succeed. Practical next steps:

1. **Decompile `app/iBMSUpper.exe` directly** — a few hundred bytes of
   Go in a binary we already have. Cheapest path.
2. Search for a leaked/published UDAN seed-to-key routine (vendor UDAN,
   hardware string `A650_C121.*`).
3. Dump the BMS firmware (NXP S32K + GD25Q64 / W25N01G) and locate the
   `27 01` handler's verify routine.

### Bootstrap RequestDownload (UNKNOWN)

Every iBMS connection includes a 1528-byte write to BMS memory at
`0x00003A00` (3 × ~516 B `TransferData` blocks, then `TransferExit`),
inserted into the bootstrap sequence after the SecAccess unlock and
before any UI polling. The payload is too small to be firmware.
TENTATIVE hypotheses:

- A bootstrap / authentication blob the tool installs into RAM
- Part of the SecAccess L1 unlock dance (post-key challenge)
- A calibration lookup table re-uploaded each session

Resolution requires (a) decompiling the iBMS Go binary to find what
prepares this blob, or (b) comparing the blob bytes across multiple
sessions to see whether the payload is per-session or static.

### Late-session routine burst (UNKNOWN trigger)

~80 seconds into the navigation-tour capture, a parallel burst of
RoutineControl calls appears alongside additional DID reads at ~1 Hz:

| Request                                  | Response (B) |
|------------------------------------------|--------------|
| `31 01 F0 09`–`31 01 F0 11` (6 RIDs)     | 3–4 each     |
| `22 09 05`, `22 09 62`                   | 10 / 4       |
| `22 06 4E`, `22 06 70`, `22 06 71`       | 3 each       |

Not visible in any captured screenshot. TENTATIVE: triggered by a Sync /
Write button or one of the SOC tab inner sub-tabs (Total volt / Current /
Cell volt / Temp) that wasn't screenshotted.

### Open questions / TODO

- **Active-charge capture.** Connect the charger, capture a session, and:
  (a) confirm the `0x94 Charging` DID-cluster field layout,
  (b) test whether `0x4000` byte order matches the Alarm CSV column order,
  (c) catch the F194F3 broadcast PGN on the OBD-II side (predicted in
  `DOCUMENTATION.md`).
- **Active-fault capture or injection.** Needed to lock down per-byte
  semantics of `0x4000`.
- **Thermal-spread capture.** Needed to nail down the `0x0102`
  temperature offset (currently TENTATIVE `°C = raw − 40`).
- **SecAccess L1 algorithm.** Decompile `app/iBMSUpper.exe` — see
  "SecAccess L1" above.
- **Bootstrap RequestDownload payload.** See above.
- **Late-session routine burst trigger.** See above.
- **Remaining tentative payload fields:** `0x2800` offsets 6/8/10;
  `0x2810` offsets 4–11; `0x0EA0`/`0x0EA1` (balancing); `0x0ED0`–`0x0ED7`
  (open-wire / short flags); `0x09xx` BMS-tab block (Hlss / HV / Hall).
- **Unmapped UDAN tags** from the iBMSUpper symbol table:
  - `0x0A` Heat and Pole Temperatures, `0x0B` Heat Pole MOS Temperatures —
    likely **features absent** on the Solectrac pack (no heat-pole MOS
    observed).
  - `0x79`, `0x86` Balancing state — UNKNOWN, but **no balancing visible
    in the capture** (cell delta < 7 mV), so possibly inactive rather than
    not implemented.
  - `0x88` (Dis)charged energy, `0x9A` Voltages, `0x9B` Peak data —
    likely **alt names** for already-mapped data (`0x89` → `0x2810`,
    `0x08` → `0x0101`, `0x06` → `0x2820`/`0x2828`/`0x2830`/`0x2838`).
  - `0x97` Enable/disable data — UNKNOWN.
  - `0x99` Charging state / ChgState — UNKNOWN; possibly fed by the
    `0x09xx` cluster currently labeled TENTATIVE Charging.

  Net: most are probably duplicate names or absent features. Confirming
  this catalog-wide needs (a) an active-charge / active-fault / balancing
  capture, and (b) an attempt to read each unmapped UDAN tag's likely DID
  range to see what (if anything) responds.
- **F700 sibling family** (informational): which hardware variants use
  TestModeSwitch alt registers `0x0EDC` / `0x0E13`.
