#!/usr/bin/env python3
"""
Test calibré : comparaison HackRF vs KrakenSDR via SNR
======================================================
Phase 1 : Capture du plancher de bruit (pas de TX)
Phase 2 : Capture avec émission Flipper
Phase 3 : Calcul SNR pour chaque récepteur
"""
import serial
import subprocess
import threading
import time
import numpy as np
import re
import json
import os

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')
FREQ_HZ = 433920000
FREQ_MHZ = 433.92
SAMPLE_RATE = 2048000
CAPTURE_DURATION = 5  # seconds per phase (must be > 3s to cover HackRF startup delay)

# Gain settings - documented
HACKRF_LNA = 32   # HackRF LNA gain (0-40 dB, 8 dB steps)
HACKRF_VGA = 32   # HackRF VGA gain (0-62 dB, 2 dB steps)
RTL_GAIN = 40.2   # RTL-SDR gain (one of the supported values)


def hackrf_capture_iq(filename, duration=CAPTURE_DURATION):
    """Capture IQ with HackRF, return complex samples."""
    n_samples = SAMPLE_RATE * duration
    cmd = [
        'hackrf_transfer', '-r', filename,
        '-f', str(FREQ_HZ), '-s', str(SAMPLE_RATE),
        '-l', str(HACKRF_LNA), '-g', str(HACKRF_VGA),
        '-n', str(n_samples),
    ]
    subprocess.run(cmd, capture_output=True, timeout=duration + 10)
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        raw = np.fromfile(filename, dtype=np.int8)
        I = raw[0::2].astype(np.float64) / 128.0
        Q = raw[1::2].astype(np.float64) / 128.0
        return I + 1j * Q
    return None


def rtl_capture_iq(device_id, filename, duration=CAPTURE_DURATION):
    """Capture IQ with RTL-SDR, return complex samples."""
    n_samples = SAMPLE_RATE * duration  # -n = number of samples, not bytes
    cmd = [
        'rtl_sdr', '-d', str(device_id),
        '-f', str(FREQ_HZ), '-s', str(SAMPLE_RATE),
        '-g', str(RTL_GAIN), '-n', str(n_samples),
        filename
    ]
    subprocess.run(cmd, capture_output=True, timeout=duration + 10)
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        raw = np.fromfile(filename, dtype=np.uint8)
        I = (raw[0::2].astype(np.float64) - 127.5) / 127.5
        Q = (raw[1::2].astype(np.float64) - 127.5) / 127.5
        return I + 1j * Q
    return None


def analyze_iq(samples):
    """Compute power statistics on IQ samples."""
    if samples is None or len(samples) == 0:
        return None
    power = np.abs(samples) ** 2
    return {
        "mean_power_dB": float(10 * np.log10(np.mean(power) + 1e-15)),
        "median_power_dB": float(10 * np.log10(np.median(power) + 1e-15)),
        "peak_power_dB": float(10 * np.log10(np.max(power) + 1e-15)),
        "std_power_dB": float(10 * np.log10(np.std(power) + 1e-15)),
        "n_samples": len(samples),
    }


def flipper_tx():
    """Transmit from Flipper Zero."""
    ser = serial.Serial('/dev/ttyACM0', 230400, timeout=0.5)
    time.sleep(1)
    ser.reset_input_buffer()
    ser.write(b'\r')
    ser.flush()
    time.sleep(0.5)
    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    cmd = f"subghz tx AABBCC {FREQ_HZ} 400 15 1\r"
    ser.write(cmd.encode())
    ser.flush()
    time.sleep(4)
    resp = b''
    while ser.in_waiting:
        resp += ser.read(ser.in_waiting)
        time.sleep(0.1)
    ser.close()
    return ANSI_ESCAPE.sub('', resp.decode('utf-8', errors='replace')).strip()


def capture_all_receivers(prefix, phase_name):
    """Capture on all 6 receivers simultaneously, return analysis dict."""
    print(f"  Capture {phase_name} en cours...")
    results = {}
    threads = []

    def do_hackrf():
        fn = f"/tmp/{prefix}_hackrf.iq"
        samples = hackrf_capture_iq(fn)
        results["hackrf"] = analyze_iq(samples)

    def do_rtl(dev_id):
        fn = f"/tmp/{prefix}_kraken_ch{dev_id}.iq"
        samples = rtl_capture_iq(dev_id, fn)
        results[f"kraken_ch{dev_id}"] = analyze_iq(samples)

    # Start all captures
    t = threading.Thread(target=do_hackrf)
    threads.append(t)
    t.start()

    for i in range(5):
        t = threading.Thread(target=do_rtl, args=(i,))
        threads.append(t)
        t.start()
        time.sleep(0.1)

    for t in threads:
        t.join(timeout=CAPTURE_DURATION + 15)

    return results


