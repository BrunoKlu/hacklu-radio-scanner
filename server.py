#!/usr/bin/env python3
"""
Radio Scanner Web Server
========================
Backend controls HackRF sweep parameters directly.
Frontend sends commands via WebSocket, backend restarts hackrf_sweep.
"""
import asyncio
import json
import time
import os
import re
import subprocess
import threading
from datetime import datetime
from collections import defaultdict, deque
from pathlib import Path

from aiohttp import web
import db

# === CONFIG ===
PORT = 8080
STATIC_DIR = Path(__file__).parent / "web"
SAMPLE_RATE = 1024000
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')

RTL433_CHANNELS = {
    0: {"freq": "433.92M", "label": "ISM 433 MHz"},
    1: {"freq": "868M",    "label": "IoT 868 MHz"},
    4: {"freq": "315M",    "label": "ISM 315 MHz"},
}
POWER_CHANNELS = {
    2: {"freq": 390000000, "label": "TETRA 390 MHz"},
    3: {"freq": 446000000, "label": "PMR446"},
}

# === SWEEP CONFIG (mutable, drives hackrf_sweep) ===
sweep_config = {
    "start_mhz": 300,
    "stop_mhz": 950,
    "lna_gain": 32,     # 0-40 dB, step 8
    "vga_gain": 32,     # 0-62 dB, step 2
    "rbw_hz": 500000,   # bin width in Hz (hackrf_sweep -w)
    "amp_enable": False, # RF amp
    "rbw_auto": True,    # auto RBW based on span
}
sweep_config_changed = threading.Event()

# === SHARED STATE ===
lock = threading.Lock()
data_event = threading.Event()
spectrum = []           # dynamic size, depends on sweep range / rbw
spectrum_freqs = []     # frequency of each bin in MHz
decoded_msgs = deque(maxlen=500)
power_levels = {}
stats = defaultdict(int)
running = True
hackrf_proc = None      # current hackrf_sweep subprocess


# ─────────────────────────────────────────────────────
# HACKRF SWEEP (dynamic, restartable)
# ─────────────────────────────────────────────────────
def hackrf_sweep_worker():
    """Run hackrf_sweep, restart when config changes."""
    global spectrum, spectrum_freqs, hackrf_proc

    while running:
        cfg = dict(sweep_config)
        start = int(cfg["start_mhz"])
        stop = int(cfg["stop_mhz"])
        rbw = int(cfg["rbw_hz"])
        lna = int(cfg["lna_gain"])
        vga = int(cfg["vga_gain"])
        amp = cfg["amp_enable"]

        cmd = [
            'hackrf_sweep',
            '-f', f'{start}:{stop}',
            '-w', str(rbw),
            '-l', str(lna),
            '-g', str(vga),
        ]
        if amp:
            cmd += ['-a', '1']

        print(f"[HackRF] Starting: {' '.join(cmd)}")

        try:
            hackrf_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1
            )

            current_line = {}
            prev_freq = 0
            last_db_save = time.time()
            sweep_config_changed.clear()

            while running and hackrf_proc.poll() is None:
                if sweep_config_changed.is_set():
                    print("[HackRF] Config changed, restarting...")
                    break

                line = hackrf_proc.stdout.readline()
                if not line:
                    continue
                try:
                    parts = line.strip().split(',')
                    if len(parts) < 7:
                        continue
                    f_start_hz = int(parts[2].strip())
                    f_step_hz = float(parts[4].strip())
                    n_bins_line = int(parts[5].strip())
                    powers = [float(p.strip()) for p in parts[6:6+n_bins_line]]

                    f_start_mhz = f_start_hz / 1e6

                    # Detect new sweep: frequency wrapped back → push completed sweep
                    if f_start_mhz < prev_freq - 10 and current_line:
                        sorted_freqs = sorted(current_line.keys())
                        sorted_powers = [current_line[f] for f in sorted_freqs]

                        with lock:
                            spectrum_freqs = sorted_freqs
                            spectrum = sorted_powers

                        data_event.set()
                        stats["sweeps"] += 1

                        now = time.time()
                        if now - last_db_save > 5:
                            db.store_spectrum(sorted_powers)
                            last_db_save = now

                        current_line = {}

                    # Accumulate bins
                    for i, p in enumerate(powers):
                        freq_mhz = (f_start_hz + i * f_step_hz) / 1e6
                        current_line[freq_mhz] = p

                    prev_freq = f_start_mhz

                except (ValueError, IndexError):
                    continue

            # Kill process
            if hackrf_proc and hackrf_proc.poll() is None:
                hackrf_proc.terminate()
                try:
                    hackrf_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    hackrf_proc.kill()
            hackrf_proc = None

        except Exception as e:
            print(f"[HackRF] ERROR: {e}")
            hackrf_proc = None
            time.sleep(1)

        if not running:
            break
        time.sleep(0.5)  # brief pause before restart


