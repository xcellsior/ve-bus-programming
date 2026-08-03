#!/usr/bin/env python3
"""
Discover all supported Victron Multi/Quattro settings by sweeping IDs 0-255.
Uses ReadSetting for current values and GetSettingInfo for metadata (min/max).
Outputs a table to stdout and optionally saves to CSV.

Requirements: pip install pyserial
Usage:        python discover_settings.py /dev/inverter
              python discover_settings.py /dev/inverter -o settings.csv
              python discover_settings.py /dev/inverter -v  (verbose)
"""

import csv
import serial
import sys
import time
from typing import Optional, Tuple

# Known setting names from our reverse engineering
KNOWN_SETTINGS = {
    0:  "Flags/AdaptiveCharge",
    2:  "UBatAbsorption",
    3:  "UBatFloat",
    9:  "AbsorpTime/ChargeParam",
    10: "ChargeCharacteristic",
    60: "Unknown60",
    65: "Unknown65",
    72: "Unknown72",
}

WINMON_SLOT = 0x58
DEBUG = False


def dbg(msg):
    if DEBUG:
        print(f"  [DBG] {msg}")


def calculate_checksum(data: bytes) -> int:
    return (256 - sum(data) % 256) % 256


def send_command(ser: serial.Serial, data: bytes) -> Optional[bytes]:
    """Send command, sleep, read response — proven working pattern."""
    dbg(f"TX: {data.hex()}")
    ser.write(data)
    time.sleep(0.1)
    if ser.in_waiting:
        response = ser.read(ser.in_waiting)
        dbg(f"RX: {response.hex()}")
        return response
    return None


def find_response(data: bytes, subcmd: int) -> Optional[bytes]:
    """
    Scan buffer for a Winmon response with given subcmd byte.
    Returns the full frame starting from the length byte, or None.
    """
    for i in range(len(data) - 4):
        if (data[i + 1] == 0xFF
                and data[i + 2] in (0x57, 0x58, 0x59, 0x5A)
                and data[i + 3] == subcmd):
            length = data[i]
            frame_end = i + length + 2
            if frame_end <= len(data):
                return bytes(data[i:frame_end])
    return None


def read_setting(ser: serial.Serial, setting_id: int) -> Optional[int]:
    """Read a single setting. Returns value or None if unsupported/no response."""
    frame = bytes([0x04, 0xFF, WINMON_SLOT, 0x31, setting_id])
    frame += bytes([calculate_checksum(frame)])

    for attempt in range(3):
        response = send_command(ser, frame)
        if response:
            # Look for ReadSetting response: NN FF <slot> 86 <lo> <hi> <checksum>
            resp_frame = find_response(response, 0x86)
            if resp_frame and len(resp_frame) >= 6:
                lo = resp_frame[4]
                hi = resp_frame[5]
                return lo | (hi << 8)
        time.sleep(0.05)

    return None


def get_setting_info(ser: serial.Serial, setting_id: int) -> Optional[bytes]:
    """
    Send GetSettingInfo (0x3C) and return the raw response payload.
    Response subcmd is 0x89.
    """
    frame = bytes([0x04, 0xFF, WINMON_SLOT, 0x3C, setting_id])
    frame += bytes([calculate_checksum(frame)])

    for attempt in range(3):
        response = send_command(ser, frame)
        if response:
            resp_frame = find_response(response, 0x89)
            if resp_frame:
                return resp_frame
        time.sleep(0.05)

    return None


def parse_setting_info(frame: bytes) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Attempt to parse GetSettingInfo response for min/max/default.
    Response format (observed, best guess):
      0E FF <slot> 89 <flag> <min_lo> <min_hi> <??> <default_lo> <default_hi>
                              <??> <??> <max_lo> <max_hi> <??> <checksum>
    This is speculative — returns (min, max, default) or Nones.
    """
    # The response payload starts at byte 4 (after len, FF, slot, 89)
    payload = frame[4:]

    if len(payload) < 10:
        return None, None, None

    # Based on the two samples we captured:
    # Setting 0:  89 01 00 00 00 B4 89 00 00 FC 6F 00
    # Setting 60: 89 01 00 00 00 00 00 00 00 FE 00 00
    #
    # Setting 0 current value was 0x89B4, and we see B4 89 at offset 4-5
    # This might be: flag, min_lo, min_hi, ??, val_lo, val_hi, ??, ??, max_lo, max_hi, ??
    # But we can't be sure without more samples.
    #
    # Just return the raw hex for now — more data will help decode the format.

    return None, None, None


def main():
    global DEBUG
    DEBUG = "-v" in sys.argv or "--verbose" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    port = args[0] if args else "/dev/inverter"

    # Check for CSV output flag
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

    print(f"Sweeping setting IDs 0–255...")
    print()

    results = []  # (id, value, info_hex, name)
    unsupported = []
    failed = []

    for sid in range(256):
        value = read_setting(ser, sid)

        if value is None:
            failed.append(sid)
            continue

        if value == 0xFFFF:
            unsupported.append(sid)
            continue

        # Got a valid value — also try GetSettingInfo for metadata
        info_frame = get_setting_info(ser, sid)
        info_hex = info_frame[4:].hex() if info_frame else ""

        name = KNOWN_SETTINGS.get(sid, "")
        results.append((sid, value, info_hex, name))

        # Progress indicator
        if sid % 32 == 31:
            print(f"  ...scanned through ID {sid}, found {len(results)} settings so far")

    print(f"\nScan complete.")
    print(f"  Supported settings:  {len(results)}")
    print(f"  Unsupported (0xFFFF): {len(unsupported)}")
    print(f"  No response:         {len(failed)}")

    # ── Results table ────────────────────────────────────────────────────────
    print()
    print("=" * 95)
    print(f"{'ID':>4}  {'Name':<25}  {'Value':>8}  {'Hex':>8}  {'GetSettingInfo Payload'}")
    print("─" * 95)

    for sid, value, info_hex, name in results:
        print(f"{sid:>4}  {name:<25}  {value:>8}  0x{value:04X}  {info_hex}")

    print("─" * 95)
    print(f"\nUnsupported IDs (returned 0xFFFF): {unsupported[:20]}{'...' if len(unsupported) > 20 else ''}")
    if failed:
        print(f"No response IDs: {failed[:20]}{'...' if len(failed) > 20 else ''}")

    # ── CSV output ───────────────────────────────────────────────────────────
    if csv_path:
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["setting_id", "name", "value_dec", "value_hex", "setting_info_hex"])
            for sid, value, info_hex, name in results:
                writer.writerow([sid, name, value, f"0x{value:04X}", info_hex])

            # Also log unsupported for completeness
            for sid in unsupported:
                writer.writerow([sid, "", 65535, "0xFFFF", "unsupported"])
            for sid in failed:
                writer.writerow([sid, "", "", "", "no_response"])

        print(f"\nResults saved to {csv_path}")

    ser.close()
    print("Done.")


if __name__ == "__main__":
    main()
