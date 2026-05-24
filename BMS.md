# iBMS PC Utility (v3.1.7) — Reverse Engineering Notes

## Installer Overview

- File: `docs/iBMSUpper-setup-x86(v3.1.7).exe`
- Type: Inno Setup installer (PE32, 36 MB)
- Company: UDAN
- Product: iBMS PC Utility v3.1.6 (build date 2020-03-14)

## Installed Files

- `app/iBMSUpper.exe` — main application (28 MB, UPX-compressed, Go binary)
- `app/kerneldlls/kerneldll.ini` — CAN hardware adapter registry
- `app/vcredist_x86.exe` / `vcredist_x86_2008.exe` — Visual C++ redistributables
- `app/mfc80.dll`, `msvcp80.dll`, `msvcr80.dll` — MFC/CRT libraries

## CAN Hardware Support (kerneldll.ini)

The software supports 47 CAN interface adapters, all from ZLG (周立功 Guangzhou Zhiyuan Electronics).
Notable entries include:
- USBCAN (multiple variants: USBCAN_E, USBCAN_4E_U, USBCAN_8E_U, USBCAN_CX, USBCAN_GC)
- CANDTU / CANDTU_MINI / CANDTU_NET / CANDTU_NET_400
- CANWIFI_TCP / CANWIFI_UDP
- PCI cards: PCI9810, PCI9820, PCI9840, PCI51XX, PCI50XX, PCIE9221, PCIE9120, PCIE9110, PCIE9140
- PCAN (Peak CAN) — separate driver (PCANBasic_386.dll, ECanVci32.dll)
- CAN232 (serial adapter)
- zpcfd_x86.dll (CANFD support), usbcanfd.dll

## Application Architecture

- **Language**: Go 1.15.15 (compiled to Windows PE32)
- **UI**: Embedded web application (serves HTML/JS on localhost via HTTP)
  - Static files embedded in the binary (go-bindata or similar)
  - JS chunks: `static/js/app.e064497ec6be8a62ce23.js`, `vendor.ee7bb8e9289003d7cac7.js`, plus 9 numbered chunks
- **Protocol layer**:
  - **F700 series**: Modbus RTU over UART-over-CAN (source: `uart_over_can.go`).
    The Modbus client library is `gitlab.udantech.com/wenjun.ye/go-modbus`.
    CAN framing uses `gitlab.udantech.com/xqp/can.(*RawClient)` — raw CAN frames
    with up to 8 bytes of UART/Modbus payload per frame. The RawClient connects
    via TCP to a CAN-over-network adapter (CANDTU, CANWIFI, etc.).
  - **P700 / U600 / UDS models**: ISO 14229 UDS over CAN via ISO-TP
    (`gitlab.udantech.com/xqp/can.(*CanTp)`), custom transport called "YJC_UDS"
- **Serialization**: Protocol Buffers (protobuf) for internal message types
- **Connection types** (Go interface `Connection`):
  - `ConnectionCan` — CAN bus (UDS/CanTp, used for UDS-capable models)
  - `ConnectionUart` — UART-over-CAN with Modbus (used for F700)
  - `ConnectionDemo` — demo/simulation mode

## CAN Protocol — Message Types

The software uses byte-coded message IDs. Messages observed in symbol/string table:

| ID     | Name / Description |
|--------|--------------------|
| 0x06   | Peak data |
| 0x08   | Voltages |
| 0x09   | Temperatures |
| 0x0A   | Heat and Pole Temperatures |
| 0x0B   | Heat Pole MOS Temperatures |
| 0x79   | Balancing state |
| 0x80   | Device list |
| 0x81   | Device info |
| 0x82   | Device list (alt) |
| 0x83   | System state |
| 0x84   | DTU info |
| 0x85   | Charging |
| 0x86   | Balancing state |
| 0x87   | Alarm state |
| 0x88   | (Dis)charged energy |
| 0x89   | (Dis)charged energy (alt) |
| 0x91   | List of supported commands |
| 0x92   | Device info (alt) |
| 0x93   | System state (alt) |
| 0x94   | Charging (alt) |
| 0x95   | (Dis)charged time |
| 0x96   | DTU info |
| 0x97   | Enable/disable data |
| 0x98   | WiFi info |
| 0x99   | Charging state / ChgState |
| 0x9A   | Voltages (alt) |
| 0x9B   | Peak data (alt) |
| 0x9D   | WiFi / DTU |
| 0x9F   | System state |
| 0xB6   | System state |
| 0xBB   | DTU / "Enter programming session" |
| 0xBE   | Temperature disabled data |
| 0xC0   | Host diagnostic data |

## UDS Services Identified

The application implements ISO 14229 UDS over CAN (via a custom transport layer called "YJC_UDS"):

- **0x10** — Diagnostic Session Control
  - "Enter default session"
  - "Enter extended session"
  - "Enter programming session"  (associated with message 0xBB)
  - "Diagnostic Session Mode Control Service" (UI label)