# hackrf_sweep RBW: valid FFT bins = 4 + 8*k, RBW = 20MHz / bins
# Curated selection for the UI dropdown
VALID_RBWS = [
    5000000, 2500000, 1000000, 500000, 250000, 200000,
    100000, 50000, 25000, 15000, 10000, 5000, 3000,
]


def auto_rbw(span_mhz):
    """Calculate optimal RBW for a given span. Targets ~500-1000 bins."""
    # Aim for ~500 bins on screen
    target_rbw = span_mhz * 1e6 / 500
    return nearest_valid_rbw(int(target_rbw))


def nearest_valid_rbw(rbw_hz):
    """Snap to nearest valid RBW that hackrf_sweep accepts.
    Valid = 20MHz / N where N = 4 + 8*k and N > 0."""
    # Calculate the exact valid value closest to requested
    n_bins = max(4, round(20e6 / max(2445, rbw_hz)))
    # Snap to nearest valid N (must satisfy (N+4)%8 == 0 → N%8 == 4 → N = 4+8k)
    remainder = n_bins % 8
    if remainder <= 4:
        n_bins = n_bins - remainder + 4
    else:
        n_bins = n_bins + (8 - remainder) + 4
    n_bins = max(4, min(8180, n_bins))  # 8180 → ~2445 Hz minimum
    return int(20e6 / n_bins)


def update_sweep_config(new_cfg):
    """Update sweep config and trigger restart."""
    changed = False
    for key in ["start_mhz", "stop_mhz", "lna_gain", "vga_gain", "rbw_hz", "amp_enable", "rbw_auto"]:
        if key in new_cfg and new_cfg[key] != sweep_config.get(key):
            sweep_config[key] = new_cfg[key]
            changed = True

    # Validate
    if sweep_config["start_mhz"] >= sweep_config["stop_mhz"]:
        sweep_config["stop_mhz"] = sweep_config["start_mhz"] + 10
    sweep_config["start_mhz"] = max(1, sweep_config["start_mhz"])
    sweep_config["stop_mhz"] = min(7250, sweep_config["stop_mhz"])
    sweep_config["lna_gain"] = max(0, min(40, sweep_config["lna_gain"]))
    sweep_config["vga_gain"] = max(0, min(62, sweep_config["vga_gain"]))

    # Auto RBW
    if sweep_config.get("rbw_auto", True):
        span = sweep_config["stop_mhz"] - sweep_config["start_mhz"]
        new_rbw = auto_rbw(span)
        if new_rbw != sweep_config["rbw_hz"]:
            sweep_config["rbw_hz"] = new_rbw
            changed = True

    sweep_config["rbw_hz"] = nearest_valid_rbw(sweep_config["rbw_hz"])

    if changed:
        print(f"[HackRF] New config: {sweep_config}")
        sweep_config_changed.set()


