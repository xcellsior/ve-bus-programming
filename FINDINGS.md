# Victron VE.Bus MK2/MK3 Protocol Reference

_Reverse-engineered from USB packet captures, iterative bench testing, and field deployment. This document covers the wire protocol used to read and write inverter/charger configuration settings via the MK3-USB adapter._

---

## 1. Physical Layer

The MK3-USB adapter presents as an FTDI USB-to-serial device. Communication parameters:

| Parameter | Value |
|-----------|-------|
| Baud rate | 2400 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |

On Linux, the device typically appears as `/dev/ttyUSB0` or can be symlinked via udev rules (e.g., `/dev/inverter`). On Windows, it shows as a COM port under "Ports (COM & LPT)" in Device Manager labeled "USB Serial Port."

**Required library**: `pyserial` (`pip install pyserial`)

```python
import serial
ser = serial.Serial(
    port="/dev/ttyUSB0",
    baudrate=2400,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=0.5,
)
```

---

## 2. Frame Format

All MK2 protocol frames follow this structure:

```
<length> 0xFF <payload...> <checksum>
```

| Field | Size | Description |
|-------|------|-------------|
| Length | 1 byte | Number of payload bytes — counts `0xFF` and the command/data bytes, but **not** the length byte itself and **not** the trailing checksum |
| Marker | 1 byte | Always `0xFF` |
| Payload | Variable | Command-specific data |
| Checksum | 1 byte | `(256 - (sum of all preceding bytes) % 256) % 256` |

> **Length byte gotcha.** The length counts everything between itself and the
> checksum (i.e. `0xFF` + payload), excluding the checksum. A ReadSetting frame
> is `04 FF 58 31 <id> <chk>` — the `04` counts `FF 58 31 <id>`, four bytes.
> A WriteViaID frame is `07 FF 58 37 01 <id> <lo> <hi> <chk>` — `07` counts the
> seven bytes `FF 58 37 01 <id> <lo> <hi>`. Do **not** include the checksum in
> the length, or every frame will be rejected.

### Checksum Calculation

```python
def calculate_checksum(data: bytes) -> int:
    return (256 - sum(data) % 256) % 256
```

The checksum covers everything: the length byte, the `0xFF` marker, and all payload bytes. The checksum itself is then appended as the final byte.

### Winmon Slot Bytes

Certain commands include a "Winmon slot" byte immediately after the `0xFF` marker. Observed values are `0x57` (W), `0x58` (X), `0x59` (Y), and `0x5A` (Z). VEConfigure rotates through these round-robin for bus arbitration. In practice, **the inverter accepts commands using any slot byte** — pick one and keep it consistent (the scripts in this project use `0x58`).

---

## 3. Initialization Sequence

Before issuing read/write commands, the MK3 adapter needs an address set. VEConfigure also sends a sync sequence, though this may not be strictly required for simple operations.

### Address Set

Sets the target device address on the VE.Bus. For a single-inverter system, address 1 is standard.

```
TX: 04 FF 41 01 00 BB
```

Breakdown:
- `04` — length (4 bytes follow)
- `FF` — frame marker
- `41` — 'A' command (Address Set)
- `01 00` — address 1, little-endian
- `BB` — checksum

After sending, wait ~100ms before issuing commands. The inverter does not send a distinct ACK for the address set, but the next response frame will use the new address.

### Optional: Sync Sequence

VEConfigure sends 5x `0x55` bytes before the address set. This may help synchronize framing on noisy buses but has not proven necessary in direct USB connections.

```
TX: 55 55 55 55 55
```

---

## 4. Command Reference

### 4.1 ReadSetting (0x31) → Response 0x86

Reads a persistent configuration setting by ID.

**Request frame:**
```
04 FF <slot> 31 <setting_id> <checksum>
```

**Response frame:**
```
05 FF <slot> 86 <value_lo> <value_hi> <checksum>
```

The value is a 16-bit unsigned integer in little-endian byte order.