- **0x22** — Read Data By Identifier. A **DID** (Data Identifier) is a 16-bit
  number naming a specific piece of data inside an ECU; each vendor defines
  its own map. Wire format over ISO-TP:

  ```
  Request:  03 22 02 09          PCI=3 bytes, SID=22, DID=0x0209
  Response: 05 62 02 09 00 00    PCI=5 bytes, SID|0x40=62, DID echoed, data
  ```

  Throughout this document, DIDs are written as `0xXXXX` (or as the request
  body `22 XX XX`); all UDAN-specific.
  - Application reads at minimum: 0x0106, 0xA500, 0xA50F, 0xF195
  - **0xF195** (CONFIRMED): ASCII firmware version. Solectrac response: `"3.0.4.4"`
  - **0xA50F** (CONFIRMED): ASCII hardware/build string. Solectrac response: `"A650_C121.074.001.01_T1.0.2"`
  - **0xA500** (TENTATIVE): used as the discovery "is anyone home?" probe. 1-byte response (`01` observed)
  - **0x0106** (UNKNOWN): 2-byte response (`A0 00` observed); meaning not yet determined
- **0x27** — Security Access ("Try to unlock to UdsSecurityLevel1")
  - Custom key calculator: `uds_udan_key_calculator_YJC.go`
- **0x31** — Routine Control ("Routine control service")
- **0x3E** — Tester Present ("TesterPresent" string)
- Read calibration information, read diagnostic information, data transfer services also present

Source files embedded in binary:
- `D:/golang/gopath/src/iBMSUpper/uds_read_data.go`
- `D:/golang/gopath/src/iBMSUpper/uds_read_data_A7.go`
- `D:/golang/gopath/src/iBMSUpper/uds_read_data_dataflash_gd25q64.go`
- `D:/golang/gopath/src/iBMSUpper/uds_read_data_dataflash_w25n01g.go`
- `D:/golang/gopath/src/iBMSUpper/uds_save_data_P7.go`
- `D:/golang/gopath/src/iBMSUpper/uds_save_data_U6.go`
- `D:/golang/gopath/src/iBMSUpper/uds_udan_key_calculator_YJC.go`

## Test Mode / F700TestModeSwitch

The function `F700TestModeSwitch` (Go method `main.(*DeviceData).F700TestModeSwitch`) is the key
function for entering test/diagnostic mode. Related functions:

- `F700SwitchRunState` — switches BMS run state (register 0x0E10)
- `F700SwitchProtocol` — switches communication protocol
- `F700TestModeSwitch` — **the test mode switch** (also has `-fm` and `.func1` variants)
- The state change is logged as a JSON diff: `{"Time":..., "OLD":..., "NEW":...}` with key `TestModeSwitch`

The "BMS request setting mode" string appears in the binary and is likely the UI label for this
feature. It is distinct from the programming session (0xBB).

### Confirmed Wire Frames

The F700 uses **Modbus RTU over UART-over-CAN** (not UDS). `F700TestModeSwitch` writes
Modbus holding register **0x0E11** using FC 0x10 (Write Multiple Registers), slave address 0x01.

The complete Modbus RTU frame (11 bytes, including CRC16-Modbus):

| Direction | Modbus RTU bytes (hex) |
|-----------|------------------------|
| Test mode ON  | `01 10 0E 11 00 01 02 00 01 8B 11` |
| Test mode OFF | `01 10 0E 11 00 01 02 00 00 4A D1` |

Field breakdown: `[slave=01] [FC=10] [reg-hi=0E] [reg-lo=11] [qty-hi=00] [qty-lo=01] [byte-count=02] [data] [CRC-lo] [CRC-hi]`

These bytes are split into CAN frames of up to 8 bytes each (UART-over-CAN framing):
- CAN frame 1 (both ON and OFF): `01 10 0E 11 00 01 02 00`
- CAN frame 2 (ON):  `01 8B 11`
- CAN frame 2 (OFF): `00 4A D1`

The CAN arbitration ID for these UART-over-CAN frames is **runtime-configured** (set when
connecting to the adapter); it is not hardcoded in the binary.

Additional registers referenced in the same template block (purpose/variant not confirmed):
- 0x0EDC — second entry in TestModeSwitch template
- 0x0E13 — third entry in TestModeSwitch template

## Connection Discovery Handshake

CONFIRMED: When initiating a connection, the iBMS tool broadcasts probe frames in parallel
on both supported protocols, sweeping multiple candidate CAN arbitration IDs to discover
what kind of BMS (and on what ID) is reachable. The cycle repeats every ~2.7 s with no
back-off if no responses are seen.

### UDS probe (P700/U600/UDS-capable models)

Sent on each candidate UDS request arbitration ID:

| Field        | Value                          |
|--------------|--------------------------------|
| Frame bytes  | `03 22 A5 00 00 00 00 00`      |
| ISO-TP PCI   | `03` (single frame, 3 payload) |
| UDS SID      | `0x22` ReadDataByIdentifier    |
| DID          | `0xA500`                       |

CONFIRMED observed request IDs used by the discovery sweep: `0x740`, `0x7D0`, `0x36E`.
(This partially resolves the previous TODO on UDS arbitration IDs — these are 11-bit
standard IDs, not 29-bit extended.)

DID `0xA500` is one of the four identifiers the iBMS tool is documented to read under
SID 0x22 (see "UDS Services Identified" above) — TENTATIVE interpretation: it is being
used here as a generic "is anyone home?" probe whose response identifies the device.

