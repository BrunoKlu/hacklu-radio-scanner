#!/usr/bin/env python3
"""
Cross-Device Radio Experiment: Flipper TX → HackRF + KrakenSDR RX
=================================================================
1. Flipper Zero émet une clé sub-GHz via CC1101 externe (433.92 MHz)
2. HackRF capture le signal en IQ brut
3. KrakenSDR (5 canaux) capture simultanément
4. Analyse comparative des captures
"""
import serial
import subprocess
import threading
import time
import numpy as np
import sys
import os
import re
import json

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')
FREQ_HZ = 433920000  # 433.92 MHz - ISM standard
FREQ_MHZ = 433.92
SAMPLE_RATE = 2048000
CAPTURE_DURATION = 3  # seconds
FLIPPER_TX_DELAY = 1  # wait before flipper transmits
FLIPPER_KEY = "AABBCC"  # 3-byte key in hex
FLIPPER_TE = 400  # timing element in µs (OOK modulation)
FLIPPER_REPEAT = 10  # repeat count

HACKRF_FILE = "/tmp/hackrf_capture.iq"
KRAKEN_FILES = [f"/tmp/kraken_ch{i}.iq" for i in range(5)]

results = {
    "experiment": "Cross-Device Radio Test",
    "frequency_mhz": FREQ_MHZ,
    "flipper_key": FLIPPER_KEY,
    "flipper_te_us": FLIPPER_TE,
    "flipper_repeats": FLIPPER_REPEAT,
    "devices": {}
}


def flipper_transmit():
    """Send a sub-GHz signal from Flipper Zero CC1101 external."""
    print("[FLIPPER] Connecting to Flipper Zero...")
    try:
        ser = serial.Serial('/dev/ttyACM0', 230400, timeout=0.5)
        time.sleep(1)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # Wake CLI
        ser.write(b'\r')
        ser.flush()
        time.sleep(0.5)

        # Drain banner
        while ser.in_waiting:
            ser.read(ser.in_waiting)
            time.sleep(0.1)

        # Transmit command: subghz tx <key> <freq> <te> <repeat> <device>
        # device 1 = CC1101_EXT (RabbitLabs module)
        cmd = f"subghz tx {FLIPPER_KEY} {FREQ_HZ} {FLIPPER_TE} {FLIPPER_REPEAT} 1\r"
        print(f"[FLIPPER] TX command: {cmd.strip()}")
        print(f"[FLIPPER] Transmitting on {FREQ_MHZ} MHz via CC1101_EXT...")

        ser.write(cmd.encode())
        ser.flush()

        # Wait for response
        time.sleep(3)
        response = b''
        while ser.in_waiting:
            response += ser.read(ser.in_waiting)
            time.sleep(0.1)

        text = ANSI_ESCAPE.sub('', response.decode('utf-8', errors='replace'))
        print(f"[FLIPPER] Response: {text.strip()}")

        results["devices"]["flipper"] = {
            "status": "TX_OK",
            "command": cmd.strip(),
            "response": text.strip(),
            "device": "CC1101_EXT (RabbitLabs)",
        }

        ser.close()
    except Exception as e:
        print(f"[FLIPPER] ERROR: {e}")
        results["devices"]["flipper"] = {"status": "ERROR", "error": str(e)}