A response value of `0xFFFF` (65535) generally means the setting ID is **unsupported** on this firmware version. A complete absence of the `0x86` response (only Version frames returned) means the setting ID does not exist at all.

```python
def read_setting(ser, setting_id):
    frame = bytes([0x04, 0xFF, 0x58, 0x31, setting_id])
    frame += bytes([calculate_checksum(frame)])
    for _ in range(3):  # retry — MK3 may interleave Version frames
        ser.write(frame)
        time.sleep(0.1)
        if ser.in_waiting:
            response = ser.read(ser.in_waiting)
            # Scan for 0x86 response
            for i in range(len(response) - 5):
                if (response[i] == 0x05
                        and response[i+1] == 0xFF
                        and response[i+3] == 0x86):
                    lo = response[i+4]
                    hi = response[i+5]
                    return lo | (hi << 8)
        time.sleep(0.05)
    return None
```

### 4.2 WriteViaID (0x37) → Response 0x88

Writes a setting value by ID, persisting to RAM and EEPROM.

**Request frame:**
```
07 FF <slot> 37 01 <setting_id> <value_lo> <value_hi> <checksum>
```

- The `0x01` byte after the command byte is a flag meaning "persist to RAM + EEPROM."

**Response frame:**
```
04 FF <slot> 88 <status> <checksum>
```

- Status `0x00` = success
- Other status values indicate failure (the specific error codes are undocumented)

**Important**: For setting IDs above ~127, the ACK response (`0x88`) may not be returned even when the write succeeds. Always verify with a follow-up ReadSetting to confirm the value took. Allow 50–200ms between writes for EEPROM write cycles.

```python
def write_setting(ser, setting_id, value):
    value_bytes = value.to_bytes(2, byteorder='little')
    frame = bytes([0x07, 0xFF, 0x58, 0x37, 0x01, setting_id]) + value_bytes
    frame += bytes([calculate_checksum(frame)])
    ser.write(frame)
    time.sleep(0.2)
    if ser.in_waiting:
        response = ser.read(ser.in_waiting)
        # Check for 0x88 ACK with status 0x00
        for i in range(len(response) - 4):
            if (response[i+1] == 0xFF
                    and response[i+3] == 0x88
                    and response[i+4] == 0x00):
                return True
    return False  # no ACK — verify with readback
```

### 4.3 GetSettingInfo (0x3C) → Response 0x89

Retrieves metadata about a setting: minimum value, maximum value, default, and scale factor. Useful for understanding value ranges and interpretation.

**Request frame:**
```
04 FF <slot> 3C <setting_id> <checksum>
```

**Response frame:**
```
<len> FF <slot> 89 <payload...> <checksum>
```

The payload structure varies by setting type. For flag/bitmask settings, the maximum field represents a bitmask of which bits are valid. The exact payload layout has not been fully decoded — raw hex is captured in sweep outputs for future analysis.

### 4.4 ReadRAMVar (0x30) → Response 0x85

Reads a live/runtime RAM variable (as opposed to persistent settings). These are used for real-time telemetry: battery voltage, current, power, temperature, operating state, etc.

**Request frame:**
```
04 FF <slot> 30 <var_id> <checksum>
```

**Response frame:**
```
05 FF <slot> 85 <value_lo> <value_hi> <checksum>
```

RAM variables change in real time. Reading the same variable multiple times can yield different values (e.g., fluctuating battery voltage). The variable ID space is 0–255, with the supported set depending on firmware.

### 4.5 WriteRAMVar (0x34)

Writes a RAM variable. Used by VEConfigure during the write phase (e.g., `WriteRAMVar id=0 = 27904` appears to enable a write mode). The exact semantics are poorly understood; use with caution.

**Request frame (observed):**
```
06 FF <slot> 34 <var_id> <value_lo> <value_hi> <checksum>
```

### 4.5b GetVariableInfo (0x36) → Response 0x8E / 0x8F

Returns the **scale, sign, and offset** for a RAM variable, so telemetry can be
decoded exactly instead of guessing divisors. Sent in a Winmon slot ('X'):