A second UDS-shaped frame has been observed on `0x34E` during the same cycle:
ISO-TP single frame of length 7, payload `01 22 0F 1A 02 08 0F`. Purpose UNKNOWN.

### F700 probe (Modbus RTU over UART-over-CAN)

Sent on the configured UART-over-CAN arbitration ID. CONFIRMED observed ID: `0x750`.
The probe is two CAN frames sent back-to-back (~100 ms apart):

| Frame | Bytes (hex)                  | Meaning                                                              |
|-------|------------------------------|----------------------------------------------------------------------|
| 1     | `AA AA 55 01` (4 bytes)      | TENTATIVE: sync/handshake preamble (`AA AA 55` is a classic preamble) |
| 2     | `01 03 0B 36 00 0A 27 E7`    | Modbus RTU: slave=0x01, FC=0x03 Read Holding Registers, addr=0x0B36, qty=10, CRC=0x27E7 |

Slave address 0x01 matches the documented default (see "Test Mode" above). The register
block at `0x0B36`–`0x0B3F` is presumably an identification / device-info block read to
confirm an F700 is present; exact contents UNKNOWN.

### Behavior summary

There is no target-specific addressing inside the probe payloads themselves — the tool
discriminates BMS family purely by which arbitration ID + protocol framing receives a
response. The same DID-0xA500 read is fired verbatim at every candidate UDS ID.

## Solectrac Pack — Observed Parameters

CONFIRMED. The Solectrac e25 pack identifies as **India series 72V 300Ah, original**
(UI project header: `C121.082.001.01`, Chinese label `印度系列72V300Ah原版`).

### Identity

| Field                             | Value                                              | Source                  |
|-----------------------------------|----------------------------------------------------|-------------------------|
| UI project number                 | `C121.082.001.01`                                  | UI header bar           |
| Hardware/build string (DID 0xA50F)| `A650_C121.074.001.01_T1.0.2`                      | UDS `22 A5 0F`          |
| Firmware version (DID 0xF195)     | `3.0.4.4`                                          | UDS `22 F1 95`          |
| BMS family                        | UDS-capable (P700 / U600 / X700)                   | Responds on 0x740, ignores F700 Modbus probe on 0x750 |

TENTATIVE on the two `C121.*` project numbers: the UI/firmware project
(`C121.082`) and the hardware string project (`C121.074`) share the `C121`
UDAN project prefix but differ in suffix; `C121.082` is likely the
firmware/UI project for this pack variant while `C121.074` is the hardware
revision identifier.

### Pack structure (CONFIRMED)

- Chemistry: NCM (Nickel Cobalt Manganese)
- Configuration: **20S × 1 subsystem** (`Cell count = 20`, `Subsys. count = 1`)
- Rated capacity: 300 Ah
- Rated current: 500 A
- Rated voltage: 72 V nominal; ~78.5 V measured at high SOC (20S × ~3.93 V)
- Temperature probes: 7 per subsystem
- HV rails: B+, HV1 (Main+), HV2, HV3 active (HV4 / HV5 marked Invalid)
- Contactors: HSS1 (Main+), HSS2–HSS5, LSS1 (only HSS1 closed during idle observation)

### CAN arbitration IDs (CONFIRMED for this pack)

| Direction               | ID    | Notes                                     |
|-------------------------|-------|-------------------------------------------|
| Tester → BMS (UDS req)  | 0x740 | Only UDS request ID that responds         |
| BMS → Tester (UDS resp) | 0x748 | Responses to 0x740 requests               |

The discovery sweep also probes 0x7D0, 0x36E, 0x34E (UDS) and 0x750 (F700
Modbus); none receive responses from this BMS.

### Unlock flow (CONFIRMED for this BMS)

The iBMS tool's session-open sequence on the Solectrac:

1. `02 10 03` — DiagnosticSessionControl, extended session
   → positive response `06 50 03 00 32 00 C8 00` (P2 / P2* timing parameters)
2. `02 27 01` — SecurityAccess Level 1, request seed
   → positive response `06 67 01 <4-byte seed>`
3. `06 27 02 <4-byte key>` — SecurityAccess Level 1, send key
   → positive response `02 67 02`

This partially resolves the prior TODO on whether UDS test mode uses 0x10
extended session or a 0x31 routine — at minimum the *unlock* uses extended
session + SecAccess L1, not a routine.

### Captured SecurityAccess seed/key pairs

Data points for reversing `uds_udan_key_calculator_YJC.go`. Seeds are
non-deterministic (different on each connection); each key below was accepted
by the BMS (positive `02 67 02` response).