def hackrf_capture():
    """Capture IQ samples with HackRF One."""
    print(f"[HACKRF] Starting capture on {FREQ_MHZ} MHz...")
    try:
        cmd = [
            'hackrf_transfer',
            '-r', HACKRF_FILE,
            '-f', str(FREQ_HZ),
            '-s', str(SAMPLE_RATE),
            '-l', '32',  # LNA gain
            '-g', '40',  # VGA gain
            '-n', str(SAMPLE_RATE * CAPTURE_DURATION),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=CAPTURE_DURATION + 10)

        if os.path.exists(HACKRF_FILE):
            size = os.path.getsize(HACKRF_FILE)
            print(f"[HACKRF] Captured {size} bytes ({size/1e6:.1f} MB)")

            # Analyze IQ data (8-bit signed IQ)
            raw = np.fromfile(HACKRF_FILE, dtype=np.int8)
            I = raw[0::2].astype(np.float32) / 128.0
            Q = raw[1::2].astype(np.float32) / 128.0
            samples = I + 1j * Q

            power = np.abs(samples) ** 2
            mean_power = 10 * np.log10(np.mean(power) + 1e-10)
            peak_power = 10 * np.log10(np.max(power) + 1e-10)

            # Detect signal bursts (power > threshold)
            threshold = np.mean(power) + 3 * np.std(power)
            bursts = np.where(power > threshold)[0]
            n_burst_samples = len(bursts)

            # FFT for frequency analysis
            fft_size = 4096
            mid = len(samples) // 2
            segment = samples[mid:mid + fft_size]
            if len(segment) == fft_size:
                fft = np.fft.fftshift(np.abs(np.fft.fft(segment)))
                fft_db = 20 * np.log10(fft + 1e-10)
                peak_bin = np.argmax(fft_db)
                freq_offset = (peak_bin - fft_size // 2) * SAMPLE_RATE / fft_size
            else:
                freq_offset = 0

            results["devices"]["hackrf"] = {
                "status": "RX_OK",
                "file": HACKRF_FILE,
                "file_size_bytes": size,
                "total_samples": len(samples),
                "mean_power_db": round(float(mean_power), 2),
                "peak_power_db": round(float(peak_power), 2),
                "burst_samples": int(n_burst_samples),
                "burst_percentage": round(n_burst_samples / len(samples) * 100, 2),
                "fft_peak_offset_hz": round(float(freq_offset), 0),
            }
            print(f"[HACKRF] Mean power: {mean_power:.1f} dB | Peak: {peak_power:.1f} dB")
            print(f"[HACKRF] Burst samples: {n_burst_samples} ({n_burst_samples/len(samples)*100:.1f}%)")
        else:
            print("[HACKRF] No capture file created")
            results["devices"]["hackrf"] = {"status": "ERROR", "error": "No file"}

    except Exception as e:
        print(f"[HACKRF] ERROR: {e}")
        results["devices"]["hackrf"] = {"status": "ERROR", "error": str(e)}


def kraken_capture(device_id):
    """Capture IQ samples from one KrakenSDR channel."""
    outfile = KRAKEN_FILES[device_id]
    try:
        num_bytes = SAMPLE_RATE * CAPTURE_DURATION * 2
        cmd = [
            'rtl_sdr', '-d', str(device_id),
            '-f', str(FREQ_HZ),
            '-s', str(SAMPLE_RATE),
            '-g', '40',
            '-n', str(num_bytes),
            outfile
        ]
        subprocess.run(cmd, capture_output=True, timeout=CAPTURE_DURATION + 10)

        if os.path.exists(outfile):
            raw = np.fromfile(outfile, dtype=np.uint8)
            I = (raw[0::2].astype(np.float32) - 127.5) / 127.5
            Q = (raw[1::2].astype(np.float32) - 127.5) / 127.5
            samples = I + 1j * Q

            power = np.abs(samples) ** 2
            mean_power = 10 * np.log10(np.mean(power) + 1e-10)
            peak_power = 10 * np.log10(np.max(power) + 1e-10)

            threshold = np.mean(power) + 3 * np.std(power)
            bursts = np.where(power > threshold)[0]

            return {
                "status": "RX_OK",
                "total_samples": len(samples),
                "mean_power_db": round(float(mean_power), 2),
                "peak_power_db": round(float(peak_power), 2),
                "burst_samples": int(len(bursts)),
                "burst_percentage": round(len(bursts) / len(samples) * 100, 2),
            }
        return {"status": "ERROR", "error": "No file"}
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


def kraken_capture_all():
    """Capture on all 5 KrakenSDR channels simultaneously."""
    print(f"[KRAKEN] Starting 5-channel capture on {FREQ_MHZ} MHz...")
    threads = []
    channel_results = [None] * 5

    def capture_thread(dev_id):
        channel_results[dev_id] = kraken_capture(dev_id)

    for i in range(5):
        t = threading.Thread(target=capture_thread, args=(i,))
        threads.append(t)
        t.start()
        time.sleep(0.15)

    for t in threads:
        t.join(timeout=CAPTURE_DURATION + 15)

    kraken_data = {"status": "RX_OK", "channels": {}}
    for i, r in enumerate(channel_results):
        if r:
            kraken_data["channels"][f"ch{i}"] = r
            status = "OK" if r["status"] == "RX_OK" else "ERR"
            if r["status"] == "RX_OK":
                print(f"[KRAKEN] Ch{i}: mean {r['mean_power_db']:.1f} dB | peak {r['peak_power_db']:.1f} dB | bursts {r['burst_percentage']:.1f}%")

    results["devices"]["krakensdr"] = kraken_data


def print_report():
    """Print final experiment report."""
    print()
    print("=" * 70)
    print("  RAPPORT D'EXPERIENCE : Cross-Device Radio Test")
    print("=" * 70)
    print()
    print(f"  Fréquence        : {FREQ_MHZ} MHz (ISM 433)")
    print(f"  Clé transmise    : 0x{FLIPPER_KEY}")
    print(f"  Modulation       : OOK, TE={FLIPPER_TE} µs, {FLIPPER_REPEAT} répétitions")
    print(f"  Sample rate      : {SAMPLE_RATE/1e6:.1f} MS/s")
    print(f"  Durée capture    : {CAPTURE_DURATION}s")
    print()

    # Flipper
    flip = results["devices"].get("flipper", {})
    print("  --- EMETTEUR : Flipper Zero (CC1101_EXT RabbitLabs) ---")
    print(f"  Status  : {flip.get('status', 'N/A')}")
    if flip.get('response'):
        print(f"  Réponse : {flip['response'][:100]}")
    print()

    # HackRF
    hrf = results["devices"].get("hackrf", {})
    print("  --- RECEPTEUR 1 : HackRF One ---")
    if hrf.get("status") == "RX_OK":
        print(f"  Samples     : {hrf['total_samples']:,}")
        print(f"  Puissance   : moy {hrf['mean_power_db']:.1f} dB | pic {hrf['peak_power_db']:.1f} dB")
        print(f"  Bursts      : {hrf['burst_samples']:,} samples ({hrf['burst_percentage']:.1f}%)")
        print(f"  Offset freq : {hrf['fft_peak_offset_hz']:.0f} Hz")
        print(f"  Fichier IQ  : {hrf['file']} ({hrf['file_size_bytes']/1e6:.1f} MB)")
    else:
        print(f"  Status: {hrf.get('status', 'N/A')} - {hrf.get('error', '')}")
    print()

    # KrakenSDR
    krak = results["devices"].get("krakensdr", {})
    print("  --- RECEPTEUR 2 : KrakenSDR (5 canaux) ---")
    channels = krak.get("channels", {})
    if channels:
        powers = []
        for ch_name, ch_data in sorted(channels.items()):
            if ch_data.get("status") == "RX_OK":
                powers.append(ch_data["mean_power_db"])
                print(f"  {ch_name} : moy {ch_data['mean_power_db']:.1f} dB | pic {ch_data['peak_power_db']:.1f} dB | bursts {ch_data['burst_percentage']:.1f}%")
        if len(powers) >= 2:
            spread = max(powers) - min(powers)
            print(f"  Ecart inter-canaux : {spread:.1f} dB")
    print()

    # Cross-device comparison
    print("  --- COMPARAISON INTER-DEVICES ---")
    hackrf_power = hrf.get("mean_power_db")
    kraken_powers = [ch.get("mean_power_db") for ch in channels.values() if ch.get("mean_power_db") is not None]
    if hackrf_power and kraken_powers:
        kraken_avg = sum(kraken_powers) / len(kraken_powers)
        delta = hackrf_power - kraken_avg
        print(f"  HackRF mean       : {hackrf_power:.1f} dB")
        print(f"  KrakenSDR mean    : {kraken_avg:.1f} dB (avg 5 ch)")
        print(f"  Delta HackRF-Krak : {delta:+.1f} dB")

    hackrf_bursts = hrf.get("burst_percentage", 0)
    kraken_bursts = [ch.get("burst_percentage", 0) for ch in channels.values()]
    if kraken_bursts:
        avg_burst = sum(kraken_bursts) / len(kraken_bursts)
        print(f"  Bursts HackRF     : {hackrf_bursts:.1f}%")
        print(f"  Bursts Kraken avg : {avg_burst:.1f}%")

    print()
    all_ok = all(d.get("status") in ("TX_OK", "RX_OK") for d in results["devices"].values()
                 if isinstance(d, dict) and "status" in d)
    if all_ok:
        print("  RESULTAT : SUCCES - Tous les devices communiquent !")
    else:
        print("  RESULTAT : PARTIEL - Vérifier les erreurs ci-dessus")

    print("=" * 70)

    # Save JSON report
    report_file = "/claude/hacklu/cross_device_report.json"
    with open(report_file, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\n  Rapport JSON sauvegardé : {report_file}")


def main():
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  EXPERIENCE : Flipper Zero TX → HackRF + KrakenSDR RX     ║")
    print("║  Fréquence  : 433.92 MHz (ISM)                            ║")
    print("║  Setup      : 1 émetteur + 6 récepteurs simultanés        ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    # Phase 1: Start all receivers
    print("▶ Phase 1 : Démarrage des récepteurs...")
    hackrf_thread = threading.Thread(target=hackrf_capture)
    kraken_thread = threading.Thread(target=kraken_capture_all)

    hackrf_thread.start()
    kraken_thread.start()

    # Phase 2: Wait a moment then transmit from Flipper
    time.sleep(FLIPPER_TX_DELAY)
    print()
    print("▶ Phase 2 : Emission depuis le Flipper Zero...")
    flipper_transmit()

    # Phase 3: Wait for captures to complete
    print()
    print("▶ Phase 3 : Attente fin des captures...")
    hackrf_thread.join(timeout=20)
    kraken_thread.join(timeout=20)

    # Phase 4: Report
    print()
    print("▶ Phase 4 : Analyse et rapport...")
    print_report()


if __name__ == '__main__':
    main()