**Request frame:**
```
05 FF 58 36 <var_id_lo> <var_id_hi> <checksum>
```

**Response payload (after the slot byte):**
```
FF 58 8E <scale_lo> <scale_hi> 8F <offset_lo> <offset_hi>
```

Decode (matches the `j9brown/victron-mk3` library):

```python
scale = scale_lo | scale_hi << 8
signed = False
if scale >= 0x8000:          # negative ⇒ the quantity is signed
    scale = 0x10000 - scale
    signed = True
if scale >= 0x4000:          # large ⇒ fractional scale
    scale = 1 / (0x8000 - scale)
offset = offset_lo | offset_hi << 8         # signed 16-bit
value = scale * (raw + offset)              # raw sign-extended if `signed`
```

Confirmed scales on the bench MultiPlus II are in §7.7. Scaling is constant for
a session, so query once per variable and cache.

### 4.6 State Command ('S' / 0x53)

Controls the inverter's operating state.

**Simple form (4 bytes payload):**
```
04 FF 53 <state> <checksum>
```

States:
- `0x00` — Force OFF
- `0x01` — Force ON (inverting)
- `0x02` — Force charger only
- `0x03` — Normal operation (automatic)

**Extended form (10 bytes, observed from VEConfigure):**
```
09 FF 53 03 00 FF 01 40 00 04
```

The extended form includes additional flags whose meaning is not fully decoded. The simple form is sufficient for power cycling: send state `0x00`, wait 2–3 seconds, send state `0x01` or `0x03`.

---

## 5. Unsolicited Traffic

The MK3 continuously sends frames without being asked. Your code must handle these when scanning for command responses.

### Version Frame ('V' / 0x56)

```
07 FF 56 28 DB 11 00 42 4E
```

These arrive every ~100ms as heartbeats. When waiting for a command response, you need to scan through received data and skip Version frames to find the actual response. This is why all read/write functions in this project scan the response buffer for the expected subcmd byte rather than assuming the first frame received is the answer.

### LED State Frames

Periodic status frames indicating front-panel LED states. These also arrive unsolicited and should be discarded when scanning for command responses.

---

## 6. Setting ID Discovery

The setting ID space is a single byte (0–255), but only a subset of IDs are supported by any given firmware version. A MultiPlus and a Quattro will support different sets. Firmware updates can add new IDs.

To discover all supported settings, sweep the full range and note which IDs return a value vs. no response:

```python
for setting_id in range(256):
    value = read_setting(ser, setting_id)
    if value is not None and value != 0xFFFF:
        print(f"Setting {setting_id}: {value} (0x{value:04X})")
```

Typical results: 79 supported settings on a MultiPlus, 84 on a Quattro. The IDs are not contiguous — there are gaps throughout the range.

---

## 7. Setting Register Map

### 7.1 Setting 0 — Primary Flags Register

Setting 0 is a 16-bit bitmask controlling major inverter features. The base value varies by model (MultiPlus vs. Quattro have different defaults for some bits).

| Bit | Mask | SET (1) | CLEAR (0) | Confirmed |
|----:|-----:|:--------|:----------|:---------:|
| 2 | 0x0004 | _Unknown_ | _Unknown_ | |
| 3 | 0x0008 | UPS function **disabled** | UPS function **enabled** | ✓ |
| 4 | 0x0010 | _Unknown_ | _Unknown_ | |
| 5 | 0x0020 | PowerAssist **enabled** | PowerAssist **disabled** | ✓ |
| 7 | 0x0080 | _Model-dependent default_ | _Model-dependent default_ | Partial |
| 8 | 0x0100 | _Unknown_ | _Unknown_ | |
| 11 | 0x0800 | Adaptive charge (lead-acid) | Fixed charge (LiFePO4) | ✓ |
| 14 | 0x4000 | Weak AC input **enabled** | Weak AC input **disabled** | ✓ |
| 15 | 0x8000 | _Unknown (set on both models)_ | _Unknown_ | |

