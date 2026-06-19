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

WINMON_SLOT = 0x58  # 'X' — any of 0x57..0x5A works; keep consistent.
VALID_SLOTS = (0x57, 0x58, 0x59, 0x5A)

# Command / response subcommand bytes.
CMD_READ_SETTING = 0x31
RSP_READ_SETTING = 0x86
CMD_WRITE_VIA_ID = 0x37
RSP_WRITE_VIA_ID = 0x88
CMD_GET_INFO = 0x3C
RSP_GET_INFO = 0x89
CMD_READ_RAMVAR = 0x30
RSP_READ_RAMVAR = 0x85
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

# WriteViaID flag byte.
FLAG_RAM_EEPROM = 0x01  # persist to RAM + EEPROM (wears EEPROM)
FLAG_RAM_ONLY = 0x03    # RAM only — lost on power cycle, no EEPROM wear

UNSUPPORTED = 0xFFFF  # ReadSetting value meaning "not applicable on this config"


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
    15: SettingMeta("Unknown toggle 15", 1.0, "", "int"),
    16: SettingMeta("Parameter block 16", 1.0, "", "int"),
    17: SettingMeta("DC threshold 17?", 100.0, "V", "volts"),
    18: SettingMeta("DC threshold 18?", 100.0, "V", "volts"),
    48: SettingMeta("Assist current boost factor", 1.0, "", "int"),
    49: SettingMeta("AC2 input current limit (Quattro)", 10.0, "A", "amps"),
    50: SettingMeta("AES low current limit", 1.0, "", "int"),
    51: SettingMeta("AES current hysteresis", 1.0, "", "int"),
    60: SettingMeta("Mode flag 60", 1.0, "", "int"),
    63: SettingMeta("DC input low pre-alarm offset", 100.0, "V", "volts"),
    64: SettingMeta("Battery capacity", 1.0, "Ah", "int"),
    65: SettingMeta("Battery SoC when bulk finished", 2.0, "%", "volts"),
    72: SettingMeta("Battery charge efficiency", 1.0, "", "int"),
    73: SettingMeta("Voltage threshold 73?", 100.0, "V", "volts"),
    81: SettingMeta("Grid code active", 1.0, "", "int"),
    128: SettingMeta("LOM config A", 1.0, "", "int"),
    190: SettingMeta("LOM config B", 1.0, "", "int"),
    191: SettingMeta("LOM config C", 1.0, "", "int"),
}


def setting_meta(setting_id: int) -> SettingMeta:
    return SETTING_META.get(setting_id, SettingMeta(f"Setting {setting_id}"))


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


# Setting 0 — primary flags register.
SETTING0_BITS: list[FlagBit] = [
    FlagBit(3, "UPS function", "UPS disabled", "UPS enabled (fast transfer)"),
    FlagBit(5, "PowerAssist", "enabled", "disabled"),
    FlagBit(11, "Charge mode", "Adaptive (lead-acid)", "Fixed (LiFePO4)"),
    FlagBit(14, "Weak AC input", "enabled", "disabled"),
    FlagBit(7, "Bit 7 (model default)", "set", "clear",
            confirmed=False, interpret=False),
    FlagBit(15, "Bit 15 (unknown)", "set", "clear",
            confirmed=False, interpret=False),
]