| Seed (hex)      | Key (hex)       | Capture                  |
|-----------------|-----------------|--------------------------|
| `0D 4A F9 74`   | `38 20 62 9F`   | `bms-connection.asc`     |
| `66 80 20 47`   | `92 0F 02 BA`   | `bms-connection-2.asc`   |
| `9C 43 69 8E`   | `9A 00 4F 4E`   | `bms-connection-3.asc`   |
| `2A 4B 8D D2`   | `3B 87 BE E1`   | `bms-connection-4.asc`   |
| `2A 64 C4 19`   | `16 37 31 4D`   | `bms-connection-5.asc`   |
| `09 E6 16 7A`   | `17 26 91 AD`   | `bms-connection-6.asc`   |
| `9F 9C 21 C7`   | `E1 42 86 06`   | `bms-connection-7.asc`   |
| `DD F5 53 1B`   | `13 1F 61 69`   | `bms-connection-8.asc`   |
| `F8 FD 0C 44`   | `B1 3E 9A 09`   | (earlier capture)        |

Cryptanalysis attempted on the 8 new pairs (`util/crack_bms_seedkey.py`,
`util/crack_bms_seedkey2.py`). RULED OUT:

- `key = seed XOR C`, `seed ± C`, `seed · C mod 2³²` for any 32-bit constant
- `key = ROL(seed, r) XOR C` for all 32 rotations (and equivalent two-stage rotate/xor compositions)
- `key = bitrev(seed) XOR C`, `byteswap(seed) XOR C`, `~seed XOR C`
- Per-nibble S-box (contradicts in nibble 0)
- LFSR shift-and-XOR with common CRC polynomials (CRC32, CRC16-CCITT, CRC16-Modbus, etc.) over 8–64 rounds
- **Any GF(2)-linear function of the seed**: the differences `Δkᵢ = kᵢ⊕k₀`
  are not a linear function of `Δsᵢ = sᵢ⊕s₀`, so the algorithm contains a
  genuinely nonlinear step (carry-propagating add, multiplication, or LUT).

CONSEQUENCE: blind brute-force on more captured pairs is unlikely to crack
this. Practical next steps for `uds_udan_key_calculator_YJC.go`:

1. Decompile the Go binary `app/iBMSUpper.exe` directly — the key calculator
   is a few hundred bytes of Go in a binary we already have. This is the
   cheapest path.
2. Look for a published/leaked UDAN seed-to-key routine (vendor `UDAN`,
   product family iBMS, hardware string `A650_C121.*`).
3. Dump the BMS firmware (NXP S32K + GD25Q64/W25N01G flash) and locate the
   `27 01` handler's verify routine.

### Live readings observed (idle, no charger, ~76.8% SOC)

| Field             | Value                            |
|-------------------|----------------------------------|
| Shown SOC         | 76.8 %                           |
| Pack voltage      | 78.5 V                           |
| Pack current      | 0.0 A                            |
| Cell voltages     | 3.926–3.928 V (delta < 5 mV)     |
| Cell temperatures | 21–23 °C                         |
| Running mode      | **Calibrating**                  |
| Wake signal       | KL15                             |
| Alarm state       | No Fault                         |
| Charger           | Not Connected                    |

"Calibrating" is a distinct Running mode visible in the System state —
separate from the UDS extended/programming sessions and from F700TestModeSwitch.
TENTATIVE: this is the BMS's normal idle/measurement mode rather than a
special diagnostic state.

## iBMS UI — Tab Structure

The iBMS PC Utility presents data in five top-level tabs, several of which
have a right-hand sub-navigation. Useful for interpreting captures: a polling
burst in a trace maps to whichever (tab, sub-nav) pair was active at that
instant.

| Top tab          | Right sub-nav                                                                                                                | Contents                                                                                                                                            |
|------------------|------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------|
| System overview  | —                                                                                                                            | SOH, SOC, total volt, pack current, System config, Peak data, Charge info, DTU, Stats summary                                                       |
| Cell info        | —                                                                                                                            | Per-cell voltage grid (mV) with balancing / open-wire / short flags; per-probe temperatures                                                         |
| Charge info      | —                                                                                                                            | Charging plug temps, Charger → BMS connection state, CC / CP measurements, self-diag, lock state                                                    |
| BMS              | Hlss state · HV detection · Hall state · Shunt state · Signal detection · On-board volt · On-board temp · BMU info · X700    | Relay / contactor state, HV rail voltages, Hall current sensing, shunt current, signal IO, on-board rails / temps, BMU listing, X700 IoT config     |
| SOC              | Cap. config · SOC calib. config · HighSoc · LowSoc                                                                           | SOC parameter calibration tables; fault thresholds (Threshold / Recovery / Delay / Recovery delay) per Sys and Cal params                            |

The SOC tab also has inner tabs (SOC / Total volt / Current / Cell volt / Temp)
for protection thresholds against each measured quantity, and exposes
**Sync / Import / Export / Read / Write** buttons. Read / Export are
presumably gated only by an active connection; **Write** almost certainly
requires the SecAccess L1 unlock.

The 0x06 / 0x08 / 0x87 / 0x9x message IDs documented in §"CAN Protocol —
Message Types" are the iBMS *internal* message tags. Mapping each tag to a
wire-level UDS DID (open TODO) is now constrained: by aligning a capture's
polling bursts against the active tab, the candidate DIDs for each tag
narrow sharply.

## UDS Read Patterns by UI Context

CONFIRMED via time-correlating a navigation-tour capture against screenshots
of the iBMS UI. Baseline polling runs at approximately 1 Hz; tab-specific
reads layer on top when their corresponding UI section is open.

