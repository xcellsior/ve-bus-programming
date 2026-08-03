"""One-shot: enable Weak AC (Setting 0, bit 14) on a MultiPlus via MK3-USB.

Self-contained (pyserial only) so it can be dropped onto a remote machine
without the vebus package. Read-modify-write with readback verification;
writes nothing if the bit is already set.

Usage: python3 weakac.py [port]   (default /dev/inverter)
"""
import serial
import sys
import time

SLOTS = (0x57, 0x58, 0x59, 0x5A)


def frame(payload):
    head = bytes([len(payload)]) + payload  # length excludes checksum
    return head + bytes([(256 - sum(head) % 256) % 256])


def find_value(buf, subcmd):  # skip MK3's unsolicited Version/LED frames
    for k in range(len(buf) - 4):
        if buf[k] == 0xFF and buf[k + 1] in SLOTS and buf[k + 2] == subcmd:
            return buf[k + 3] | (buf[k + 4] << 8)
    return None


def read_setting0(s):
    for _ in range(5):  # retry: heartbeats can crowd out the reply
        s.reset_input_buffer()
        s.write(frame(bytes([0xFF, 0x58, 0x31, 0x00])))  # ReadSetting, id 0
        time.sleep(0.7)
        v = find_value(s.read(256), 0x86)
        if v is not None:
            return v
    return None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/inverter"
    s = serial.Serial(port, 2400, timeout=1)
    cur = read_setting0(s)
    if cur is None:
        sys.exit("no response reading Setting 0 - port busy or wrong device?")
    print(f"Setting 0 = 0x{cur:04X}  (Weak AC {'already ON' if cur & 0x4000 else 'OFF'})")

    if not cur & 0x4000:
        new = cur | 0x4000
        lo, hi = new & 0xFF, new >> 8
        # CommandWriteViaID: FF <slot> 37 <flag=01 RAM+EEPROM> <id=00> <lo> <hi>
        s.write(frame(bytes([0xFF, 0x58, 0x37, 0x01, 0x00, lo, hi])))
        time.sleep(0.7)
        rb = read_setting0(s)  # readback is authoritative, not the ACK
        status = "OK" if rb == new else "FAILED"
        rb_txt = f"0x{rb:04X}" if rb is not None else "none"
        print(f"wrote 0x{new:04X}, readback {rb_txt} -> {status}")
    s.close()


if __name__ == "__main__":
    main()
