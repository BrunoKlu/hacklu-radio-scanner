#!/usr/bin/env python3
"""Parse hackrf_sweep CSV and show spectrum summary."""
import csv
import sys
from collections import defaultdict

def analyze_sweep(filename):
    """Analyze hackrf_sweep output and find active frequencies."""
    freq_power = defaultdict(list)

    with open(filename) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 7:
                continue
            try:
                f_start = int(row[2].strip())
                f_step = float(row[4].strip())
                n_bins = int(row[5].strip())
                powers = [float(x.strip()) for x in row[6:6+n_bins]]

                for i, pwr in enumerate(powers):
                    freq = f_start + int(i * f_step)
                    freq_power[freq].append(pwr)
            except (ValueError, IndexError):
                continue

    # Average power per frequency
    avg_power = {}
    for freq, powers in freq_power.items():
        avg_power[freq] = sum(powers) / len(powers)

    return avg_power

def print_spectrum(avg_power, threshold=-55):
    """Print a text-based spectrum with notable signals."""
    if not avg_power:
        print("No data")
        return

    freqs = sorted(avg_power.keys())
    min_p = min(avg_power.values())
    max_p = max(avg_power.values())

    print(f"Spectrum: {freqs[0]/1e6:.0f} - {freqs[-1]/1e6:.0f} MHz")
    print(f"Power range: {min_p:.1f} to {max_p:.1f} dBm")
    print(f"Total frequency bins: {len(freqs)}")
    print()

    # Find peaks above threshold
    peaks = [(f, p) for f, p in avg_power.items() if p > threshold]
    peaks.sort(key=lambda x: -x[1])

    if peaks:
        print(f"=== Signals above {threshold} dBm ===")
        # Known frequency bands
        bands = {
            (300e6, 350e6): "ISM 315 MHz",
            (430e6, 440e6): "ISM 433 MHz (domotique, meteo, telecommandes)",
            (460e6, 470e6): "PMR446 (talkies-walkies)",
            (862e6, 876e6): "ISM 868 MHz (LoRa, IoT)",
            (880e6, 915e6): "GSM 900 uplink",
            (925e6, 960e6): "GSM 900 downlink",
            (380e6, 400e6): "TETRA (services urgence)",
            (440e6, 450e6): "Amateur 70cm",
            (470e6, 790e6): "DVB-T / TNT",
            (790e6, 862e6): "LTE 800",
        }

        for freq, power in peaks[:30]:
            band_name = "Unknown"
            for (lo, hi), name in bands.items():
                if lo <= freq <= hi:
                    band_name = name
                    break
            bar = "#" * max(1, int((power - min_p) / (max_p - min_p) * 40))
            print(f"  {freq/1e6:8.2f} MHz | {power:6.1f} dBm | {bar} | {band_name}")
    else:
        print(f"No signals above {threshold} dBm")

    # Text-based waterfall (coarse)
    print()
    print("=== Spectrum overview (10 MHz blocks) ===")
    block_size = 10e6
    block_power = defaultdict(list)
    for freq, power in avg_power.items():
        block = int(freq / block_size) * int(block_size)
        block_power[block].append(power)

    for block in sorted(block_power.keys()):
        avg = sum(block_power[block]) / len(block_power[block])
        peak = max(block_power[block])
        bar_len = max(0, int((avg - min_p) / (max_p - min_p) * 50))
        bar = "█" * bar_len
        marker = " *" if peak > threshold else ""
        print(f"  {block/1e6:6.0f} MHz | avg {avg:6.1f} dBm | {bar}{marker}")


if __name__ == '__main__':
    filename = sys.argv[1] if len(sys.argv) > 1 else '/tmp/hackrf_sweep_wide.csv'
    threshold = float(sys.argv[2]) if len(sys.argv) > 2 else -55
    avg_power = analyze_sweep(filename)
    print_spectrum(avg_power, threshold)
