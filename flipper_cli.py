#!/usr/bin/env python3
"""Flipper Zero CLI interface via serial."""
import serial
import time
import sys
import re
import select

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')

class FlipperCLI:
    def __init__(self, port='/dev/ttyACM0', baudrate=230400):
        self.ser = serial.Serial(port, baudrate, timeout=0.5)
        time.sleep(1)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        # Wake up CLI
        self.ser.write(b'\r')
        self.ser.flush()
        time.sleep(1)
        self._read_all()  # discard banner

    def _read_all(self, timeout=2):
        """Read all available data with a timeout."""
        data = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                data += chunk
                deadline = time.time() + 0.5  # extend on activity
            else:
                time.sleep(0.05)
        return data

    def cmd(self, command, timeout=5):
        """Send a command and return cleaned response."""
        self.ser.reset_input_buffer()
        self.ser.write(f'{command}\r'.encode())
        self.ser.flush()

        # Read until we see the next prompt ">:"
        data = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                data += chunk
                # Check if we have the prompt indicating command finished
                if b'>:' in data.split(f'{command}\r'.encode(), 1)[-1]:
                    break
                deadline = min(deadline, time.time() + 2)
            else:
                time.sleep(0.02)

        text = data.decode('utf-8', errors='replace')
        text = ANSI_ESCAPE.sub('', text)
        lines = []
        for line in text.split('\r\n'):
            line = line.strip()
            if line and line != f'>: {command}' and line != '>:':
                lines.append(line)
        return '\n'.join(lines)

    def close(self):
        self.ser.close()

if __name__ == '__main__':
    command = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else 'help'
    flip = FlipperCLI()
    result = flip.cmd(command, timeout=8)
    print(result)
    flip.close()