### Connection bootstrap (one-shot at session start, ~0.5 s)

Sequence executed once when the iBMS tool connects to this BMS:

| Step | Request                              | Purpose                                                       |
|------|--------------------------------------|---------------------------------------------------------------|
| 1    | `22 A5 00`                           | Discovery probe (also used in the candidate-ID sweep)         |
| 2    | `22 01 06`                           | UNKNOWN 2-byte value (poll continues during session)          |
| 3    | `22 F1 95`                           | Firmware version string (one-shot only)                       |
| 4    | `22 A5 0F`                           | Hardware/build string (also continues during session)         |
| 5    | `22 28 00`                           | System state snapshot (12 B incl. SOC, SOH, HV1, current — see Confirmed mappings below) |
| 6    | `10 03`                              | DiagnosticSessionControl → extended                           |
| 7    | `27 01` / `27 02`                    | SecurityAccess Level 1 (seed/key exchange)                    |
| 8    | `34 00 24 00 00 3A 00 05 F8`         | RequestDownload: 1528 bytes to memory address `0x00003A00`    |
| 9    | `36 01` / `36 02` / `36 03`          | TransferData blocks (3 × ~516 B request payloads)             |
| 10   | `37`                                 | TransferExit                                                  |
| 11   | `22 A5 03`, `22 A5 05`, `22 A5 0D`   | Additional one-shot reads to populate the UI                  |

UNKNOWN: **the RequestDownload step.** 1528 bytes written to a fixed memory
address on every connection is too small to be firmware. TENTATIVE
hypotheses: (a) a bootstrap / auth blob the tool installs into RAM,
(b) part of the SecAccess L1 unlock dance, (c) a calibration lookup table
re-uploaded each session.

### Steady-state baseline polling (~30 DIDs at ~1 Hz)

Present continuously throughout any iBMS session regardless of active tab.
TENTATIVE: drives the "System overview" tab and the top-of-window summary
fields (SOH, total volt, pack current, SOC, alarm state).

| DID range                                                                                                       | Response (B)    | TENTATIVE category                            |
|-----------------------------------------------------------------------------------------------------------------|-----------------|-----------------------------------------------|
| `0x0100`–`0x0105`                                                                                               | 3–43            | Cell / pack voltage block (incl. 43 B array)  |
| `0x0200`, `0x0202`, `0x0203`, `0x0205`, `0x0206`, `0x0208`, `0x0209`, `0x020B`                                  | 3–23            | Current / temperature / status block          |
| `0x0620`, `0x0621`, `0x0648`                                                                                    | 3–21            | UNKNOWN sub-block                             |
| `0x0E21`, `0x0F50`, `0x0F60`                                                                                    | 4–9             | UNKNOWN                                       |
| `0x2800`, `0x2801`, `0x2810`, `0x2820`, `0x2828`, `0x2830`, `0x2832`, `0x2838`, `0x283A`, `0x2850`              | 3–23            | TENTATIVE: extremum / SOC / cycle-count info  |
| `0x4000`                                                                                                        | 34              | UNKNOWN                                       |
| `0xA500`, `0xA503`, `0xA505`, `0xA50D`, `0xA50F`                                                                | 4–31            | Identity / status block (A50F = build string) |

### Confirmed UDAN-ID ↔ UDS-DID mappings

Five mappings established by time-correlating a navigation-tour capture
against the iBMS UI live values. CSV row values in the historical exports
do **not** match the trace (BMS state has changed since the CSV was
generated, and the BMS clock is wrong) — confirmation is by
schema-shape + live-UI-value match rather than CSV-value match.

| UDAN msg ID                 | Wire DID  | Resp (B) | Payload format                                                    | Evidence                                                                  |
|-----------------------------|-----------|----------|-------------------------------------------------------------------|---------------------------------------------------------------------------|
| `0x08 Voltages`             | `0x0101`  | 43       | 20 × big-endian uint16, mV                                        | 3923–3930 mV across 20 cells; matches UI 3926–3928 mV                     |
| `0x09 Temperatures`         | `0x0102`  | 10       | 7 × uint8 with constant offset (TENTATIVE `°C = raw − 40`)        | Raw `41 41 41 41 40 41 41` → ~22 °C; matches UI 21–23 °C                  |
| `0x93 System state`         | `0x2800`  | 15       | u16 BE block; SOC×10, SOH×10, HV1×10, current, …                  | SOH `0x03E8`=100.0 % and HV1 `0x0311`=78.5 V are exact UI matches         |
| `0x95 (Dis)charged time`    | `0x2801`  | 19       | 4 × big-endian uint32, seconds                                    | One field ticks 1/s during the trace; acc-discharge ≈ 160 h matches UI ≈ 167 h |
| `0x89 (Dis)charged energy`  | `0x2810`  | 23       | mixed; trailing pair of BE uint32 = capacities × 0.01 Ah          | 7762 → 77.62 Ah charge, 7877 → 78.77 Ah discharge; cycle-count `0x0007` matches UI |

Per-DID payload detail follows.

#### DID `0x0101` — Voltages (`0x08`)

