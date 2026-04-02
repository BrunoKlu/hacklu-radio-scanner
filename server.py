#!/usr/bin/env python3
"""
Radio Scanner Web Server
========================
Backend: aiohttp (HTTP + WebSocket on same port)
Streams: spectrum, waterfall, decoded messages, power levels
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
FREQ_MIN = 300  # MHz
FREQ_MAX = 950  # MHz
FREQ_BINS = 650  # 1 MHz per bin
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

# === SHARED STATE (thread-safe via lock + event) ===
lock = threading.Lock()
data_event = threading.Event()  # signaled when any data is ready
spectrum = [(-100.0)] * FREQ_BINS
decoded_msgs = deque(maxlen=500)
power_levels = {}
stats = defaultdict(int)
ws_clients = set()
running = True


# ─────────────────────────────────────────────────────
# RADIO MODULES (run in threads, only write shared state)
# ─────────────────────────────────────────────────────
def hackrf_sweep_worker():
    """HackRF wideband sweep → spectrum."""
    global spectrum
    try:
        proc = subprocess.Popen(
            ['hackrf_sweep', '-f', f'{FREQ_MIN}:{FREQ_MAX}', '-w', '1000000'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
        )
        current_line = [(-100.0)] * FREQ_BINS
        last_push = time.time()
        last_db_save = time.time()

        while running and proc.poll() is None:
            line = proc.stdout.readline()
            if not line:
                continue
            try:
                parts = line.strip().split(',')
                if len(parts) < 7:
                    continue
                f_start = int(parts[2].strip()) // 1_000_000
                f_step = float(parts[4].strip()) / 1_000_000
                powers = [float(p.strip()) for p in parts[6:]]

                for i, p in enumerate(powers):
                    freq_idx = int(f_start + i * f_step) - FREQ_MIN
                    if 0 <= freq_idx < FREQ_BINS:
                        current_line[freq_idx] = p

                stats["sweeps"] += 1

                now = time.time()
                if now - last_push > 0.15:
                    with lock:
                        spectrum = current_line[:]
                    data_event.set()
                    # Save spectrum to DB every 5s
                    if now - last_db_save > 5:
                        db.store_spectrum(current_line[:])
                        last_db_save = now
                    current_line = [(-100.0)] * FREQ_BINS
                    last_push = now

            except (ValueError, IndexError):
                continue
        proc.terminate()
    except Exception as e:
        print(f"[HackRF] ERROR: {e}")


def rtl433_worker(device_id, freq, label):
    """rtl_433 decoder on one RTL-SDR channel."""
    """rtl_433 decoder on one RTL-SDR channel."""  # removed stale global
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
    """Power level monitor using rtl_sdr with file output."""  # removed stale global
    import numpy as np
    tmpfile = f"/tmp/power_ch{device_id}.iq"
    n_samples = 2048000  # 1s at 2.048 MS/s
    try:
        while running:
            try:
                # Remove stale file
                if os.path.exists(tmpfile):
                    os.remove(tmpfile)
                result = subprocess.run(
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
                # Kill stuck rtl_sdr process
                subprocess.run(['pkill', '-f', f'power_ch{device_id}'], capture_output=True)
                print(f"[{label}] timeout, retrying...")
            except Exception as ex:
                print(f"[{label}] error: {ex}, retrying...")
            time.sleep(1.5)
    except Exception as e:
        print(f"[{label}] FATAL: {e}")


# ─────────────────────────────────────────────────────
# ASYNC PUSH LOOP (runs in event loop, reads shared state)
# ─────────────────────────────────────────────────────
async def push_loop():
    """Push data to WebSocket clients at ~5 Hz."""
    while running:
        await asyncio.sleep(0.2)

        if not ws_clients:
            continue

        # Snapshot all data under lock
        with lock:
            spec_data = spectrum[:]
            s = dict(stats)
            levels = dict(power_levels)
            msgs = list(decoded_msgs)[-10:]
            total = stats["decoded"]

        dead = set()

        # Send spectrum
        try:
            payload = json.dumps({
                "type": "spectrum",
                "data": spec_data,
                "freq_min": FREQ_MIN,
                "freq_max": FREQ_MAX,
                "stats": s,
            })
            for ws in ws_clients:
                try:
                    await ws.send_str(payload)
                except Exception:
                    dead.add(ws)
        except Exception:
            pass

        # Send power
        if levels:
            try:
                payload = json.dumps({"type": "power", "levels": levels})
                for ws in ws_clients:
                    try:
                        await ws.send_str(payload)
                    except Exception:
                        dead.add(ws)
            except Exception:
                pass

        # Send decoded (if any)
        if msgs:
            try:
                payload = json.dumps({"type": "decoded", "messages": msgs, "total": total})
                for ws in ws_clients:
                    try:
                        await ws.send_str(payload)
                    except Exception:
                        dead.add(ws)
            except Exception:
                pass

        ws_clients -= dead


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

            await ws.send_str(json.dumps({
                "type": "spectrum",
                "data": spec_data,
                "freq_min": FREQ_MIN,
                "freq_max": FREQ_MAX,
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
    await ws.send_str(json.dumps({
        "type": "init",
        "freq_min": FREQ_MIN,
        "freq_max": FREQ_MAX,
        "freq_bins": FREQ_BINS,
        "channels": {
            "rtl433": {str(k): v for k, v in RTL433_CHANNELS.items()},
            "power": {str(k): v for k, v in POWER_CHANNELS.items()},
        },
    }))

    # Send history from DB on connect
    history = db.get_recent_decoded(50)
    if history:
        await ws.send_str(json.dumps({
            "type": "decoded",
            "messages": history,
            "total": db.get_decoded_stats()["total"],
        }))

    # Start sender as background task, read loop handles pings/close
    sender_task = asyncio.create_task(ws_sender(ws))
    try:
        async for msg in ws:
            pass  # just consume pings/pongs/close
    finally:
        sender_task.cancel()
        print(f"[WS] Client disconnected")
    return ws


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
            "db_stats": db.get_decoded_stats(),
        })


async def api_history(request):
    """Return decoded message history from DB."""
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
    """Start radio threads and push loop on app startup."""
    # Reset KrakenSDR USB hub to clear stale handles
    subprocess.run(['sudo', 'usbreset', '0424:2517'], capture_output=True)
    await asyncio.sleep(2)
    print("[Server] USB hub reset done")

    # HackRF sweep
    t = threading.Thread(target=hackrf_sweep_worker, daemon=True)
    t.start()

    # rtl_433 decoders
    for dev_id, cfg in RTL433_CHANNELS.items():
        t = threading.Thread(target=rtl433_worker, args=(dev_id, cfg["freq"], cfg["label"]), daemon=True)
        t.start()
        await asyncio.sleep(0.2)

    # Power monitors (delay to let rtl_433 finish init)
    await asyncio.sleep(3)
    for dev_id, cfg in POWER_CHANNELS.items():
        t = threading.Thread(target=power_monitor_worker, args=(dev_id, cfg["freq"], cfg["label"]), daemon=True)
        t.start()
        await asyncio.sleep(1)

    print("[Server] All modules started")


async def on_shutdown(app):
    global running
    running = False
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

    print()
    print("╔═══════════════════════════════════════════════════════════════╗")
    print("║  RADIO SCANNER - Web Server                                 ║")
    print(f"║  UI: http://192.168.7.100:{PORT}                              ║")
    print("╠═══════════════════════════════════════════════════════════════╣")
    print("║  HackRF     : Sweep 300-950 MHz → Spectrum + Waterfall     ║")
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