def main():
    print()
    print("╔═══════════════════════════════════════════════════════════════╗")
    print("║  TEST CALIBRE : Comparaison HackRF vs KrakenSDR via SNR    ║")
    print("╠═══════════════════════════════════════════════════════════════╣")
    print(f"║  Fréquence   : {FREQ_MHZ} MHz                                 ║")
    print(f"║  HackRF gain : LNA={HACKRF_LNA} dB, VGA={HACKRF_VGA} dB                    ║")
    print(f"║  RTL-SDR gain: {RTL_GAIN} dB                                      ║")
    print(f"║  Sample rate : {SAMPLE_RATE/1e6:.1f} MS/s                                  ║")
    print("╚═══════════════════════════════════════════════════════════════╝")
    print()

    # === PHASE 1 : Noise floor ===
    print("▶ PHASE 1 : Mesure du plancher de bruit (pas d'émission)")
    noise = capture_all_receivers("noise", "bruit")
    print()

    # === PHASE 2 : Signal + noise (Flipper TX) ===
    print("▶ PHASE 2 : Emission Flipper + capture simultanée")

    # Start receivers first
    signal_results = {}
    capture_thread = threading.Thread(
        target=lambda: signal_results.update(
            capture_all_receivers("signal", "signal")
        )
    )
    capture_thread.start()

    # Wait 0.5s then TX
    time.sleep(0.5)
    print("  Flipper TX en cours...")
    tx_response = flipper_tx()
    print(f"  Flipper: {tx_response[:80]}")

    capture_thread.join(timeout=20)
    print()

    # === PHASE 3 : Compute SNR ===
    print("▶ PHASE 3 : Calcul du SNR")
    print()
    print("=" * 75)
    print(f"  {'Récepteur':<18} │ {'Bruit (dB)':>11} │ {'Signal (dB)':>12} │ {'SNR (dB)':>9} │ {'Samples':>10}")
    print("─" * 75)

    snr_data = {}
    receivers = ["hackrf"] + [f"kraken_ch{i}" for i in range(5)]
    labels = {
        "hackrf": "HackRF One",
        "kraken_ch0": "Kraken Ch0",
        "kraken_ch1": "Kraken Ch1",
        "kraken_ch2": "Kraken Ch2",
        "kraken_ch3": "Kraken Ch3",
        "kraken_ch4": "Kraken Ch4",
    }

    for rx in receivers:
        n = noise.get(rx)
        s = signal_results.get(rx)
        label = labels.get(rx, rx)

        if n and s:
            noise_db = n["mean_power_dB"]
            signal_db = s["mean_power_dB"]
            snr = signal_db - noise_db
            snr_data[rx] = snr

            # Visual bar
            bar_len = max(0, int(snr * 2))
            bar = "█" * bar_len

            print(f"  {label:<18} │ {noise_db:>+10.2f} │ {signal_db:>+11.2f} │ {snr:>+8.2f} │ {s['n_samples']:>10,}")
            print(f"  {'':18} │ {'':>11} │ {'':>12} │ {bar}")
        else:
            err_n = "OK" if n else "FAIL"
            err_s = "OK" if s else "FAIL"
            print(f"  {label:<18} │ noise:{err_n} signal:{err_s}")

    print("─" * 75)

    # === Summary ===
    if snr_data:
        hackrf_snr = snr_data.get("hackrf")
        kraken_snrs = {k: v for k, v in snr_data.items() if k.startswith("kraken")}

        print()
        print("  RESUME :")
        if hackrf_snr is not None:
            print(f"    HackRF SNR          : {hackrf_snr:+.2f} dB")

        if kraken_snrs:
            # All channels
            all_avg = sum(kraken_snrs.values()) / len(kraken_snrs)
            print(f"    Kraken SNR (5 ch)   : {all_avg:+.2f} dB (moyenne)")

            # Best 3 channels
            sorted_ch = sorted(kraken_snrs.items(), key=lambda x: -x[1])
            best3 = sorted_ch[:3]
            best3_avg = sum(v for _, v in best3) / 3
            best3_names = ", ".join(labels[k] for k, _ in best3)
            print(f"    Kraken SNR (top 3)  : {best3_avg:+.2f} dB ({best3_names})")

            # Coherence between channels
            if len(kraken_snrs) >= 2:
                vals = list(kraken_snrs.values())
                spread = max(vals) - min(vals)
                std = np.std(vals)
                print(f"    Ecart inter-canaux  : {spread:.2f} dB (spread) / {std:.2f} dB (std)")

            # Fair comparison
            if hackrf_snr is not None:
                print()
                delta_all = hackrf_snr - all_avg
                delta_best3 = hackrf_snr - best3_avg
                print(f"    Delta HackRF vs Kraken (5ch)  : {delta_all:+.2f} dB")
                print(f"    Delta HackRF vs Kraken (top3) : {delta_best3:+.2f} dB")
                print()
                if abs(delta_best3) < 3:
                    print("    → Performances comparables (< 3 dB de différence)")
                elif delta_best3 > 0:
                    print(f"    → HackRF meilleur de {delta_best3:.1f} dB (sensibilité supérieure)")
                else:
                    print(f"    → KrakenSDR meilleur de {-delta_best3:.1f} dB")

    print()
    print("=" * 75)

    # Save report
    report = {
        "experiment": "Calibrated SNR comparison",
        "frequency_mhz": FREQ_MHZ,
        "gains": {"hackrf_lna": HACKRF_LNA, "hackrf_vga": HACKRF_VGA, "rtl_gain": RTL_GAIN},
        "noise_floor": {k: v for k, v in noise.items() if v},
        "signal": {k: v for k, v in signal_results.items() if v},
        "snr": snr_data,
    }
    with open("/claude/hacklu/calibrated_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print("  Rapport : /claude/hacklu/calibrated_report.json")


if __name__ == '__main__':
    main()