Big-endian uint16 array, units of mV. Wire-level source for both the
System overview Peak-data summary and the Cell info per-cell grid.
Sample (idle, ~76.8 % SOC):
`3925 3925 3926 3926 3925 3924 3928 3927 3925 3925 3925 3923 3928 3926 3929 3929 3925 3927 3930 3929` mV.

#### DID `0x0102` — Temperatures (`0x09`)

7 bytes, one per probe. uint8 with a small constant offset (likely
`°C = raw − 40` — a common CAN-message offset — but cannot be pinned
from a trace where all probes are within 1 °C of each other). Sample
`41 41 41 41 40 41 41` → ~22 °C across 7 probes, matching UI 21–23 °C.

#### DID `0x2800` — System state (`0x93`)

12 data bytes after the `62 28 00` header. Three of six BE uint16 fields
identified by live-UI match:

| Offset | BE u16   | Field                          | Live value     |
|--------|----------|--------------------------------|----------------|
| 0      | `0x0312` | Real SOC × 10                  | 78.6 %         |
| 2      | `0x03E8` | **SOH × 10**                   | 100.0 %        |
| 4      | `0x0311` | **HV1 / Pack voltage × 10**    | 78.5 V         |
| 6      | `0xFFED` | TENTATIVE: signed pack current | ≈ −0.2 A idle  |
| 8      | small    | UNKNOWN counter / flag         | 5–7            |
| 10     | varies   | UNKNOWN                        | ~`0x33xx`      |

The headline live state lives in `0x2800` but the full "System state"
page in the iBMS UI is fed by the **entire `0x28xx` family** polled in
parallel (`0x2800`/`0x2801`/`0x2810`/`0x2820`/`0x2828`/`0x2830`/
`0x2832`/`0x2838`/`0x283A`/`0x2850`). UDAN message ID `0x93` is the
iBMS-internal label for the aggregate, with `0x2800` as its primary block.

#### DID `0x2801` — (Dis)charged time (`0x95`)

16 data bytes = 4 × BE uint32, all in seconds:

| Offset | BE u32 (sample) | Field                                                                   |
|--------|-----------------|-------------------------------------------------------------------------|
| 0      | 832,857,443     | TENTATIVE: lifetime counter (ms? epoch-like?); ticks 1/s                |
| 4      | 1,329           | Session uptime (zero at session boot, ticks 1/s)                        |
| 8      | 3,873,795       | Acc. charge time (constant during this trace — tractor not charging)   |
| 12     | 576,772         | Acc. discharge / usage time (≈ 160 h; UI showed ≈ 167 h)                |

**Heartbeat byte:** byte 3 of the payload (low byte of the offset-0 u32)
increments by 1 every ~1 s. This is the byte exported as the
`Heartbeat` column in `System state 0x93.csv`.

#### DID `0x2810` — (Dis)charged energy (`0x89`)

20 data bytes; structure (TENTATIVE except where noted):

| Offset | Width | Sample         | Field                                                              |
|--------|-------|----------------|--------------------------------------------------------------------|
| 0–1    | u16   | `0x0014`       | Cell count = 20                                                    |
| 2–3    | u16   | `0x0007`       | **Cycle count = 7** (matches UI exactly)                           |
| 4–7    | u32   | `0x0F564100`   | UNKNOWN (possibly avg cell × scale, or pack-V derivative)          |
| 8–11   | u32   | varies         | UNKNOWN (instantaneous quantity)                                   |
| 12–15  | u32   | `0x00001E52`   | **Acc. charge capacity × 0.01 Ah** → 77.62 Ah                      |
| 16–19  | u32   | `0x00001EC5`   | **Acc. discharge capacity × 0.01 Ah** → 78.77 Ah                   |

#### Ruled out

- **`0x4000`** (31 data B) is **not** System state. Payload is mostly zero
  with scattered `0xFF` bytes. TENTATIVE: the **fault-flags block**,
  matching the CSV columns `Chg Self-Diag Fault` … `Other Dchg Fault`
  (all currently `NoFault`).
- **`0x0205`** (7 data B) is **not** Temperatures. Returns the constant
  sequence `[0, 1, 2, 4, 5, 6, 7]` — a fixed probe-index / channel map,
  not live temperatures.
- **`0x0100`** is constant (`4611`=18.43 V, `5000`, `700`, …) —
  TENTATIVE: pack-level threshold / calibration limits, not live state.
- **`0x0202`** payload is the fixed sequence
  `00 01 02 03 … 0a 0b 0c 0d 0e 0f 14 15 16 17` — a cell-index table,
  not live state.

### Cell info tab (additive layer)

Adds when the Cell info top-level tab is opened:

| DID(s)                  | Response (B) | TENTATIVE meaning                                |
|-------------------------|--------------|--------------------------------------------------|
| `0x0EA0`, `0x0EA1`      | 8 / 13       | Per-cell balancing flags                         |
| `0x0ED0`–`0x0ED2`       | 7 each       | Per-cell open-wire / fault flags                 |
| `0x0ED6`, `0x0ED7`      | 3 each       | Per-cell short flags                             |
| `0x2803`, `0x2804`      | 7 / 8        | Cell-level extremum / index info                 |
| `0x0960`, `0x0961`      | 4 each       | UNKNOWN                                          |

