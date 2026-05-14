# Solectrac CAN bus — system documentation

Reverse-engineered protocol and hardware documentation for a Solectrac
electric tractor (~70 V class). All decode information is derived from
captured CAN traffic, vendor manual tables, the COBO cluster datasheet,
the "BMS Update" document, the Solectrac Parts Catalog (e25), and live
injection tests on the tractor.

Confidence markers used throughout:

- **CONFIRMED** — verified by injection, cross-validation, or operator
  ground truth.
- **TENTATIVE** — single-source or partial evidence; encoding plausible
  but not nailed.
- **UNKNOWN** — observed but not decoded.


## Contents

- [Vehicle and pack](#vehicle-and-pack)
- [CAN bus topology](#can-bus-topology)
- [Source-address map](#source-address-map)
- [BMS (SA 0xF3)](#bms-sa-0xf3)
- [Motor controller (SA 0xCA)](#motor-controller-sa-0xca)
- [Charger (SA 0xE5)](#charger-sa-0xe5)
- [Vehicle controller (SA 0xD0)](#vehicle-controller-sa-0xd0)
- [Error code system](#error-code-system)
- [Instrument cluster hardware](#instrument-cluster-hardware)
- [Vendor error code tables](#vendor-error-code-tables)
- [Open questions](#open-questions)


## Vehicle and pack

The tractor is a **Solectrac 25G** (non-HST variant).

"Pack" refers to the tractor's traction battery — the high-voltage
lithium-ion battery that powers the motor, as distinct from the 12 V
accessory battery that runs the cluster and lights.

| Property                  | Service manual              | Vendor BMS GUI       |
|---------------------------|-----------------------------|----------------------|
| Pack chemistry            | NMC                         | "NiCoMn"             |
| Cell P/N                  | `SEPNI-8688190P-17.5AH-5P`  | —                    |
| Cells in series           | 20 (one per module)         | 20                   |
| Cells in parallel         | 4 modules × 5P1S = 20P      | "~20 cells in parallel" |
| Series × parallel         | **20S4P at module level**   | "20 series"          |
| Temperature probes        | not stated                  | 7 active (rest of F155.. channels are padding) |
| Nominal pack voltage      | **73.0 V**                  | 72.0 V               |
| Voltage operating range   | 60–84 V                     | —                    |
| Rated capacity            | **350 Ah**                  | 300 Ah               |
| Rated energy              | 25.5 kWh @ 23 ± 2 °C        | —                    |
| Rated discharge           | —                           | 300 A                |
| Rated charge (DC / AC)    | —                           | 78 A / 39 A          |
| Pack vendor               | "Escorts Solution"          | (BMS GUI footer "ESCORTS-INTERNAL") |
| Main HV fuse              | 350 A                       | —                    |
| Bus baud                  | 250 kbaud (J1939 default)   | Confirmed (capture)  |
| Cluster supply            | 12 V (accessory, not pack)  | Hardware             |

The 73 V / 350 Ah service-manual nameplate and the 72 V / 300 Ah BMS
GUI readout disagree by ~17 % on capacity. The GUI may reflect a
derated/usable capacity (top + bottom buffer excluded), or the unit
shipped with different nameplate parameters than the spec table —
either is plausible. The scripts' Wh display in `solectrac-analyze.py`
and `solectrac-stream.py` is calibrated against the GUI-reported
SOC, so it continues to use the 72 V × 300 Ah = 21.6 kWh basis for
back-compatibility with prior captures; the manual values are
documented here for nameplate accuracy.

Escorts Kubota is the corporate parent of Farmtrac; the BMS-GUI
"ESCORTS-INTERNAL" footer matches the manual's "Escorts Solution"
pack-vendor attribution. The service manual's BMS troubleshooting
section delegates all live-data inspection to a host-side application
called **UDAAN** (referenced repeatedly: "Connect UDAAN and check the
minimum cell voltage", etc.). UDAAN has been identified as the
**UDAN iBMS Upper Utility** from **Anhui UDAN Technology Co., Ltd.**
— a Chinese BMS vendor. The Solectrac/Farmtrac pack is rebadged UDAN
hardware. The tool is publicly downloadable Windows software (CAN @
250 kbit/s, supports cheap CANalyst-II / PCAN / USBCAN dongles); the
V3.1 user manual is in this repo at
`docs/UDAN_iBMS_Upper_Utility_v3.1_manual.pdf`. The service manual
itself contains no byte-level payload tables for the BMS broadcast
frames, but UDAN's tool has a `Comm. Message` recording feature that
captures the raw CAN exchange alongside a labeled Excel export of
every System Overview UI field — running both simultaneously produces
a time-aligned raw-CAN + labeled-field log, i.e. an empirical DBC.
See the open-questions section for the practical decode path.

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

Schematic 5.7 uses BMS-internal terminal letters that do **not** map
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


## CAN bus topology

Single shared CAN bus at 250 kbaud. The diagnostic port we capture from
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
not an extra node. Per schematic 5.8, it follows standard OBD-II
HS-CAN pinout: pin 4 ground, pin 5 chassis ground, **pin 6 CAN_H**
(yellow 0.75 mm²), **pin 14 CAN_L** (green 0.75 mm²), pin 16 +12 V
battery. The older Solectrac topology diagram's "DB9" connector label
is a mislabel — it's the OBD-II port.

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

Cable spec from the schematic: twisted pair, 33 ± 2 twists per metre.

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

Bus measures **30 Ω** across J35/J36 at the cluster (key off, all
nodes connected) versus the textbook **60 Ω** for two 120 Ω
terminators in parallel — four 120 Ω resistors in parallel, two
beyond what the schematic draws. Drivers tolerate it and captures are
clean. The extra terminators are almost certainly inside the BMS and
the Charger (both ECUs commonly ship with internal termination);
unplug-and-measure with each node removed would localize them
precisely.

### Second 2-pin CAN port

A separate 2-pin connector on the tractor remains un-tapped. The
leading hypothesis is that it carries the **BMS debug CAN** pair shown
on schematic 5.7 as `CANDE-H` / `CANDE-L`, explicitly labelled "TO
BMS DEBUG CONNECTOR PIN-1 / PIN-2". The BMS thus exposes two CAN
pairs: the main vehicle bus (above) and this debug pair. If tapped,
expect BMS-internal diagnostic chatter — likely the same protocol that
the host-side **UDAAN** tool consumes. This is no longer believed to
be a hydraulic bus.

### Identified components (from Parts Catalog e25)

The Solectrac parts catalog names the major electrical assemblies. This
turns several previously-anonymous "drive ECU" / "hydraulic node"
references into specific parts with public datasheets and fault-code
references.

| Catalog table | Part                                                    | Role                              |
|---------------|---------------------------------------------------------|-----------------------------------|
| Table 60      | Curtis controller (1238E per catalog; nameplate not verified) | Traction inverter (SA 0xCA); service manual confirms "Curtis controller, 200 A" without giving the model number |
| Table 60      | Albright SW200 main contactor                           | HV pack ↔ inverter contactor; service manual §4.2.3 names the SW200 explicitly |
| Table 60      | Hydraulic contactor (discrete; separate from main SW200)| HV pack ↔ hydraulic pump motor    |
| Table 60      | 350 A battery cut-off fuse                              | HV pack main fuse (service manual §4.3.5; catalog shows 355 A — same component, rounding) |
| Table 46      | Kelly KLS7212M / KLS7218 hydraulic motor controller     | E-Hydraulic node, **not on the CAN bus** — schematic 5.11 shows the e-hydraulic controller's interface is entirely discrete (speed switch, on/off, throttle pot, Hall encoder). Either the Kelly's CAN port is unwired or the catalog refers to a different controller variant |
| Table 65      | 500 W DC-DC converter (72 V → 12 V)                     | Accessory rail supply             |
| Table 65      | 12 V 20 Ah accessory battery                            | Accessory rail backup / cluster   |
| Table 65      | OPC (Operator Presence Control) timer module — "UNIT ENGINE SHUT OFF CONTROLLER TIMER (SEAT AND PARK OPC)" | Seat-switch + park-brake gating. Not shown as a CAN node on schematic 5.10 — see SA 0xD0 discussion |
| Table 65      | Motor encoder pigtail                                   | Curtis speed/position feedback. 2-channel A/B quadrature, no Z pulse (service manual motor section); PPR is not documented |
| Table 67      | "FARMTRAC" key ignition lock                            | Confirms Farmtrac (Escorts Kubota) chassis lineage — matches BMS GUI "ESCORTS-INTERNAL" footer and service manual "Escorts Solution" pack attribution |
| Table 61      | 72 V 300 Ah traction battery box                        | Matches vendor-GUI pack spec; service manual nameplates the pack at 73 V / 350 Ah / 25.5 kWh — see Vehicle and pack section for the discrepancy |

The discrete hydraulic contactor and the no-CAN finding from
schematic 5.11 jointly explain why hydraulic activity produces no
signature on this bus: the hydraulic subsystem is gated on/off by a
coil energized from the main E-Controller's key-switch wire, and the
e-hydraulic controller that drives the BLDC pump motor has no CAN
interface at all.


## Source-address map

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
| 0x41 | Address-claim type frame              | Few frames; not analyzed                                     |


## BMS (SA 0xF3)

All scalings derived empirically. Byte numbering is 1-based with
explicit `data[N]` (0-based) annotations where helpful.

### F113..F13C — Per-cell voltages — CONFIRMED

8 bytes = 4 × big-endian uint16, millivolts.

    F113 = cells  0.. 3
    F114 = cells  4.. 7
    ...
    F117 = cells 16..19
    F118..F13C reserved (cells 20..167); 0xFFFF / 0 sentinel on this pack.

Cells read ~3.6–3.7 V at ~40 % SOC, ~4.16 V/cell at 100 %.

### F155..F15E — Module temperatures — CONFIRMED

8 bytes = 8 × uint8 with J1939 +40 °C offset (raw 53 = 13 °C).

    F155 = channels 0..7
    F156 = channels 8..15
    ...
    F15E = channels 72..79

Only the first 7 channels are populated on this pack; the rest are
0xFF (not present).

### F102F3 — Cell min/max summary — CONFIRMED (max/min/spread)

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

### F100F3 — Pack status — CONFIRMED (voltage, current); TENTATIVE (SOC)

| Byte | data[]  | Meaning                                                     |
|------|---------|-------------------------------------------------------------|
| 1    | data[0] | 0x03 constant                                               |
| 2    | data[1] | **Pack terminal voltage**: V = raw × 0.1 + 76.8             |
| 3..4 | data[2..3] BE | **Signed pack current**: A = (be16 − 0x7D00) × 0.1    |
| 5    | data[4] | **BMS-published SOC** (TENTATIVE; calibrated top 10 % only) |
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

    SOC % = data[4] × 0.385 + 3.8

calibrated on (raw 224, 90 %) and (raw 250, 100 %) from
`charging-120V-90ish-to-100.asc`, saturating at raw 250 in
`soc-100-idle.asc`. Only the top 10 % of the range has been observed;
LSB and below-90 % linearity want a deeper-discharge capture.

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

### F104F3 — Pack temperature min/max summary

Pack-wide hottest/coldest module-temperature summary, analogous to
F102. Byte-level decode UNKNOWN; not currently parsed into its own
CSV.

### F106F3 — BMS state — TENTATIVE

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

### F107F3 — BMS limits — TENTATIVE

Layout matches the standard J1939 limits-frame template:

| Bytes | Likely meaning                          | Observed                                       |
|-------|-----------------------------------------|------------------------------------------------|
| 0..1  | Discharge current limit, 0.1 A/bit      | 0x2710 (charger inserted) / 0x38A4 (driving)   |
| 2..3  | Charge current limit, 0.1 A/bit         | 0x2710 in every capture (sentinel)             |
| 4..5  | Voltage limit, 0.2 V/bit (guess)        | 0x0000 (charger inserted) / 0x0176 (driving)   |
| 6..7  | (unknown)                               | 0x0000                                         |

0x2710 = 10000 is almost certainly a J1939 "not available" sentinel
(the more conventional 0xFFFF wasn't used here). Pinning this down
needs a charge capture from low SOC where meaningful charge-current
limits are published.

### F108F3 — BMS active fault bitmap — CONFIRMED via injection

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

#### F108 byte 7 mapping

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

- Code 146 ("Maintenance mode status") is **not** encoded in F108
  anywhere. Operator transcriptions of "146" in cycling captures are
  almost certainly 145.
- Bit 6 genuinely re-asserts code 144 (re-verified with single-bit
  injection). Likely a severity-pair the dashboard renders
  identically.
- Bits 1 and 2 might still carry internal flags that don't surface as
  numeric codes.

#### F108 cross-validation against pre-injection captures

`bms-fullcharge-102-109-140.asc` — operator-confirmed cycling 102, 109, 140:

    F108 = 10 00 04 00 00 00 00 01
    byte 0 = 0x10 → bits 4-5 (pair 2) → code 102  ✓
    byte 2 = 0x04 → bits 2-3 (pair 1) → code 109  ✓
    byte 7 = 0x01 → code 140                      ✓

`bms-124-140-142-143-144-146.asc` — operator-confirmed cycling 124,
140, 142, 143, 144, 146:

    F108 = 00 00 00 00 00 01 00 BB
    byte 5 = 0x01 → bit 0 → code 124
    byte 7 = 0xBB → {140, 142, 143, 144, 145}

(The operator's "146" maps to 145 — see notable above.)

Codes 100..127 (bytes 0..5) and 140..145 (byte 7) are merged and
deduplicated by the decoder.


## Motor controller (Curtis 1238E, SA 0xCA)

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

### FF21CA — Motor telemetry — CONFIRMED (RPM, throttle, temp, state)

Broadcast at ~85 Hz. Full 29-bit ID is `0x0CFF21CA` (priority 3, not
the default 6 — higher priority than BMS broadcasts, consistent with a
real-time inverter feed).

| Byte | data[]  | Meaning                                                          |
|------|---------|------------------------------------------------------------------|
| 1    | data[0] | Throttle pedal position, raw (0..0xCC observed; SPN 91 candidate) |
| 2    | data[1] | 0x00 constant — fault-bitmap candidate (UNKNOWN)                  |
| 3..4 | data[2..3] LE | **Motor RPM**: rpm = (le16) − 0x0C80                       |
| 5    | data[4] | Three-state field 0x28 / 0x3B / 0x3C — startup-calibration related (UNKNOWN) |
| 6    | data[5] | **Controller temperature**: °C = raw − 40                         |
| 7    | data[6] | 0x00 constant — fault-bitmap candidate (UNKNOWN)                  |
| 8    | data[7] | **Packed transmission state** (high nibble = range, low = F/N/R)  |

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

Verified by two controlled captures (2026-05-10):

- `drive-r-n-f.asc` — operator walks F/N/R lever R → N → F with range
  held at 3, no pedal. data[7] = 0x28 → 0x20 → 0x24. Low nibble walks
  8 → 0 → 4; high nibble pinned at 0x2.
- `range-1-2-3.asc` — operator walks range 1 → 2 → 3 in Forward.
  data[7] = 0x04 → 0x14 → 0x24. High nibble walks 0 → 1 → 2; low
  nibble pinned at 0x4.

Filename hygiene note: older "neutral" captures
(`full-throttle-range2-neutral.asc`, `full-throttle-hydraulics.asc`)
actually have data[7] = 0x14 = Range 2 / Forward. Their filenames
likely refer to the tractor not being driven (parking brake set,
hydraulics-only test) rather than the F/N/R lever being in N.

### FECA (DM1) — MC fault channel — CONFIRMED via injection

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


## Charger (SA 0xE5)

### FF50E5 — Charger telemetry — CONFIRMED (V, A, status)

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


## Vehicle controller (SA 0xD0)

### F100D0 — VC heartbeat — TENTATIVE

Same PGN as the BMS pack-status frame, disambiguated by source
address.

| byte 0 | Inferred meaning |
|--------|------------------|
| 0x00   | init             |
| 0x0C   | ready            |

The transition is sharp and aligns with the operator-described wake-up
event, which is what gives confidence in the labels. Other bytes
change but haven't been decoded.

SA 0xF4 also acts as a vehicle-side requester (sends 1806E5F4 →
charger 0xE5). Whether 0xF4 is a separate physical module or a logical
address inside another ECU's firmware is open.

The parts catalog (Table 65, Ref 10) names a separate **OPC (Operator
Presence Control) timer module** — "UNIT ENGINE SHUT OFF CONTROLLER
TIMER (SEAT AND PARK OPC)" — gating shutdown on the seat switch and
park brake. The F100D0 heartbeat's 0x00 → 0x0C wake-up transition is
consistent with an OPC-style module asserting "operator present, park
brake released" once those preconditions are met.

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


## Error code system

The dashboard renders codes from two distinct channels, both numeric
but with non-overlapping ranges and different transport mechanisms:

| Subsystem | Code range | Channel                       | Prefix on dash |
|-----------|------------|-------------------------------|----------------|
| MC        | 12..99     | J1939 DM1 from SA 0xCA, SPN field | "MC" prepended |
| BMS       | 100..146   | F108F3 proprietary bitmap     | numeric only   |

A dashboard "code 47" is unambiguously MC; "code 124" is unambiguously
BMS.

**The cluster does not have a unified DM1 path.** It selects a
decoder based on source address — MC uses DM1, BMS uses F108. A
populated DM1 from SA 0xF3 is ignored.

**Latch behavior is subsystem-specific:**

- MC DM1 codes latch on receipt and persist until **key cycle**, even
  if DM1 returns to empty. This violates standard J1939 (which says
  3 s of absence should mark DTCs "previously active").
- BMS F108 codes follow the bitmap in real time — clearing the bit
  clears the dash.


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

| Pin | COBO ID | Generic function                                    | Solectrac usage                |
|-----|---------|-----------------------------------------------------|--------------------------------|
| J1  | RELE'   | Out 1 high-side, 150 mA (relay drive)               | (unused)                       |
| J2  | IDBL    | Positive digital input                              | BACK LIGHT (+)                 |
| J3  | 30      | + Battery (constant 12 V)                       ★   | + BATTERY                      |
| J4  | 15      | + Key (ignition / KL15)                         ★   | IGN ON (+)                     |
| J5  | FR1     | Frequency input, ≤1500 Hz                           | (unused — speed via CAN)       |
| J6  | ID9     | Positive digital input                              | TURN RIGHT (+)                 |
| J7  | ID10    | Positive digital input                              | TURN LEFT (+)                  |
| J8  | 31      | GND                                             ★   | GND                            |
| J9  | ID3     | Negative digital input                              | (unused)                       |
| J10 | ID1     | Negative digital input                              | FWD (−) — forward indicator    |
| J11 | ID5     | Negative digital input                              | (unused)                       |
| J12 | ID20    | Positive digital input                              | TURN TRAILER (+)               |
| J13 | ID2     | Negative digital input                              | (unused)                       |
| J14 | ID8     | Positive digital input                              | HEADLIGHTS (+)                 |
| J15 | AN2     | Analog resistive input, 90 Ω pull-up (sender)       | (unused)                       |
| J16 | AN1     | Analog resistive input, 90 Ω pull-up (sender)       | (unused)                       |
| J17 | ID12    | Positive digital input                              | RUNNING LIGHTS (+)             |
| J18 | ID13    | Negative digital input                              | PTO (−)                        |
| J19 | P/BR    | Positive digital input (probable Park Brake)        | (unused)                       |
| J20 | ID17    | Positive digital input                              | BATTERY CHARGING (+)           |
| J21 | ID6     | Positive digital input                              | (unused)                       |
| J22 | ID21    | Negative digital input                              | (unused)                       |
| J23 | ID18    | Negative digital input                              | PARKING BRAKE (−)              |
| J24 | ID16    | Positive digital input                              | (unused)                       |
| J25 | PB/L    | Positive digital input (probable Park Brake Light)  | (unused)                       |
| J26 | ID15    | Negative digital input                              | (unused)                       |
| J27 | ID19    | Negative digital input                              | (unused)                       |
| J28 | ID14    | Negative digital input                              | (unused)                       |
| J29 | ID11    | Negative digital input                              | (unused)                       |
| J30 | ID7     | Positive digital input                              | (unused)                       |
| J31 | ID4     | Negative digital input                              | (unused)                       |
| J32 | BUZZER  | Out 2 low-side, 150 mA (audible alert)              | (unused)                       |
| J33 | D+      | D+ alternator excite, neg. digital input            | (unused — no alternator)       |
| J34 | CS      | CAN shield                                      ★   | (unused — no shield drain)     |
| J35 | CL      | CAN L                                           ★   | CAN L                          |
| J36 | CH      | CAN H                                           ★   | CAN H                          |

### Diagram errata

The Solectrac harness wiring diagram has three labelling issues and
one omission relative to the as-built tractor:

1. The + BATTERY pin is labelled "Pin 1" on the diagram. The actual
   cavity is J3 (J1 is an empty cavity in the populated grid).
2. J10 is labelled "4WD (−)". The actual function is FWD (forward
   direction indicator). Tractor-confirmed 2026-05-13.
3. J14 is labelled "DIPPED BEAM (+)". Solectrac uses it as the general
   HEADLIGHTS indicator. ("Dipped beam" is the EU term for low-beam
   headlights.)
4. J18 is populated but not on the diagram. Identified as PTO
   indicator (−), switch-to-ground when PTO is engaged.

### Diagnostic tap

A non-destructive diagnostic harness can T-tap J35/J36 (row 9 cols
3-4) without unplugging the cluster — the display stays functional
while a capture tool reads the live bus.


## Vendor error code tables

Reproduced from the operator manual for cross-reference. The
disambiguation in the F108F3 and DM1 sections above maps these numbers
to bit positions and SPN values respectively.

### BMS codes (100..146)

    100  SOC is too high
    101  SOC is too low
    102  Total voltage is too high
    103  Total voltage is too low
    104  Charge current fault
    105  Discharge current fault
    106  Battery temperature is too low
    107  Battery temperature is too high
    108  Battery under voltage
    109  Battery over voltage
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

    12  Controller Over Current
    13  Current Sensor Fault
    15  Controller Severe Undertemp
    16  Controller Severe Overtemp
    17  Severe B+ Undervoltage
    18  Severe B+ Overvoltage
    18  Severe KSI Overvoltage           [duplicate S.No. 18]
    22  Controller Over temp Cutback
    23  B+ Undervoltage Cutback
    24  B+ Overvoltage Cutback
    25  +5V Supply Failure
    28  Motor Temp Hot Cutback
    29  Motor Temp Sensor Fault
    31  Coil1 Driver Open/Short
    31  Main Open/Short                  [duplicate S.No. 31]
    32  Coil2 Driver Open/Short
    32  EM Brake Open/Short              [duplicate S.No. 32]
    36  Encoder Fault
    36  Sin/Cos Sensor Fault             [duplicate S.No. 36]
    37  Motor Open
    38  Main Contactor Welded
    39  Main Contactor Did Not Close
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
- **SOC linearity below 90 %.** F100F3 data[4] is calibrated only on
  the top 10 %. A sustained discharge capture from a known starting
  SOC down to a lower known SOC would lock in the slope and tell us
  whether the field is truly linear or only locally linear near full.
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
  CONNECTOR PIN-1/PIN-2". Confirmation = tap the 2-pin connector and
  see if traffic resembles BMS-internal diagnostic chatter. The
  previously-suspected "Kelly KLS hydraulic CAN" interpretation is
  ruled out by schematic 5.11, which shows the e-hydraulic controller
  has no CAN pins at all.
- **UDAAN tool — identified, downloadable; one practical blocker.**
  UDAAN is the **UDAN iBMS Upper Utility** from Anhui UDAN Technology
  Co., Ltd. (Chinese BMS vendor); the Solectrac pack is rebadged UDAN
  hardware. Windows software, CAN @ 250 kbit/s (matches our bus),
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
- **SA 0xD0 and 0xF4 physical home.** Schematic 5.10 only documents
  four CAN nodes (MC, BMS, Charger, Cluster). The OPC module shown on
  schematic 5.9 is wired entirely discretely (no CAN). So either the
  schematic omits one or more physical nodes, or 0xD0 / 0xF4 are
  logical SAs emitted by one of the four documented ECUs (cluster is
  the natural candidate). Worth re-examining the captures with the
  4-node constraint as the prior.
- **Extra terminators.** Bus measures 30 Ω instead of the
  schematic-predicted 60 Ω — two additional 120 Ω terminators that
  schematic 5.10 doesn't show. Unplug-and-measure with each node
  removed would localize them; most likely BMS and Charger (both
  commonly ship with internal termination).
- **True throttle full-scale.** FF21CA data[0] = 0xCC = 204 observed
  in forward under real load; J1939 SPN 91 convention is raw 250 =
  100 % but not yet ground-truth. A "pedal mashed hard in F under
  load" capture would settle it.
- **Motor encoder PPR and motor → wheel gear ratio.** Neither is
  documented in the service manual's motor section. PPR is needed to
  convert encoder pulses (if they ever surface) to RPM; gear ratio is
  needed to convert motor RPM to ground speed. The cluster does not
  display ground speed — only motor RPM — so this is the standing gap
  for any "vehicle speed in km/h" derivation. Likely findable in the
  Transmission chapter (pp 240+) or via a Curtis 1313 programmer
  read of the controller's parameter file.


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
- **UDAAN = UDAN iBMS Upper Utility** from Anhui UDAN Technology
  Co., Ltd. — the Chinese BMS vendor whose hardware is rebadged into
  the Solectrac/Farmtrac pack. Public download:
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
  — source for the component identifications in the "Identified
  components" subsection (Curtis 1238E MC, Kelly KLS7212M/KLS7218
  hydraulic controller, Albright SW200 main contactor, 500 W DC-DC
  converter, OPC timer module, 72 V 300 Ah pack box, "FARMTRAC" key
  ignition). Note: the Kelly identification predates the FT 25G
  service manual; schematic 5.11 shows the e-hydraulic controller has
  no CAN pins regardless of which controller it actually is.
- Curtis 1238 controller manual — public fault-code-list reference
  for the MC short codes reproduced above.
- Kelly KLS7218MC / KLS7218NC CAN protocol:
  `docs/COMPAGE DOCUMENT KLS7218MC & KLS718NC FORMAT.pdf` — kept for
  reference, but the e-hydraulic controller on this vehicle is not
  wired to a CAN bus, so the Kelly protocol is not applicable here.