**Important behavioral notes:**

- **Bit 3 (UPS)**: This is an inverted/"disable" flag. The bit being CLEAR means UPS is active. UPS enabled gives sub-20ms AC transfer times using electromagnetic relay zero-crossing detection. UPS disabled falls back to ~20 sine-wave cycles of observation before transfer (~333ms–1s at 60Hz).

- **Bit 5 (PowerAssist)**: Supplements AC input with battery power when load exceeds the input current limit. Requires precise waveform tracking.

- **Bit 11 (Adaptive Charge)**: Lead-acid batteries use adaptive absorption duration based on bulk charge time. LiFePO4 batteries use fixed-duration absorption. Clearing this bit is part of applying a LiFePO4 charge profile.

- **Bit 14 (Weak AC)**: Relaxes waveform quality requirements for the AC input. Intended for poor-quality grid or generator connections. In one observed case, having Weak AC enabled on a unit with UPS mode active correlated with degraded AC transfer times (~500ms instead of the expected sub-20ms), though the causal mechanism has not been confirmed by testing. It is possible that Weak AC relaxes the waveform tracking that UPS mode relies on for fast zero-crossing handoff.

- **Bit 7**: Observed to differ between MultiPlus (SET) and Quattro (CLEAR) as a model default. Also changes when a grid code is applied and does not always revert when the grid code is removed. Treat as model-specific; do not blindly copy between different models.

**Example values:**

| Configuration | Value | Binary |
|:---|:---|:---|
| Quattro **or MultiPlus II**, UPS on, PA on, fixed charge, WAC off | `0x8134` | `1000 0001 0011 0100` |
| Quattro, UPS on, PA on, fixed charge, WAC on | `0xC134` | `1100 0001 0011 0100` |
| MultiPlus, UPS off, PA on, fixed charge, WAC off | `0x81BC` | `1000 0001 1011 1100` |
| MultiPlus, UPS on, PA on, fixed charge, WAC off | `0x81B4` | `1000 0001 1011 0100` |
| MultiPlus, UPS off, PA off, fixed charge, WAC on | `0xC194` | `1100 0001 1001 0100` |

### 7.2 Setting 1 — Secondary Flags Register

Setting 1 is another 16-bit bitmask for additional features.

| Bit | Mask | SET (1) | CLEAR (0) | Confirmed |
|----:|-----:|:--------|:----------|:---------:|
| 11 | 0x0800 | Accept Wide Frequency Range **enabled** | **disabled** | ✓ |
| 12 | 0x1000 | Dynamic Current Limiter **enabled** | **disabled** | ✓ |

The remaining bits in Setting 1 have not been individually isolated. Full mapping requires the same toggle-and-diff methodology used for Setting 0.

### 7.3 Charge Profile Settings

These settings control battery charging behavior. Voltage values use ÷100 scaling (e.g., `5680` = `56.80V`).

| ID | Name | Scale | Notes |
|---:|:-----|:------|:------|
| 2 | Absorption voltage | ÷100 → volts | e.g., 5680 = 56.80V |
| 3 | Float voltage | ÷100 → volts | e.g., 5400 = 54.00V |
| 4 | Charge current | Direct amps | e.g., 70 = 70A |
| 9 | Absorption time / param | Varies | Set to 1 for LiFePO4 fixed profile |
| 10 | Charge characteristic | Enum | 0=variable (lead-acid), 1=fixed (LiFePO4), 2=fixed+storage |
| 11 | Low battery cutoff | ÷100 → volts | e.g., 4450 = 44.50V |

### 7.4 Grid Code / LOM Settings

These settings appear when a grid code has been configured and control Loss of Mains (LOM) detection behavior.

| ID | Description | Behavior |
|---:|:------------|:---------|
| 81 | Grid code active flag | 0 = no grid code, 1 = grid code active |
| 128 | LOM configuration A | Value depends on selected grid code and LOM mode |
| 190 | LOM configuration B | Varies slightly by LOM mode; may be read-only or firmware-managed |
| 191 | LOM configuration C | Value depends on selected grid code and LOM mode |