# Setting 1 — secondary flags register.
SETTING1_BITS: list[FlagBit] = [
    FlagBit(11, "Accept Wide Frequency Range", "enabled", "disabled"),
    FlagBit(12, "Dynamic Current Limiter", "enabled", "disabled"),
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


# ─────────────────────────────────────────────────────────────────────────────
# Operating state
# ─────────────────────────────────────────────────────────────────────────────

STATE_NAMES = {
    0x00: "Off",
    0x01: "On (inverting)",
    0x02: "Charger only",
    0x03: "Normal (auto)",
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
# IDs/units confirmed on live hardware and cross-checked with j9brown/victron-mk3.
@dataclass
class TelemetryVar:
    var_id: int
    label: str
    unit: str
    kind: str = "linear"   # linear | period (→ frequency Hz) | bit (→ on/off)


TELEMETRY: list[TelemetryVar] = [
    TelemetryVar(4, "Battery voltage", "V"),
    TelemetryVar(5, "Battery current", "A"),
    TelemetryVar(14, "DC power", "W"),
    TelemetryVar(6, "Battery ripple", "V"),
    TelemetryVar(0, "Mains voltage", "V"),
    TelemetryVar(1, "Mains current", "A"),
    TelemetryVar(15, "Mains power", "W"),
    TelemetryVar(8, "Mains frequency", "Hz", "period"),
    TelemetryVar(2, "Inverter voltage", "V"),
    TelemetryVar(3, "Inverter current", "A"),
    TelemetryVar(16, "Inverter power", "W"),
    TelemetryVar(7, "Inverter frequency", "Hz", "period"),
    TelemetryVar(13, "Battery SoC", "%", "soc"),
    TelemetryVar(9, "AC load current", "A"),
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
                           charge_a: Optional[int]) -> list[WriteStep]:
    """
    Build the exact ordered write sequence VEConfigure uses for the LiFePO4
    fixed charge profile (FINDINGS §7.6). Setting 0 is read-modify-write:
    bit 11 (adaptive charge) is cleared from the *current* value first.

    Charge current is appended as setting 4 when provided.
    """
    new_setting0 = current_setting0 & ~SETTING0_BITS[2].mask  # clear bit 11
    steps = [
        WriteStep(0, new_setting0,
                  "Disable adaptive charging (clear Setting 0 bit 11)",
                  note=f"read-modify-write: 0x{current_setting0:04X} → 0x{new_setting0:04X}"),
        WriteStep(60, 16, "Mode flag (Setting 60 = 16)"),
        WriteStep(65, 190, "Charge parameter (Setting 65 = 190)"),
        WriteStep(72, 242, "Charge parameter (Setting 72 = 242)"),
        WriteStep(10, 1, "Charge characteristic = Fixed (Setting 10 = 1)"),
        WriteStep(2, volts_to_raw(absorption_v),
                  f"Absorption voltage = {absorption_v:.2f}V (Setting 2)"),
        WriteStep(3, volts_to_raw(float_v),
                  f"Float voltage = {float_v:.2f}V (Setting 3)"),
        WriteStep(9, 1, "Absorption time parameter (Setting 9 = 1)"),
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
        raise NotImplementedError

    # ── convenience helpers shared by both backends ────────────────────────

    def read_setting_supported(self, setting_id: int) -> bool:
        v = self.read_setting(setting_id)
        return v is not None and v != UNSUPPORTED

    def identify(self) -> DeviceInfo:
        s0 = self.read_setting(0)
        # 49 and 88 are Quattro-only; either being supported ⇒ Quattro.
        quattro_only = (self.read_setting_supported(88)
                        or self.read_setting_supported(49))
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
        it goes silent, no Version heartbeats. Flushing buffers and sending the
        5×0x55 sync sequence (FINDINGS §3) before the address-set realigns
        framing and brings it back. Caller must hold self._lock.
        """
        if self.ser is None:
            return
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass
        self.ser.write(bytes([0x55] * 5))   # sync / framing realignment
        time.sleep(0.05)
        self.ser.write(bytes([0x04, 0xFF, 0x41, 0x01, 0x00, 0xBB]))  # address set
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
        ss = cfg.switch_state_code
        frame = build_frame(bytes([0xFF, CMD_STATE, ss,
                                   value & 0xFF, (value >> 8) & 0xFF, 0x01, 0x80]))
        with self._lock:
            self.ser.write(frame)
            time.sleep(self.WRITE_SLEEP)
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
        These arrive ~every 100ms, so grab a ~300ms window and scan it.
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
        with self._lock:
            resp = self._send(frame)
            time.sleep(self.WRITE_SLEEP)
            if resp:
                f = find_response(resp, RSP_WRITE_VIA_ID)
                if f and len(f) >= 5 and f[4] == 0x00:
                    acked = True

        readback = None
        if verify:
            # High IDs frequently omit the ACK even on success — readback rules.
            readback = self.read_setting(setting_id)

        ok = (readback == value) if verify else acked
        if verify:
            if ok:
                msg = "Readback confirmed." + ("" if acked else " (no ACK, but value took)")
            else:
                msg = f"Readback mismatch: wrote {value}, read {readback}."
        else:
            msg = "ACK received." if acked else "No ACK (unverified)."
        return WriteResult(ok=ok, acked=acked, readback=readback, message=msg)

    def set_state(self, state: int) -> bool:
        frame = build_frame(bytes([0xFF, CMD_STATE, state]))
        with self._lock:
            self._send(frame)
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

    def set_state(self, state: int) -> bool:
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