### BMS tab (Hlss / HV / Hall / Shunt / Signal / On-board / BMU / X700)

Adds when the BMS top-level tab is opened (regardless of sub-nav — all
sub-pane DIDs are read in parallel):

| DID(s)                                            | Response (B)       | TENTATIVE meaning                       |
|---------------------------------------------------|--------------------|-----------------------------------------|
| `0x0900`, `0x0901`, `0x0902`                      | 10 / 17 / 17       | Hlss state + HV detection rails         |
| `0x0E00`                                          | 15                 | Hall state                              |
| `0x0E40`                                          | 10                 | Shunt state                             |
| `0x0E70`, `0x0E71`, `0x0E72`                      | 11 / 11 / 3        | Signal detection                        |
| `0x0EF0`, `0x0F10`, `0x0F30`                      | 5 / 6 / 7          | On-board voltage / temperature rails    |
| `0x1600`, `0x1620`                                | 25 / 10            | BMU info                                |
| `0xA501`, `0xA502`, `0xA506`, `0xA507`, `0xA50E`  | 7 / 8 / 15 / 43 / 5 | X700 IoT subsystem fields              |
| `0x0641`–`0x0647`                                 | 3 each             | UNKNOWN (per-channel something, 7 values) |

### SOC tab "Read" — one-shot dump of calibration tables

Pressing the **Read** button on the SOC tab triggers an exhaustive
one-shot read of ~80 DIDs in the `0x30xx` / `0x40xx` ranges (plus
`0x0E11`, `0x0E61`). Almost all responses are 35 bytes (32 B of data
after the `62 XX XX` header). These populate the Cap.config /
SOC calib.config / HighSoc / LowSoc threshold tables visible in the UI
after the Read completes.

DID groups observed (each polled exactly once per Read):
- `0x3010`
- `0x3030`–`0x305F`, `0x3060`–`0x3061`, `0x3070`–`0x3071`, `0x3080`–`0x3093`
- `0x30A0`–`0x30D7`, `0x30E0`–`0x30E6`
- `0x3140`–`0x3153`
- `0x4011`–`0x4012`, `0x4019`–`0x401A`

The numerically-paired DIDs (e.g. `0x3030`/`0x3031`, `0x3040`/`0x3041`)
TENTATIVE: charge-side vs discharge-side, or high-side vs low-side, of
the same parameter.

### Late-session routine burst (UNKNOWN trigger)

After the SOC calibration read, a burst of routine-control calls appears
alongside additional DID reads at ~1 Hz. Tab context UNKNOWN — possibly
triggered by a Sync / Write button or an SOC inner tab (Total volt /
Current / Cell volt / Temp) not captured in screenshots.

| Request                                | Response (B) | TENTATIVE                  |
|----------------------------------------|--------------|----------------------------|
| `31 01 F0 09`–`31 01 F0 11` (6 RIDs)   | 3–4 each     | StartRoutine, six routines |
| `22 09 05`, `22 09 62`                 | 10 / 4       | UNKNOWN                    |
| `22 06 4E`, `22 06 70`, `22 06 71`     | 3 each       | UNKNOWN                    |

## X700 IoT Subsystem

CONFIRMED that the Solectrac pack's BMS exposes an "X700" sub-section in
the iBMS UI (under BMS tab → right sub-nav). X700 is also listed among the
UDAN product models, but here it appears as a *subsystem within this
UDS-family BMS* responsible for cellular / cloud telemetry.

Fields exposed (mostly empty in the observed unit, but the schema is fixed):

| Field                         | Purpose                          |
|-------------------------------|----------------------------------|
| HWID                          | Hardware identifier              |
| FWVersion                     | X700 firmware version            |
| HWVersion                     | X700 hardware version            |
| DeviceName                    | Cloud-side device name           |
| Host / Port                   | Cloud endpoint                   |
| APN UserName / APN Password   | Cellular APN credentials         |
| MQTT UserName / MQTT Password | MQTT broker credentials          |

Related observed UI: the DTU panel on the System overview tab shows
GPRS / LAC / MCC / MNC / Carrier / signal-strength fields, also currently
empty. Consistent with the BMS having a built-in cellular modem capability
that is not provisioned on the as-shipped Solectrac unit.

TENTATIVE: UDANN message IDs 0x98 (WiFi info) and 0x9D (WiFi / DTU)
documented above are likely how this data is read / written over UDS;
explicit DID mapping UNKNOWN.

## Remote/Force Control Functions

The software can remotely command the BMS via protobuf messages:

- `RemoteControlMessage` — top-level control envelope
  - `WorkModeControlMessage` — system lock/unlock, system reset
  - `MosForceControlMessage` — force MOS switch states (per MOS index)
  - `ForceControlState` — enum: `MosStateEnum`
- `F700WriteForceContrl` — write force control to F700 BMS
- `P700MOSForceContrl` — force MOS control on P700
- `U600ElecLockForceContrl` — electric lock force control
- `U600HLSSForceContrl` — HLSS force control
- `ChgForceControl` / `ChgForceControlTime` — charger force control

## Protobuf Message Types