**Observed values across configurations:**

| Setting | No Grid Code | Grid Code "Other" + LOM B | Grid Code "Other" + No LOM | After Revert to "None" |
|--------:|:-------------|:--------------------------|:---------------------------|:----------------------|
| 81 | 0 | 1 | 1 | 0 |
| 128 | unsupported | 1 (0x0001) | 257 (0x0101) | 512 (0x0200) — **residual** |
| 190 | unsupported | 65525 (0xFFF5) | 65526 (0xFFF6) | 65525 (0xFFF5) — **residual** |
| 191 | unsupported | 1 (0x0001) | 257 (0x0101) | 512 (0x0200) — **residual** |

**Critical finding**: Reverting a grid code back to "None" does not fully clean up. Settings 128, 190, and 191 persist with residual values instead of returning to the "unsupported" state. These residuals can cause behavioral issues, particularly when LOM detection interferes with generator disconnect handling.

To clean up residuals, write `0xFFFF` to settings 128 and 191. Setting 190 appears to be read-only or firmware-managed — writes to it are silently ignored, but its residual value (`0xFFF5`) does not appear to cause behavioral problems in practice.

### 7.5 Other Settings (Partial)

Settings in the ranges 16–27, 28–39, and 50–59 appear to be parameter blocks that repeat across groups (possibly per-AC-input or per-operating-mode). Identical values across these ranges for some parameters suggest the same setting repeated for different contexts.

| ID | Likely Function | Notes |
|---:|:----------------|:------|
| 15 | Unknown toggle | Binary 0/1, differs between otherwise identical units |
| 16 | Parameter (block 1) | Paired with 28 and 52 |
| 17 | DC voltage threshold? | ÷100, often 6400 = 64.00V — battery overvoltage disconnect on a 48V system |
| 18 | DC voltage threshold? | ÷100, often 4700 = 47.00V |
| 49 | AC2 input current limit | Quattro-only (MultiPlus has only AC1) |
| 60 | Solar & Wind Priority flag | Bitmask: bit 4 = enabled; 16 = off, 528 = on |
| 64 | Battery capacity | Ah; 0 = battery monitor disabled |
| 65 | Battery SoC when bulk finished | ×0.5 → %. 190 = 95% after LiFePO4 profile |
| 72 | Battery charge efficiency | 242 ≈ 95% after LiFePO4 profile |
| 73 | Voltage threshold? | ÷100, varies significantly between configs |
| 88 | Solar & Wind Priority voltage | Sustain voltage ×100 (1320 = 13.20 V for LiFePO4) |

Additional setting IDs identified from the `victron-vebus-mk3-control` library
(names; scales partly confirmed on the bench MultiPlus II):

| ID | Name | Notes |
|---:|:-----|:------|
| 5 | Inverter output voltage | direct V (120 on the 120 V bench unit) |
| 6 | AC1 input current limit | ÷10 A — the persistent form of the "shore" limit (matched the runtime 9.9 A) |
| 7 | Repeated absorption time | |
| 8 | Repeated absorption interval | |
| 9 | Maximum absorption time | (set to 1 for the LiFePO4 fixed profile) |
| 11 | DC input low shutdown | ÷100 V (low-battery cutoff) |
| 12 | DC input low restart offset | ÷100 V above the shutdown voltage |
| 48 | Assist current boost factor | |
| 49 | AC2 input current limit | Quattro-only |
| 64 | Battery capacity | Ah |

**Note — possible Setting 0 bit 11 discrepancy.** This project reverse-engineered
Setting 0 bit 11 as adaptive(set)/fixed(clear) charge (§7.1, confirmed by the
LiFePO4 toggle-and-diff). The `victron-vebus-mk3-control` flag map instead labels
Flags0 bit 11 "reduced float enabled". These may be firmware/model differences;
the §7.1 interpretation is the one validated on this hardware and is what the
tooling uses. Treat the other flag-bit names from that library as unconfirmed
here until isolated by toggle-and-diff.

