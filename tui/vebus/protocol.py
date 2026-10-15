#!/usr/bin/env python3
"""
vebus.protocol — reusable VE.Bus MK2/MK3 backend.

Framework-agnostic. Contains:
  * The proven serial / checksum / frame-scanning wire protocol
    (reused from settings_sweep.py, ram_sweep.py, setv.py).
  * SerialBackend  — talks to a real MK3-USB adapter.
  * MockBackend    — a drop-in fake with plausible settings + drifting telemetry.
  * Decoders for Setting 0/1 flag registers, the charge profile, grid/LOM
    settings, RAM telemetry and operating state.
  * Input validation and the ordered LiFePO4 "fixed" charge-profile builder.

Everything here is import-safe with no UI dependencies, so the existing
discovery scripts and unit tests can reuse it.

See FINDINGS.md for the full protocol reference this is derived from.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover - serial only needed for the real backend
    serial = None  # MockBackend works without pyserial installed


# ─────────────────────────────────────────────────────────────────────────────
# Wire protocol primitives
# ─────────────────────────────────────────────────────────────────────────────

WINMON_SLOT = 0x58  # 'X'. Replies echo the letter; X/Y/Z rotate to match them (FINDINGS §2).
VALID_SLOTS = (0x57, 0x58, 0x59, 0x5A)

# Command / response subcommand bytes.
CMD_READ_SETTING = 0x31
RSP_READ_SETTING = 0x86
RSP_SETTING_UNSUPPORTED = 0x91
CMD_WRITE_VIA_ID = 0x37
RSP_WRITE_VIA_ID = 0x88      # setting written
RSP_WRITE_RAMVAR = 0x87      # RAM variable written
RSP_NOT_SUPPORTED = 0x80     # command or argument not recognised
RSP_ACCESS_LEVEL = 0x9B      # write refused: access level required
# The spec and VictronConnect use 0x35; 0x3C came from a VEConfigure capture
# and is unverified (FINDINGS §4.3, §13).
CMD_GET_INFO = 0x3C
RSP_GET_INFO = 0x89
CMD_READ_RAMVAR = 0x30
RSP_READ_RAMVAR = 0x85
RSP_RAMVAR_UNSUPPORTED = 0x90
CMD_GET_RAMVAR_INFO = 0x36   # X-slot: 05 FF 58 36 <id_lo> <id_hi> <chk>
RSP_RAMVAR_INFO_SCALE = 0x8E
RSP_RAMVAR_INFO_OFFSET = 0x8F
CMD_CONFIG = 0x46            # 'F' — arg 5 = GetConfig; response type byte 0x41
RSP_CONFIG = 0x41
CMD_LED = 0x4C              # 'L' — response FF 4C <on> <blink> ...
RSP_LED = 0x4C
CMD_STATE = 0x53            # 'S' — switch state (+ current limit, long form)
RSP_VERSION = 0x56

# Switch states for the long-form 'S' command (matches victron_mk3 SwitchState).
SWITCH_CHARGER_ONLY = 1
SWITCH_INVERTER_ONLY = 2
SWITCH_ON = 3
SWITCH_OFF = 4

# WriteViaID flag byte: bit 0 = setting (1) or RAM var (0); bit 1 = RAM only.
FLAG_RAM_EEPROM = 0x01  # setting, RAM + EEPROM (wears EEPROM)
FLAG_RAM_ONLY = 0x03    # setting, RAM only — lost on power cycle, no EEPROM wear

# A ReadSetting value seen for settings that exist but don't apply to this
# config. A setting the firmware lacks answers 0x91 instead (FINDINGS §9).
UNSUPPORTED = 0xFFFF


def calculate_checksum(data: bytes) -> int:
    """(256 - sum(bytes) % 256) % 256 — covers length + 0xFF + payload."""
    return (256 - sum(data) % 256) % 256


def build_frame(payload: bytes) -> bytes:
    """
    Wrap a payload (everything after the length byte, i.e. starting with 0xFF)
    into a complete framed message with leading length byte and trailing
    checksum.

    The length byte counts the payload bytes only — it does NOT include itself
    or the trailing checksum. (The working frames prove this: a ReadSetting is
    ``04 FF 58 31 <id> <chk>`` where 04 counts FF,58,31,id. FINDINGS' prose
    saying "including checksum" is wrong; the examples there are correct.)
    The checksum covers the length byte plus the payload.
    """
    length = len(payload)
    head = bytes([length]) + payload
    return head + bytes([calculate_checksum(head)])


def scan_value_response(data: bytes, subcmd: int) -> Optional[int]:
    """
    Extract a 16-bit value from a fixed-layout response (ReadSetting 0x86 /
    ReadRAMVar 0x85), matching on the ``FF <slot> <subcmd> <lo> <hi>`` signature
    and reading lo/hi directly — WITHOUT trusting the length byte.

    A desynced MK3 can return frames whose length byte is corrupted (e.g.
    ``87 ff 58 86 34 81 ...`` instead of ``05 ff 58 86 34 81 <chk>``). The
    strict, length-validating find_response rightly rejects those, but for these
    fixed 2-byte payloads the value is still recoverable. Version frames (0x56)
    never match a value subcmd, so this won't false-positive on heartbeats.
    """
    for k in range(len(data) - 4):
        if (data[k] == 0xFF
                and data[k + 1] in VALID_SLOTS
                and data[k + 2] == subcmd):
            return data[k + 3] | (data[k + 4] << 8)
    return None


def find_response(data: bytes, subcmd: int) -> Optional[bytes]:
    """
    Scan a receive buffer for a Winmon response frame with the given subcmd
    byte, skipping unsolicited Version/LED frames. Returns the full frame
    (from its length byte) or None.

    This is the exact scan logic from settings_sweep.py / ram_sweep.py.
    """
    for i in range(len(data) - 4):
        if (data[i + 1] == 0xFF
                and data[i + 2] in VALID_SLOTS
                and data[i + 3] == subcmd):
            length = data[i]
            frame_end = i + length + 2
            if frame_end <= len(data):
                return bytes(data[i:frame_end])
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Setting metadata: scaling, units, human names
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SettingMeta:
    name: str
    scale: float = 1.0      # raw / scale = engineering value
    unit: str = ""
    kind: str = "int"       # int | volts | amps | flags | enum
    enum: Optional[dict] = None


# Setting names/scales: voltages/currents confirmed on this project's hardware;
# the battery-monitor and AC-input names/scales (5,6,7,8,12,49,64,65,72, …) are
# from the victron-vebus-mk3-control library's setting map (KNOWN_SETTING_INFO).
SETTING_META: dict[int, SettingMeta] = {
    0:  SettingMeta("Flags register 0", kind="flags"),
    1:  SettingMeta("Flags register 1", kind="flags"),
    2:  SettingMeta("Absorption voltage", 100.0, "V", "volts"),
    3:  SettingMeta("Float voltage", 100.0, "V", "volts"),
    4:  SettingMeta("Charge current limit", 1.0, "A", "amps"),
    5:  SettingMeta("Inverter output voltage", 1.0, "V", "int"),
    6:  SettingMeta("AC1 input current limit", 10.0, "A", "amps"),
    7:  SettingMeta("Repeated absorption time", 1.0, "", "int"),
    8:  SettingMeta("Repeated absorption interval", 1.0, "", "int"),
    9:  SettingMeta("Maximum absorption time", 1.0, "", "int"),
    10: SettingMeta("Charge characteristic", 1.0, "", "enum",
                    enum={0: "Variable (lead-acid)",
                          1: "Fixed (LiFePO4)",
                          2: "Fixed + storage"}),
    11: SettingMeta("DC input low shutdown (cutoff)", 100.0, "V", "volts"),
    12: SettingMeta("DC input low restart offset", 100.0, "V", "volts"),
    15: SettingMeta("Virtual switch usage", 1.0, "", "int"),
    16: SettingMeta("VS on load higher than", 1.0, "", "int"),
    17: SettingMeta("VS on when Udc lower than", 100.0, "V", "volts"),
    18: SettingMeta("VS on when Udc higher than", 100.0, "V", "volts"),
    48: SettingMeta("Assist current boost factor", 1.0, "", "int"),
    49: SettingMeta("AC2 input current limit (Quattro)", 10.0, "A", "amps"),
    50: SettingMeta("AES low current limit", 1.0, "", "int"),
    51: SettingMeta("AES current hysteresis", 1.0, "", "int"),
    60: SettingMeta("Flags register 2", kind="flags"),
    61: SettingMeta("Flags register 3", kind="flags"),
    63: SettingMeta("Low battery pre-alarm offset (+0x8000)", 1.0, "", "int"),
    64: SettingMeta("Battery capacity", 1.0, "Ah", "int"),
    65: SettingMeta("Battery SoC when bulk finished", 2.0, "%", "volts"),
    72: SettingMeta("Battery charge efficiency", 1.0, "", "int"),
    73: SettingMeta("Inverter current limit during assist", 1.0, "", "int"),
    81: SettingMeta("Grid code", 1.0, "", "int"),
    88: SettingMeta("Do-not-charge voltage (Solar & Wind)", 100.0, "V", "volts"),
    128: SettingMeta("LOM config A", 1.0, "", "int"),
    190: SettingMeta("LOM config B", 1.0, "", "int"),
    191: SettingMeta("LOM config C", 1.0, "", "int"),
}

# Setting names by ID, as VictronConnect's VE.Bus driver lists them (FINDINGS
# §12.2). Used for IDs SETTING_META has no scale for; the values stay raw.
SETTING_NAMES: dict[int, str] = {
    0: "flags0", 1: "flags1", 2: "Absorption voltage", 3: "Float voltage",
    4: "Charge current", 5: "Inverter output voltage", 6: "AC current limit",
    7: "Repeated absorption time", 8: "Repeated absorption interval",
    9: "Maximum absorption time", 10: "Charge characteristics",
    11: "Inverter DC shutdown voltage", 12: "Inverter DC restart voltage",
    13: "Number of slaves", 14: "Special three phase", 15: "Virtual switch usage",
    16: "VS on load higher than", 17: "VS on when Udc lower than",
    18: "VS on when Udc higher than", 19: "VS time on load high",
    20: "VS time on DC voltage high", 21: "VS time on DC voltage low",
    22: "VS time on when not charging", 23: "VS time on fan enabled",
    24: "VS time on temperature alarm", 25: "VS time on low battery alarm",
    26: "VS time on overload alarm", 27: "VS time on ripple alarm",
    28: "VS off load lower than", 29: "VS off when Udc lower than",
    30: "VS off when Udc higher than", 31: "VS time off load low",
    32: "VS time off DC voltage high", 33: "VS time off DC voltage low",
    34: "VS off when charging for", 35: "VS time off fan disabled",
    36: "VS off bulk finished for", 37: "VS off no ON condition for",
    38: "VS off when no AC input for", 39: "VS off no temperature alarm",
    40: "VS off no battery alarm", 41: "VS off no overload alarm",
    42: "VS off no ripple alarm", 43: "VS minimum on time",
    44: "AC low switch input off", 45: "AC low switch input on",
    46: "AC high switch input off", 47: "AC high switch input on",
    48: "Assist current boost factor", 49: "Second input current limit",
    50: "Load for starting AES mode", 51: "Offset for ending AES mode",
    52: "VS2 on load higher than", 53: "VS2 time on load high",
    54: "VS2 on lower DC voltage than", 55: "VS2 on low DC voltage for",
    56: "VS2 off load lower than", 57: "VS2 off load lower for",
    58: "VS2 off DC voltage high", 59: "VS2 off DC voltage for",
    60: "flags2", 61: "flags3", 62: "System period", 63: "Low battery pre-alarm offset",
    64: "Battery capacity", 65: "SoC when bulk finished",
    66: "Frequency shift Ubat start", 67: "Frequency shift start delay",
    68: "Frequency shift Ubat stop", 69: "Frequency shift stop delay",
    70: "VS2 on SoC level", 71: "Assistant temp compensation",
    72: "Charge efficiency", 73: "Inverter current limit during assist",
    74: "SoC reset value when switching from absorption to float",
    75: "Battery current for stop absorption", 81: "Grid code",
    82: "Permanent flags 0", 83: "SoC low shutdown", 84: "SoC low restart",
    85: "Info ID 0", 86: "Stop charging at temperature",
    87: "Temperature compensation slope", 88: "Do-not-charge voltage",
}


def setting_meta(setting_id: int) -> SettingMeta:
    if setting_id in SETTING_META:
        return SETTING_META[setting_id]
    return SettingMeta(SETTING_NAMES.get(setting_id, f"Setting {setting_id}"))


def format_setting_value(setting_id: int, value: Optional[int]) -> str:
    """Human string for a raw setting value, applying scale/enum/units."""
    if value is None:
        return "—"
    if value == UNSUPPORTED:
        return "unsupported (0xFFFF)"
    m = setting_meta(setting_id)
    if m.kind == "flags":
        return f"0x{value:04X}"
    if m.kind == "enum" and m.enum is not None:
        label = m.enum.get(value, "?")
        return f"{value} ({label})"
    if m.scale != 1.0:
        return f"{value / m.scale:.2f}{m.unit}"
    return f"{value}{(' ' + m.unit) if m.unit else ''}"


# ─────────────────────────────────────────────────────────────────────────────
# Flag register decoding
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FlagBit:
    bit: int
    name: str
    set_meaning: str
    clear_meaning: str
    confirmed: bool = True
    interpret: bool = True   # False => model-dependent/unknown; show raw only

    @property
    def mask(self) -> int:
        return 1 << self.bit


# Bit names from Victron's MK2 Protocol 3.14 §7.3.13.3 and VictronConnect's flag
# table (FINDINGS §7.1, §12.3). confirmed = toggled on the bench.
BIT_REDUCED_FLOAT = 11   # Setting 0
BIT_LITHIUM = 4          # Setting 60 (flags2)

# Setting 0 — flags0.
SETTING0_BITS: list[FlagBit] = [
    FlagBit(0, "Multi-phase system", "set", "clear", confirmed=False),
    FlagBit(1, "Multi-phase leader", "set", "clear", confirmed=False),
    FlagBit(2, "60 Hz", "60 Hz", "50 Hz", confirmed=False),
    FlagBit(3, "UPS function", "UPS disabled (wave check off)",
            "UPS enabled (fast transfer)"),
    FlagBit(4, "Don't stop after 10 h bulk", "set", "clear", confirmed=False),
    FlagBit(5, "PowerAssist", "enabled", "disabled"),
    FlagBit(6, "Charger disabled", "set", "clear", confirmed=False),
    # The PDF calls bit 7 the inverse of bit 3; VictronConnect, back-feed lock.
    FlagBit(7, "Bit 7 (sources disagree)", "set", "clear",
            confirmed=False, interpret=False),
    FlagBit(8, "Disable AES", "set", "clear", confirmed=False),
    FlagBit(9, "Reserved (\"don't change me\")", "set", "clear",
            confirmed=False, interpret=False),
    FlagBit(10, "Reserved (\"don't change me\")", "set", "clear",
            confirmed=False, interpret=False),
    FlagBit(BIT_REDUCED_FLOAT, "Reduced float (storage mode)", "enabled", "disabled",
            confirmed=False),
    FlagBit(12, "Reserved (\"don't change me\")", "set", "clear",
            confirmed=False, interpret=False),
    FlagBit(13, "Disable ground relay", "set", "clear", confirmed=False),
    FlagBit(14, "Weak AC input", "enabled", "disabled"),
    FlagBit(15, "Remote overrules AC2", "set", "clear", confirmed=False),
]

# Setting 1 — flags1 (flags 16–31).
SETTING1_BITS: list[FlagBit] = [
    FlagBit(0, "VS on bulk protection", "set", "clear", confirmed=False),
    FlagBit(1, "VS on temperature warning", "set", "clear", confirmed=False),
    FlagBit(2, "VS on battery warning", "set", "clear", confirmed=False),
    FlagBit(3, "VS on overload warning", "set", "clear", confirmed=False),
    FlagBit(4, "VS on ripple warning", "set", "clear", confirmed=False),
    FlagBit(5, "VS off temperature warning", "set", "clear", confirmed=False),
    FlagBit(6, "VS off battery warning", "set", "clear", confirmed=False),
    FlagBit(7, "VS off overload warning", "set", "clear", confirmed=False),
    FlagBit(8, "VS off ripple warning", "set", "clear", confirmed=False),
    FlagBit(9, "VS on when failure present", "set", "clear", confirmed=False),
    FlagBit(10, "Invert VS behaviour", "set", "clear", confirmed=False),
    FlagBit(11, "Accept Wide Frequency Range", "enabled", "disabled"),
    FlagBit(12, "Dynamic Current Limiter", "enabled", "disabled"),
    FlagBit(13, "Tubular plate traction curve", "set", "clear", confirmed=False),
    FlagBit(14, "Remote overrules AC1", "set", "clear", confirmed=False),
    FlagBit(15, "Low power shutdown in AES", "set", "clear", confirmed=False),
]

# Setting 60 — flags2 (flags 32–41; 42 and up are spare).
SETTING60_BITS: list[FlagBit] = [
    FlagBit(0, "VS2 off when AC1 available", "set", "clear", confirmed=False),
    FlagBit(1, "Invert VS2 behaviour", "set", "clear", confirmed=False),
    FlagBit(2, "Frequency changes by VS", "set", "clear", confirmed=False),
    FlagBit(3, "Frequency changes by Ubat", "set", "clear", confirmed=False),
    FlagBit(BIT_LITHIUM, "Lithium battery", "set", "clear", confirmed=False),
    FlagBit(5, "Allow enable feed-in", "set", "clear", confirmed=False),
    FlagBit(6, "External mains current sensor", "set", "clear", confirmed=False),
    FlagBit(7, "Configured for VE.Bus BMS", "set", "clear", confirmed=False),
    FlagBit(8, "Disable Ubat VSense (diagnostics)", "set", "clear", confirmed=False),
    FlagBit(9, "Prefer renewable energy (Solar & Wind)", "set", "clear",
            confirmed=False),
]


@dataclass
class DecodedFlag:
    bit: int
    name: str
    is_set: bool
    meaning: str
    confirmed: bool
    interpret: bool


def decode_flags(value: int, bits: list[FlagBit]) -> list[DecodedFlag]:
    out = []
    for b in bits:
        is_set = bool(value & b.mask)
        meaning = b.set_meaning if is_set else b.clear_meaning
        out.append(DecodedFlag(b.bit, b.name, is_set,
                               meaning, b.confirmed, b.interpret))
    return out


def decode_setting0(value: int) -> list[DecodedFlag]:
    return decode_flags(value, SETTING0_BITS)


def decode_setting1(value: int) -> list[DecodedFlag]:
    return decode_flags(value, SETTING1_BITS)


def decode_setting60(value: int) -> list[DecodedFlag]:
    return decode_flags(value, SETTING60_BITS)


# ─────────────────────────────────────────────────────────────────────────────
# Operating state
# ─────────────────────────────────────────────────────────────────────────────

# Switch states the 'S' command accepts (MK2 protocol §5); there is no state 0.
STATE_NAMES = {
    SWITCH_CHARGER_ONLY: "Charger only",
    SWITCH_INVERTER_ONLY: "Inverter only",
    SWITCH_ON: "On",
    SWITCH_OFF: "Off",
}


# ─────────────────────────────────────────────────────────────────────────────
# RAM telemetry interpretation
# ─────────────────────────────────────────────────────────────────────────────

def signed16(value: int) -> int:
    return value if value < 0x8000 else value - 0x10000


@dataclass
class RamVarInfo:
    """
    Per-variable scaling from GetVariableInfo (0x36).

    Two value shapes (decoded as in victron-vebus-mk3-control):
      * linear:  value = scale * (raw + offset), raw sign-extended if `signed`.
      * bit:     when the info offset field is 0x8000 the variable is a boolean
                 living at `bit` of the raw value (bit = |scale field| - 1).
    """
    signed: bool = False
    scale: Optional[float] = None
    offset: int = 0
    bit: Optional[int] = None

    def parse(self, raw_u16: int) -> float:
        if self.bit is not None:
            return 1.0 if (raw_u16 & (1 << self.bit)) else 0.0
        v = signed16(raw_u16) if self.signed else raw_u16
        return self.scale * (v + self.offset)


def parse_ramvar_info(data: bytes) -> Optional[RamVarInfo]:
    """
    Decode a GetVariableInfo response: FF <slot> 8E <scale16> 8F <offset16>.

    Returns None for an unsupported variable (scale field 0). Handles the
    boolean "bit" form (offset field 0x8000) and the linear form, with negative
    scale meaning signed and a scale magnitude ≥ 0x4000 meaning a fractional
    scale of 1/(0x8000 - magnitude).
    """
    for k in range(len(data) - 7):
        if (data[k] == 0xFF and data[k + 1] in VALID_SLOTS
                and data[k + 2] == RSP_RAMVAR_INFO_SCALE
                and data[k + 5] == RSP_RAMVAR_INFO_OFFSET):
            scale_raw = data[k + 3] | (data[k + 4] << 8)
            if scale_raw == 0:
                return None  # variable not supported on this firmware/config
            offset_raw = data[k + 6] | (data[k + 7] << 8)
            if offset_raw == 0x8000:
                return RamVarInfo(bit=abs(signed16(scale_raw)) - 1)
            sc = signed16(scale_raw)
            mag = abs(sc)
            if mag >= 0x4000:
                denom = 0x8000 - mag
                if denom == 0:
                    return None
                scale = 1 / denom
            else:
                scale = float(mag)
            return RamVarInfo(signed=(sc < 0), scale=scale, offset=signed16(offset_raw))
    return None


def period_to_frequency(period: float) -> float:
    """MK3 reports AC period; frequency = 10 / period (Hz), 0 if period is 0."""
    return round(10.0 / period, 2) if period else 0.0


# Variables reported by GetVariableInfo are the authoritative scale/sign source;
# this table only adds a human label, unit, and the period→frequency handling.
# Labels follow VictronConnect's RAM variable names (FINDINGS §12.4): 16 is the
# AC-out (load) power, which includes pass-through from shore, not inverter power.
@dataclass
class TelemetryVar:
    var_id: int
    label: str
    unit: str
    kind: str = "linear"   # linear | period (→ frequency Hz) | bit (→ on/off)


TELEMETRY: list[TelemetryVar] = [
    TelemetryVar(4, "Battery voltage", "V"),
    TelemetryVar(5, "Battery current", "A"),
    TelemetryVar(14, "Inverter/charger power", "W"),
    TelemetryVar(6, "Battery ripple", "V"),
    TelemetryVar(0, "Mains voltage", "V"),
    TelemetryVar(1, "Mains current", "A"),
    TelemetryVar(15, "AC input power", "W"),
    TelemetryVar(8, "Mains frequency", "Hz", "period"),
    TelemetryVar(2, "AC output voltage", "V"),
    TelemetryVar(3, "AC output current", "A"),
    TelemetryVar(16, "AC output power", "W"),
    TelemetryVar(7, "AC output frequency", "Hz", "period"),
    TelemetryVar(13, "Battery SoC", "%", "soc"),
    TelemetryVar(9, "AC output current (signed)", "A"),
    TelemetryVar(10, "Virtual switch", ""),
    TelemetryVar(11, "Ignore AC input", "", "bit"),
    TelemetryVar(12, "Multi-func relay", "", "bit"),
]
TELEMETRY_BY_ID = {tv.var_id: tv for tv in TELEMETRY}
TELEMETRY_VARS = [tv.var_id for tv in TELEMETRY]

# Fallback divisors used only when GetVariableInfo is unavailable (e.g. mock or
# an older firmware). Confirmed against live hardware; raw values are always
# shown alongside so nothing is hidden.
_FALLBACK_SCALE = {
    0: (100.0, False), 1: (100.0, True), 2: (100.0, False), 3: (100.0, True),
    4: (100.0, False), 5: (10.0, True),
}


def telemetry_value(var_id: int, raw: int,
                    info: Optional[RamVarInfo] = None) -> Optional[float]:
    """Engineering value for a telemetry var, using live info when available."""
    if raw is None or raw == UNSUPPORTED:
        return None
    tv = TELEMETRY_BY_ID.get(var_id)
    if info is not None:
        val = info.parse(raw)
    elif var_id in _FALLBACK_SCALE:
        div, signed = _FALLBACK_SCALE[var_id]
        val = (signed16(raw) if signed else raw) / div
    else:
        return None
    if tv is not None and tv.kind == "period":
        return period_to_frequency(val)
    if tv is not None and tv.kind == "soc":
        # SoC info scale is fractional; normalize a 0–1 value to a percentage.
        return val * 100 if 0 <= val <= 1 else val
    return val


def format_telemetry(var_id: int, value: Optional[int],
                     info: Optional[RamVarInfo] = None) -> str:
    """Display string for a telemetry value (handles linear / period / bit)."""
    if value is None or value == UNSUPPORTED:
        return "—"
    val = telemetry_value(var_id, value, info)
    if val is None:
        return "—"
    tv = TELEMETRY_BY_ID.get(var_id)
    if tv is not None and tv.kind == "bit":
        return "on" if val else "off"
    unit = tv.unit if tv else ""
    return f"{val:.2f} {unit}".strip()


def interpret_ramvar(var_id: int, value: int,
                     info: Optional[RamVarInfo] = None) -> str:
    """Human string for a RAM variable (uses live scaling info when given)."""
    if value == UNSUPPORTED:
        return "unsupported"
    return format_telemetry(var_id, value, info)


# ─────────────────────────────────────────────────────────────────────────────
# Front-panel LEDs, switch register, and device config (shore current limit)
# ─────────────────────────────────────────────────────────────────────────────

# Front-panel LED bits (victron_mk3 LEDState). Displayed in panel order.
LED_BITS = {
    0x01: "Mains", 0x02: "Absorption", 0x04: "Bulk", 0x08: "Float",
    0x10: "Inverter", 0x20: "Overload", 0x40: "Low battery", 0x80: "Temperature",
}
LED_DISPLAY_ORDER = [
    (0x01, "Mains"), (0x04, "Bulk"), (0x02, "Absorption"), (0x08, "Float"),
    (0x10, "Inverter"), (0x20, "Overload"), (0x40, "Low battery"), (0x80, "Temperature"),
]

# Switch register bits (victron_mk3 SwitchRegister).
SW_DIRECT_CHARGE = 0x01
SW_DIRECT_INVERT = 0x02
SW_FRONT_UP = 0x04
SW_FRONT_DOWN = 0x08
SW_CHARGE = 0x10
SW_INVERT = 0x20


@dataclass
class LedInfo:
    on: int
    blink: int

    def states(self) -> list[tuple[str, str]]:
        """[(name, 'on'|'blink'|'off')] in panel display order."""
        out = []
        for bit, name in LED_DISPLAY_ORDER:
            state = "blink" if self.blink & bit else ("on" if self.on & bit else "off")
            out.append((name, state))
        return out


@dataclass
class ConfigInfo:
    """Decoded GetConfig (0x41) response — AC input (shore) current limit etc."""
    min_current: float
    max_current: float
    actual_current: float    # the live "shore power current limit"
    switch_register: int

    @property
    def front_switch_up(self) -> bool:
        return bool(self.switch_register & SW_FRONT_UP)

    @property
    def switch_state_name(self) -> str:
        charge = bool(self.switch_register & SW_CHARGE)
        invert = bool(self.switch_register & SW_INVERT)
        if charge and invert:
            return "On"
        if charge:
            return "Charger only"
        if invert:
            return "Inverter only"
        return "Off"

    @property
    def switch_state_code(self) -> int:
        """SwitchState code to resend so a current-limit write preserves operation."""
        charge = bool(self.switch_register & SW_CHARGE)
        invert = bool(self.switch_register & SW_INVERT)
        if charge and invert:
            return SWITCH_ON
        if charge:
            return SWITCH_CHARGER_ONLY
        if invert:
            return SWITCH_INVERTER_ONLY
        return SWITCH_OFF


def parse_config(payload: bytes) -> Optional[ConfigInfo]:
    """Decode a 0x41 config payload (current limits ÷10, switch register)."""
    if len(payload) < 13 or payload[0] != RSP_CONFIG:
        return None
    return ConfigInfo(
        min_current=(payload[6] | payload[7] << 8) / 10,
        max_current=(payload[8] | payload[9] << 8) / 10,
        actual_current=(payload[10] | payload[11] << 8) / 10,
        switch_register=payload[12],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Value scaling + input validation
# ─────────────────────────────────────────────────────────────────────────────

def volts_to_raw(volts: float) -> int:
    return int(round(volts * 100))


def raw_to_volts(raw: int) -> float:
    return raw / 100.0


@dataclass
class ValidationResult:
    ok: bool
    message: str = ""


# Plausible bands for a 48V-nominal system. Out-of-band warns (does not hard
# block) so unusual-but-intentional values are still possible after confirming.
VOLTAGE_BANDS = {
    2:  (48.0, 60.0, "absorption"),
    3:  (48.0, 58.0, "float"),
    11: (36.0, 52.0, "low-battery cutoff"),
}
CHARGE_CURRENT_MAX = 300  # A — generous ceiling; warns above.


def validate_setting_write(setting_id: int, value: int) -> ValidationResult:
    """Sanity-check a value before writing. Returns ok + optional warning."""
    if not (0 <= value <= 0xFFFF):
        return ValidationResult(False, f"Value {value} out of 16-bit range.")

    if setting_id in VOLTAGE_BANDS:
        lo, hi, label = VOLTAGE_BANDS[setting_id]
        v = raw_to_volts(value)
        if not (lo <= v <= hi):
            return ValidationResult(
                False,
                f"{v:.2f}V is outside the plausible {label} band "
                f"({lo:.0f}–{hi:.0f}V for a 48V system). Double-check before writing.")

    if setting_id == 4 and value > CHARGE_CURRENT_MAX:
        return ValidationResult(
            False, f"{value}A charge current looks too high (> {CHARGE_CURRENT_MAX}A).")

    if setting_id == 10 and value not in (0, 1, 2):
        return ValidationResult(False, f"Charge characteristic must be 0/1/2, got {value}.")

    return ValidationResult(True)


def validate_voltage_ordering(absorption: float, float_v: float,
                              cutoff: Optional[float] = None) -> ValidationResult:
    """Cross-field sanity: absorption >= float > cutoff."""
    if float_v >= absorption:
        return ValidationResult(
            False, f"Float ({float_v:.2f}V) should be below absorption ({absorption:.2f}V).")
    if cutoff is not None and cutoff >= float_v:
        return ValidationResult(
            False, f"Cutoff ({cutoff:.2f}V) should be well below float ({float_v:.2f}V).")
    return ValidationResult(True)


# ─────────────────────────────────────────────────────────────────────────────
# LiFePO4 "fixed" charge-profile sequence
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WriteStep:
    setting_id: int
    value: int
    label: str            # what this step does, human readable
    note: str = ""        # extra context (e.g. read-modify-write)


def build_lifepo4_sequence(current_setting0: int,
                           absorption_v: float,
                           float_v: float,
                           charge_a: Optional[int],
                           current_setting60: int) -> list[WriteStep]:
    """
    Build the ordered write sequence VEConfigure uses for the LiFePO4 fixed
    charge profile (FINDINGS §7.6). Settings 0 and 60 are flag registers, so
    both are read-modify-write from their *current* values: Setting 0 clears
    bit 11 (reduced float / storage mode) and Setting 60 sets bit 4 (lithium
    battery). VEConfigure's capture wrote 60 = 16 outright, which would also
    clear "configured for VE.Bus BMS" and the other flags2 bits.

    Charge current is appended as setting 4 when provided.
    """
    new_setting0 = current_setting0 & ~(1 << BIT_REDUCED_FLOAT)
    new_setting60 = current_setting60 | (1 << BIT_LITHIUM)
    steps = [
        WriteStep(0, new_setting0,
                  "Disable reduced float / storage mode (clear Setting 0 bit 11)",
                  note=f"read-modify-write: 0x{current_setting0:04X} → 0x{new_setting0:04X}"),
        WriteStep(60, new_setting60, "Lithium battery flag (set Setting 60 bit 4)",
                  note=f"read-modify-write: 0x{current_setting60:04X} → 0x{new_setting60:04X}"),
        WriteStep(65, 190, "SoC when bulk finished = 95% (Setting 65 = 190)"),
        WriteStep(72, 242, "Charge efficiency (Setting 72 = 242)"),
        WriteStep(10, 1, "Charge characteristic = Fixed (Setting 10 = 1)"),
        WriteStep(2, volts_to_raw(absorption_v),
                  f"Absorption voltage = {absorption_v:.2f}V (Setting 2)"),
        WriteStep(3, volts_to_raw(float_v),
                  f"Float voltage = {float_v:.2f}V (Setting 3)"),
        WriteStep(9, 1, "Maximum absorption time (Setting 9 = 1)"),
    ]
    if charge_a is not None:
        steps.append(WriteStep(4, int(charge_a),
                               f"Charge current limit = {int(charge_a)}A (Setting 4)"))
    return steps


# ─────────────────────────────────────────────────────────────────────────────
# Model detection
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DeviceInfo:
    model: str = "Unknown"
    firmware: Optional[int] = None
    setting0_base: Optional[int] = None
    detail: str = ""


def detect_model(setting0: Optional[int], quattro_only_supported: bool) -> DeviceInfo:
    """
    Best-effort model detection.

    The RELIABLE discriminator is the Quattro-only settings 49/88 — present on
    a Quattro, absent otherwise. Setting 0 bit 7 is NOT reliable: a MultiPlus II
    reads Setting 0 = 0x8134 with bit 7 CLEAR, the same value FINDINGS once
    attributed to a Quattro. So we only assert "Quattro" vs "MultiPlus family"
    and never try to split MultiPlus from MultiPlus II from flags alone.
    """
    info = DeviceInfo(setting0_base=setting0)
    if quattro_only_supported:
        info.model = "Quattro"
        info.detail = "Quattro-only setting (49/88) supported"
    else:
        info.model = "MultiPlus / MultiPlus II"
        info.detail = "Quattro-only settings 49/88 absent (not a Quattro)"
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Backend interface
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WriteResult:
    ok: bool                       # readback confirmed the value (authoritative)
    acked: bool                    # 0x88 ACK with status 0x00 seen
    readback: Optional[int]        # value read back after the write
    message: str = ""


class Backend:
    """Abstract backend interface shared by SerialBackend and MockBackend."""

    connected: bool = False
    is_mock: bool = False
    description: str = ""
    _healthy: bool = True

    @property
    def healthy(self) -> bool:
        """True if the link last looked alive (saw a response/heartbeat)."""
        return self._healthy

    def open(self) -> None: ...
    def close(self) -> None: ...

    def resync(self) -> None:
        """Force a bus resync (no-op for mock)."""

    def read_setting(self, setting_id: int) -> Optional[int]:
        raise NotImplementedError

    def get_setting_info(self, setting_id: int) -> Optional[bytes]:
        raise NotImplementedError

    def read_ramvar(self, var_id: int) -> Optional[int]:
        raise NotImplementedError

    def read_ramvar_info(self, var_id: int) -> Optional["RamVarInfo"]:
        """Per-variable scale/sign/offset (GetVariableInfo). None if unavailable."""
        return None

    def read_version(self) -> Optional[int]:
        raise NotImplementedError

    def read_config(self) -> Optional["ConfigInfo"]:
        """Shore (AC input) current limit + switch register. None if unavailable."""
        return None

    def read_leds(self) -> Optional["LedInfo"]:
        """Front-panel LED on/blink state. None if unavailable."""
        return None

    def set_current_limit(self, amps: float, verify: bool = True) -> "WriteResult":
        """Set the AC input (shore) current limit, preserving switch state."""
        raise NotImplementedError

    def write_setting(self, setting_id: int, value: int,
                      ram_only: bool = False, verify: bool = True) -> WriteResult:
        raise NotImplementedError

    def set_state(self, state: int) -> bool:
        """Send a switch state (STATE_NAMES), keeping the current input limit.
        False if the state is invalid or the limit can't be read first."""
        raise NotImplementedError

    # ── convenience helpers shared by both backends ────────────────────────

    def read_setting_supported(self, setting_id: int) -> bool:
        v = self.read_setting(setting_id)
        return v is not None and v != UNSUPPORTED

    def identify(self) -> DeviceInfo:
        s0 = self.read_setting(0)
        # Setting 49 (AC2 input current limit) is Quattro-exclusive — MultiPlus
        # has only AC1, so 49 is absent. Setting 88 (do-not-charge voltage) is
        # available on both models; require both for confirmation only.
        # See FINDINGS §9 for cross-model differences.
        quattro_only = (self.read_setting_supported(88)
                        and self.read_setting_supported(49))
        info = detect_model(s0, quattro_only)
        info.firmware = self.read_version()
        return info


