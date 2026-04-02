# hacklu-radio-scanner

Multi-SDR radio scanner lab on Raspberry Pi 4.

## Hardware
- **Flipper Zero** (Momentum mntm-012) — /dev/ttyACM0 @ 230400 baud, CC1101 EXT + GPS
- **HackRF One** — wideband SDR, spectrum sweep
- **KrakenSDR** — 5x coherent RTL-SDR (devices 0-4)

## Architecture
- `server.py` — aiohttp web server, WebSocket streaming, launches all radio modules
- `web/index.html` — live spectrum, waterfall, decoded messages UI
- `flipper_cli.py` — serial CLI helper for Flipper Zero
- `calibrated_test.py` / `cross_device_test.py` — device comparison experiments
- Python venv at `./venv`

## Critical setup (after reboot)
```bash
# USB buffer for KrakenSDR 5-channel
sudo sh -c "echo 0 > /sys/module/usbcore/parameters/usbfs_memory_mb"
# DVB blacklist is already persisted in /etc/modprobe.d/
```

## Running
```bash
source venv/bin/activate
python3 server.py  # → http://192.168.7.100:8080
```

## Language
User communicates in French.