### 7.6 Applying a LiFePO4 "Fixed" Charge Profile

VEConfigure writes these 8 settings when switching to the LiFePO4 fixed charge profile. Order matters — the flags register is written first to disable adaptive charging before the profile mode and voltages are changed.

| Order | Setting ID | Value | Purpose |
|------:|-----------:|------:|:--------|
| 1 | 0 | Clear bit 11 | Disable adaptive charging |
| 2 | 60 | 16 | Mode flag |
| 3 | 65 | 190 | Battery SoC when bulk finished (×0.5 → 95%) |
| 4 | 72 | 242 | Battery charge efficiency (≈95%) |
| 5 | 10 | 1 | Charge characteristic = fixed |
| 6 | 2 | 5680 | Absorption voltage = 56.80V |
| 7 | 3 | 5400 | Float voltage = 54.00V |
| 8 | 9 | 1 | Absorption time parameter |

### 7.7 RAM Variable Map (Live Telemetry)

RAM variables (ReadRAMVar 0x30 → 0x85) hold live measurements. Their scale/sign
should be read from **GetVariableInfo (0x36)** rather than guessed — see §4.5b.
IDs and scales below were confirmed on the bench MultiPlus II and cross-checked
against the `j9brown/victron-mk3` library:

| ID | Quantity | Scale | Signed | Notes |
|---:|:---------|:------|:------:|:------|
| 0 | Mains voltage | ×0.01 V | no | |
| 1 | Mains current | ×0.01 A | yes | |
| 2 | Inverter voltage | ×0.01 V | no | |
| 3 | Inverter current | ×0.01 A | no* | *MultiPlus II reports negative values despite the info flagging it unsigned — override to signed |
| 4 | Battery voltage | ×0.01 V | no | e.g. 5302 → 53.02 V |
| 5 | Battery current | ×0.1 A | yes | e.g. 0xFFEA → −2.2 A |
| 6 | Battery ripple voltage | ×0.01 V | no | |
| 7 | Inverter period | ×0.000512 (+256) | no | frequency = 10 / period Hz |
| 8 | Mains period | ×0.001024 | no | frequency = 10 / period Hz |
| 9 | Signed AC load current | ×0.01 A | yes | |
| 10 | Virtual switch position | — | — | not supported on the bench MultiPlus II |
| 11 | Ignore AC input | bit 4 | — | boolean (see "bit variables" below) |
| 12 | Multi-functional relay state | bit 5 | — | boolean |
| 13 | Battery State of Charge | ×0.005 → ×100 % | no | only meaningful when the battery monitor is on (setting 64 capacity > 0); otherwise reads 100 % |
| 14 | DC power | ×1 W | yes | negative = discharging |
| 15 | Mains power | ×1 W | yes | |
| 16 | Inverter power | ×1 W | yes | |

**Frequency** is derived from the period variables: `f = round(10 / (scale·raw), 2)` Hz.

**Bit (boolean) variables.** When the GetVariableInfo *offset* field is `0x8000`,
the variable is a single boolean bit at `bit = |scale field| − 1` of the raw
value (e.g. var 11 scale `0x0005` → bit 4; var 12 scale `0x0006` → bit 5). A scale
field of `0` means the variable is unsupported.

**Battery SoC** uses a fractional scale that yields a 0–1 value; multiply by 100
for a percentage.

**Library cross-references.** Two complementary open-source references were used
to confirm and extend this map; neither covers the persistent setting read/write
(`0x31`/`0x37`) or charge-profile work that is this project's focus:
- `j9brown/victron-mk3` — `V`/`F`/`L`/`S`/`H` info frames and `W`/`X`/`Y`/`Z` RAM reads.
- `victron-vebus-mk3-control` (Home Assistant integration) — identifies RAM
  variables 6/9/10/11/12/13 (above) and a battery-monitor **setting** map (§7.5).

---

## 8. Read-Modify-Write for Flag Registers