# ─────────────────────────────────────────────────────────────────────────────
# Real serial backend
# ─────────────────────────────────────────────────────────────────────────────

class SerialBackend(Backend):
    """
    Talks to a real MK3-USB adapter. Reuses the proven send/scan/retry logic.
    All access is serialized by a lock so a telemetry refresh and a user write
    never interleave on the wire.
    """

    READ_RETRIES = 3
    POST_CMD_SLEEP = 0.1
    WRITE_SLEEP = 0.2

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 2400):
        if serial is None:
            raise RuntimeError("pyserial is not installed; cannot use SerialBackend.")
        self.port = port
        self.baudrate = baudrate
        self.ser: Optional["serial.Serial"] = None
        self._lock = threading.Lock()
        self.is_mock = False
        self._healthy = True  # last command saw a live bus (Version frames/response)
        self._ramvar_info: dict[int, Optional[RamVarInfo]] = {}  # cached 0x36 results

    # ── lifecycle ───────────────────────────────────────────────────────────

    def open(self) -> None:
        # exclusive=True (POSIX TIOCEXCL) makes a second opener fail with
        # "Device or resource busy" instead of silently sharing the tty — two
        # processes on one un-locked serial port corrupt each other's frames,
        # which looks exactly like a hung/dead bus. Fail loud, not silent.
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
                exclusive=True,
            )
        except TypeError:
            # Older pyserial without the `exclusive` kwarg.
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
            )
        with self._lock:
            self._resync()
        self.connected = True
        self.description = f"{self.port} @ {self.baudrate} 8N1"

    def _resync(self) -> None:
        """
        Recover a desynchronized / silent MK3 and (re)set the bus address.

        If a previous program was interrupted mid-frame (a Ctrl-C'd script,
        VEConfigure, a killed process), the MK3 can stop responding entirely —
        it goes silent, no Version heartbeats. Flushing buffers and sending
        5×0x55 before the address-set brings it back (FINDINGS §3; VictronConnect
        sends 0x55 runs as the MK3's autobaud string). Caller must hold self._lock.
        """
        if self.ser is None:
            return
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass
        self.ser.write(bytes([0x55] * 5))   # MK3 autobaud string
        time.sleep(0.05)
        # 'A' <action 1 = set> <address 0>: selects device address 0.
        self.ser.write(bytes([0x04, 0xFF, 0x41, 0x01, 0x00, 0xBB]))
        time.sleep(0.2)
        try:
            if self.ser.in_waiting:
                self.ser.read(self.ser.in_waiting)
        except Exception:
            pass

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None
        self.connected = False

    # ── low-level send/receive ───────────────────────────────────────────────

    def _send(self, frame: bytes) -> Optional[bytes]:
        assert self.ser is not None
        self.ser.write(frame)
        time.sleep(self.POST_CMD_SLEEP)
        if self.ser.in_waiting:
            return self.ser.read(self.ser.in_waiting)
        return None

    def _command_value(self, payload: bytes, resp_subcmd: int) -> Optional[int]:
        """
        Send a 16-bit-value query and scan for its response. Retries 3×.

        Distinguishes two failure modes:
          * We saw bytes (Version frames etc.) but no matching response → the
            setting/var simply isn't there. Return None, no resync.
          * Total silence across all retries → the MK3 is desynced. Resync once
            (flush + sync + re-address) and retry, so a stuck bus self-heals.
        """
        frame = build_frame(payload)
        saw_bytes = False
        for _ in range(self.READ_RETRIES):
            resp = self._send(frame)
            if resp:
                saw_bytes = True
                val = scan_value_response(resp, resp_subcmd)
                if val is not None:
                    self._healthy = True
                    return val
            time.sleep(0.05)

        if not saw_bytes:
            # Silent bus — try to recover, then one more pass.
            self._resync()
            for _ in range(self.READ_RETRIES):
                resp = self._send(frame)
                if resp:
                    saw_bytes = True
                    val = scan_value_response(resp, resp_subcmd)
                    if val is not None:
                        self._healthy = True
                        return val
                time.sleep(0.05)
        self._healthy = saw_bytes  # bytes seen ⇒ link alive, value just absent
        return None

    # ── reads ─────────────────────────────────────────────────────────────────

    def read_setting(self, setting_id: int) -> Optional[int]:
        with self._lock:
            return self._command_value(
                bytes([0xFF, WINMON_SLOT, CMD_READ_SETTING, setting_id]),
                RSP_READ_SETTING)

    def read_ramvar(self, var_id: int) -> Optional[int]:
        with self._lock:
            return self._command_value(
                bytes([0xFF, WINMON_SLOT, CMD_READ_RAMVAR, var_id]),
                RSP_READ_RAMVAR)

    def read_ramvar_info(self, var_id: int) -> Optional[RamVarInfo]:
        """
        GetVariableInfo (0x36) → exact scale/sign/offset for a RAM variable.
        Cached per var (scaling is constant for the session). Applies the
        MultiPlus II quirk where inverter current (var 3) reads negative despite
        the firmware reporting it unsigned.
        """
        if var_id in self._ramvar_info:
            return self._ramvar_info[var_id]
        frame = build_frame(bytes([0xFF, WINMON_SLOT, CMD_GET_RAMVAR_INFO,
                                   var_id & 0xFF, (var_id >> 8) & 0xFF]))
        result = None
        with self._lock:
            for _ in range(self.READ_RETRIES):
                resp = self._send(frame)
                if resp:
                    info = parse_ramvar_info(resp)
                    if info is not None:
                        if var_id == 3:  # MP-II: signed despite unsigned info
                            info.signed = True
                        result = info
                        break
                time.sleep(0.05)
        self._ramvar_info[var_id] = result
        return result

    def get_setting_info(self, setting_id: int) -> Optional[bytes]:
        frame = build_frame(bytes([0xFF, WINMON_SLOT, CMD_GET_INFO, setting_id]))
        with self._lock:
            for _ in range(self.READ_RETRIES):
                resp = self._send(frame)
                if resp:
                    f = find_response(resp, RSP_GET_INFO)
                    if f:
                        return f
                time.sleep(0.05)
        return None

    def _read_synced(self, match, timeout: float = 1.0) -> Optional[bytes]:
        """
        Read length-prefixed, checksum-validated frames from the stream and
        return the first payload for which match(payload) is True.

        Used for the info-frame family (GetConfig 0x41, LED) which — unlike the
        Winmon value responses — does not always start with the 0xFF marker, so
        the dump-and-scan approach can't align to them. Reading length-then-body
        and validating the checksum (resyncing by dropping a byte on mismatch)
        is the robust method (same as victron_mk3 / inverter.py). Caller holds
        the lock.
        """
        assert self.ser is not None
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
            i = 0
            n = len(buf)
            while i < n:
                ln = buf[i]
                if ln == 0 or ln > 64:        # implausible length ⇒ garbage byte
                    i += 1
                    continue
                if i + ln + 1 >= n:           # plausible frame, need more bytes
                    break
                payload = bytes(buf[i + 1:i + 1 + ln])
                cks = buf[i + 1 + ln]
                if (ln + sum(payload) + cks) & 0xFF == 0:
                    if match(payload):
                        return payload
                    i += ln + 2               # consume valid but unwanted frame
                else:
                    i += 1                    # resync
            del buf[:i]
        return None

    def read_config(self) -> Optional[ConfigInfo]:
        frame = build_frame(bytes([0xFF, CMD_CONFIG, 0x05]))  # 'F' 5 = GetConfig
        with self._lock:
            for _ in range(self.READ_RETRIES):
                self.ser.reset_input_buffer()
                self.ser.write(frame)
                payload = self._read_synced(
                    lambda p: len(p) >= 13 and p[0] == RSP_CONFIG, timeout=1.0)
                if payload:
                    self._healthy = True
                    return parse_config(payload)
                time.sleep(0.05)
        return None

    def read_leds(self) -> Optional[LedInfo]:
        frame = build_frame(bytes([0xFF, CMD_LED]))  # 'L'
        with self._lock:
            for _ in range(self.READ_RETRIES):
                self.ser.reset_input_buffer()
                self.ser.write(frame)
                payload = self._read_synced(
                    lambda p: len(p) >= 4 and p[0] == 0xFF and p[1] == RSP_LED,
                    timeout=0.8)
                if payload:
                    self._healthy = True
                    return LedInfo(on=payload[2], blink=payload[3])
                time.sleep(0.05)
        return None

    def _send_state(self, state: int, deciamps: int) -> None:
        """Long-form 'S': state, input limit in A×10 (flags bit 7), as
        VictronConnect and victron_mk3 send it. Never send a limit of 0: it
        means invert-only or bypass, depending on PowerAssist (FINDINGS §4.6)."""
        frame = build_frame(bytes([0xFF, CMD_STATE, state,
                                   deciamps & 0xFF, (deciamps >> 8) & 0xFF, 0x01, 0x80]))
        with self._lock:
            self.ser.write(frame)
            time.sleep(self.WRITE_SLEEP)

    def set_current_limit(self, amps: float, verify: bool = True) -> WriteResult:
        """
        Set the AC input (shore) current limit via the long-form 'S' command,
        re-sending the *current* switch state so operation is not disturbed.
        Verifies by reading the config back (the value is in deci-amps).
        """
        cfg = self.read_config()
        if cfg is None:
            return WriteResult(False, False, None, "No config response — cannot set limit.")
        amps = max(cfg.min_current, min(amps, cfg.max_current))
        value = max(0, min(int(round(amps * 10)), 0x7FFF))
        if value <= 0:
            return WriteResult(False, False, None, "Refusing a 0 A limit (special value).")
        self._send_state(cfg.switch_state_code, value)
        readback = None
        ok = not verify
        if verify:
            time.sleep(2.0)  # device needs a moment to apply + report
            cfg2 = self.read_config()
            if cfg2 is not None:
                readback = int(round(cfg2.actual_current * 10))
                ok = abs(cfg2.actual_current - amps) < 0.2
        msg = (f"Shore limit set to {amps:.1f}A (switch state {cfg.switch_state_name})."
               if ok else "Limit write not confirmed by readback.")
        return WriteResult(ok=ok, acked=False, readback=readback, message=msg)

    def read_version(self) -> Optional[int]:
        """
        Parse an unsolicited Version frame: 07 FF 56 <ver[4 LE]> <mode> <chk>.
        <mode> is the selected device address, or 'B' before one is set.
        Grab a short window and scan it (FINDINGS §5 on how often they arrive).
        """
        assert self.ser is not None
        with self._lock:
            time.sleep(0.2)
            data = self.ser.read(max(self.ser.in_waiting, 24))
        for i in range(len(data) - 6):
            if data[i + 1] == 0xFF and data[i + 2] == RSP_VERSION:
                return (data[i + 3] | (data[i + 4] << 8)
                        | (data[i + 5] << 16) | (data[i + 6] << 24))
        return None

    # ── writes ────────────────────────────────────────────────────────────────

    def write_setting(self, setting_id: int, value: int,
                      ram_only: bool = False, verify: bool = True) -> WriteResult:
        if value is None:
            return WriteResult(False, False, None, "No value to write.")
        flag = FLAG_RAM_ONLY if ram_only else FLAG_RAM_EEPROM
        lo, hi = value & 0xFF, (value >> 8) & 0xFF
        frame = build_frame(
            bytes([0xFF, WINMON_SLOT, CMD_WRITE_VIA_ID, flag, setting_id, lo, hi]))

        acked = False
        refusal = ""
        with self._lock:
            resp = self._send(frame)
            time.sleep(self.WRITE_SLEEP)
            if resp:
                # The two bytes after 0x88 are don't-care (spec §7.3.11).
                acked = find_response(resp, RSP_WRITE_VIA_ID) is not None
                if find_response(resp, RSP_ACCESS_LEVEL):
                    refusal = " Device refused: access level required (0x9B)."
                elif find_response(resp, RSP_NOT_SUPPORTED):
                    refusal = " Device refused: not supported (0x80)."

        readback = None
        if verify:
            # Readback is authoritative: some writes return no 0x88 at all.
            readback = self.read_setting(setting_id)

        ok = (readback == value) if verify else acked
        if verify:
            if ok:
                msg = "Readback confirmed." + ("" if acked else " (no ACK, but value took)")
            else:
                msg = f"Readback mismatch: wrote {value}, read {readback}." + refusal
        else:
            msg = ("ACK received." if acked else "No ACK (unverified).") + refusal
        return WriteResult(ok=ok, acked=acked, readback=readback, message=msg)

    def set_state(self, state: int) -> bool:
        if state not in STATE_NAMES:
            return False
        cfg = self.read_config()
        if cfg is None:
            return False
        limit = int(round(cfg.actual_current * 10))
        if limit <= 0:
            return False
        self._send_state(state, limit)
        return True

    def resync(self) -> None:
        with self._lock:
            self._resync()