# ─────────────────────────────────────────────────────
# RTL433 + POWER MONITORS (unchanged)
# ─────────────────────────────────────────────────────
def rtl433_worker(device_id, freq, label):
    """rtl_433 decoder on one RTL-SDR channel."""
    try:
        proc = subprocess.Popen(
            ['rtl_433', '-d', str(device_id), '-f', freq,
             '-s', '1024000',
             '-F', 'json', '-M', 'time:utc', '-M', 'level'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
        )
        while running and proc.poll() is None:
            line = proc.stdout.readline()
            if not line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg["_freq"] = freq
                msg["_label"] = label
                msg["_ts"] = datetime.now().isoformat()
                with lock:
                    decoded_msgs.append(msg)
                    stats["decoded"] += 1
                db.store_decoded(msg)
                data_event.set()
                print(f"[{label}] >> {msg.get('model', '?')}")
            except json.JSONDecodeError:
                pass
        proc.terminate()
    except Exception as e:
        print(f"[{label}] ERROR: {e}")


def power_monitor_worker(device_id, freq, label):
    """Power level monitor using rtl_sdr with file output."""
    import numpy as np
    tmpfile = f"/tmp/power_ch{device_id}.iq"
    n_samples = 2048000
    try:
        while running:
            try:
                if os.path.exists(tmpfile):
                    os.remove(tmpfile)
                subprocess.run(
                    ['rtl_sdr', '-d', str(device_id), '-f', str(int(freq)),
                     '-s', '2048000', '-g', '40.2',
                     '-n', str(n_samples), tmpfile],
                    capture_output=True, timeout=15
                )
                if os.path.exists(tmpfile) and os.path.getsize(tmpfile) > 1000:
                    raw = np.fromfile(tmpfile, dtype=np.uint8)
                    I = (raw[0::2].astype(np.float32) - 127.5) / 127.5
                    Q = (raw[1::2].astype(np.float32) - 127.5) / 127.5
                    pwr = np.mean(I**2 + Q**2)
                    pwr_db = float(10 * np.log10(pwr + 1e-15))
                    peak = np.max(I**2 + Q**2)
                    peak_db = float(10 * np.log10(peak + 1e-15))
                    with lock:
                        power_levels[label] = {
                            "mean_db": round(pwr_db, 1),
                            "peak_db": round(peak_db, 1),
                            "freq_mhz": freq / 1e6,
                            "t": time.time(),
                        }
                        stats["power_readings"] += 1
                    db.store_power(label, freq / 1e6, round(pwr_db, 1), round(peak_db, 1))
                    data_event.set()
                    os.remove(tmpfile)
            except subprocess.TimeoutExpired:
                subprocess.run(['pkill', '-f', f'power_ch{device_id}'], capture_output=True)
                print(f"[{label}] timeout, retrying...")
            except Exception as ex:
                print(f"[{label}] error: {ex}, retrying...")
            time.sleep(1.5)
    except Exception as e:
        print(f"[{label}] FATAL: {e}")


# ─────────────────────────────────────────────────────
# WEBSOCKET (handles commands from frontend)
# ─────────────────────────────────────────────────────
async def ws_sender(ws):
    """Send data to a single WebSocket client at ~5 Hz."""
    try:
        while not ws.closed and running:
            with lock:
                spec_data = spectrum[:]
                s = dict(stats)
                levels = dict(power_levels)
                msgs = list(decoded_msgs)[-10:]
                total = stats["decoded"]
                cfg = dict(sweep_config)

            await ws.send_str(json.dumps({
                "type": "spectrum",
                "data": spec_data,
                "freq_min": cfg["start_mhz"],
                "freq_max": cfg["stop_mhz"],
                "n_bins": len(spec_data),
                "sweep_config": cfg,
                "stats": s,
            }))

            if levels:
                await ws.send_str(json.dumps({"type": "power", "levels": levels}))
            if msgs:
                await ws.send_str(json.dumps({"type": "decoded", "messages": msgs, "total": total}))

            await asyncio.sleep(0.2)
    except Exception:
        pass


async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    print(f"[WS] Client connected")

    # Send initial config
    with lock:
        cfg = dict(sweep_config)
    await ws.send_str(json.dumps({
        "type": "init",
        "sweep_config": cfg,
        "channels": {
            "rtl433": {str(k): v for k, v in RTL433_CHANNELS.items()},
            "power": {str(k): v for k, v in POWER_CHANNELS.items()},
        },
    }))

    # Send history from DB
    history = db.get_recent_decoded(50)
    if history:
        await ws.send_str(json.dumps({
            "type": "decoded",
            "messages": history,
            "total": db.get_decoded_stats()["total"],
        }))

    # Start sender + read loop (handles commands + pings)
    sender_task = asyncio.create_task(ws_sender(ws))
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    cmd = json.loads(msg.data)
                    await handle_ws_command(cmd, ws)
                except json.JSONDecodeError:
                    pass
    finally:
        sender_task.cancel()
        print(f"[WS] Client disconnected")
    return ws


async def handle_ws_command(cmd, ws):
    """Process a command from the frontend."""
    action = cmd.get("cmd")

    if action == "set_sweep":
        # Update sweep parameters
        new_cfg = {}
        if "start_mhz" in cmd: new_cfg["start_mhz"] = float(cmd["start_mhz"])
        if "stop_mhz" in cmd: new_cfg["stop_mhz"] = float(cmd["stop_mhz"])
        if "lna_gain" in cmd: new_cfg["lna_gain"] = int(cmd["lna_gain"])
        if "vga_gain" in cmd: new_cfg["vga_gain"] = int(cmd["vga_gain"])
        if "rbw_hz" in cmd: new_cfg["rbw_hz"] = int(cmd["rbw_hz"])
        if "rbw_auto" in cmd: new_cfg["rbw_auto"] = bool(cmd["rbw_auto"])
        if "amp_enable" in cmd: new_cfg["amp_enable"] = bool(cmd["amp_enable"])

        # Handle center/span → start/stop conversion
        if "center_mhz" in cmd and "span_mhz" in cmd:
            center = float(cmd["center_mhz"])
            span = float(cmd["span_mhz"])
            new_cfg["start_mhz"] = center - span / 2
            new_cfg["stop_mhz"] = center + span / 2

        update_sweep_config(new_cfg)

        # Acknowledge with new config
        await ws.send_str(json.dumps({
            "type": "sweep_config",
            "sweep_config": dict(sweep_config),
        }))

    elif action == "get_config":
        await ws.send_str(json.dumps({
            "type": "sweep_config",
            "sweep_config": dict(sweep_config),
        }))


# ─────────────────────────────────────────────────────
# HTTP ROUTES
# ─────────────────────────────────────────────────────
async def index_handler(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def api_status(request):
    with lock:
        return web.json_response({
            "running": running,
            "stats": dict(stats),
            "power": dict(power_levels),
            "decoded_count": stats["decoded"],
            "uptime": time.time() - start_time,
            "sweep_config": dict(sweep_config),
            "db_stats": db.get_decoded_stats(),
        })


async def api_history(request):
    limit = int(request.query.get("limit", "100"))
    return web.json_response({
        "messages": db.get_recent_decoded(limit),
        "stats": db.get_decoded_stats(),
    })


# ─────────────────────────────────────────────────────
# STARTUP / SHUTDOWN
# ─────────────────────────────────────────────────────
start_time = time.time()

async def on_startup(app):
    subprocess.run(['sudo', 'usbreset', '0424:2517'], capture_output=True)
    await asyncio.sleep(2)
    print("[Server] USB hub reset done")

    t = threading.Thread(target=hackrf_sweep_worker, daemon=True)
    t.start()

    for dev_id, cfg in RTL433_CHANNELS.items():
        t = threading.Thread(target=rtl433_worker, args=(dev_id, cfg["freq"], cfg["label"]), daemon=True)
        t.start()
        await asyncio.sleep(0.2)

    await asyncio.sleep(3)
    for dev_id, cfg in POWER_CHANNELS.items():
        t = threading.Thread(target=power_monitor_worker, args=(dev_id, cfg["freq"], cfg["label"]), daemon=True)
        t.start()
        await asyncio.sleep(1)

    print("[Server] All modules started")


async def on_shutdown(app):
    global running
    running = False
    if hackrf_proc and hackrf_proc.poll() is None:
        hackrf_proc.terminate()
    print("[Server] Shutting down...")


def main():
    STATIC_DIR.mkdir(exist_ok=True)

    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/api/status', api_status)
    app.router.add_get('/api/history', api_history)
    app.router.add_static('/static/', STATIC_DIR, show_index=False)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    cfg = sweep_config
    print()
    print("╔═══════════════════════════════════════════════════════════════╗")
    print("║  RADIO SCANNER - Web Server                                 ║")
    print(f"║  UI: http://192.168.7.100:{PORT}                              ║")
    print("╠═══════════════════════════════════════════════════════════════╣")
    print(f"║  HackRF     : Sweep {cfg['start_mhz']}-{cfg['stop_mhz']} MHz (dynamic)            ║")
    print("║  Kraken Ch0 : rtl_433 @ 433 MHz                            ║")
    print("║  Kraken Ch1 : rtl_433 @ 868 MHz                            ║")
    print("║  Kraken Ch2 : Power @ 390 MHz (TETRA)                      ║")
    print("║  Kraken Ch3 : Power @ 446 MHz (PMR446)                     ║")
    print("║  Kraken Ch4 : rtl_433 @ 315 MHz                            ║")
    print("╚═══════════════════════════════════════════════════════════════╝")
    print()

    web.run_app(app, host='0.0.0.0', port=PORT, print=lambda s: print(f"  {s}"))


if __name__ == '__main__':
    main()