When changing a single bit in a flags register (Setting 0 or Setting 1), always use a read-modify-write pattern to avoid clobbering other flags:

```python
# Example: Disable Weak AC (clear bit 14 of Setting 0)
current = read_setting(ser, 0)
if current is not None and (current & 0x4000):
    new_value = current & ~0x4000  # clear bit 14 only
    write_setting(ser, 0, new_value)
    verify = read_setting(ser, 0)
    assert verify == new_value
```

**Never** blindly write a full flags value copied from a different unit or model. The base flag values differ between MultiPlus and Quattro, and unknown bits may have model-specific defaults.

---

## 9. Practical Notes and Gotchas

### MK3 Heartbeat Traffic

The MK3 sends Version frames (`0x56`) roughly every 100ms regardless of whether commands are being sent. Any read operation must scan through the response buffer and skip these frames to find the actual command response. A simple approach is pattern-matching for the expected response subcmd byte.

### Timing

- Allow **100ms** after each command before reading the response (`time.sleep(0.1)` after `ser.write()`).
- Allow **200ms** between consecutive writes for EEPROM write cycles.
- VEConfigure inserts a ~250ms gap before its last batch of writes, possibly for EEPROM cooldown.
- **3 retries** per read is a good default to handle cases where a Version frame lands in the read window instead of the command response.

### ACK Behavior for High Setting IDs

WriteViaID responses (`0x88`) are reliably returned for setting IDs in the low range (0–80). For higher IDs (128+), the ACK response is often missing even when the write succeeds. Always verify with a follow-up ReadSetting rather than relying solely on the ACK.

### "Unsupported" vs. "No Response"

- Value `0xFFFF` returned by ReadSetting: the setting ID exists in the firmware's table but is not applicable to the current configuration. The setting *can* become active if the configuration changes (e.g., enabling a grid code activates settings 128/190/191).
- No response at all (only Version frames after retries): the setting ID does not exist in this firmware version.

These are functionally different states — a setting that was never supported behaves differently from one that was activated and then reverted.

### Grid Code Residuals

Setting a grid code and then reverting to "None" leaves residual configuration behind. This is a firmware behavior, not a protocol issue. The residuals include:

1. Settings 128/191 retaining their grid-code values instead of returning to unsupported
2. Setting 190 retaining its value (and this value cannot be overwritten)
3. Certain bits in Setting 0 (like bit 14 / Weak AC) may remain set

Writing `0xFFFF` to settings 128 and 191 successfully clears them. Setting 190 writes are silently dropped. The Weak AC bit in Setting 0 must be manually cleared via read-modify-write.

### Cross-Model Differences

Do not assume settings or flag values are portable between different Victron models. Confirmed differences:

| Aspect | MultiPlus | Quattro |
|:-------|:----------|:--------|
| Setting 0 base value | `0x81BC` (original MultiPlus, typical) | `0x8134` (typical) |
| Setting 0 bit 7 | SET (original MultiPlus) | CLEAR |
| Supported setting count | ~79 | ~84 |
| Settings 49, 88 | Not supported | Supported |
| Settings 128/190/191 | Only with grid code | Only with grid code |

**Correction — Setting 0 bit 7 and `0x8134` are NOT reliable model discriminators.**
A **MultiPlus II** was directly observed reading Setting 0 = `0x8134` with **bit 7
CLEAR** — the same value/flag state this table previously attributed to a Quattro.
So bit 7 distinguishes (at most) the *original* MultiPlus from the others, not the
model family in general, and `0x8134` is not Quattro-exclusive.

The reliable way to identify a Quattro is **setting 49** (AC2 input current limit):
present (responds) on a Quattro, absent (no response) on MultiPlus / MultiPlus II.
Setting 88 (Solar & Wind Priority sustain voltage) is available on both models —
use only as confirmation, not as the discriminator. Setting 0 bit 7 is unreliable
for model detection (a MultiPlus II was observed reading `0x8134` with bit 7 CLEAR).
Use setting 49 for model detection; treat bit 7 as model-specific raw data.

