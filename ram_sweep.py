#!/usr/bin/env python3
"""
Discover all supported Victron Multi/Quattro RAM variables by sweeping IDs 0-255.
RAM variables hold live/runtime data (voltage, current, state, etc.) as opposed
to EEPROM settings which hold configuration.

Requirements: pip install pyserial
Usage:        python discover_ramvars.py /dev/inverter
              python discover_ramvars.py /dev/inverter -o ramvars.csv
              python discover_ramvars.py /dev/inverter -n 5   (read each var 5 times to spot live-changing values)
              python discover_ramvars.py /dev/inverter -v      (verbose)
"""

import csv
import serial
import sys
import time
from typing import Optional

# Known RAM var IDs from the VEConfigure capture (polled as IDs 0-13)
KNOWN_RAMVARS = {
    0:  "Unknown0 (polled)",
    1:  "Unknown1 (polled)",
    2:  "Unknown2 (polled)",
    4:  "Unknown4 (polled)",
    5:  "Unknown5 (polled)",
    6:  "Unknown6 (polled)",
    7:  "Unknown7 (polled)",
    8:  "Unknown8 (polled)",
    9:  "Unknown9 (polled)",
    11: "Unknown11 (polled)",
    12: "Unknown12 (polled)",
    13: "Unknown13 (polled)",
}

WINMON_SLOT = 0x58
DEBUG = False


def dbg(msg):
    if DEBUG:
        print(f"  [DBG] {msg}")


def calculate_checksum(data: bytes) -> int:
    return (256 - sum(data) % 256) % 256


def send_command(ser: serial.Serial, data: bytes) -> Optional[bytes]:
    dbg(f"TX: {data.hex()}")
    ser.write(data)
    time.sleep(0.1)
    if ser.in_waiting:
        response = ser.read(ser.in_waiting)
        dbg(f"RX: {response.hex()}")
        return response
    return None


def find_response(data: bytes, subcmd: int) -> Optional[bytes]:
    """Scan buffer for a Winmon response with given subcmd byte."""
    for i in range(len(data) - 4):
        if (data[i + 1] == 0xFF
                and data[i + 2] in (0x57, 0x58, 0x59, 0x5A)
                and data[i + 3] == subcmd):
            length = data[i]
            frame_end = i + length + 2
            if frame_end <= len(data):
                return bytes(data[i:frame_end])
    return None


def read_ramvar(ser: serial.Serial, var_id: int) -> Optional[int]:
    """
    Read a single RAM variable.
    Command:  04 FF <slot> 30 <var_id> <checksum>
    Response: 05 FF <slot> 85 <lo> <hi> <checksum>
    Returns value or None.
    """
    frame = bytes([0x04, 0xFF, WINMON_SLOT, 0x30, var_id])
    frame += bytes([calculate_checksum(frame)])

    for attempt in range(3):
        response = send_command(ser, frame)
        if response:
            resp_frame = find_response(response, 0x85)
            if resp_frame and len(resp_frame) >= 6:
                lo = resp_frame[4]
                hi = resp_frame[5]
                return lo | (hi << 8)
        time.sleep(0.05)

    return None


def guess_interpretation(var_id: int, value: int, signed_val: int) -> str:
    """Take a rough guess at what a value might represent."""
    hints = []

    # 48V battery voltage range (40.00V - 60.00V as val/100)
    if 4000 <= value <= 6500:
        hints.append(f"{value/100:.2f}V?")

    # Current in 0.1A scale (common for Victron)
    if 0 < value <= 5000 and value not in (1, 2, 3, 4, 5):
        hints.append(f"{value/10:.1f}A?")

    # Signed current (charging positive, discharging negative)
    if -5000 <= signed_val < 0:
        hints.append(f"{signed_val/10:.1f}A?")

    # Percentage (0-100)
    if 0 <= value <= 100:
        hints.append(f"{value}%?")

    # Temperature in 0.01°C + offset (Victron sometimes uses K*100)
    if 27000 <= value <= 32000:
        temp_c = (value / 100) - 273.15
        hints.append(f"{temp_c:.1f}°C?")

    # Frequency (4900-6100 = 49.00-61.00 Hz)
    if 4900 <= value <= 6100:
        hints.append(f"{value/100:.2f}Hz?")

    # AC voltage (2100-2500 = 210.0-250.0V at /10 or 21000-25000 at /100)
    if 2100 <= value <= 2500:
        hints.append(f"{value/10:.1f}Vac?")
    if 21000 <= value <= 25000:
        hints.append(f"{value/100:.1f}Vac?")

    # Power in watts
    if value > 100 and not hints:
        hints.append(f"{value}W?")

    return " | ".join(hints) if hints else ""


