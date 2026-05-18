# Solectrac CAN bus — system documentation

Reverse-engineered protocol and hardware documentation for a Solectrac electric
tractork. All decode information is derived from captured CAN traffic, vendor
manual tables, the COBO cluster datasheet, the "BMS Update" document, the
Solectrac Parts Catalog (e25), and live injection tests on the tractor.

Confidence markers used throughout:

- **CONFIRMED** — verified by injection, cross-validation, or operator
  ground truth.
- **TENTATIVE** — single-source or partial evidence; encoding plausible
  but not nailed.
- **UNKNOWN** — observed but not decoded.


## Contents

- [Vehicle and pack](#vehicle-and-pack)
- [CAN bus topology](#can-bus-topology)
- [J1939 Decodings](#j1939-decodings)
  - [Source-address map](#source-address-map)
  - [BMS (SA 0xF3)](#bms-sa-0xf3)
  - [Motor controller (SA 0xCA)](#motor-controller-sa-0xca)
  - [Charger (SA 0xE5)](#charger-sa-0xe5)
  - [Vehicle controller (SA 0xD0)](#vehicle-controller-sa-0xd0)
  - [Instrument cluster hardware](#instrument-cluster-hardware)
- [Vendor error code tables](#vendor-error-code-tables)
- [Open questions](#open-questions)


## Vehicle and pack

The tractor is a **Solectrac 25G** (non-HST variant).

"Pack" refers to the tractor's traction battery — the high-voltage
lithium-ion battery that powers the motor, as distinct from the
**12 V / 20 Ah accessory battery** that runs the cluster and lights.
The accessory rail is fed by a **500 W 72 V → 12 V DC-DC converter**
off the pack (parts catalog Table 65).

| Property                  | Brochure                              | Operator manual (CET)                              | Service manual              | BMS GUI                 | Observed |
|---------------------------|---------------------------------------|----------------------------------------------------|-----------------------------|-------------------------|----------|
| Bus baud                  | —                                     | —                                                  | 250 kbaud (J1939 default)   | —                       | —        |
| Cell P/N                  | —                                     | —                                                  | `SEPNI-8688190P-17.5AH-5P`  | —                       | `SEPNI8688190P-15Ah` (battery faceplate) |
| Cells in parallel         | —                                     | —                                                  | 4 modules × 5P1S = 20P      | "~20 cells in parallel" | —        |
| Charging temp range       | 0–40 °C                               | —                                                  | —                           | —                       | —        |
| Charging time             | 5.5 hr (Lvl 2, 220 VAC, 20→80%); 11 hr (Lvl 1, 110 VAC) | 8 hr (0→100%, on-board charger)  | —                           | —                       | —        |
| Charging-target voltage   | 83 V                                  | 82 VDC (§9.1)                                      | —                           | —                       | —        |
| Cluster supply            | —                                     | 12 V / 20 Ah aux battery                           | 12 V (accessory, not pack)  | —                       | —        |
| Cycle life                | 2500 cycles @ 25 °C                   | 2500 cycles @ 25 °C                                | —                           | —                       | —        |
| Main HV fuse              | —                                     | —                                                  | 350 A                       | —                       | —        |
| Manufacture date          | —                                     | —                                                  | —                           | —                       | 2021-12-02 (battery faceplate) |
| Nominal pack voltage      | 72 V                                  | 72 V (§1.2 plate, §9.1)                            | **73.0 V**                  | 72.0 V                  | 72 V (battery faceplate) |
| Operating temp range      | −20 to 55 °C                          | —                                                  | —                           | —                       | —        |
| Pack chemistry            | Li NMC                                | NMC (Li-ion)                                       | NMC                         | NiCoMn                  | —        |
| Pack model number         | —                                     | `EV-008-72V300Ah-01` (§1.2 plate)                  | —                           | —                       | `EV-008-72V300Ah-02` (battery faceplate) |
| Pack serial number        | —                                     | —                                                  | —                           | —                       | NO.079 / QR `031PE0021Y020ABC20100079` (sticker) (battery faceplate) |
| Pack vendor               | —                                     | Soundon New Energy Technology Co., Ltd. (§1.2 plate) | "Escorts Solution"        | "ESCORTS-INTERNAL"      | Soundon New Energy Technology Co., Ltd. (engraved) + Escorts (sticker) (battery faceplate) |
| Pack weight               | —                                     | 175 ± 15 kg (§1.2 plate)                           | —                           | —                       | 175 ± 15 kg (battery faceplate) |
| Rated capacity            | 350 Ah                                | 300 Ah (270 Ah opt., §9.1; 300 Ah on §1.2 plate)   | **350 Ah**                  | 300 Ah                  | 300 Ah (battery faceplate) |
| Rated charge (DC / AC)    | —                                     | charger out 3.3 kW @ 220 V; in AC 85–265 V, 50/60 Hz, IP67 | —                   | 78 A / 39 A             | —        |
| Rated energy              | —                                     | 21.6 kWh (§1.2 plate)                              | 25.5 kWh @ 23 ± 2 °C        | —                       | 21.6 kWh (battery faceplate) |
| Temperature probes        | —                                     | —                                                  | -                           | 7 active                | 7 active (CAN captures) |
| Voltage operating range   | —                                     | 60–84 V (§1.2 plate)                               | 60–84 V                     | —                       | 60–84 V (battery faceplate) |

Five sources supply pack specs: the Solectrac **brochure**
(`docs/Solectrac-e25G-Brochure-230818.pdf`); the **operator manual**
(CET, `docs/CET Operator Manual.pdf`, especially §1.2 nameplate photo
and §9.1 specification table); the FT 25G **service manual** battery
section; the **BMS GUI** as relayed second-hand; and the **battery
faceplate** observed directly on this tractor.

**The 300 Ah vs 350 Ah split is a two-SKU situation, not a single-pack
disagreement.** The service manual cell P/N is
`SEPNI-8688190P-17.5AH-5P` (17.5 Ah cells); the as-installed pack's
nameplate sticker is `SEPNI8688190P-15Ah` (15 Ah cells). Same cell
family (SEPNI 86 × 88 × 190 mm prismatic NMC), different capacity
grade. Plugged into the 20-series × 4-module × 5-parallel topology:

- 17.5 Ah cells → 4 × 5 × 17.5 = **350 Ah pack, 25.5 kWh @ 73 V** —
  service-manual and brochure-quoted SKU.
- 15 Ah cells → 4 × 5 × 15 = **300 Ah pack, 21.6 kWh @ 72 V** —
  this tractor's installed SKU. The operator manual's example
  faceplate (§1.2) and the BMS GUI both describe the same SKU.

The `solectrac-analyze.py` / `solectrac-stream.py` Wh display uses
72 V × 300 Ah = 21.6 kWh, which matches the faceplate exactly.

**Pack vendor is Soundon; UDAN is the BMS firmware/tool vendor, not
the pack maker.** The battery faceplate is laser-etched **Soundon New
Energy Technology Co., Ltd.** (Chinese NMC pack manufacturer; the
`docs/BMS Update Error and Data Extraction - MS Soundon Battery.pdf`
file in this repo is from them). An Escorts-branded white sticker
rides on top: Escorts Kubota Limited (Farmtrac's parent in India,
Solectrac's US distribution brand) buys the pack from Soundon, applies
its own QR-coded serial ("Escorts 72V300Ah NO.079"), and ships it into
Farmtrac/Solectrac tractors. The BMS-GUI "ESCORTS-INTERNAL" footer and
the service manual's "Escorts Solution" pack-vendor attribution both
reflect the Escorts integrator label, not the upstream pack
manufacturer. Whether the BMS PCB itself is Soundon-built, UDAN-built,
or third-party is not resolved by the available documents.

The service manual's BMS troubleshooting section delegates all
live-data inspection to a host-side application called **UDAAN**
(referenced repeatedly: "Connect UDAAN and check the minimum cell
voltage", etc.). UDAAN has been identified as the **UDAN iBMS Upper
Utility** from **Anhui UDAN Technology Co., Ltd.** — a Chinese BMS
firmware/diagnostic-tool vendor. The tool is publicly downloadable
Windows software (CAN @ 250 kbit/s, supports cheap CANalyst-II / PCAN
/ USBCAN dongles). The service manual itself contains no byte-level
payload tables for the BMS broadcast frames, but UDAN's tool has a
`Comm. Message` recording feature that captures the raw CAN exchange
alongside a labeled Excel export of every System Overview UI field —
running both simultaneously produces a time-aligned raw-CAN +
labeled-field log, i.e. an empirical DBC. See the open-questions
section for the practical decode path.

This tractor's pack carries manufacture date **2021-12-02** and
Escorts serial **NO.079** — useful as a calendar-age anchor for SOH
and capacity-fade discussions.

**BMS field connector** is part number **`RT061412SNHEC03`** (12-pin
circular). Per the manual's DTC 125 troubleshooting (page 30 of the
battery section), main vehicle CAN exits on **pins D and E** — a 60 Ω
resistance test across D↔E (two 120 Ω terminators in parallel)
confirms a healthy bus. Pins A/B/C are 12 V power rails: **B is GND**,
A and C are switched/unswitched +12 V (DTCs 140/142/143/144 all
prescribe "12 V between A↔B and C↔B" as the integrity check). The
remaining field-connector pins F/G/H/J/K/L are unassigned in
troubleshooting steps and are the most likely physical home of the
**second (debug) CAN pair** — see "Second 2-pin CAN port" under the
CAN topology section.

Schematic 5.7 in the FT 25G service manual uses BMS-internal terminal letters that do **not** map
1:1 to the field connector — it shows main CAN on pins H/J
(`CAN_H3`/`CAN_L3`) and a second pair on F/G (`CANDE-H`/`CANDE-L`)
labelled "TO BMS DEBUG CONNECTOR PIN-1/PIN-2". The schematic's H/J
and the field connector's D/E refer to the same physical bus; the
two pin-naming conventions are independent.

100 % SOC reference set (5 captures at full charge):

| Condition                  | 20 × mean cell mV |
|----------------------------|-------------------|
| Idle, no charger           | 83.29 V           |
| Charger inserted, no AC    | 83.31 V           |
| Full throttle (neutral)    | 83.22 V           |
| Accel/decel (neutral)      | 83.18 V           |
| Full throttle + hydraulics | 83.06 V           |

The 0.25 V sag from idle to peak load is the cleanest pack-V load
response observed.

**HV power path.** The pack feeds the traction inverter through the
**Albright SW200** main contactor (service manual §4.2.3) protected
by a **350 A battery cut-off fuse** (§4.3.5; parts catalog Table 60
lists 355 A — same component, rounding). A separate **discrete
hydraulic contactor** (Table 60) gates HV between the pack and the
BLDC hydraulic pump motor; its coil is energized from the main
E-Controller's key-switch wire. Neither contactor is on the CAN
bus, which is part of why hydraulic activity produces no CAN
signature (the other part being that the e-hydraulic controller has
no CAN pins at all — see "What is NOT on this bus").


## CAN bus topology

Single shared CAN bus at 250 kbaud. The ODB-2 diagnostic port we capture from
is on the same bus as the ECUs that run the tractor — it is not a
separate diagnostic segment.

Per **schematic 5.10** in the FT 25G service manual, the bus has
**exactly four** CAN nodes, with terminators at the two physical ends
of the linear bus:

    1. MOTOR CONTROLLER (SA 0xCA) — Curtis controller, pins 23/35;
                                    120 Ω terminator at this end
                                    (per parts catalog, model is Curtis
                                    1238E; nameplate not verified in
                                    this corpus)
    2. BMS (SA 0xF3)              — pins H/J of the BMS internal terminals,
                                    field-connector pins D/E
    3. CHARGER (SA 0xE5)          — on-board AC charger; pins 1/2.
                                    Also speaks CAN to the BMS — DTC 124
                                    is "Fast Charger CAN connection fault"
    4. CLUSTER                    — COBO ECO MATRIX VT3 instrument
                                    cluster, pins 35/36; 120 Ω terminator
                                    at this end

The **OBD-II diagnostic connector** is a passive tap on the same bus —
not an extra node. It follows standard OBD-II HS-CAN pinout with four
cavities populated (verified by physical inspection):

- **Pin 4** — chassis ground
- **Pin 6** — CAN_H (yellow 0.75 mm²)
- **Pin 14** — CAN_L (green 0.75 mm²)
- **Pin 16** — +12 V battery

Pin 5 (signal ground) is **not** populated — only pin 4 carries
ground. For a CAN-only diagnostic tap this is fine: pin 4 gives any
attached tool a common reference for the differential pair, and pin 5
is mostly a legacy/emissions-tool concession. A generic OBD-II adapter
expecting pin 5 to be live may need a jumper from pin 4 to pin 5 on
the dongle side, or simply tie its signal ground to pin 4.

The older Solectrac topology diagram's "DB9" connector label is a
mislabel — it's the OBD-II port.

```
   [120 Ω]─┬────────┬────────┬────────┬────────┬─[120 Ω]
           │        │        │        │        │
        ┌──┴──┐  ┌──┴──┐  ┌──┴──┐  ┌──┴──┐  ┌──┴──┐
        │ MC  │  │ BMS │  │ CHG │  │ OBD │  │ CLU │
        │0xCA │  │0xF3 │  │0xE5 │  │ tap │  │     │
        └─────┘  └─────┘  └─────┘  └─────┘  └─────┘
```

- `[120 Ω]` = terminator drawn on schematic 5.10 (MC and Cluster).
- OBD = OBD-II capture port (passive tap, no SA).
- Node order on the schematic is as drawn; physical electrical order
  on the wire is not verified.

### What is NOT on this bus

**The E-Hydraulic Controller has no CAN pins.** Schematic 5.11 in the
service manual shows it driven by a discrete control interface:
LOW/HIGH speed-selection switch, hydraulic-motor on/off switch,
throttle wiper potentiometer (10 kΩ), Hall/encoder, key-switch wire
from the main E-Controller (`KS01A`), and three-phase U/V/W out to a
BLDC pump motor. It is not a CAN-speaking ECU on this vehicle.

The parts catalog identifies the e-hydraulic as a **Kelly KLS7212M /
KLS7218** controller, which is a CAN-capable family. Either the
Kelly's CAN port is physically present but not wired, or the catalog
identification is for a different controller variant. Either way, the
CAN-decode-from-the-Kelly-protocol-PDF plan is not applicable on this
vehicle.

The rear 3-point hitch, lift, PTO, power steering, and remote
hydraulics are all mechanical-hydraulic with no electrical interface
(per the manual's Hydraulic System chapter, pp 295-319 — a fully
mechanical Escorts design with draft + position levers, rocker
top-link spring, mechanical position-feedback cam, manual auxiliary
spool, and no solenoids/sensors/transducers anywhere).

### Bus termination

Bus measures **40 Ω** across CAN_H/CAN_L at the OBD-II port (key off,
all nodes connected) — three 120 Ω resistors in parallel, one beyond
what the schematic draws. Unplugging the BMS field connector raises
the reading to the textbook **60 Ω**, confirming the extra terminator
is **internal to the BMS**. The Charger does not terminate internally
(otherwise the BMS-disconnected reading would have stayed at 40 Ω).
The remaining two terminators are the schematic 5.10 endpoints
(at the MC and Cluster locations). Drivers tolerate the 3-terminator
config and captures are clean.

### Second 2-pin CAN port

A separate 2-pin connector on the tractor remains un-tapped. The
leading hypothesis is that it carries the **BMS debug CAN** pair shown
on schematic 5.7 as `CANDE-H` / `CANDE-L`, explicitly labelled "TO
BMS DEBUG CONNECTOR PIN-1 / PIN-2". The BMS thus exposes two CAN
pairs: the main vehicle bus (above) and this debug pair. If tapped,
expect BMS-internal diagnostic chatter — likely the same protocol that
the host-side **UDAAN** tool consumes. This is no longer believed to
be a hydraulic bus.

**Resistance confirms it is a separate, BMS-only bus.** Measured
key-off with nothing plugged in, the 2-pin connector reads **120 Ω
across the pair** — i.e. exactly one 120 Ω terminator on that pair.
If the 2-pin were a tap onto the main bus, it would read the same
as any other tap on the main bus (40 Ω at OBD-II in the same
conditions, §"Bus termination" above). Unplugging the BMS field
connector causes the 2-pin reading to go **open** (overload), which
proves the only node electrically present on that pair is the BMS —
no other module in the harness taps it. The single 120 Ω is therefore
the BMS's internal terminator on its debug pair, and the 2-pin
connector is its physical termination at the harness end (no second
terminator until a tool is plugged in). All consistent with the
schematic 5.7 `CANDE-H`/`CANDE-L` debug-pair interpretation.

## J1939 decodings

Almost all of the traffic monitored on the Solectrac is
[J1939](https://www.csselectronics.com/pages/j1939-explained-simple-intro-tutorial),
a standardized language for heavy duty vehicles on top of the CAN protocol.
Each 29-bit J1939 identifier breaks down as:

| Bits   | Field               | Notes                                                                 |
|--------|---------------------|-----------------------------------------------------------------------|
| 28..26 | Priority (P)        | 0 = highest, 7 = lowest. Priority 6 is typical for periodic broadcasts.|
| 25     | Reserved (R) / EDP  | Always 0 in classic J1939.                                            |
| 24     | Data Page (DP)      | Selects between page 0 (default) and page 1.                          |
| 23..16 | PDU Format (PF)     | PF < 0xF0 → PDU1 (destination-specific). PF ≥ 0xF0 → PDU2 (broadcast).|
| 15..8  | PDU Specific (PS)   | Destination Address (DA) for PDU1, or Group Extension (GE) for PDU2.  |
| 7..0   | Source Address (SA) | The transmitter's J1939 address.                                      |

From this identifier, the Parameter Group Number (PGN) is reconstructed
according to the following logic so that broadcasts can use the address space
as additional data storage:

```
  if (PF < 0xF0) {
      // PDU1: PS is DA, not in PGN                                                                                                
      PGN = (DP << 16) | (PF << 8);                                                                                                
      DA  = PS;                                                                                                                    
  } else {                                                                                                                         
      // PDU2: PS is GE, part of PGN                                                                                               
      PGN = (DP << 16) | (PF << 8) | PS;
  }    
```

In data J1939 data collected for the Solectrac, > 99% (all except the 1806E5F4
request from the vehicle-controller 0xF4 to the on-board charger 0xE5).

### Source-address map

Every J1939 frame's 29-bit CAN ID ends in an 8-bit source address (SA)
identifying which node on the bus sent it. The table below pairs each
SA seen in our captures with the ECU we believe is behind it and the
frames it emits, and is the basis for the per-source decoder dispatch
elsewhere in this document.

| SA   | Role                                  | Frames observed                                             |
|------|---------------------------------------|--------------------------------------------------------------|
| 0xF3 | BMS                                   | F100/F102/F104/F106/F107/F108/F113../F155..                  |
| 0xE5 | On-board charger                      | FF50 telemetry                                               |
| 0xF4 | Vehicle controller (drive-side)       | Sends 1806E5F4 (request to charger)                          |
| 0xD0 | Vehicle controller / dashboard accy.  | Periodic F100D0 heartbeat; byte-0 0x00 → 0x0C at wake-up     |
| 0xCA | Motor controller / drive ECU          | DM1 (FECA) + FF21 motor telemetry (~85 Hz); silent while charging |
| 0x12 | Unknown                               | Constant FF21 payload `01 00 00 00 00 00 00 00`              |
| 0x041 (11-bit) | Ignition event marker (non-J1939) | Standard CAN 2.0A, not J1939. Constant payload `20 12 01 00 00 00 01 11`. Observed exactly twice per full ignition cycle (one frame at key-on, one at key-off); absent from captures that don't span a power transition. Source ECU unconfirmed. |


### BMS (SA 0xF3)

All scalings derived empirically. Byte numbering is 1-based with
explicit `data[N]` (0-based) annotations where helpful.

#### F113..F13C — Per-cell voltages — CONFIRMED

8 bytes = 4 × big-endian uint16, millivolts.

    F113 = cells  0.. 3
    F114 = cells  4.. 7
    ...
    F117 = cells 16..19
    F118..F13C reserved (cells 20..167); 0xFFFF / 0 sentinel on this pack.

Cells read ~3.6–3.7 V at ~40 % SOC, ~4.16 V/cell at 100 %.

#### F155..F15E — Module temperatures — CONFIRMED

8 bytes = 8 × uint8 with J1939 +40 °C offset (raw 53 = 13 °C).

    F155 = channels 0..7
    F156 = channels 8..15
    ...
    F15E = channels 72..79

Only the first 7 channels are populated on this pack; the rest are
0xFF (not present).

#### F102F3 — Cell min/max summary — CONFIRMED (max/min/spread)

| Byte    | Meaning                                            |
|---------|----------------------------------------------------|
| 1..2 BE | max cell mV                                        |
| 3..4 BE | min cell mV                                        |
| 5       | max-cell **number, 1-based**                       |
| 6       | min-cell **number, 1-based**                       |
| 8       | status/flag bits (semantics TENTATIVE)             |

**Indexing convention:** byte 5/6 use 1-based cell numbers as the BMS
GUI displays them ("Max cell #19"). The parser's `cell_index` in
`cells.csv` is 0-based — subtract 1 to map. Cross-validated against
contemporaneous per-cell PGN snapshots in
`recorded-data/charging.csv`.

When several cells tie at the max, the BMS reports the lowest-index
winner.

The reported `min_mv` is occasionally 1 mV higher than the actual
lowest voltage in the per-cell snapshot taken alongside it — likely a
timing-skew or filtering artifact in the BMS. The *index* still
correctly identifies the right cell.

Smallest spread observed across all captures: 3 mV (124 frames in
`recorded-data/charging.csv`).

#### F100F3 — Pack status — CONFIRMED (voltage, current); TENTATIVE (SOC)

| Byte | data[]  | Meaning                                                     |
|------|---------|-------------------------------------------------------------|
| 1    | data[0] | 0x03 constant                                               |
| 2    | data[1] | **Pack terminal voltage**: V = raw × 0.1 + 76.8             |
| 3..4 | data[2..3] BE | **Signed pack current**: A = (be16 − 0x7D00) × 0.1    |
| 5    | data[4] | **BMS-published SOC** (TENTATIVE; two-point fit at 80 %/90 %) |
| 6    | data[5] | 0xFA constant — **leading SOH candidate** (250 raw × 0.4 %/bit = 100 %) |
| 7    | data[6] | 0x14 (= 20) — series cell count                             |
| 8    | data[7] | 0x00 constant                                               |

**Pack terminal voltage** anchored by linear regression of data[1]
versus 20 × mean(cell mV) across 24 captures (residuals < 0.55 V), and
cross-checked against the FF50 charger frame which uses an identical
encoding (R² = 0.986 across 2863 active-charging frames).

**Pack current** is signed BE-16 with a fixed bias of 0x7D00 (raw
32000 = 0 A) at 0.1 A/bit. Convention: positive = drawing from pack,
negative = charging into pack.

Cross-validation against operator-confirmed dashboard amperage
(amp-*.asc steady-state captures, 2026-05-09):

| File          | data[2..3] range  | mean decoded A | dash A |
|---------------|-------------------|----------------|--------|
| amp-1.asc     | 0x7D12 (constant) |   1.8          |   1    |
| amp-18.asc    | 0x7D9D – 0x7DC5   |  17.6          |  18    |
| amp-35.asc    | 0x7E53 – 0x7E94   |  37.0          |  35    |
| amp-42.asc    | 0x7E8E – 0x7EC5   |  41.7          |  42    |
| amp-58.asc    | 0x7F32 – 0x8061   |  62.1          |  58    |

Mean decoded current matches dashboard to within ~1 A across the full
0–60 A range, exercising the 0x7D→0x7E and 0x7F→0x80 high-byte
boundaries. amp-1.asc is the only true-idle capture: data[2..3] is
constant at 0x7D12 = 1.8 A standby draw (BMS + dashboard + DC-DC).
Putting the tractor in DRIVE energizes inverter/contactor circuitry
that adds ~16 A above standby — earlier captures sat at ~17 A "idle"
for this reason.

**Pitfall warning.** A naive "data[3] alone, 1 A/bit" decode matches
the dashboard at idle by coincidence: data[2] sits at 0x7D, the bias
cancels, and data[3] reads as 0..25.5 A. The moment real current
crosses ~25.6 A, data[2] ticks to 0x7E and data[3] rolls back near
zero, making the byte-only decode appear to "saturate" under load.
Always read both bytes BE with the bias.

**SOC (data[4])** is TENTATIVE. Streamer fit:

    SOC % = data[4] × 0.4 − 0.8

calibrated against two direct dashboard-screen readings: raw 202 at
80 % and raw 227 at 90 %. Slope = 10/25 = 0.4, intercept = −0.8. Raw
saturates at 250 (= 99.2 %) in `soc-100-idle.asc`. Calibration points
still sit in the top ~20 % of the range; linearity below 80 % wants a
deeper-discharge capture.

**SOH candidate (data[5])** TENTATIVE. data[5] is 0xFA = 250 across
every capture (42 captures, all BMS frames swept by
`util/soh_byte_sweep.py`). 250 raw × 0.4 %/bit decodes to 100 %, which
matches the SOH reading in the "BMS Update" document. SOH on a
healthy low-cycle-count NMC pack should be effectively constant at
100 % across short captures, so "looks like a fixed config field" and
"is the real SOH at 100 %" produce identical evidence in this corpus.
The byte-constancy sweep eliminates every other plausible SOH location
in the visible BMS frames — every other constant byte is either
already attributed (cell count, voltage, current, SOC) or is a J1939
sentinel (0x00 / 0xFF). Upgrading from leading candidate to CONFIRMED
needs a capture where SOH demonstrably differs from 100 % (older
firmware, older/degraded pack, or an injected spoof watched on the
vendor GUI).

#### F104F3 — Pack temperature min/max summary

Pack-wide hottest/coldest module-temperature summary, analogous to
F102. Byte-level decode UNKNOWN.

#### F106F3 — BMS state — TENTATIVE

Periodic frame; byte 0 carries a state-machine vocabulary:

| byte 0 | Inferred meaning           |
|--------|----------------------------|
| 0x00   | init / boot                |
| 0x80   | standby / charger detected |
| 0x45   | ready / driving            |

Observed full payloads:

    driving captures:               45 E0 FC FF FF FF FF FF
    ignition with charger inserted: 80 C4 FC FF FF FF FF FF

Vendor GUI implies more states exist (Calibrating, Charging,
Discharging, Fault, Sleep); not observed in captured data.

#### F107F3 — BMS limits — TENTATIVE

Layout matches the standard J1939 limits-frame template:

| Bytes | Likely meaning                          | Observed                                       |
|-------|-----------------------------------------|------------------------------------------------|
| 0..1  | Discharge current limit, 0.1 A/bit      | 0x2710 (charger inserted) / 0x38A4 (driving)   |
| 2..3  | Charge current limit, 0.1 A/bit         | 0x2710 in every capture                        |
| 4..5  | Voltage limit, 0.2 V/bit (guess)        | 0x0000 (charger inserted) / 0x0176 (driving)   |
| 6..7  | (unknown)                               | 0x0000                                         |

0x2710 = 10000 is almost certainly a J1939 "not available" sentinel
(the more conventional 0xFFFF wasn't used here). Pinning this down
needs a charge capture from low SOC where meaningful charge-current
limits are published.

#### F108F3 — BMS active fault bitmap — CONFIRMED via injection

Active BMS fault flags. All bytes 0x00 in healthy idle (verified
against `asc/bms-error-codes/idle-no-bms.asc`).

Every per-bit assignment below was established by spoofing F108 with
each bit set in isolation and reading the resulting code off the
dashboard. The layout is non-uniform — different bytes use different
bits-per-code rates:

| Byte | Encoding         | Codes                                  |
|------|------------------|----------------------------------------|
| 0    | 2 bits per code  | 100, 101, 102, 103                     |
| 1    | 2 bits per code  | 104, 105, 106, 107                     |
| 2    | 2 bits per code  | 108, 109, 110, 111                     |
| 3    | 2 bits per code  | 112, 113 (bits 4..7 silent; 114/115 reserved) |
| 4    | 1 bit per code   | bit 0=116, bit 1=117, ..., bit 7=123   |
| 5    | 1 bit per code   | bit 0=124, bit 1=125, bit 2=126, bit 3=127 (bits 4..7 silent) |
| 6    | (all silent)     | —                                      |
| 7    | 1 bit per code, with gaps | (see byte-7 table below)      |

For the 2-bit bytes the dashboard treats either bit of a pair as the
code being asserted — SAE J1939 "2-bit status" convention (00 = off,
01/10/11 = on at varying severity, dashboard renders any non-00 pair
as the code on).

##### F108 byte 7 mapping

| Bit | Mask | Code | Meaning                                       |
|-----|------|------|-----------------------------------------------|
| 0   | 0x01 | 140  | System fault level                            |
| 1   | 0x02 | —    | (silent)                                      |
| 2   | 0x04 | —    | (silent)                                      |
| 3   | 0x08 | 142  | BMS fault need maintenance                    |
| 4   | 0x10 | 143  | Battery fault need maintenance                |
| 5   | 0x20 | 144  | Battery system fault needs maintenance        |
| 6   | 0x40 | 144  | Duplicate of bit 5 (re-verified)              |
| 7   | 0x80 | 145  | Full charge/discharge cycle needed            |

Notable:

- Code 146 ("Maintenance mode status") is listed in the manual but is **not**
  encoded in F108
  anywhere.
- Bit 6 genuinely re-asserts code 144 (re-verified with single-bit
  injection). Likely a severity-pair the dashboard renders
  identically.
- Bits 1 and 2 might still carry internal flags that don't surface as
  numeric codes.

##### F108 cross-validation against pre-injection captures

`bms-fullcharge-102-109-140.asc` — operator-confirmed cycling 102, 109, 140:

    F108 = 10 00 04 00 00 00 00 01
    byte 0 = 0x10 → bits 4-5 (pair 2) → code 102  ✓
    byte 2 = 0x04 → bits 2-3 (pair 1) → code 109  ✓
    byte 7 = 0x01 → code 140                      ✓

`bms-124-140-142-143-144-146.asc` — operator-confirmed cycling 124,
140, 142, 143, 144, 145:

    F108 = 00 00 00 00 00 01 00 BB
    byte 5 = 0x01 → bit 0 → code 124
    byte 7 = 0xBB → {140, 142, 143, 144, 145}

Codes 100..127 (bytes 0..5) and 140..145 (byte 7) are merged and
deduplicated by the decoder.

**Latch behavior.** BMS F108 codes track the bitmap in real time —
clearing the bit clears the dash. This is the opposite of the MC's
DM1 channel, which latches DTCs until a key cycle (see FECA section).


### Motor controller (Curtis 1238E, SA 0xCA)

The motor controller is a **Curtis 1238E** AC induction motor
controller (parts catalog Table 60, Ref 4). The MC error code table
reproduced below (codes 12, 22, 36, 41–46, 47, 49, 87–89, 99 ...)
matches the public Curtis 1238 fault-code list one-for-one, so the
Curtis 1238 manual is the authoritative reference for any FF21CA
byte questions not yet resolved here.

The motor controller emits two frames on this bus: FF21CA (motor
telemetry) and FECA (DM1, fault codes). FF21CA is suppressed entirely
while charging — the controller goes silent when traction contactors
(Albright SW200) are open.

#### FF21CA — Motor telemetry — CONFIRMED (RPM, throttle, temp, state)

Broadcast at ~85 Hz. Full 29-bit ID is `0x0CFF21CA` (priority 3, not
the default 6 — higher priority than BMS broadcasts, consistent with a
real-time inverter feed).

| Byte | data[]        | Meaning                                                          |
|------|---------------|------------------------------------------------------------------|
| 1    | data[0]       | Throttle pedal position, raw (0..0xCC observed; SPN 91 candidate) |
| 2    | data[1]       | 0x00 constant — fault-bitmap candidate (UNKNOWN)                  |
| 3..4 | data[2..3] LE | **Motor RPM**: rpm = (le16) − 0x0C80                       |
| 5    | data[4]       | Three-state field 0x28 / 0x3B / 0x3C — startup-calibration related (UNKNOWN) |
| 6    | data[5]       | **Controller temperature**: °C = raw − 40                         |
| 7    | data[6]       | 0x00 constant — fault-bitmap candidate (UNKNOWN)                  |
| 8    | data[7]       | **Packed transmission state** (high nibble = range, low = F/N/R)  |

**Motor RPM.** Little-endian uint16 with bias 0x0C80 (=3200). At
commanded zero, data[2..3] = `80 0C` → 0 RPM. At pegged throttle in
neutral, sweeps to `30..40 16` → ~2480..2496 RPM. The
`accellerate-decelerate.asc` capture shows a textbook 0 → 2500 → 0
ramp matching operator-reported ~2500 RPM at full throttle.

RPM is **magnitude only** — values below 0x0C80 are not emitted even
in reverse. Reverse is signaled separately by data[7]; the 0x0C80 bias
is best understood as a fixed configuration constant, not a
signed-value zero. Form a signed value as `direction × |rpm|` if
needed.

**Throttle pedal position (data[0]).** Consistent with J1939 SPN 91
(Accel Pedal Position 1) at 0.4 %/bit with raw 250 = 100 %:

    0x69 = 42 % (neutral-only captures, max observed)
    0xCC = 82 % (forward, real load)
    0x96 = 60 % (reverse, real load — same pedal hardware)

The F/R ceiling asymmetry strongly suggests a controller-side
reverse-speed limiter applied before the byte goes on the wire. Idle
resting offset ~3 (sensor noise); below raw ~14 the controller's dead
band keeps RPM near 0. True 250-bit full-scale is a J1939-convention
guess pending a "pedal mashed hard in F under load" capture.

**Controller temperature (data[5]).** u8 with the J1939 +40 °C offset
(53 → 13 °C). Near ambient across all captures.

**Packed transmission state (data[7]).**

    high nibble (data[7] >> 4)  = range gear
        0x0 = Range 1
        0x1 = Range 2
        0x2 = Range 3

    low nibble  (data[7] & 0xF) = F/N/R lever
        0x0 = Neutral
        0x4 = Forward
        0x8 = Reverse

Verified by two controlled captures:

- `drive-r-n-f.asc` — operator walks F/N/R lever R → N → F with range
  held at 3, no pedal. data[7] = 0x28 → 0x20 → 0x24. Low nibble walks
  8 → 0 → 4; high nibble pinned at 0x2.
- `range-1-2-3.asc` — operator walks range 1 → 2 → 3 in Forward.
  data[7] = 0x04 → 0x14 → 0x24. High nibble walks 0 → 1 → 2; low
  nibble pinned at 0x4.

**Startup interlock.** data[7] reflects lever position only, not
drivetrain readiness. After power-on the motor controller requires the
F/N/R lever to pass through Neutral before it will accept a drive
direction: the tractor will not move even if the F or R nibble is
present in data[7]. There is no CAN signal that distinguishes this
"not-yet-armed" state from normal operation — the byte is identical in
both cases. Applications must track power-on state independently and
prompt the operator to cycle through Neutral before commanding motion.

**Range → ground speed.** Range 1/2/3 are the L/M/H positions on the
mechanical range gear shift lever (Low/Medium/High; the L-M-N-H lever
also has a Neutral position, which disengages drive entirely — the
electrical bus reports only the three driven positions). The CET
Operator Manual page 34 publishes the full motor-RPM → ground-speed
table for both tire options. The relationship is linear in motor RPM
within each range:

| Range            | km/h per 1000 motor RPM | km/h at 2800 RPM (max) |
|------------------|-------------------------|------------------------|
| 1 (Low, Agri)    | 1.64                    | 4.6                    |
| 2 (Medium, Agri) | 3.14                    | 8.8                    |
| 3 (High, Agri)   | 6.25                    | 17.5                   |
| 1 (Low, Turf)    | 2.04                    | 5.7                    |
| 2 (Medium, Turf) | 3.07                    | 8.6                    |
| 3 (High, Turf)   | 6.07                    | 17.0                   |

"Agri" = 5×12 front / 8.0×18 rear; "Turf" = 23×8.5-12 front /
33×13.5-16.5 rear. The S/N/F switch is a motor-RPM cap (2000/2500/
2800 RPM), not a gear stage — it does not change the ratio, only the
maximum motor RPM and therefore the maximum ground speed within the
selected range.

This resolves the motor → wheel ground-speed derivation without
needing to compute the gear ratio explicitly: read motor RPM from
FF21CA data[3..4], read range from data[7] high nibble, multiply by
the per-range coefficient above.


#### FECA (DM1) — MC fault channel — CONFIRMED via injection

Standard J1939 DM1 broadcast from SA 0xCA. Empty payload in all
recorded captures (`00 00 00 00 00 00 FF FF`) because no MC faults
occurred organically — DM1 is the right channel; it just was never
populated until injection.

| Bytes | Meaning                                                |
|-------|--------------------------------------------------------|
| 0..1  | J1939 lamp/flash bytes                                 |
| 2..4  | **SPN (= displayed MC code number)**                   |
| 5     | FMI / occurrence count                                 |
| 6..7  | 0xFFFF terminator                                      |

Confirmed via `util/mc_inject.py` injecting `0x18FECACA` with
single-DTC payloads:

    SPN 12 (0x0C) → dashboard "MC12"  (Controller Over Current)
    SPN 47 (0x2F) → dashboard "MC47"  (HPD/Sequencing Fault)
    SPN 99 (0x63) → dashboard "MC99"  (Parameter Mismatch)

The cluster prepends "MC" based on source address. A populated DM1
injected from SA 0xF3 (BMS) was **ignored** by the cluster — the
cluster has subsystem-specific decoders rather than a unified DM1
path:

- MC (SA 0xCA): J1939 DM1, SPN = displayed number.
- BMS (SA 0xF3): proprietary F108 bitmap (continuous broadcast).

**Latch quirk.** The cluster latches DM1 DTCs on receipt and does
**not** unlatch when DM1 returns to empty. Standard J1939 prescribes
DTCs going "previously active" after 3 s of frame absence; this
cluster keeps them on screen until a key cycle. When iterating on
injection tests, key-cycle between probes so you can tell whether a
new code came from the new injection or from a stale latch.

FF21CA bytes 1 and 6 (data[1], data[6]) are constant zero and remain
fault-bitmap candidates for non-DM1 status surfacing — injection of
non-zero values into FF21CA byte 7 flashed dashboard lamps but never
produced a numeric code.


### Speed encoder connector

The motor's 2-channel A/B quadrature encoder (no Z pulse, PPR not
yet measured) connects to the MC via a 4-pin IC pigtail. Pin
assignments from the service manual's code-12 and code-36 DTC
troubleshooting procedures:

- **Pins 1 & 4** — 12 V supply (12 V should appear between these pins
  with the pigtail unplugged from the motor, IGN on)
- **Pins 2 & 3** — A and B signal channels (frequency increases
  proportionally with motor speed)

To measure PPR, probe pins 2 and 3 while spinning at a known RPM —
see open questions.


### Charger (SA 0xE5)

#### FF50E5 — Charger telemetry — CONFIRMED (V, A, status)

Proprietary B frame from the on-board charger.

| Byte | data[]  | Meaning                                                |
|------|---------|--------------------------------------------------------|
| 1    | data[0] | **Status / mode**                                      |
| 2..3 | data[1..2] LE | Output voltage: V = raw × 0.1 + 76.8 (only valid while status = 0x03) |
| 4..5 | data[3..4] LE | Output current: A = raw × 0.1     (only valid while status = 0x03) |

Status byte vocabulary:

| Value      | Meaning                                                       |
|------------|---------------------------------------------------------------|
| 0x00       | Idle / not delivering                                         |
| 0x01, 0x02 | Transient handshake (only seen briefly during wake-up / ramp) |
| 0x03       | **Actively delivering charge**                                |

Voltage encoding is identical to F100F3 byte 2 (data[1]) — same scale,
same +76.8 V offset. Anchored against contemporaneous F100F3 readings
in `charging-120V-90ish-to-100.asc`:

- data[1..2] vs Pack_V: slope 0.1024, intercept 77.04, R² = 0.9856 →
  factor 0.1, offset 76.8 V.
- data[3..4] vs |Pack_I|: slope 0.0989, intercept −0.07, R² = 0.9985 →
  0.1 A/bit, no offset.

End-to-end the decoded current showed a textbook CC→CV taper
(~18.5 A → ~9.9 A → ~2.9 A) at constant ~83 V over the capture.

**Don't trust V/I unless status == 0x03.** The charger module beacons
self-test artifacts during wake-up and when the plug is inserted
without AC mains:

- Plug inserted, no AC: status = 0x00, v_raw = 2, i_raw = 2048
  (constant). FF50E5 still beacons at ~10 Hz.
- No charger connected: status briefly cycles 0x00 → 0x01 → 0x02
  during wake-up with nonsensical v/i values for a few frames.

Status = 0x00 means "not actively delivering" — it does **not**
distinguish charger absent / charger present but unpowered / charger
present but BMS-inhibited. Charger-presence detection therefore lives
elsewhere (current best candidate: F108F3 byte 7 maintenance codes
asserting when plug is inserted at 100 % SOC).


### Vehicle controller (SA 0xD0)

#### F100D0 — VC heartbeat — CONFIRMED (byte 0 OPC state)

Same PGN as the BMS pack-status frame, disambiguated by source
address. Broadcast at ~40 Hz.

| byte 0 | Meaning                          |
|--------|----------------------------------|
| 0x00   | Operator unseated / OPC cut off  |
| 0x0C   | Operator seated / OPC enabled    |

Byte 0 is the authoritative OPC (Operator Presence Control) state on
the CAN bus. The transition is a single clean step in both directions,
confirmed across three captures (`otp-seatedon-unseatedoff.asc`,
`otp-unseatedoff-seatedon.asc`, `otp-bouncing-5s.asc`). Other bytes
remain 0xFF and have not been decoded.

**OPC timer.** The VC does not trip instantly when the operator leaves
the seat — there is a hardware grace timer (the OPC timer module; see
below). The service manual specifies the timer as **7 s** (§Tractor
Controls SOP).

**Dashboard wrench indicator.** A blinking wrench appears on the
cluster immediately when the operator leaves the seat, even before the
OPC timer fires and byte 0 transitions. The wrench is therefore driven
by a discrete seat-switch input directly to a cluster pin, not by the
CAN OPC state. This is consistent with schematic 5.9, which wires the
seat switch through discrete signals.

**OPC shutdown sequence** (from `otp-seatedon-unseatedoff.asc`,
relative to OPC trip):

```
t+0ms     18F100D0 b0:  0x0C → 0x00   VC declares operator absent
t+354ms   0CFF21CA:     last motor frame (motor controller goes silent)
t+361ms   18F100D0:     last VC frame (VC goes silent)
t+10.4s   18F108F3:     BMS fires codes 124, 140, 142, 143, 144, 145
t+41.7s   18F108F3:     BMS stops broadcasting (end of capture)
```

The BMS fault codes that fire ~10 s after shutdown are **not real
faults** — they are the BMS reacting to CAN silence after the rest of
the bus goes dark. Code 124 ("Clock fault") and the maintenance codes
(140/142/143/144/145) appear because the BMS loses contact with the
other nodes and interprets the silence as communication errors. The BMS
is the last node still broadcasting, talking to a dead bus.

SA 0xF4 also acts as a vehicle-side requester (sends 1806E5F4 →
charger 0xE5). Whether 0xF4 is a separate physical module or a logical
address inside another ECU's firmware is open.

The parts catalog (Table 65, Ref 10) names a separate **OPC (Operator
Presence Control) timer module** — "UNIT ENGINE SHUT OFF CONTROLLER
TIMER (SEAT AND PARK OPC)" — gating shutdown on the seat switch and
park brake. The F100D0 heartbeat's OPC-state byte is consistent with
this module broadcasting its interlock status on CAN.

**Tension with service manual schematic 5.10.** That schematic shows
the main CAN bus carrying exactly four nodes (MC, BMS, Charger,
Cluster) with no OPC module drawn. If 0xD0 (and/or 0xF4) frames are
real, three possibilities:

1. The schematic omits the OPC module — it is a fifth physical CAN
   node not drawn but present on the harness.
2. 0xD0 / 0xF4 are *logical* source addresses emitted by one of the
   four documented nodes (cluster is the natural candidate — it
   aggregates accessory state).
3. The frames are bridged from the BMS debug CAN by the BMS firmware.

Service manual schematic **5.9 (Seat OPC)** wires the OPC entirely
through discrete signals (CSS, DIS, CT, charge-drive interlock relay,
seat switch, PTO bypass, park-brake switch) — no CAN H/L on the OPC
connector. That favors option (2) or (3) over (1): the OPC module
documented in the schematic is wired discretely, not on CAN. The
parts-catalog OPC-timer-module identification may be a different
component, or may itself be a misidentification. The SA 0xD0 home is
therefore unresolved and warrants a fresh look at the captures with
the 4-node bus constraint in mind.


## Instrument cluster hardware

The dashboard cluster is a **COBO ECO MATRIX VT3** (Italian Tier-1
off-highway cluster, also marketed under COBO's "Unideck" sub-brand).

### Identification

| Property                          | Value                       |
|-----------------------------------|-----------------------------|
| Manufacturer                      | COBO S.p.A. (Leno, Brescia) |
| Family                            | ECO MATRIX VT3              |
| Platform                          | ECO HW UNICO VT3            |
| COBO internal part number         | 2050394                     |
| Solectrac OEM part number         | 2167780 REV.04              |
| Software revision                 | 102                         |
| Year of manufacture               | 2022 (Solectrac label 2021-08-23) |
| Display                           | 128 × 64 dot-matrix LCD + 2 cross-coil gauges + 21 LEDs |
| Housing                           | 230 × 120 mm                |
| Supply                            | 12 V (accessory)            |
| Protocols                         | CAN J1939 / ISOBUS          |
| Internal CAN termination          | None (mid-bus tap)          |
| Supported baud                    | 125 / 250 / 500 kbaud       |

Symbol layout and warning-light assignments (L1..L21) are
Solectrac-specific firmware loaded via COBO's VT3 WYSIWYG tool. A
generic ECO MATRIX VT3 unit sourced from another OEM (e.g. the Faresin
12 V variant sold by si-parts.com) would need reflashing before it
would behave correctly on the Solectrac harness.

### Connector

| Component                          | Part number          |
|------------------------------------|----------------------|
| Header on cluster                  | Tyco AMP 36-way (4 cols × 9 rows) |
| Mating connector (harness side)    | Tyco / TE 1-0640526-0 |
| Terminals                          | Tyco / TE 0-0641294-1 |

Cavity numbering is **row-major, left-to-right** (J1 = row 1 col 1,
J4 = row 1 col 4, J5 = row 2 col 1, ..., J36 = row 9 col 4). This was
inferred from the COBO datasheet J-table cross-referenced with the
Solectrac harness wiring diagram, and confirmed empirically by DMM
probing on 2026-05-13.

Observed population on this unit (15 of 36 cavities, viewed from the
harness mating face):

    col→  1 2 3 4
    row 1 . x x x      J1 empty;  J2,  J3,  J4  populated
    row 2 . x x x      J5 empty;  J6,  J7,  J8  populated
    row 3 . x . x      J9 empty;  J10 pop; J11 empty; J12 pop
    row 4 . x . .      J13 empty; J14 pop; J15, J16 empty
    row 5 x x . x      J17 pop;   J18 pop; J19 empty; J20 pop
    row 6 . . x .      J21, J22 empty; J23 pop; J24 empty
    row 7 . . . .      J25..J28 all empty
    row 8 . . . .      J29..J32 all empty
    row 9 . . x x      J33, J34 empty; J35, J36 populated

### Pinout

★ = minimum pins required for a powered cluster on CAN (J3, J4, J8,
J35, J36). Solectrac populates 15 cavities total: the five required
plus 10 discrete-input wires.

`(r,c)` = (row, col) on the populated-grid diagram above.

| Pin | (r,c) | COBO ID | Generic function                                    | Solectrac usage                |
|-----|-------|---------|-----------------------------------------------------|--------------------------------|
| J1  | 1,1   | RELE'   | Out 1 high-side, 150 mA (relay drive)               | (unused)                       |
| J2  | 1,2   | IDBL    | Positive digital input                              | BACK LIGHT (+)                 |
| J3  | 1,3   | 30      | + Battery (constant 12 V)                       ★   | + BATTERY                      |
| J4  | 1,4   | 15      | + Key (ignition / KL15)                         ★   | IGN ON (+)                     |
| J5  | 2,1   | FR1     | Frequency input, ≤1500 Hz                           | (unused — speed via CAN)       |
| J6  | 2,2   | ID9     | Positive digital input                              | TURN RIGHT (+)                 |
| J7  | 2,3   | ID10    | Positive digital input                              | TURN LEFT (+)                  |
| J8  | 2,4   | 31      | GND                                             ★   | GND                            |
| J9  | 3,1   | ID3     | Negative digital input                              | (unused)                       |
| J10 | 3,2   | ID1     | Negative digital input                              | 4WD (−) — forward indicator    |
| J11 | 3,3   | ID5     | Negative digital input                              | (unused)                       |
| J12 | 3,4   | ID20    | Positive digital input                              | TURN TRAILER (+)               |
| J13 | 4,1   | ID2     | Negative digital input                              | (unused)                       |
| J14 | 4,2   | ID8     | Positive digital input                              | HEADLIGHTS (+)                 |
| J15 | 4,3   | AN2     | Analog resistive input, 90 Ω pull-up (sender)       | (unused)                       |
| J16 | 4,4   | AN1     | Analog resistive input, 90 Ω pull-up (sender)       | (unused)                       |
| J17 | 5,1   | ID12    | Positive digital input                              | RUNNING LIGHTS (+)             |
| J18 | 5,2   | ID13    | Negative digital input                              | PTO (−)                        |
| J19 | 5,3   | P/BR    | Positive digital input (probable Park Brake)        | (unused)                       |
| J20 | 5,4   | ID17    | Positive digital input                              | BATTERY CHARGING (+)           |
| J21 | 6,1   | ID6     | Positive digital input                              | (unused)                       |
| J22 | 6,2   | ID21    | Negative digital input                              | (unused)                       |
| J23 | 6,3   | ID18    | Negative digital input                              | PARKING BRAKE (−)              |
| J24 | 6,4   | ID16    | Positive digital input                              | (unused)                       |
| J25 | 7,1   | PB/L    | Positive digital input (probable Park Brake Light)  | (unused)                       |
| J26 | 7,2   | ID15    | Negative digital input                              | (unused)                       |
| J27 | 7,3   | ID19    | Negative digital input                              | (unused)                       |
| J28 | 7,4   | ID14    | Negative digital input                              | (unused)                       |
| J29 | 8,1   | ID11    | Negative digital input                              | (unused)                       |
| J30 | 8,2   | ID7     | Positive digital input                              | (unused)                       |
| J31 | 8,3   | ID4     | Negative digital input                              | (unused)                       |
| J32 | 8,4   | BUZZER  | Out 2 low-side, 150 mA (audible alert)              | (unused)                       |
| J33 | 9,1   | D+      | D+ alternator excite, neg. digital input            | (unused — no alternator)       |
| J34 | 9,2   | CS      | CAN shield                                      ★   | (unused — no shield drain)     |
| J35 | 9,3   | CL      | CAN L                                           ★   | CAN L                          |
| J36 | 9,4   | CH      | CAN H                                           ★   | CAN H                          |

### Diagram errata

The Solectrac harness wiring diagram has three labelling issues and
one omission relative to the as-built tractor:

1. The + BATTERY pin is labelled "Pin 1" on the diagram. The actual
   cavity is J3 (J1 is an empty cavity in the populated grid).
2. J14 is labelled "DIPPED BEAM (+)". Solectrac uses it as the general
   HEADLIGHTS indicator. ("Dipped beam" is the EU term for low-beam
   headlights.)
3. J18 is populated but not on the diagram. Identified as PTO
   indicator (−), switch-to-ground when PTO is engaged.

### Diagnostic tap

A non-destructive diagnostic harness can T-tap J35/J36 (row 9 cols
3-4) without unplugging the cluster — the display stays functional
while a capture tool reads the live bus.


## Vendor error code tables

Reproduced from the operator manual for cross-reference. Detecting
conditions in parentheses are from the service manual DTC
troubleshooting section; codes without a parenthetical have no
explicit threshold data in this corpus. The disambiguation in the
F108F3 and DM1 sections above maps these numbers to bit positions and
SPN values respectively. The two ranges do not overlap, so a dashboard
"code 47" is unambiguously MC and "code 124" is unambiguously BMS.

### BMS codes (100..146)

    100  SOC is too high                   (pack V > 84 V)
    101  SOC is too low                    (SOC ≤ 15 %; pack V < 60 V)
    102  Total voltage is too high         (pack V > 84 V)
    103  Total voltage is too low          (SOC ≤ 15 %; pack V < 60 V)
    104  Charge current fault              (charge I differs from programmed)
    105  Discharge current fault           (discharge I differs from programmed)
    106  Battery temperature is too low    (cell temp < −10 °C)
    107  Battery temperature is too high   (cell temp > 54 °C)
    108  Battery under voltage             (SOC ≤ 15 %; pack V < 60 V)
    109  Battery over voltage              (pack V > 84 V)
    110  Battery temperature unbalance
    111  Battery voltage unbalance
    112  The battery does not match
    113  The temperature of the output pole is too high
    [114, 115 not in manual — reserved]
    116  The parameters of memory fault
    117  Data memory fault
    118  Cell voltage detection fault
    119  Temperature detection fault
    120  Current detection fault
    121  Internal total voltage detection fault
    122  External total voltage detection fault
    123  Insulation monitoring fault
    124  Clock fault
    125  Internal CAN communication fault
    126  Serious insulation fault
    127  Slight insulation fault
    [128..139 not in manual — reserved]
    140  System fault level
    [141 not in manual — reserved]
    142  BMS fault need maintenance
    143  Battery fault need maintenance
    144  Battery system fault needs maintenance
    145  The battery needs to maintenance (full charging and full discharging)
    146  Maintenance mode status

32 codes; F108 has 64 bits, so the layout has plenty of headroom.

### MC codes (12..99)

    12  Controller Over Current          (current > limit or phase short; motor phase R < 9 mΩ)
    13  Current Sensor Fault             (sensor reading invalid or absent)
    15  Controller Severe Undertemp      (controller temp < −10 °C)
    16  Controller Severe Overtemp       (controller temp > 75 °C)
    17  Severe B+ Undervoltage           (B+ input < 62 V)
    18  Severe B+ Overvoltage            (regen pushes pack > 84 V)
    18  Severe KSI Overvoltage           (KSI pin > 84 V) [duplicate S.No. 18]
    22  Controller Over temp Cutback     (controller temp > 60 °C; cutback, not shutdown)
    23  B+ Undervoltage Cutback
    24  B+ Overvoltage Cutback           (pack > 84 V)
    25  +5V Supply Failure               (pin 26 load impedance too low)
    28  Motor Temp Hot Cutback           (motor temp > 125 °C)
    29  Motor Temp Sensor Fault
    31  Coil1 Driver Open/Short          (contactor coil; 150 Ω at J1-6↔J1-13)
    31  Main Open/Short                  [duplicate S.No. 31]
    32  Coil2 Driver Open/Short
    32  EM Brake Open/Short              [duplicate S.No. 32]
    36  Encoder Fault                    (signal invalid; 12 V on pins 1&4, signal on pins 2&3)
    36  Sin/Cos Sensor Fault             [duplicate S.No. 36]
    37  Motor Open                       (phase open; motor phase R < 9 mΩ)
    38  Main Contactor Welded            (won't open after IGN off)
    39  Main Contactor Did Not Close     (didn't close at startup)
    41  Throttle Wiper High
    42  Throttle Wiper Low
    43  Pot2 Wiper High
    44  Pot2 Wiper Low
    45  Pot Low Over Current
    46  EEPROM Failure
    47  HPD/Sequencing Fault
    49  Parameter Change Fault
    51  Vehicle lock without applying hand brake   [out of order in manual]
    72  PDO Timeout
    73  Stall Detected
    83  Driver Supply
    87  Motor Characterization Fault
    88  Encoder Pulse Count Fault
    89  Motor Type Fault
    92  EM Brake failed to set
    99  Parameter Mismatch

39 entries, 35 distinct S.No. values. Codes 18, 31, 32, 36 each have
two definitions sharing a number — a numeric code on the dashboard
does not uniquely identify the underlying fault for those four;
disambiguation needs additional context (which subsystem is implicated
by other simultaneous symptoms, vendor service-tool readout, etc.).
Code 51 is listed out of numeric order in the manual.


## Open questions

- **F108 bytes 0..6 mapping below the byte-7 line.** Bytes 0..5
  bit-to-code mapping is laid out (see F108F3 section) but
  bit-to-code positions for bytes 0..3 in *every* slot have not been
  spot-checked against live captures. `bms-fullcharge-102-109-140.asc`
  confirms bytes 0 and 2 carry fault info; bytes 1, 3, 4, 6 have not
  been seen nonzero. Single-code captures would replicate the
  popcount-matches-displayed argument that nailed byte 7
  unambiguously.
- **SOC linearity below 80 %.** F100F3 data[4] is calibrated against
  two direct screen readings at 80 % and 90 %, both still in the top
  ~20 % of the range. A sustained discharge capture down to a lower
  known SOC would confirm whether the field is truly linear or only
  locally linear near full.
- **SOH confirmation.** F100F3 data[5] = 0xFA (250 raw × 0.4 %/bit =
  100 %) is the leading candidate — the only byte across 42 captures
  that is both constant everywhere and decodes to 100 % under a
  plausible scaling. See the F100F3 section. Promoting from leading
  candidate to CONFIRMED needs a capture where SOH differs from 100 %
  (older firmware/pack, or an injected spoof watched on the vendor
  GUI).
- **KL15 / wake-up status bit.** Vendor GUI shows "Wake-up signal:
  KL15" — implying an ignition-status bit lives somewhere in the BMS
  broadcasts. Not yet identified.
- **F104 byte-level decode.** Pack temperature min/max summary,
  analogous to F102 for cells. Not parsed.
- **F106 / F107 byte-level decode.** Running-mode vocabulary and
  limits-frame layout are partially mapped but most bytes UNKNOWN.
- **Full running-mode enumeration.** Vendor GUI implies at least
  Calibrating, Charging, Discharging, Fault, Sleep beyond the
  init/standby/ready states observed.
- **Second 2-pin CAN port — most likely BMS debug.** Schematic 5.7
  shows the BMS exposing two CAN pairs: the main bus (CAN_H3/CAN_L3
  on internal pins H/J, field-connector pins D/E) and a debug pair
  (`CANDE-H`/`CANDE-L` on internal pins F/G) labeled "TO BMS DEBUG
  CONNECTOR PIN-1/PIN-2". 120 Ω across the 2-pin pair (key-off,
  nothing plugged in) electrically confirms it is a separate,
  single-terminated bus — see the "Second 2-pin CAN port" section.
  What remains open is *what protocol* runs on it; confirmation =
  tap the connector and see if traffic resembles BMS-internal
  diagnostic chatter. The previously-suspected "Kelly KLS hydraulic
  CAN" interpretation is ruled out by schematic 5.11, which shows the
  e-hydraulic controller has no CAN pins at all.
- **UDAAN tool — identified, downloadable; one practical blocker.**
  UDAAN is the **UDAN iBMS Upper Utility** from Anhui UDAN Technology
  Co., Ltd. (Chinese BMS firmware/tool vendor; the physical pack is
  Soundon, see "Vehicle and pack" above). Windows software,
  CAN @ 250 kbit/s (matches our bus),
  supports cheap CANalyst-II / PCAN / USBCAN dongles. V3.1 manual is
  in `docs/UDAN_iBMS_Upper_Utility_v3.1_manual.pdf`. Download portal:
  `https://www.ievcloud.com/burner_en.html`. Practical decode path:
  install the tool + a CANalyst-II dongle, tap the OBD-II port (pin 6
  CAN_H, pin 14 CAN_L), enable both the `Comm. Message` checkbox (raw
  CAN log) and the `Data Storage` checkbox (Excel of every System
  Overview UI field every 2 s), and operate the tractor through known
  states. Cross-referencing the two logs produces an empirical DBC for
  every BMS broadcast frame — closing out F100/F102/F104/F107/
  F113..F117 byte-decoding without vendor cooperation. The one
  practical blocker: read/write features require login, and the
  manual doesn't document credential acquisition. View-only mode may
  expose the System Overview screen without login; if not, request
  access via Solectrac/Farmtrac support citing the service-manual
  references to UDAAN by name.
- **FF21CA byte 1, 4, 6 semantics.** data[1] and data[6] are
  constant-zero fault-bitmap candidates; data[4] is a three-state
  field changing near startup calibration.
- **SA 0x12 role.** Emits a constant FF21 payload
  `01 00 00 00 00 00 00 00`. Distinct from FF21CA from 0xCA despite
  sharing a PGN.
- **OPC timer duration — CONFIRMED 7 s** per service manual §Tractor
  Controls SOP; VC heartbeat section updated.
- **SA 0xD0 and 0xF4 physical home.** Schematic 5.10 only documents
  four CAN nodes (MC, BMS, Charger, Cluster). The OPC module shown on
  schematic 5.9 is wired entirely discretely (no CAN). So either the
  schematic omits one or more physical nodes, or 0xD0 / 0xF4 are
  logical SAs emitted by one of the four documented ECUs (cluster is
  the natural candidate). Worth re-examining the captures with the
  4-node constraint as the prior.
- **True throttle full-scale.** FF21CA data[0] = 0xCC = 204 observed
  in forward under real load; J1939 SPN 91 convention is raw 250 =
  100 % but not yet ground-truth. A "pedal mashed hard in F under
  load" capture would settle it.
- **Motor encoder PPR.** Not documented in any manual (service manual,
  CET operator manual, parts catalog, Curtis 1238 manual). The encoder
  connector pinout is now known — signal on pins 2 & 3, supply on
  pins 1 & 4 (see "Speed encoder connector" in the MC section) — so
  tapping the pigtail is straightforward. The Curtis parameter name
  for PPR is **"Encoder Steps"** (from the code-88 DTC description);
  readable directly with a Curtis 1313 programmer. Alternatively,
  spin the motor at a known RPM and count pulses on pins 2 and 3. **Motor → wheel ground-speed conversion is
  resolved** via the operator manual's travel-speed table — see
  "Range → ground speed" in the MC section.


## Sources

- COBO ECO MATRIX VT3 datasheet:
  https://www.si-parts.com/cataloghi_cobo/display-quadri-bordo/ECO_MATRIX_VT3.pdf
- COBO product page (Faresin 12 V variant):
  https://www.si-parts.com/en/instruments-clusters/13181-eco-matrix-faresin-12v-panel.html
- COBO Group corporate page: https://www.cobogroup.net/
- COBO USA distribution: https://www.cobointernational.com/
- "BMS Update" document:
  https://docs.thebackyard.engineer/solectrac/troubleshooting-guides/documentation
- Solectrac master schematic set (harness wiring + CAN topology
  diagrams; harness sheet has the three labelling errata documented
  above): https://solectracsupport.com/support/manuals
- **FT 25G service manual** (319 pages, dated 2023-07-13) — the
  primary electrical/CAN authority for this vehicle:
  https://solectracsupport.com/FT_25G_Service_manual-10-08-2023.pdf
  Key section anchors:
  - §**Cluster & Dashboard Switches** (p7) — cluster indicators, mode
    switches, RPM caps per S/N/F (S<2000, N<2500, F<2800, R<2240).
  - §**Battery** (p20) — pack nameplate (73 V / 350 Ah / 20S4P / 25.5
    kWh NMC, cell `SEPNI-8688190P-17.5AH-5P`); BMS connector
    `RT061412SNHEC03` 12-pin; per-DTC troubleshooting (CAN on field
    pins D/E confirmed via 60 Ω test in DTC 125, page 30).
  - §**Motor** (p60) — 15 kW AC induction, 90 Nm, 2800 RPM, 200 A
    controller, "KEC" vendor hint, 2-channel A/B quadrature encoder
    (no Z pulse, PPR not stated).
  - §**E-Box** (p73) — Curtis controller named explicitly, Albright
    SW200 main contactor, FT25G vs FT25G HST connector differences.
  - §**Schematics** (p168) — internal index 5.1..5.16. **5.10 CAN
    Connection** is the authoritative bus topology (4 nodes); **5.11
    E-Hydraulic Controller** proves the e-hydraulic has zero CAN
    pins; **5.7 BMS** shows the `CANDE-H/L` debug pair; **5.8
    Diagnostic Connector** confirms standard OBD-II HS-CAN pinout
    (pin 6 CAN_H, pin 14 CAN_L).
  - §**Error Code** (p187) — Motor Controller fault table (J1939 SPN
    + Curtis-internal short code 12..99) and BMS fault table (codes
    100..146, all shared Message ID `0x18F108F3` = priority 6, PGN
    F108, SA 0xF3).
  - §**Hydraulic System** (p295) — confirms lift / 3-point / remotes
    are fully mechanical with zero electrical interface.
- **CET Operator Manual** (63 pages, the international "Compact
  Electric Tractor" rebadge of the FT 25G):
  https://solectracsupport.com/FT25GUSAOPM.pdf
  The PDF has no text layer (CorelDRAW vector export), so text search
  doesn't work; read by page number. Key sections:
  - p20–21 PTO ratios: rear 540 PTO at 2504 motor RPM; rear 540E at
    2035 motor RPM. (Manual also lists a mid-PTO option at 2200 RPM
    / 2410 motor RPM — not fitted on this tractor.)
  - p33 spec table — three-range constant-mesh transmission with
    L/M/N/H lever, "Bull Gear" rear-axle reduction. Note: this table
    lists max motor torque as **84 Nm** vs the FT 25G service
    manual's 90 Nm — small discrepancy, likely rebadge nameplate
    variation.
  - p34 Travel-speed table — full motor-RPM → ground-speed table for
    both tire options, the source for the "Range → ground speed"
    section above.
- **UDAAN = UDAN iBMS Upper Utility** from Anhui UDAN Technology
  Co., Ltd. — Chinese BMS firmware/diagnostic-tool vendor. (The
  physical pack on this tractor is manufactured by Soundon New
  Energy Technology Co., Ltd.; see "Vehicle and pack".) Public download:
  `https://www.ievcloud.com/burner_en.html`. Corporate site:
  `https://www.udantech.com/en/`. V3.1 user manual (35 pages, dated
  2023-10-07) is in this repo at
  `docs/UDAN_iBMS_Upper_Utility_v3.1_manual.pdf`. CAN @ 250 kbit/s,
  supports CANalyst-II / PCAN / USBCAN dongles. `Comm. Message` +
  `Data Storage` recording features produce a time-aligned raw-CAN +
  labeled-UI-field log — effectively an empirical DBC for the BMS
  broadcast frames. Login required for full read/write; view-only
  mode permitted without login.
- Solectrac Parts Catalog (e25):
  https://docs.thebackyard.engineer/solectrac/troubleshooting-guides/documentation
  — source for the named components referenced throughout this
  document: Curtis 1238E MC (Table 60), Kelly KLS7212M/KLS7218
  hydraulic controller (Table 46), Albright SW200 main contactor +
  discrete hydraulic contactor + 350 A cut-off fuse (Table 60),
  500 W DC-DC converter + 12 V/20 Ah accessory battery + OPC timer
  module + motor encoder pigtail (Table 65), 72 V 300 Ah pack box
  (Table 61), "FARMTRAC" key ignition (Table 67). Note: the Kelly
  identification predates the FT 25G service manual; schematic 5.11
  shows the e-hydraulic controller has no CAN pins regardless of
  which controller it actually is.
- Curtis 1238 controller manual — public fault-code-list reference
  for the MC short codes reproduced above.
- Kelly KLS7218MC / KLS7218NC CAN protocol:
  `docs/COMPAGE DOCUMENT KLS7218MC & KLS718NC FORMAT.pdf` — kept for
  reference, but the e-hydraulic controller on this vehicle is not
  wired to a CAN bus, so the Kelly protocol is not applicable here.