### Grid Code Password

The grid code configuration in VEConfigure (specifically the "no LOM detection" option) requires a password. The password (`TPWMBU2A4GCC`) is widely known in the Victron community.

### VEConfigure Write Sequence

When VEConfigure writes settings, it follows this per-setting pattern:

1. `GetSettingInfo` (0x3C) — query metadata (min/max/scale)
2. `ReadSetting` (0x31) — read current value
3. `WriteViaID` (0x37) — write new value
4. Wait for ACK (0x88)

For direct scripting, steps 1 and 2 can be skipped if you already know the valid range and current value. The `GetSettingInfo` step is useful during discovery but adds latency to bulk operations.

---

## 10. Complete Python Recipes

### Read a single setting

```python
import serial, time

def read_setting(port, setting_id):
    ser = serial.Serial(port, 2400, timeout=0.5)
    # Set address
    addr = bytes([0x04, 0xFF, 0x41, 0x01, 0x00, 0xBB])
    ser.write(addr)
    time.sleep(0.1)
    ser.read(ser.in_waiting)  # discard

    frame = bytes([0x04, 0xFF, 0x58, 0x31, setting_id])
    chk = (256 - sum(frame) % 256) % 256
    frame += bytes([chk])

    for _ in range(3):
        ser.write(frame)
        time.sleep(0.1)
        if ser.in_waiting:
            resp = ser.read(ser.in_waiting)
            for i in range(len(resp) - 5):
                if resp[i] == 0x05 and resp[i+1] == 0xFF and resp[i+3] == 0x86:
                    val = resp[i+4] | (resp[i+5] << 8)
                    ser.close()
                    return val
        time.sleep(0.05)
    ser.close()
    return None
```

### Write a single setting

```python
def write_setting(port, setting_id, value):
    ser = serial.Serial(port, 2400, timeout=0.5)
    ser.write(bytes([0x04, 0xFF, 0x41, 0x01, 0x00, 0xBB]))
    time.sleep(0.1)
    ser.read(ser.in_waiting)

    lo = value & 0xFF
    hi = (value >> 8) & 0xFF
    frame = bytes([0x07, 0xFF, 0x58, 0x37, 0x01, setting_id, lo, hi])
    chk = (256 - sum(frame) % 256) % 256
    frame += bytes([chk])

    ser.write(frame)
    time.sleep(0.2)
    if ser.in_waiting:
        ser.read(ser.in_waiting)

    # Verify with readback
    verify = read_setting_raw(ser, setting_id)  # reuse read logic
    ser.close()
    return verify == value
```

### Flip a single bit in a flags register

```python
def set_bit(port, setting_id, bit, enable=True):
    current = read_setting(port, setting_id)
    if current is None:
        return False
    if enable:
        new_val = current | (1 << bit)
    else:
        new_val = current & ~(1 << bit)
    if new_val == current:
        return True  # already in desired state
    return write_setting(port, setting_id, new_val)
```

---

## 11. Methodology for Mapping Unknown Settings

The most reliable way to identify what an unknown setting controls:

1. Run a full setting sweep (IDs 0–255) and save to CSV as a baseline
2. Change **one** parameter in VEConfigure
3. Run the sweep again and diff the two CSV files
4. The changed setting ID(s) reveal what that parameter maps to

```bash
python discover_settings.py /dev/inverter -o baseline.csv
# ... change one thing in VEConfigure ...
python discover_settings.py /dev/inverter -o after.csv
diff baseline.csv after.csv
```

For flag registers (Settings 0 and 1), toggle one on/off switch at a time and diff. Each toggle isolates one bit. Be aware that VEConfigure may silently change related settings when you toggle something — always change one thing at a time and verify nothing else moved unexpectedly.

For RAM variables (live telemetry), read each variable multiple times and look for values that change between reads. Static values are configuration state; changing values are live measurements. Cross-reference plausible ranges for your system voltage (e.g., 48V nominal → battery voltage readings in the 4400–5800 range with ÷100 scaling).