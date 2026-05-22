# Solectrac CAN Monitor (ESP32)

Firmware that reads J1939 CAN frames from a Solectrac e25G electric tractor and
exposes the decoded state in four different ways:

- A mobile-friendly HTML dashboard over WiFi
- A JSON endpoint for scripting / scraping
- Raw CAN frames over USB (SLCAN)
- Raw CAN frames over WiFi (socketcand)

## Hardware

| Component | Notes |
|---|---|
| Microcontroller board | Adafruit ESP32-S3 Reverse TFT Feather |
| CAN transceiver | Adafruit CAN Pal |
| Bus speed | 250 kbit/s, J1939 (29-bit extended frames) |

Pin connections expected by the firmware (in `src/main.cpp`):

| Function | GPIO | Notes |
|---|---|---|
| CAN TX → transceiver TX | GPIO 8 (A5) | `CAN_TX_PIN` |
| CAN RX ← transceiver RX | GPIO 14 (A4) | `CAN_RX_PIN` |
| Status RGB NeoPixel | GPIO 33 | gated by GPIO 21 (NeoPixel power) |

The Reverse TFT display on the board is not used by this firmware.

## What the LED tells you

| Pattern | Meaning |
|---|---|
| Red blink | CAN driver failed to initialize |
| Amber blink | Booted, waiting for WiFi |
| Dim white (solid) | WiFi connected, no CAN frames received recently |
| Green blink | CAN frames arriving on the bus |

## Setting up on a new computer

1. **Install PlatformIO** — either the VS Code extension or the standalone CLI:

   ```bash
   pip install platformio          # or: brew install platformio
   ```

2. **Clone the repo and enter this folder**:

   ```bash
   git clone <repo-url>
   cd solectrac/esp32
   ```

3. **Set WiFi credentials** as environment variables — the build embeds them
   into the firmware (the firmware refuses to compile without them):

   ```bash
   export WIFI_SSID="your-network"
   export WIFI_PASS="your-password"
   ```

   Add these to your shell profile (`~/.zshrc`, `~/.config/fish/config.fish`)
   if you'd like them to persist.

4. **Plug the board in via USB-C.** On macOS the serial port appears as
   `/dev/cu.usbmodemXXXXX`; PlatformIO auto-detects it.

## Common commands

| Command | What it does |
|---|---|
| `pio run` | Build firmware only |
| `pio run -t upload` | Build and flash to the connected board |
| `pio device monitor -b 115200` | Open USB serial console (also speaks SLCAN — see below) |
| `pio run -t clean` | Wipe build cache (useful if PIO ever gets confused) |

If `upload` fails with `port is busy`, something else (often a leftover
`pio device monitor` in another shell) is holding the serial port. Find and
close it:

```bash
lsof /dev/cu.usbmodem*
```

## Endpoints

Once the board is on the network it advertises itself as `solectrac.local`
via mDNS.

| URL / Port | Purpose |
|---|---|
| `http://solectrac.local/` | Auto-refreshing dashboard |
| `http://solectrac.local/json` | Decoded state as JSON |
| `solectrac.local:28600` | socketcand TCP stream of raw CAN frames |
| `/dev/cu.usbmodem*` (USB CDC) | SLCAN stream of raw CAN frames |

### Consuming raw frames with `python-can`

Over USB (SLCAN):

```bash
python -m can.viewer -i slcan -c /dev/cu.usbmodem14301 -b 250000
```

Over WiFi (socketcand). Note that `can.viewer`'s CLI silently ignores extra
interface kwargs, so use `can.logger` (or a Python snippet) when you need to
pass `host` / `port`:

```bash
uv run python -m can.viewer -i socketcand -c can0 --bus-kwargs host=solectrac.local port=28600
```

Only one socketcand client can be connected at a time; a second connection
drops the first.

## Source layout

```
esp32/
├── platformio.ini          # board + build configuration
├── README.md               # this file
└── src/
    ├── main.cpp            # all firmware code (decode, HTTP, SLCAN, socketcand, LED)
    └── dashboard.html      # embedded into firmware at build time
```