Main data messages (all implement ProtoMessage/ProtoReflect):
- `ChargeMessage` — charge request current/voltage, connect state, fault flags
- `DiagnosisMessage` — alarm count, diagnosis info
- `ExtremumMessage` — max/min cell voltage & temp, SOC parameters
- `TotalPackageMessage` — wraps ChargeMessage, DiagnosisMessage, ExtremumMessage, RemoteControlMessage, CloudServiceConfigMessage
- `MosForceControlMessage` — MOS index, force control value, timestamp
- `WorkModeControlMessage` — system lock/unlock, system reset
- `RemoteControlMessage` — control type, cell balance state, plus above
- `CloudServiceConfigMessage` — cloud service configuration

## MCU Targets Mentioned

- S32K142, S32K314 — NXP S32K automotive MCUs (both are ARM Cortex-M variants)
- GD25Q64 — SPI NOR flash (Gigadevice, 64Mbit)
- W25N01G — NAND flash (Winbond, 1Gbit)

## Product Models

F700/F702/F715/F717/F718/F719/F720/F721/F722/F723/F728/F729/F730/F732/F733/F735/F750/F751/F752/F753/F780/F781/F782/F783/F785/F786/F788
E700/E720/E721/E730/E750/E751/E752/E753
P700 (parallel BMS)
U600 (another BMS variant)
X700

## Known CAN Interface Thread Log Prefixes

- `[USBCAN]`, `[USBCAN_CX]`, `[USBCAN_GC]`, `[USBCAN_GC_PRO]`, `[USBCAN_2E_U]`, `[USBCAN_E_U]`
- `[CANDTU]`
- `[PCAN]`
- `[isCAN]`
- `[zqwl_CAN]`

## TODO / Still Unknown

- ~~Exact CAN arbitration ID(s) used for UDS communication (7E0/7DF range vs extended 29-bit)~~ **PARTIALLY RESOLVED**: discovery sweep uses 11-bit standard IDs `0x740`, `0x7D0`, `0x36E` (and possibly `0x34E`); see "Connection Discovery Handshake" above. Response IDs and full per-model mapping still UNKNOWN.
- ~~Exact byte sequence of the F700TestModeSwitch CAN frame~~ **RESOLVED**: Modbus FC 0x10 write to register 0x0E11; see "Test Mode" section above
- Security access seed/key algorithm in uds_udan_key_calculator_YJC.go. Nine captured seed/key pairs recorded in §"Captured SecurityAccess seed/key pairs"; all simple/linear forms ruled out — the algorithm is nonlinear and won't fall to more captures alone. Path forward: decompile `app/iBMSUpper.exe` (the Go binary that computed these keys).
- ~~Whether UDS test mode (P700/U600) uses 0x10 extended session or a custom routine (0x31); F700 does not use UDS at all~~ **PARTIALLY RESOLVED for the unlock step**: extended session (`10 03`) + SecAccess L1 (`27 01`/`27 02`). Whether *entering test mode* additionally requires a 0x31 routine or a `2E` write is still UNKNOWN.
- ~~Mapping between the 0x8X message IDs and the wire-level UDS DIDs returned by `22 XX XX`~~ **5 mappings CONFIRMED**: `0x08`↔`0x0101` (Voltages), `0x09`↔`0x0102` (Temperatures), `0x93`↔`0x2800` (System state), `0x95`↔`0x2801` (Dis/charged time), `0x89`↔`0x2810` (Dis/charged energy). Still to map: `0x06` Peak data, `0x87` Alarm state, `0x94` Charging, plus the remaining `0x0Axx`/`0x0Bxx`/`0x79`/`0x86`/`0x88`/`0x97`/`0x99`/`0x9A`/`0x9B` family.
- What does the connection-bootstrap RequestDownload write to memory address `0x00003A00` (1528 bytes, 3 × ~516 B TransferData blocks)? Possibly part of the SecAccess L1 unlock dance, an authentication blob, or a calibration table re-uploaded each session.
- Which UI action triggers the late-session routine burst (`31 01 F0 09`–`31 01 F0 11` plus DIDs `0x0905`, `0x0962`, `0x064E`, `0x0670`, `0x0671`)? Not captured in screenshots — possibly an SOC inner tab (Total volt / Current / Cell volt / Temp) or a Sync/Write action.
- Decode the still-tentative payloads: temperature offset for `0x0102` (need a capture with thermal spread); the remaining `0x2800` fields (offsets 6/8/10); `0x2810` offsets 4–11; `0x0EA0`/`0x0EA1` (balancing flags); `0x0ED0`–`0x0ED7` (open-wire/short flags); and the `0x09xx` BMS-tab block (Hlss / HV / Hall).
- Promote `0x4000` from UNKNOWN to TENTATIVE "fault-flags block" (matches Alarm-state CSV column layout — all-zero in healthy state, scattered `0xFF` sentinels). Need a capture with an active fault to confirm.
- Which F700 hardware variants use the alternate TestModeSwitch registers 0x0EDC and 0x0E13
- Modbus slave address: default appears to be 0x01 but is configurable via `ReinitSlaveAddress`
- CAN arbitration ID for UART-over-CAN Tx frames (runtime-configured, not hardcoded)