# ─────────────────────────────────────────────────────────────────────────────
# Mock backend
# ─────────────────────────────────────────────────────────────────────────────

class MockBackend(Backend):
    """
    Drop-in fake. Mirrors the real Quattro this was developed against so the
    UI looks realistic offline. Telemetry drifts deterministically (no RNG) on
    each read so the auto-refresh visibly moves. Writes update the in-memory
    store and read back correctly.
    """

    def __init__(self):
        self.is_mock = True
        self.description = "MOCK (MultiPlus II)"
        self._healthy = True
        self._tick = 0
        # Mirrors the real MultiPlus II this was developed against.
        self.settings: dict[int, int] = {
            0: 0x8134, 1: 0x45FE, 2: 5680, 3: 5400, 4: 35,
            9: 1, 10: 1, 11: 4450, 15: 0, 16: 1, 17: 6400, 18: 4700,
            60: 16, 64: 200, 65: 190, 72: 242, 73: 5200,
            81: 0,  # no grid code
        }
        # Present-but-not-applicable on this config (ReadSetting → 0xFFFF).
        self._unsupported = {128, 190, 191}
        # 49/88 are Quattro-only and simply don't exist here → no response.
        # Shore (AC input) current limit + switch state for read_config/set_current_limit.
        self._current_limit = 30.0
        self._switch_register = SW_CHARGE | SW_INVERT | SW_FRONT_UP  # on, front up

    def open(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def read_setting(self, setting_id: int) -> Optional[int]:
        if setting_id in self.settings:
            return self.settings[setting_id]
        if setting_id in self._unsupported:
            return UNSUPPORTED
        return None  # no such setting

    def get_setting_info(self, setting_id: int) -> Optional[bytes]:
        if setting_id in self.settings:
            return bytes([0x0E, 0xFF, WINMON_SLOT, RSP_GET_INFO, 0x01,
                          0, 0, 0, 0, 0, 0, 0xFE, 0, 0])
        return None

    def read_version(self) -> Optional[int]:
        return 0x0011DB28

    # GetVariableInfo scaling mirrors the real MultiPlus II (see FINDINGS §7.7).
    _MOCK_INFO = {
        0: RamVarInfo(False, 0.01, 0), 1: RamVarInfo(True, 0.01, 0),
        2: RamVarInfo(False, 0.01, 0), 3: RamVarInfo(True, 0.01, 0),
        4: RamVarInfo(False, 0.01, 0), 5: RamVarInfo(True, 0.1, 0),
        6: RamVarInfo(False, 0.01, 0), 7: RamVarInfo(False, 0.000512, 256),
        8: RamVarInfo(False, 0.001024, 0), 9: RamVarInfo(True, 0.01, 0),
        11: RamVarInfo(bit=4), 12: RamVarInfo(bit=5),
        13: RamVarInfo(False, 0.005, 0),  # SoC: fractional, normalized to %
        14: RamVarInfo(True, 1.0, 0), 15: RamVarInfo(True, 1.0, 0),
        16: RamVarInfo(True, 1.0, 0),
    }

    def read_ramvar_info(self, var_id: int) -> Optional[RamVarInfo]:
        return self._MOCK_INFO.get(var_id)

    def read_ramvar(self, var_id: int) -> Optional[int]:
        t = self._tick
        self._tick += 1
        drift = (t % 7) - 3  # -3..+3, deterministic
        base = {
            0: 24010 + drift,      # mains ~240.1V (×0.01)
            1: signed_to_u16(0),   # no mains current (off-grid)
            2: 24000 + drift,      # inverter ~240V
            3: signed_to_u16(60 + (t % 5)),    # inverter current ×0.01
            4: 5302 + drift,       # battery ~53.0V
            5: signed_to_u16(-22 + ((t % 9) - 4) * 3),  # battery current ×0.1, varies sign
            6: 18 + (t % 6),       # battery ripple ×0.01 → ~0.2V
            7: 70 + (t % 3),       # inverter period → ~60Hz
            8: 162 + (t % 3),      # mains period → ~60Hz
            9: signed_to_u16(75 + (t % 8)),    # signed AC load current ×0.01
            11: 0x8002,            # ignore AC input: bit4 clear → off
            12: 0x8C70,            # multi-func relay: bit5 set → on
            13: 170 + (t % 5),     # SoC ×0.005 → ~85%
            14: signed_to_u16(-96 - (t % 8)),  # DC power (W), discharging
            15: signed_to_u16(0),              # mains power
            16: signed_to_u16(95 + (t % 10)),  # inverter output power
        }
        if var_id in base:
            return base[var_id] & 0xFFFF
        if 0 <= var_id <= 20:
            return 0
        return None

    def write_setting(self, setting_id: int, value: int,
                      ram_only: bool = False, verify: bool = True) -> WriteResult:
        self.settings[setting_id] = value & 0xFFFF
        self._unsupported.discard(setting_id)
        readback = self.settings[setting_id] if verify else None
        # Simulate the high-ID missing-ACK quirk.
        acked = setting_id < 128
        ok = (readback == value) if verify else acked
        msg = "Readback confirmed (mock)." if ok else "Mock mismatch."
        return WriteResult(ok=ok, acked=acked, readback=readback, message=msg)

    _STATE_SWITCHES = {
        SWITCH_CHARGER_ONLY: SW_CHARGE,
        SWITCH_INVERTER_ONLY: SW_INVERT,
        SWITCH_ON: SW_CHARGE | SW_INVERT,
        SWITCH_OFF: 0,
    }

    def set_state(self, state: int) -> bool:
        if state not in self._STATE_SWITCHES:
            return False
        keep = self._switch_register & ~(SW_CHARGE | SW_INVERT)
        self._switch_register = keep | self._STATE_SWITCHES[state]
        return True

    def read_config(self) -> Optional[ConfigInfo]:
        return ConfigInfo(min_current=9.4, max_current=50.0,
                          actual_current=self._current_limit,
                          switch_register=self._switch_register)

    def read_leds(self) -> Optional[LedInfo]:
        # Mains + Bulk lit, like the bench unit; flips inverter on if "off".
        on = 0x01 | 0x04 if (self._switch_register & SW_CHARGE) else 0x10
        return LedInfo(on=on, blink=0x00)

    def set_current_limit(self, amps: float, verify: bool = True) -> WriteResult:
        amps = max(9.4, min(amps, 50.0))
        self._current_limit = round(amps, 1)
        return WriteResult(ok=True, acked=False,
                           readback=int(self._current_limit * 10),
                           message=f"Shore limit set to {self._current_limit:.1f}A (mock).")


def signed_to_u16(v: int) -> int:
    return v & 0xFFFF


# ─────────────────────────────────────────────────────────────────────────────
# Backend factory
# ─────────────────────────────────────────────────────────────────────────────

def open_backend(port: str = "/dev/ttyUSB0", mock: bool = False,
                 fallback_to_mock: bool = True) -> Backend:
    """
    Open a backend. If mock=True, always returns MockBackend. Otherwise tries
    the serial port; on failure, falls back to MockBackend (unless disabled).
    """
    if mock:
        b = MockBackend()
        b.open()
        return b

    try:
        b = SerialBackend(port=port)
        b.open()
        return b
    except Exception as e:
        if not fallback_to_mock:
            raise
        m = MockBackend()
        m.open()
        m.description = f"MOCK (fallback — {port}: {e})"
        return m
