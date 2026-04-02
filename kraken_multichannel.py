#!/usr/bin/env python3
"""KrakenSDR multi-channel simultaneous capture on 5 RTL-SDRs."""
import subprocess
import threading
import time
import struct
import numpy as np
import sys

def capture_rtlsdr(device_id, freq_hz, sample_rate, duration_s, output):
    """Capture IQ samples from one RTL-SDR receiver."""
    num_samples = int(sample_rate * duration_s)
    num_bytes = num_samples * 2  # 8-bit IQ = 2 bytes per sample

    cmd = [
        'rtl_sdr', '-d', str(device_id),
        '-f', str(int(freq_hz)),
        '-s', str(int(sample_rate)),
        '-g', '40',
        '-n', str(num_bytes),
        '-'
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=duration_s + 5)
        raw = result.stdout

        if len(raw) > 0:
            # Convert unsigned 8-bit IQ to complex float
            iq = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            iq = (iq - 127.5) / 127.5
            I = iq[0::2]
            Q = iq[1::2]
            samples = I + 1j * Q

            # Calculate power
            power_db = 10 * np.log10(np.mean(np.abs(samples)**2) + 1e-10)
            peak_db = 10 * np.log10(np.max(np.abs(samples)**2) + 1e-10)

            output[device_id] = {
                'samples': len(samples),
                'mean_power_db': float(power_db),
                'peak_power_db': float(peak_db),
                'raw_bytes': len(raw),
            }
        else:
            output[device_id] = {'error': 'No data received'}

    except subprocess.TimeoutExpired:
        output[device_id] = {'error': 'Timeout'}
    except Exception as e:
        output[device_id] = {'error': str(e)}


def main():
    freq_mhz = float(sys.argv[1]) if len(sys.argv) > 1 else 390.0
    freq_hz = freq_mhz * 1e6
    sample_rate = 2.048e6
    duration = 1.0

    print(f"KrakenSDR Multi-Channel Capture")
    print(f"Frequency: {freq_mhz} MHz | Sample rate: {sample_rate/1e6:.1f} MS/s | Duration: {duration}s")
    print(f"Capturing on 5 channels simultaneously...")
    print()

    results = {}
    threads = []

    start = time.time()
    for dev_id in range(5):
        t = threading.Thread(target=capture_rtlsdr,
                           args=(dev_id, freq_hz, sample_rate, duration, results))
        threads.append(t)
        t.start()
        time.sleep(0.2)  # Stagger start slightly to avoid USB contention

    for t in threads:
        t.join(timeout=10)

    elapsed = time.time() - start

    print(f"Capture completed in {elapsed:.2f}s")
    print()
    print(f"{'Channel':>8} | {'Samples':>10} | {'Mean Power':>12} | {'Peak Power':>12} | {'Status'}")
    print("-" * 70)

    for dev_id in range(5):
        r = results.get(dev_id, {'error': 'No result'})
        if 'error' in r:
            print(f"  SDR #{dev_id}  | {'':>10} | {'':>12} | {'':>12} | ERROR: {r['error']}")
        else:
            print(f"  SDR #{dev_id}  | {r['samples']:>10} | {r['mean_power_db']:>9.1f} dBm | {r['peak_power_db']:>9.1f} dBm | OK ({r['raw_bytes']} bytes)")


if __name__ == '__main__':
    main()
