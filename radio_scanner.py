#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════╗
║  RADIO SCANNER / DECODER AMBIANT                                ║
║  HackRF (sweep) + KrakenSDR (5ch decode) + Flipper (GPS)       ║
╚═══════════════════════════════════════════════════════════════════╝

Modules:
  - HackRF:     Wideband spectrum sweep 300-950 MHz
  - Kraken Ch0: rtl_433 @ 433.92 MHz (ISM devices, meteo, telecommandes)
  - Kraken Ch1: rtl_433 @ 868 MHz (LoRa, IoT, alarmes)
  - Kraken Ch2: Power monitor @ 390 MHz (TETRA)
  - Kraken Ch3: Power monitor @ 446 MHz (PMR446)
  - Kraken Ch4: rtl_433 @ 315 MHz (US ISM, voitures, TPMS)
  - Flipper:    GPS position
"""
import subprocess
import threading
import time
import json
import sys
import os
import signal
import re
import serial
from datetime import datetime
from collections import defaultdict

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')

# === CONFIG ===
SCAN_DURATION = 60  # seconds (default, override with argv)
RTL433_DEVICES = {
    0: {"freq": "433.92M", "label": "ISM 433 MHz", "name": "kraken_ch0"},
    1: {"freq": "868M",    "label": "IoT 868 MHz", "name": "kraken_ch1"},
    4: {"freq": "315M",    "label": "ISM 315 MHz", "name": "kraken_ch4"},
}
POWER_MONITORS = {
    2: {"freq": 390000000, "label": "TETRA 390 MHz", "name": "kraken_ch2"},
    3: {"freq": 446000000, "label": "PMR446",        "name": "kraken_ch3"},
}
SAMPLE_RATE = 1024000  # 1.024 MS/s for power monitors

# === SHARED STATE ===
decoded_messages = []
spectrum_data = []
power_readings = defaultdict(list)
gps_data = {}
stats = defaultdict(int)
lock = threading.Lock()
running = True


def log(source, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    with lock:
        print(f"  [{ts}] [{source:>12}] {msg}")


# ─────────────────────────────────────────────────────
# MODULE 1: HackRF Spectrum Sweep
# ─────────────────────────────────────────────────────
def hackrf_sweep_loop():
    """Continuous spectrum sweep 300-950 MHz."""
    log("HackRF", "Starting wideband sweep 300-950 MHz...")
    try:
        proc = subprocess.Popen(
            ['hackrf_sweep', '-f', '300:950', '-w', '1000000'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
        )
        sweep_count = 0
        while running and proc.poll() is None:
            line = proc.stdout.readline()
            if not line:
                continue
            try:
                parts = line.strip().split(',')
                if len(parts) >= 7:
                    f_start = int(parts[2].strip())
                    powers = [float(p.strip()) for p in parts[6:]]
                    max_power = max(powers) if powers else -100
                    with lock:
                        spectrum_data.append({
                            "time": time.time(),
                            "freq_start": f_start,
                            "max_power": max_power,
                        })
                        stats["hackrf_sweeps"] += 1
                    sweep_count += 1
                    if sweep_count == 1:
                        log("HackRF", "First sweep data received")
            except (ValueError, IndexError):
                continue
        proc.terminate()
    except Exception as e:
        log("HackRF", f"ERROR: {e}")


# ─────────────────────────────────────────────────────
# MODULE 2: rtl_433 Decoders (3 channels)
# ─────────────────────────────────────────────────────
def rtl433_decoder(device_id, freq, label, name):
    """Run rtl_433 on a specific RTL-SDR channel."""
    log(label, f"Starting rtl_433 on device {device_id}...")
    try:
        proc = subprocess.Popen(
            [
                'rtl_433',
                '-d', f':{device_id}',
                '-f', freq,
                '-F', 'json',
                '-M', 'time:utc',
                '-M', 'level',
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
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
                msg["_scanner_channel"] = name
                msg["_scanner_freq"] = freq
                msg["_scanner_label"] = label
                with lock:
                    decoded_messages.append(msg)
                    stats[f"decoded_{name}"] += 1
                    stats["total_decoded"] += 1

                # Pretty print the decoded message
                model = msg.get("model", "Unknown")
                device_info = msg.get("id", msg.get("channel", ""))
                extra = []
                for key in ["temperature_C", "humidity", "battery_ok",
                            "wind_avg_km_h", "rain_mm", "pressure_hPa",
                            "code", "button", "cmd", "data"]:
                    if key in msg:
                        extra.append(f"{key}={msg[key]}")
                extra_str = " | ".join(extra) if extra else ""
                rssi = msg.get("rssi", msg.get("snr", ""))
                rssi_str = f" [{rssi} dB]" if rssi else ""

                log(label, f">> {model} (id:{device_info}){rssi_str} {extra_str}")

            except json.JSONDecodeError:
                # Not JSON, might be stderr leak or status
                if "Found" in line or "Tuned" in line:
                    log(label, line[:80])
        proc.terminate()
    except Exception as e:
        log(label, f"ERROR: {e}")


# ─────────────────────────────────────────────────────
# MODULE 3: Power Monitors (2 channels)
# ─────────────────────────────────────────────────────
def power_monitor(device_id, freq, label, name):
    """Monitor signal power on a specific frequency."""
    log(label, f"Starting power monitor on device {device_id}...")
    try:
        while running:
            # Capture 0.5s of samples
            result = subprocess.run(
                [
                    'rtl_sdr', '-d', str(device_id),
                    '-f', str(freq), '-s', str(SAMPLE_RATE),
                    '-g', '40.2', '-n', str(SAMPLE_RATE // 2),
                    '-'
                ],
                capture_output=True, timeout=5
            )
            if result.stdout:
                import numpy as np
                raw = np.frombuffer(result.stdout, dtype=np.uint8)
                I = (raw[0::2].astype(np.float32) - 127.5) / 127.5
                Q = (raw[1::2].astype(np.float32) - 127.5) / 127.5
                power = np.mean(I**2 + Q**2)
                power_db = 10 * np.log10(power + 1e-15)
                peak = np.max(I**2 + Q**2)
                peak_db = 10 * np.log10(peak + 1e-15)

                with lock:
                    power_readings[name].append({
                        "time": time.time(),
                        "mean_db": round(float(power_db), 1),
                        "peak_db": round(float(peak_db), 1),
                    })
                    stats[f"power_{name}"] += 1

                # Log if signal is strong (above noise)
                if power_db > -20:
                    log(label, f"SIGNAL: mean {power_db:.1f} dB | peak {peak_db:.1f} dB")
                elif stats[f"power_{name}"] % 10 == 1:
                    # Periodic status every ~5s
                    log(label, f"noise floor: {power_db:.1f} dB")

            time.sleep(0.2)
    except Exception as e:
        log(label, f"ERROR: {e}")


# ─────────────────────────────────────────────────────
# MODULE 4: Flipper Zero GPS
# ─────────────────────────────────────────────────────
def flipper_gps_reader():
    """Read GPS from Flipper Zero."""
    log("Flipper GPS", "Connecting...")
    try:
        ser = serial.Serial('/dev/ttyACM0', 230400, timeout=0.5)
        time.sleep(1)
        ser.reset_input_buffer()
        ser.write(b'\r')
        ser.flush()
        time.sleep(0.5)
        while ser.in_waiting:
            ser.read(ser.in_waiting)
            time.sleep(0.1)

        log("Flipper GPS", "Connected, polling GPS...")
        while running:
            ser.reset_input_buffer()
            ser.write(b'gpio\r')
            ser.flush()
            time.sleep(2)
            response = b''
            while ser.in_waiting:
                response += ser.read(ser.in_waiting)
                time.sleep(0.1)
            text = ANSI_ESCAPE.sub('', response.decode('utf-8', errors='replace'))
            # GPS data might be in the response
            with lock:
                gps_data["last_check"] = datetime.now().isoformat()
                gps_data["raw"] = text.strip()[:200]
            time.sleep(10)
        ser.close()
    except Exception as e:
        log("Flipper GPS", f"ERROR: {e}")


# ─────────────────────────────────────────────────────
# MODULE 5: Live Dashboard
# ─────────────────────────────────────────────────────
def print_dashboard():
    """Print periodic status dashboard."""
    import numpy as np

    while running:
        time.sleep(10)
        if not running:
            break

        with lock:
            now = datetime.now().strftime("%H:%M:%S")
            elapsed = time.time() - start_time
            n_decoded = stats["total_decoded"]
            n_sweeps = stats["hackrf_sweeps"]

            print()
            print(f"  ┌─── DASHBOARD @ {now} ({elapsed:.0f}s) ─────────────────────────────────┐")
            print(f"  │ Decoded messages: {n_decoded:<6} | HackRF sweeps: {n_sweeps:<6}       │")

            # Recent spectrum peaks
            recent_spectrum = [s for s in spectrum_data if s["time"] > time.time() - 10]
            if recent_spectrum:
                from collections import defaultdict as dd
                band_peaks = dd(lambda: -100)
                for s in recent_spectrum:
                    f_mhz = s["freq_start"] / 1e6
                    band = int(f_mhz / 50) * 50
                    band_peaks[band] = max(band_peaks[band], s["max_power"])
                active = [(b, p) for b, p in band_peaks.items() if p > -50]
                active.sort(key=lambda x: -x[1])
                if active:
                    bands_str = " ".join(f"{b}MHz:{p:.0f}dB" for b, p in active[:5])
                    print(f"  │ Active bands: {bands_str:<49}│")

            # Power monitor status
            for name, readings in power_readings.items():
                if readings:
                    last = readings[-1]
                    label = name.replace("kraken_", "").upper()
                    avg_readings = readings[-10:]
                    avg_p = np.mean([r["mean_db"] for r in avg_readings])
                    print(f"  │ {label}: avg {avg_p:.1f} dB | last {last['mean_db']:.1f} dB                          │"[:72] + "│")

            # Recent decoded messages
            recent_msgs = decoded_messages[-5:]
            if recent_msgs:
                print(f"  │ Last decoded:                                                   │")
                for msg in recent_msgs:
                    model = msg.get("model", "?")[:20]
                    freq = msg.get("_scanner_freq", "?")
                    print(f"  │   {model} @ {freq}                                         │"[:72] + "│")

            print(f"  └─────────────────────────────────────────────────────────────────┘")
            print()


# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────
def final_report():
    """Print final summary report."""
    elapsed = time.time() - start_time
    print()
    print("=" * 70)
    print("  RAPPORT FINAL - RADIO SCANNER AMBIANT")
    print("=" * 70)
    print(f"  Durée du scan    : {elapsed:.0f} secondes")
    print(f"  Messages décodés : {stats['total_decoded']}")
    print(f"  Sweeps HackRF    : {stats['hackrf_sweeps']}")
    print()

    # Decoded messages summary
    if decoded_messages:
        models = defaultdict(int)
        for msg in decoded_messages:
            m = msg.get("model", "Unknown")
            models[m] += 1
        print("  === PROTOCOLES DETECTES ===")
        for model, count in sorted(models.items(), key=lambda x: -x[1]):
            freq = ""
            for msg in decoded_messages:
                if msg.get("model") == model:
                    freq = msg.get("_scanner_freq", "")
                    break
            print(f"    {model:<35} x{count:>4}  ({freq})")
        print()

        # Unique devices
        devices = set()
        for msg in decoded_messages:
            model = msg.get("model", "?")
            dev_id = msg.get("id", msg.get("channel", "?"))
            devices.add(f"{model}:{dev_id}")
        print(f"  === APPAREILS UNIQUES : {len(devices)} ===")
        for dev in sorted(devices):
            print(f"    {dev}")
        print()

        # Interesting data points
        print("  === DONNEES CAPTEES ===")
        for msg in decoded_messages:
            interesting = {}
            for key in ["temperature_C", "humidity", "battery_ok",
                        "wind_avg_km_h", "rain_mm", "pressure_hPa",
                        "code", "button"]:
                if key in msg:
                    interesting[key] = msg[key]
            if interesting:
                model = msg.get("model", "?")
                ts = msg.get("time", "")
                print(f"    [{ts}] {model}: {interesting}")
        print()

    # Power monitor summary
    if power_readings:
        import numpy as np
        print("  === NIVEAUX DE PUISSANCE ===")
        for name, readings in power_readings.items():
            if readings:
                powers = [r["mean_db"] for r in readings]
                peaks = [r["peak_db"] for r in readings]
                label_map = {**{v["name"]: v["label"] for v in POWER_MONITORS.values()}}
                label = label_map.get(name, name)
                print(f"    {label}:")
                print(f"      Moyenne : {np.mean(powers):.1f} dB")
                print(f"      Min/Max : {np.min(powers):.1f} / {np.max(powers):.1f} dB")
                print(f"      Pic max : {np.max(peaks):.1f} dB")
                print(f"      Mesures : {len(readings)}")
        print()

    # Spectrum summary
    if spectrum_data:
        from collections import defaultdict as dd
        print("  === SPECTRE (bandes les plus actives) ===")
        band_peaks = dd(list)
        for s in spectrum_data:
            band = int(s["freq_start"] / 10e6) * 10
            band_peaks[band].append(s["max_power"])
        import numpy as np
        active_bands = []
        for band, powers in band_peaks.items():
            avg = np.mean(powers)
            if avg > -55:
                active_bands.append((band, avg, max(powers)))
        active_bands.sort(key=lambda x: -x[1])
        for band, avg, peak in active_bands[:15]:
            known = ""
            if 380 <= band <= 400: known = "TETRA"
            elif 430 <= band <= 440: known = "ISM 433"
            elif 460 <= band <= 470: known = "PMR446"
            elif 500 <= band <= 600: known = "DVB-T"
            elif 790 <= band <= 860: known = "LTE 800"
            elif 920 <= band <= 960: known = "GSM 900"
            elif 860 <= band <= 870: known = "ISM 868"
            bar = "█" * max(1, int((avg + 60) * 1.5))
            print(f"    {band:>4} MHz | avg {avg:>5.1f} dB | peak {peak:>5.1f} dB | {bar} {known}")
        print()

    # GPS
    if gps_data:
        print(f"  === GPS ===")
        print(f"    Last check: {gps_data.get('last_check', 'N/A')}")
        print()

    # Save full report
    report = {
        "duration_s": elapsed,
        "stats": dict(stats),
        "decoded_messages": decoded_messages,
        "power_readings": {k: v for k, v in power_readings.items()},
        "gps": gps_data,
    }
    report_file = "/claude/hacklu/scanner_report.json"
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  Rapport complet : {report_file}")
    print("=" * 70)


def main():
    global running, start_time

    duration = int(sys.argv[1]) if len(sys.argv) > 1 else SCAN_DURATION
    print()
    print("╔═══════════════════════════════════════════════════════════════════╗")
    print("║  RADIO SCANNER AMBIANT - 7 récepteurs simultanés               ║")
    print("╠═══════════════════════════════════════════════════════════════════╣")
    print("║  HackRF     : Sweep 300-950 MHz                                ║")
    print("║  Kraken Ch0 : rtl_433 @ 433.92 MHz (ISM, météo, domotique)    ║")
    print("║  Kraken Ch1 : rtl_433 @ 868 MHz (IoT, LoRa, alarmes)         ║")
    print("║  Kraken Ch2 : Power monitor @ 390 MHz (TETRA)                 ║")
    print("║  Kraken Ch3 : Power monitor @ 446 MHz (PMR446)                ║")
    print("║  Kraken Ch4 : rtl_433 @ 315 MHz (TPMS, remotes)              ║")
    print("║  Flipper    : GPS                                              ║")
    print(f"║  Durée      : {duration}s                                             ║"[:68] + "║")
    print("╚═══════════════════════════════════════════════════════════════════╝")
    print()

    start_time = time.time()
    threads = []

    # Start all modules
    modules = [
        ("HackRF Sweep", hackrf_sweep_loop),
        ("Dashboard", print_dashboard),
        ("Flipper GPS", flipper_gps_reader),
    ]

    # rtl_433 decoders
    for dev_id, cfg in RTL433_DEVICES.items():
        modules.append((
            cfg["label"],
            lambda d=dev_id, f=cfg["freq"], l=cfg["label"], n=cfg["name"]: rtl433_decoder(d, f, l, n)
        ))

    # Power monitors
    for dev_id, cfg in POWER_MONITORS.items():
        modules.append((
            cfg["label"],
            lambda d=dev_id, f=cfg["freq"], l=cfg["label"], n=cfg["name"]: power_monitor(d, f, l, n)
        ))

    for name, func in modules:
        t = threading.Thread(target=func, daemon=True, name=name)
        threads.append(t)
        t.start()
        time.sleep(0.3)

    # Run for specified duration
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass

    print()
    log("SCANNER", f"Arrêt après {duration}s...")
    running = False
    time.sleep(2)

    final_report()


if __name__ == '__main__':
    main()