def main():
    global DEBUG
    DEBUG = "-v" in sys.argv or "--verbose" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    port = args[0] if args else "/dev/inverter"

    # Number of reads per variable (to detect changing values)
    num_reads = 1
    for i, a in enumerate(sys.argv):
        if a == "-n" and i + 1 < len(sys.argv):
            num_reads = int(sys.argv[i + 1])

    csv_path = None
    for i, a in enumerate(sys.argv):
        if a == "-o" and i + 1 < len(sys.argv):
            csv_path = sys.argv[i + 1]

    print(f"Connecting to MK3-USB on {port} at 2400 baud...")
    try:
        ser = serial.Serial(
            port=port,
            baudrate=2400,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5,
        )
    except serial.SerialException as e:
        print(f"ERROR: Could not open {port}: {e}")
        sys.exit(1)

    # Set address
    addr_cmd = bytes([0x04, 0xFF, 0x41, 0x01, 0x00, 0xBB])
    send_command(ser, addr_cmd)
    time.sleep(0.1)

    print(f"Sweeping RAM var IDs 0–255 ({num_reads} read(s) per var)...")
    print()

    results = []  # (id, values[], name)
    unsupported = []
    failed = []

    for vid in range(256):
        values = []
        for n in range(num_reads):
            value = read_ramvar(ser, vid)
            if value is not None:
                values.append(value)
            if num_reads > 1:
                time.sleep(0.2)  # space out repeated reads

        if not values:
            failed.append(vid)
            continue

        # If all reads returned 0xFFFF, treat as unsupported
        if all(v == 0xFFFF for v in values):
            unsupported.append(vid)
            continue

        name = KNOWN_RAMVARS.get(vid, "")
        results.append((vid, values, name))

        if vid % 32 == 31:
            print(f"  ...scanned through ID {vid}, found {len(results)} vars so far")

    print(f"\nScan complete.")
    print(f"  Supported RAM vars:   {len(results)}")
    print(f"  Unsupported (0xFFFF): {len(unsupported)}")
    print(f"  No response:          {len(failed)}")

    # ── Results table ────────────────────────────────────────────────────────
    multi_read = num_reads > 1
    print()

    if multi_read:
        print("=" * 110)
        print(f"{'ID':>4}  {'Name':<22}  {'Latest':>8}  {'Hex':>8}  {'Signed':>8}  {'Min':>8}  {'Max':>8}  {'Δ':>5}  Guesses")
        print("─" * 110)
    else:
        print("=" * 95)
        print(f"{'ID':>4}  {'Name':<22}  {'Value':>8}  {'Hex':>8}  {'Signed':>8}  Guesses")
        print("─" * 95)

    for vid, values, name in results:
        latest = values[-1]
        signed_val = latest if latest < 0x8000 else latest - 0x10000

        guesses = guess_interpretation(vid, latest, signed_val)

        if multi_read:
            vmin = min(values)
            vmax = max(values)
            delta = vmax - vmin
            changed = "***" if delta > 0 else ""
            print(f"{vid:>4}  {name:<22}  {latest:>8}  0x{latest:04X}  {signed_val:>8}  {vmin:>8}  {vmax:>8}  {delta:>4}{changed}  {guesses}")
        else:
            print(f"{vid:>4}  {name:<22}  {latest:>8}  0x{latest:04X}  {signed_val:>8}  {guesses}")

    if multi_read:
        print("─" * 110)
    else:
        print("─" * 95)

    if multi_read:
        changing = [(vid, vals) for vid, vals, _ in results if max(vals) - min(vals) > 0]
        if changing:
            print(f"\n⚡ {len(changing)} var(s) changed between reads (marked with ***):")
            for vid, vals in changing:
                name = KNOWN_RAMVARS.get(vid, f"var {vid}")
                print(f"  ID {vid} ({name}): {vals}")

    print(f"\nUnsupported IDs (returned 0xFFFF): {unsupported[:20]}{'...' if len(unsupported) > 20 else ''}")
    if failed:
        print(f"No response IDs: {failed[:20]}{'...' if len(failed) > 20 else ''}")

    # ── CSV output ───────────────────────────────────────────────────────────
    if csv_path:
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)

            if multi_read:
                writer.writerow(["var_id", "name", "latest_dec", "latest_hex", "signed",
                                 "min", "max", "delta", "guesses", "all_reads"])
                for vid, values, name in results:
                    latest = values[-1]
                    signed_val = latest if latest < 0x8000 else latest - 0x10000
                    guesses = guess_interpretation(vid, latest, signed_val)
                    writer.writerow([vid, name, latest, f"0x{latest:04X}", signed_val,
                                     min(values), max(values), max(values) - min(values),
                                     guesses, "|".join(str(v) for v in values)])
            else:
                writer.writerow(["var_id", "name", "value_dec", "value_hex", "signed", "guesses"])
                for vid, values, name in results:
                    latest = values[-1]
                    signed_val = latest if latest < 0x8000 else latest - 0x10000
                    guesses = guess_interpretation(vid, latest, signed_val)
                    writer.writerow([vid, name, latest, f"0x{latest:04X}", signed_val, guesses])

            for vid in unsupported:
                writer.writerow([vid, "", 65535, "0xFFFF", "", "unsupported"])
            for vid in failed:
                writer.writerow([vid, "", "", "", "", "no_response"])

        print(f"\nResults saved to {csv_path}")

    ser.close()
    print("Done.")


if __name__ == "__main__":
    main()
