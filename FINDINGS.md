# Victron VE.Bus MK2/MK3 Protocol Reference

_Reverse-engineered from USB packet captures, iterative bench testing, and field deployment. This document covers the wire protocol used to read and write inverter/charger configuration settings via the MK3-USB adapter._

_Cross-checked (October 2026) against Victron's MK2 Protocol 3.14 PDF ("the PDF") and a static read of the VE.Bus driver inside VictronConnect ("VictronConnect"; §12). Corrections from that pass are marked **Corrected**. §12 lists the setting, flag and RAM-variable names VictronConnect uses, and §13 lists what still needs a bench test._

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

If bit 7 of the length byte is set, the frame has LED status appended (PDF §3.2). That happens only when bit 1 of the `S` command's flags byte asks for it (§4.6).

### Winmon Slot Bytes

Certain commands include a "Winmon slot" byte immediately after the `0xFF` marker. Observed values are `0x57` (W), `0x58` (X), `0x59` (Y), and `0x5A` (Z). In practice, **the inverter accepts commands using any slot byte** — pick one and keep it consistent (the scripts in this project use `0x58`).

**Corrected:** the letter is the Winmon command, not a bus-arbitration slot (PDF §7.3):

- `W` is the original command.
- `X`, `Y` and `Z` behave the same, and the reply echoes the letter, so successive commands can be told apart.
- Lowercase `x`, `y` and `z` add a destination address at the end of the frame.

VictronConnect rotates `X` → `Y` → `Z` and matches each reply's letter against its request. A fixed letter works as long as only one command is outstanding at a time.

---

## 3. Initialization Sequence

Before issuing read/write commands, the MK3 adapter needs an address set. VEConfigure also sends a sync sequence, though this may not be strictly required for simple operations.

### Address Set

Selects which VE.Bus device the following Winmon commands go to.

```
TX: 04 FF 41 01 00 BB
```

Breakdown:
- `04` — length (4 bytes follow)
- `FF` — frame marker
- `41` — 'A' command (Address Set)
- `01` — action: bit 0 set means "set the address" (clear means "just report it")
- `00` — address 0
- `BB` — checksum

**Corrected:** this frame selects address **0**, not address 1. The format is `'A' <action> <address>` (PDF §7.1), and VictronConnect builds exactly `04 FF 41 01 <address>`. On a single-unit system address 0 is the only device, which is why every script here works.

The MK3 does reply, with `'A' <action> <address>`. It starts at address `0xFF`, which is invalid. Valid addresses are `0x00`–`0x1F`, and the Version frame's last byte shows the current address (`'B'` until one is set, §5).

On multi-unit systems, VictronConnect treats addresses 0–2 as the phase masters for L1–L3 and higher addresses as slaves, with phase = address mod 3 (§12.8). That fits the bench note that the MK3-attached unit of the split-phase pair is address 1, L2.

After sending, wait ~100ms before issuing commands.

### Optional: Sync Sequence

VEConfigure sends 5x `0x55` bytes before the address set. On this bench it brings a silent MK3 back after an interrupted session, and the TUI's resync relies on that.

```
TX: 55 55 55 55 55
```

VictronConnect sends runs of `0x55` only as the MK3's autobaud string, when it switches the MK3's baud rate (§12.9). That is probably why it revives a confused MK3; it isn't needed for normal 2400-baud traffic.

---

## 4. Command Reference

Winmon reply codes worth recognising:

| Code | Meaning |
|-----:|:--------|
| `0x80` | Command or argument not recognised |
| `0x85` / `0x90` | RAM read OK / RAM variable not supported |
| `0x86` / `0x91` | Setting read OK / setting not supported |
| `0x87` / `0x88` | RAM variable written / setting written |
| `0x89` | Setting info |
| `0x8E` | RAM variable info |
| `0x94` | Device state |
| `0x99` | Snapshot |
| `0x9B` | Write refused: access level required |

These codes come from the PDF and VictronConnect's reply parsers. §12.1 lists every command VictronConnect sends.

### 4.1 ReadSetting (0x31) → Response 0x86

Reads a persistent configuration setting by ID.

**Request frame (as this repo sends it):**
```
04 FF <slot> 31 <setting_id> <checksum>
```

**Request frame (PDF and VictronConnect):**
```
05 FF <slot> 31 <setting_id_lo> <setting_id_hi> <checksum>
```

The bench answers the one-byte form for IDs 0–255; the two-byte form is the documented one.

**Response frame:**
```
05 FF <slot> 86 <value_lo> <value_hi> <checksum>
```

The value is a 16-bit unsigned integer in little-endian byte order.

A response value of `0xFFFF` (65535) shows up for settings that exist but don't apply to the current configuration (see §9).

**Corrected:** a setting the firmware doesn't have is answered with reply code `0x91`, not with silence (PDF §7.3.4). The scanners here match only `0x86`, so a `0x91` reply looked like "no response".

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

- The byte after the command byte is a flags byte. **Corrected** (PDF §7.3.11, firmware ≥ 412):
  - bit 0: 1 = setting, 0 = RAM variable
  - bit 1: 1 = RAM only, 0 = RAM + EEPROM

  So `0x01` writes a setting to RAM + EEPROM, and `0x03` writes it to RAM only.

**Response frame:**
```
<len> FF <slot> <code> XX XX <checksum>
```

- `0x88` = setting written, `0x87` = RAM variable written.
- `0x80` = not supported, `0x9B` = access level required.
- The two bytes after the code are don't-care. **Corrected:** this doc used to treat a `00` after `0x88` as the success marker, which can mark a good write as failed.

**Important**: For setting IDs above ~127, the ACK response (`0x88`) may not be returned even when the write succeeds. Always verify with a follow-up ReadSetting to confirm the value took. Allow 50–200ms between writes for EEPROM write cycles. (Some of those "missing ACKs" may be `0x9B` refusals that the scanner discarded; see §9.)

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

### 4.3 GetSettingInfo (0x35) → Response 0x89

Retrieves metadata about a setting: minimum value, maximum value, default, and scale factor. Useful for understanding value ranges and interpretation.

**Corrected:** the documented command is `0x35` (PDF §7.3.8), and it is the only one VictronConnect sends. This repo's tools send `0x3C`, recorded from a VEConfigure capture. `0x3C` appears in neither the PDF nor VictronConnect, so whether it is an undocumented alias is a bench question (§13).

**Request frame (PDF and VictronConnect):**
```
05 FF <slot> 35 <setting_id_lo> <setting_id_hi> <checksum>
```

**Request frame (as this repo sends it):**
```
04 FF <slot> 3C <setting_id> <checksum>
```

**Response frame (long Winmon mode):**
```
<len> FF <slot> 89 <scale16> <offset16> <default16> <min16> <max16> <access> <checksum>
```

All fields are little-endian. VictronConnect's parser reads them in this order and takes the access-level byte when the frame is long enough.

- Scale: signed. If it is > 0 the scale is that value; otherwise it is `1 / -scale`.
- Display value: `scale * (raw + offset)`.
- For flag registers, `max` is a mask of the valid bits.
- A setting the firmware lacks returns a bare `0x86` or a scale of 0 (PDF).

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

One request can carry up to six one-byte IDs (`30 <id0> … <id5>`). The `0x85` reply then holds one 16-bit value per ID, in order; `victron_mk3` reads 14, 15 and 16 in a single request. `0x90` = variable not supported (PDF §7.3.3).

### 4.5 WriteRAMVar (0x32), WriteSetting (0x33) and WriteData (0x34)

**Corrected:** `0x34` is **WriteData**, not WriteRAMVar (PDF §7.3.5–7.3.7). A write is two frames:

1. `0x32 <id_lo> <id_hi>` (RAM variable) or `0x33 <id_lo> <id_hi>` (setting). No reply.
2. `0x34 <data_lo> <data_hi>`. Replies `0x87` (RAM) or `0x88` (setting).

The VEConfigure frames this doc used to call "WriteRAMVar" (`34 …`, e.g. "id=0 = 27904") are WriteData frames. They belonged to the `0x32`/`0x33` frame just before them.

VictronConnect uses this two-step for settings, as well as `0x37` WriteViaID (§12.1). The README's note that the two-step "does not work" was a bench result whose cause is unknown; the one-byte setting ID used elsewhere in this repo is one candidate.

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

Sets the panel state: switch position plus AC input current limit (PDF §5). **Corrected:** this section used to describe a short `04 FF 53 <state>` form with states 0–3. That form is not in the PDF and VictronConnect never sends it, and the state numbers were wrong.

**Frame (as VictronConnect and `victron_mk3` send it):**
```
<len> FF 53 <state> <limit_lo> <limit_hi> 01 <flags> [<reserved> <flags_ext0> [<eeprom_flags>]] <checksum>
```

States:
- `1` — Charger only
- `2` — Inverter only
- `3` — On
- `4` — Off

There is no state 0.

`<flags>`:

| Bit | Meaning |
|----:|:--------|
| 0 | Send the panel state to VE.Bus automatically every ½ s |
| 1 | Append LED status to every frame the MK3 sends (length bit 7 set) |
| 4 | Don't send the panel state |
| 6 | Forward panel frames received from other devices |
| 7 | 1 = limit in A × 10; 0 = limit as pot value + panel scale |

`<flags_ext0>`:

| Bit | Meaning |
|----:|:--------|
| 0 | Winmon mode 1 (short frames) |
| 2 | Forward config responses from all devices |
| 3 | Ignore block-mode requests |

VictronConnect always sets flags bit 7, so the limit is in A × 10. `victron_mk3` sends `<len> FF 53 <state> <lo> <hi> 01 80`.

**A limit of 0 is special:** with PowerAssist on, the unit switches to invert mode; with it off, it goes to bypass with the charger disabled (PDF §5). Never send 0 as a default.

**The VEConfigure capture decoded:**
```
09 FF 53 03 00 FF 01 40 00 04   (checksum not shown)
```
- state 3 (On)
- pot value `0x00`, panel scale `0xFF` (flags bit 7 clear, so not amps)
- `01`
- flags `0x40`: forward panel frames
- reserved `00`
- flags_ext0 `0x04`: forward config responses from all devices

To power-cycle, send Off (4), wait 2–3 seconds, then On (3), each with the current limit you want to keep.

---

## 5. Unsolicited Traffic

The MK3 continuously sends frames without being asked. Your code must handle these when scanning for command responses.

### Version Frame ('V' / 0x56)

```
07 FF 56 28 DB 11 00 42 4E
```

Format: `'V' <version, 4 bytes LE> <mode>`. The sample decodes to version `0x0011DB28` = **1170216**, which is the MK3 firmware VictronConnect bundles. Mode `0x42` = `'B'` means no device address had been set yet; once one is set, `<mode>` holds it (PDF §3.4).

This doc observed these arriving every ~100ms as heartbeats. The PDF says roughly once per second when nothing else is being sent; either way, a reader must skip them. When waiting for a command response, you need to scan through received data and skip Version frames to find the actual response. This is why all read/write functions in this project scan the response buffer for the expected subcmd byte rather than assuming the first frame received is the answer.

### LED State Frames

LED status arrives either as the reply to an `L` command, or appended to every MK3 frame (length bit 7 set) when the `S` flags ask for it (§4.6). Discard it when scanning for command responses.

The `L` reply carries more than the two LED bytes; VictronConnect decodes alarms, warnings and the VE.Bus error code from it (§12.5).

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

The PDF defines ReadSetting with a 16-bit ID. VictronConnect's name table covers IDs 0–88 (§12.2); 128, 190 and 191 (§7.4) are outside it.

---

## 7. Setting Register Map

### 7.1 Setting 0 — Primary Flags Register

Setting 0 is a 16-bit bitmask controlling major inverter features. The base value varies by model (MultiPlus vs. Quattro have different defaults for some bits).

Names come from VictronConnect's flag table (§12.3). ✓ = isolated on the bench by toggle-and-diff; the rest are VictronConnect's names, not yet toggled here.

| Bit | Mask | Meaning when SET | Bench |
|----:|-----:|:-----------------|:-----:|
| 0 | 0x0001 | Multi-phase system | |
| 1 | 0x0002 | Multi-phase leader | |
| 2 | 0x0004 | 60 Hz (clear = 50 Hz) | |
| 3 | 0x0008 | Disable wave check = UPS function **disabled** | ✓ |
| 4 | 0x0010 | Don't stop after 10 h bulk | |
| 5 | 0x0020 | PowerAssist **enabled** | ✓ |
| 6 | 0x0040 | Charger disabled | |
| 7 | 0x0080 | Disable back-feed lock on large currents | |
| 8 | 0x0100 | Disable AES | |
| 9 | 0x0200 | _Reserved_ — VictronConnect labels it "don't change me!" | |
| 10 | 0x0400 | _Reserved_ — "don't change me!" | |
| 11 | 0x0800 | **Reduced float (storage mode) enabled** | |
| 12 | 0x1000 | _Reserved_ — "don't change me!" | |
| 13 | 0x2000 | Disable ground relay | |
| 14 | 0x4000 | Weak AC input **enabled** | ✓ |
| 15 | 0x8000 | Remote overrules AC2 | |

**Important behavioral notes:**

- **Bit 3 (UPS)**: This is an inverted/"disable" flag; VictronConnect names it "disable wave check". The bit being CLEAR means UPS is active. UPS enabled gives sub-20ms AC transfer times using electromagnetic relay zero-crossing detection. UPS disabled falls back to ~20 sine-wave cycles of observation before transfer (~333ms–1s at 60Hz).

- **Bit 5 (PowerAssist)**: Supplements AC input with battery power when load exceeds the input current limit. Requires precise waveform tracking.

- **Bit 11 (Reduced float / storage mode)**. **Corrected:** this doc called bit 11 adaptive (set) vs. fixed (clear) charge. VictronConnect names it "enable reduced float", matching the `victron-vebus-mk3-control` library. Adaptive vs. fixed lives in **setting 10** (charge characteristic). VEConfigure's LiFePO4 template turns storage mode off and sets setting 10 to fixed in the same pass, which is why the diff pointed at bit 11.

- **Bit 14 (Weak AC)**: Relaxes waveform quality requirements for the AC input. Intended for poor-quality grid or generator connections. In one observed case, having Weak AC enabled on a unit with UPS mode active correlated with degraded AC transfer times (~500ms instead of the expected sub-20ms), though the causal mechanism has not been confirmed by testing. It is possible that Weak AC relaxes the waveform tracking that UPS mode relies on for fast zero-crossing handoff.

- **Bit 7**: Observed to differ between MultiPlus (SET) and Quattro (CLEAR) as a model default. Also changes when a grid code is applied and does not always revert when the grid code is removed. Treat as model-specific; do not blindly copy between different models.
  - VictronConnect names it "disable back-feed lock on large currents".
  - `victron-vebus-mk3-control` treats it as an inverted copy of bit 3. The `0x81BC`/`0x81B4` pair below contradicts that library: bit 3 flips while bit 7 stays set.

- **Bits 0, 1 and 2** describe the system itself (multi-phase membership, mains frequency). Never toggle them from a script.

**Example values:**

| Configuration | Value | Binary |
|:---|:---|:---|
| Quattro **or MultiPlus II**, UPS on, PA on, reduced float off, WAC off | `0x8134` | `1000 0001 0011 0100` |
| Quattro, UPS on, PA on, reduced float off, WAC on | `0xC134` | `1100 0001 0011 0100` |
| MultiPlus, UPS off, PA on, reduced float off, WAC off | `0x81BC` | `1000 0001 1011 1100` |
| MultiPlus, UPS on, PA on, reduced float off, WAC off | `0x81B4` | `1000 0001 1011 0100` |
| MultiPlus, UPS off, PA off, reduced float off, WAC on | `0xC194` | `1100 0001 1001 0100` |

With the full map, `0x8134` reads as: 60 Hz, don't stop after 10 h bulk, PowerAssist, AES disabled, remote overrules AC2.

### 7.2 Setting 1 — Secondary Flags Register

Setting 1 is another 16-bit bitmask for additional features. It holds VictronConnect's flags 16–31 (bit N = flag 16 + N, §12.3).

| Bit | Mask | Meaning when SET | Bench |
|----:|-----:|:-----------------|:-----:|
| 0 | 0x0001 | VS on bulk protection | |
| 1 | 0x0002 | VS on temperature warning | |
| 2 | 0x0004 | VS on battery warning | |
| 3 | 0x0008 | VS on overload warning | |
| 4 | 0x0010 | VS on ripple warning | |
| 5 | 0x0020 | VS off temperature warning | |
| 6 | 0x0040 | VS off battery warning | |
| 7 | 0x0080 | VS off overload warning | |
| 8 | 0x0100 | VS off ripple warning | |
| 9 | 0x0200 | VS on when failure present | |
| 10 | 0x0400 | Invert VS behaviour | |
| 11 | 0x0800 | Accept Wide Frequency Range **enabled** | ✓ |
| 12 | 0x1000 | Dynamic Current Limiter **enabled** | ✓ |
| 13 | 0x2000 | Tubular plate traction curve | |
| 14 | 0x4000 | Remote overrules AC1 | |
| 15 | 0x8000 | Low power shutdown in AES | |

The two bench-confirmed bits are exactly VictronConnect's flags 27 and 28. That match is the evidence the numbering in §12.3 applies to these registers. "VS" is the Virtual Switch (the programmable relay).

### 7.3 Charge Profile Settings

These settings control battery charging behavior. Voltage values use ÷100 scaling (e.g., `5680` = `56.80V`).

| ID | Name | Scale | Notes |
|---:|:-----|:------|:------|
| 2 | Absorption voltage | ÷100 → volts | e.g., 5680 = 56.80V |
| 3 | Float voltage | ÷100 → volts | e.g., 5400 = 54.00V |
| 4 | Charge current | Direct amps | e.g., 70 = 70A |
| 9 | Maximum absorption time | Varies | Set to 1 for LiFePO4 fixed profile |
| 10 | Charge characteristic | Enum | 0=variable (lead-acid), 1=fixed (LiFePO4), 2=fixed+storage |
| 11 | Low battery cutoff (VictronConnect: inverter DC shutdown voltage) | ÷100 → volts | e.g., 4450 = 44.50V |
| 12 | Inverter DC restart voltage | ÷100 → volts | Above the shutdown voltage |

### 7.4 Grid Code / LOM Settings

These settings appear when a grid code has been configured and control Loss of Mains (LOM) detection behavior.

| ID | Description | Behavior |
|---:|:------------|:---------|
| 81 | Grid code | 0 = no grid code. VictronConnect reads this as the grid code **number**, so 1 means the code that was selected ("Other"), not just "active". |
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

Settings 128, 190 and 191 are not in VictronConnect's settings table, so their names are unknown. The "silently ignored" write to 190 may be a `0x9B` (access level required) reply that the scanner discarded (§13).

### 7.5 Other Settings (Partial)

Settings in the ranges 16–27, 28–39, and 50–59 appear to be parameter blocks that repeat across groups (possibly per-AC-input or per-operating-mode). Identical values across these ranges for some parameters suggest the same setting repeated for different contexts.

**Corrected:** VictronConnect's settings table (§12.2) explains these blocks. 15–43 configure the first Virtual Switch, 44–47 the AC low/high switch inputs, and 52–59 the second Virtual Switch (VS2). 16, 28 and 52 are "load higher/lower than" thresholds, which is why they pair up. These settings only act when the Virtual Switch is in use.

| ID | Function (VictronConnect name) | Notes |
|---:|:-------------------------------|:------|
| 15 | Virtual switch usage | Binary 0/1, differs between otherwise identical units |
| 16 | VS on load higher than | Paired with 28 (VS off load lower than) and 52 (VS2 on load higher than) |
| 17 | VS on when Udc lower than | ÷100, often 6400. **Corrected:** a Virtual Switch threshold, not a battery overvoltage disconnect |
| 18 | VS on when Udc higher than | ÷100, often 4700. **Corrected:** a Virtual Switch threshold |
| 49 | Second input current limit (AC2) | Quattro-only (MultiPlus has only AC1) |
| 60 | **flags2** (flag register) | **Corrected:** bit 4 = lithium battery, bit 9 = prefer renewable energy (Solar & Wind priority). 16 = lithium only; 528 = lithium + Solar & Wind. See §12.3. |
| 61 | flags3 | Flag register; all bits spare in VictronConnect's table |
| 63 | Low DC alarm level | ÷100 V |
| 64 | Battery capacity | Ah; 0 = battery monitor disabled |
| 65 | Battery SoC when bulk finished | ×0.5 → %. 190 = 95% after LiFePO4 profile |
| 72 | Battery charge efficiency | 242 ≈ 95% after LiFePO4 profile |
| 73 | Inverter current limit during assist | **Corrected:** a current, not a voltage |
| 88 | Do-not-charge voltage | ×100 (1320 = 13.20 V). VictronConnect's name; it is the Solar & Wind priority threshold this doc described |

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

**Resolved — Setting 0 bit 11.** This project once read bit 11 as adaptive (set) vs. fixed (clear) charge, from the LiFePO4 diff. The `victron-vebus-mk3-control` library labels it "reduced float enabled", and VictronConnect's own flag table agrees with the library (§7.1, §12.3). The other flag names that library uses for bits 5, 8, 13, 14 and Setting 1 bits 11–12 also match VictronConnect. Its bit-7 label does not (§7.1).

### 7.6 Applying a LiFePO4 "Fixed" Charge Profile

VEConfigure writes these 8 settings when switching to the LiFePO4 fixed charge profile. Order matters — the flags register is written first, then the profile mode and voltages are changed.

| Order | Setting ID | Value | Purpose |
|------:|-----------:|------:|:--------|
| 1 | 0 | Clear bit 11 | Turn off reduced float (storage mode) |
| 2 | 60 | 16 | flags2: set bit 4, lithium battery |
| 3 | 65 | 190 | Battery SoC when bulk finished (×0.5 → 95%) |
| 4 | 72 | 242 | Battery charge efficiency (≈95%) |
| 5 | 10 | 1 | Charge characteristic = fixed |
| 6 | 2 | 5680 | Absorption voltage = 56.80V |
| 7 | 3 | 5400 | Float voltage = 54.00V |
| 8 | 9 | 1 | Maximum absorption time |

**Setting 60 is a flag register; write it read-modify-write.** VEConfigure wrote `16` because the bench unit's other flags2 bits were clear. Writing `16` outright on a unit that has other flags2 bits set would clear them:
- bit 6, external mains current sensor
- bit 7, configured for VE.Bus BMS
- bit 9, prefer renewable energy

Set bit 4 on the current value instead. The TUI's sequence builder does this.

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
| 13 | Battery State of Charge | ×0.005 → ×100 % | no | only meaningful when the battery monitor is on (setting 64 capacity > 0); otherwise reads 100 %. VictronConnect clamps the raw value to 200, so 0.5 % steps |
| 14 | Inverter power (filtered) | ×1 W | yes | Power converted between AC and DC by the inverter/charger. Positive = charging, negative = inverting |
| 15 | Input power (filtered) | ×1 W | yes | AC input (mains) |
| 16 | Output power (filtered) | ×1 W | yes | **Corrected:** AC **output** power, the loads. It includes pass-through from shore, so it is not inverter power |

**Frequency** is derived from the period variables: `f = round(10 / (scale·raw), 2)` Hz.

**More RAM variables** (names from VictronConnect's table, §12.4; scales not bench-checked):

| ID | Quantity |
|---:|:---------|
| 17–19 | Instantaneous (unfiltered) versions of 14–16: the last cycle's value |
| 21 | Battery temperature |
| 23 | Command RAM flags |
| 24 | AC output current, split-phase L2 |
| 26 | Info RAM flags |
| 32–37 | Split-phase L2: AC input voltage, AC input current, instantaneous input power, input power, instantaneous output power, output power |

The split-phase L2 variables belong to single units with two output legs (the 120/240 V models). A pair of separate units, one per phase, reports each leg per device instead (§12.8). The `victron_mk3` library names RAM 14 `dc_power` and RAM 16 `ac_inverter_power`.

VictronConnect also names RAM 3 "AC Output Current" and RAM 9 "AC Output Current (signed)". So when the MultiPlus II returns negative values on RAM 3, RAM 9 is the variable meant to carry a sign.

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

The MK3 sends Version frames (`0x56`) unsolicited (this doc observed ~100ms; the PDF says about once per second when idle, §5). Any read operation must scan through the response buffer and skip these frames to find the actual command response. A simple approach is pattern-matching for the expected response subcmd byte.

### Timing

- Allow **100ms** after each command before reading the response (`time.sleep(0.1)` after `ser.write()`).
- Allow **200ms** between consecutive writes for EEPROM write cycles.
- VEConfigure inserts a ~250ms gap before its last batch of writes, possibly for EEPROM cooldown.
- **3 retries** per read is a good default to handle cases where a Version frame lands in the read window instead of the command response.

### ACK Behavior for High Setting IDs

WriteViaID responses (`0x88`) are reliably returned for setting IDs in the low range (0–80). For higher IDs (128+), the ACK response is often missing even when the write succeeds. Always verify with a follow-up ReadSetting rather than relying solely on the ACK.

The high IDs are the grid-code settings. VictronConnect checks a per-setting access level ("Setting %d requires access level %d"), and the device answers `0x9B` when the level is missing. The scanners here only look for `0x88`, so a `0x9B` would read as a missing ACK. That is untested; see §13.

### "Unsupported" vs. "No Response"

- Value `0xFFFF` returned by ReadSetting: the setting ID exists in the firmware's table but is not applicable to the current configuration. The setting *can* become active if the configuration changes (e.g., enabling a grid code activates settings 128/190/191).
- No `0x86` at all after retries: the setting ID does not exist in this firmware version. **Corrected:** per the PDF the device answers that case with reply code `0x91` ("setting not supported"), and `0x90` for RAM variables. The scanners here ignore those codes, so it looks like silence.

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
| Setting 49 | Not supported | Supported |
| Settings 128/190/191 | Only with grid code | Only with grid code |

**Correction — Setting 0 bit 7 and `0x8134` are NOT reliable model discriminators.**
A **MultiPlus II** was directly observed reading Setting 0 = `0x8134` with **bit 7
CLEAR** — the same value/flag state this table previously attributed to a Quattro.
So bit 7 distinguishes (at most) the *original* MultiPlus from the others, not the
model family in general, and `0x8134` is not Quattro-exclusive.

The reliable way to identify a Quattro is **setting 49** (AC2 input current limit):
present (responds) on a Quattro, absent (no response) on MultiPlus / MultiPlus II.
Setting 88 (do-not-charge voltage, the Solar & Wind priority threshold) is available on both models —
use only as confirmation, not as the discriminator. Setting 0 bit 7 is unreliable
for model detection (a MultiPlus II was observed reading `0x8134` with bit 7 CLEAR).
Use setting 49 for model detection; treat bit 7 as model-specific raw data.

### Grid Code Password

The grid code configuration in VEConfigure (specifically the "no LOM detection" option) requires a password. The password (`TPWMBU2A4GCC`) is widely known in the Victron community.

### VEConfigure Write Sequence

When VEConfigure writes settings, it follows this per-setting pattern:

1. `GetSettingInfo` — query metadata (min/max/scale). Recorded as `0x3C`; the PDF and VictronConnect use `0x35` (§4.3)
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
    # Select device address 0 (§3)
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
---

## 12. Cross-check Against VictronConnect

### 12.0 Source and method

- **Installer:** `VictronConnectInstaller.exe` (Inno Setup 6.3, files dated 2026-09-21), SHA-256 `105d0d1880985a5ed37f905299317658d5cc5a16998c923d312fd97ee749650d`.
- **VE.Bus driver:** `app/vebus.dll` (x86-64), SHA-256 `51b293e4f9f5a6dd81227a52ca14268192ee9cf60291b84eff09c43592670d2c`. It publishes the same `com.victronenergy.vebus` D-Bus paths as the VE.Bus service on GX devices.
- **Bundled MK3 firmware:** version 1170216 (`Device=MK3,Version=1170216`).

This was a static read only: string tables, frame builders and reply parsers, read in a disassembler. Nothing was run against an inverter. "Inference" marks conclusions the code supports but doesn't state outright.

### 12.1 Winmon commands VictronConnect sends

| Cmd | Arguments | Reply | Purpose | In the PDF |
|----:|:----------|:------|:--------|:----------:|
| `05` / `06` | — | — | Software version, low / high half | yes |
| `07` | 2 bytes | ? | Unidentified | no |
| `0E` | state | `94 <state> <substate>` | Read device state; 1/2/3 force equalise / absorption / float | yes |
| `0F` | flags | `98` + 10 bytes | Energy (kWh) counters, §12.7 | no |
| `14` | 4 bytes | ? | Battery voltage (Ubat) diagnostics | no |
| `30` | up to 6 IDs | `85` / `90` | Read RAM variables | yes |
| `31` | id lo, id hi | `86` / `91` | Read setting | yes |
| `32` / `33` then `34` | id, then data | `87` / `88` | Write RAM variable / setting, two-step | yes |
| `35` | id lo, id hi | `89` | Setting info (§4.3) | yes |
| `36` | id lo, id hi | `8E` | RAM variable info | yes |
| `37` | flags, id, lo, hi | `87` / `88` / `9B` / `80` | Write via ID | yes |
| `38` | — | `99` | Read snapshot | yes |
| `3A` | ? | ? | Unidentified | no |
| `3B` | ? | `9D` | VE.Bus serial number | no |
| `42` | entry | `A0` | Interface protection log entry | no |
| `43` | 2 bytes | `A1` | Read app EEPROM data | no |
| `44` | selector | `A2` | "Specific info", §12.6 | no |
| `45` | info ID | `97` | Reset an info counter (e.g. uptime) | no |
| `5F` | 2 bytes (block) | ? | VLL block read | no |
| `60` | flags | ? | Per-port energy counters | no |

MK3-level commands it uses:
- `A`: address
- `F`: info frames, config, snapshot, reset, BOL
- `H`: panel-detect / standby line levels
- `L`: LED status
- `S`: state
- `V`: version
- `N`: baud change, §12.9

### 12.2 Settings 0–88 by name

The PDF names 0–49. This table extends it, and agrees with the PDF wherever both name an ID. A name here doesn't mean a given unit supports the setting.

| ID | Name | ID | Name |
|---:|:-----|---:|:-----|
| 0 | flags0 (§7.1) | 45 | AC low switch input on |
| 1 | flags1 (§7.2) | 46 | AC high switch input off |
| 2 | Absorption voltage | 47 | AC high switch input on |
| 3 | Float voltage | 48 | Assist current boost factor |
| 4 | Charge current | 49 | Second input current limit |
| 5 | Inverter output voltage | 50 | Load for starting AES mode |
| 6 | AC current limit | 51 | Offset for ending AES mode |
| 7 | Repeated absorption time | 52 | VS2 on load higher than |
| 8 | Repeated absorption interval | 53 | VS2 time on load high |
| 9 | Maximum absorption time | 54 | VS2 on lower DC voltage than |
| 10 | Charge characteristics | 55 | VS2 on low DC voltage for |
| 11 | Inverter DC shutdown voltage | 56 | VS2 off load lower than |
| 12 | Inverter DC restart voltage | 57 | VS2 off load lower for |
| 13 | Number of slaves | 58 | VS2 off DC voltage high |
| 14 | Special three phase | 59 | VS2 off DC voltage for |
| 15 | Virtual switch usage | 60 | flags2 (§12.3) |
| 16 | VS on load higher than | 61 | flags3 |
| 17 | VS on when Udc lower than | 62 | System period |
| 18 | VS on when Udc higher than | 63 | Low DC alarm level |
| 19 | VS time on load high | 64 | Battery capacity |
| 20 | VS time on DC voltage high | 65 | SOC when bulk finished |
| 21 | VS time on DC voltage low | 66 | Frequency shift Ubat start |
| 22 | VS time on when not charging | 67 | Frequency shift start delay |
| 23 | VS time on fan enabled | 68 | Frequency shift Ubat stop |
| 24 | VS time on temperature alarm | 69 | Frequency shift stop delay |
| 25 | VS time on low battery alarm | 70 | VS2 on SOC level |
| 26 | VS time on overload alarm | 71 | Assistant temp compensation |
| 27 | VS time on ripple alarm | 72 | Charge efficiency |
| 28 | VS off load lower than | 73 | Inverter current limit during assist |
| 29 | VS off when Udc lower than | 74 | SOC reset value when switching from absorption to float |
| 30 | VS off when Udc higher than | 75 | Battery current for stop absorption |
| 31 | VS time off load low | 76–80 | "Vebus config. Ignore" |
| 32 | VS time off DC voltage high | 81 | Grid code |
| 33 | VS time off DC voltage low | 82 | Permanent flags 0 |
| 34 | VS off when charging for | 83 | SOC low shutdown |
| 35 | VS time off fan disabled | 84 | SOC low restart |
| 36 | VS off bulk finished for | 85 | Info ID 0 |
| 37 | VS off no ON condition for | 86 | Stop charging at temperature |
| 38 | VS off when no AC input for | 87 | Temperature compensation slope |
| 39 | VS off no temperature alarm | 88 | DoNotChargeVoltage |
| 40 | VS off no battery alarm | | |
| 41 | VS off no overload alarm | | |
| 42 | VS off no ripple alarm | | |
| 43 | Minimum on time | | |
| 44 | AC low switch input off | | |

### 12.3 Flag numbering

VictronConnect names 64 flags; flag N is bit `N % 16` of register `N // 16`. The settings table names the registers: flags0 = setting 0, flags1 = setting 1, flags2 = setting 60, flags3 = setting 61.

Four bench results line up with this numbering:
- Setting 0 bits 5 and 14
- Setting 1 bits 11 and 12 (flags 27 and 28)
- the setting 60 values 16 and 528, which are flags 36 and 41

Settings 0 and 1 are tabled in §7.1 and §7.2. flags2 (setting 60):

| Bit | Flag | Meaning when SET |
|----:|-----:|:-----------------|
| 0 | 32 | VS2 off when AC1 available |
| 1 | 33 | Invert VS2 behaviour |
| 2 | 34 | Frequency changes by VS |
| 3 | 35 | Frequency changes by Ubat |
| 4 | 36 | Lithium battery |
| 5 | 37 | Allow enable feed-in |
| 6 | 38 | External mains current sensor connected |
| 7 | 39 | Configured for VE.Bus BMS |
| 8 | 40 | Disable Ubat VSense (for diagnostic purposes) |
| 9 | 41 | Prefer renewable energy (Solar & Wind priority) |

Flags 42–63 are listed as spare.

On multi-unit systems VictronConnect checks that the phase masters agree on these words and rewrites any that differ ("flags for phase = %d out of sync"). A script that writes flags to address 0 only leaves the other units out of step (§12.8).

### 12.4 RAM variables 0–37 by name

The PDF covers 0–19; the names agree.

| ID | Name | ID | Name |
|---:|:-----|---:|:-----|
| 0 | AC Input Voltage | 19 | Instantaneous Output Power |
| 1 | AC Input Current | 20 | To be defined |
| 2 | AC Output Voltage | 21 | Battery temperature |
| 3 | AC Output Current | 22 | To be defined |
| 4 | DC Voltage | 23 | Command RAM flags |
| 5 | DC Current | 24, 25 | AC Output Current split phase L2 |
| 6 | DC ripple | 26 | Info RAM flags |
| 7 | Output period | 27–31 | Reserved |
| 8 | Input period | 32 | AC Input Voltage split phase L2 |
| 9 | AC Output Current (signed) | 33 | AC Input Current split phase L2 |
| 10 | Virtual switch | 34 | Instantaneous Input Power split phase L2 |
| 11 | Ignore AC input | 35 | Input Power split phase L2 |
| 12 | Functional relay state | 36 | Instantaneous Output Power split phase L2 |
| 13 | State of charge | 37 | Output Power split phase L2 |
| 14 | Inverter Power | | |
| 15 | Input Power | | |
| 16 | Output Power | | |
| 17 | Instantaneous Inverter Power | | |
| 18 | Instantaneous Input Power | | |

### 12.5 The full `L` reply

VictronConnect parses the `L` reply when it is at least 6 bytes long. Positions count from the length byte:

| Byte | Content |
|-----:|:--------|
| 0 | Length |
| 1 | `FF` |
| 2 | `L` |
| 3 | LED on |
| 4 | LED blink |
| 5 | Alarm/warning byte (below) |
| 6 | Bits 0–4: VE.Bus error code (0 = none). Bits 5–7: other status. Bit 7 = bus in **block mode** |
| 7 | Not used by VictronConnect |
| 8 | Only when the length is above 7. Bits 0–2 gate the L1/L2/L3 alarm items (inference: phases present) |

**Block mode (byte 6, bit 7):** VictronConnect keeps re-sending `L` while it is set and logs "Blockmode finished." once it clears. Typically another tool (VEConfigure) holds the bus.

**Alarm/warning byte** (each condition reads 0 = ok, 1 = warning, 2 = alarm):

| Condition | Warning bit | Alarm bit |
|:----------|------------:|----------:|
| High temperature | 0 | 4 |
| Low battery | 1 | 5 |
| Overload | 2 | 6 |
| Ripple | 3 | 7 |

The byte is system-wide. VictronConnect copies it to its per-phase alarm items when the matching byte 8 bit is set.

VictronConnect also derives two sensor alarms from the LED pattern, skipping the check when both LED bytes read `0x1F` (status unknown, PDF §4):
- **Temperature sensor alarm:** absorption and float blinking, neither steady.
- **Voltage sensor alarm:** absorption and bulk blinking, neither steady.

The VE.Bus error code numbers are in Victron's public VE.Bus error code list; `vebus.dll` only logs them as "Error %d". It suppresses error 1 while an alarm is active ("Suppressed vebus error 1 caused by alarm").

`victron_mk3` and this repo read only bytes 3 and 4.

### 12.6 "Specific info" (`0x44`)

- Request: `04 FF <slot> 44 <selector>`
- Reply: `<len> FF <slot> A2 <selector> <n> <data…>`, with up to 15 data bytes
- `0x80` = not supported

| Selector | Content |
|:--------:|:--------|
| `FF` | **Extended status flags.** `<n>` is the number of valid bits (VictronConnect caps it at 32). The data starts with a 32-bit little-endian word. VictronConnect reads it per device and ORs the words together. |
| `FD` | **Error and warning flags.** Stored separately; the bit meanings weren't decoded. |

Bits of the extended status word that VictronConnect turns into alarms:
- bit 10: high DC voltage alarm
- bit 11: high DC current alarm
- bit 22: a further alarm, unidentified

It ignores a bit when `<n>` says that bit isn't valid. On three-phase systems it also logs the per-phase words ("devL[0]->extendedStatusFlags").

### 12.7 Energy counters (`0x0F`)

- Request, per device: `04 FF <slot> 0F <flags>`. The flags byte wasn't decoded.
- Reply: `98` followed by **10 one-byte counters**. `0x80` = not supported ("kWh counters not supported").

Each counter wraps at 256 and counts in units of **2^16 J (about 18.2 Wh)**. VictronConnect adds each counter's change since the last read (mod 256) to a running total, so a poller has to read before any counter moves 256 ticks (about 4.6 kWh).

Counter order. The order is inference, taken from VictronConnect's item numbering:

| # | Energy path |
|--:|:------------|
| 0 | AC-in 1 → inverter |
| 1 | AC-in 2 → inverter |
| 2 | AC-in 1 → AC-out |
| 3 | AC-in 2 → AC-out |
| 4 | inverter → AC-in 1 |
| 5 | inverter → AC-in 2 |
| 6 | AC-out → AC-in 1 |
| 7 | AC-out → AC-in 2 |
| 8 | inverter → AC-out |
| 9 | AC-out → inverter |

VictronConnect resets its running totals while the system is switched off. `0x60` is a per-port variant ("port based kWh counters").

### 12.8 Multi-unit systems

**Addressing.** Addresses 0–2 are the phase masters for L1–L3. Higher addresses are slaves on phase `address mod 3`.

**Power readings.** When VictronConnect reads the power RAM variables, it does two things:
- Multiplies RAM 14 (inverter power) and RAM 15 (input power) from a phase master by the number of units on that phase.
- Ignores a slave's values ("workaround for master PINPUT_RAMVAR … count", "workaround for slave").

Inference: a phase master reports a per-unit value. With one unit per phase (the 240 V split-phase pair) the factor is 1, but a parallel stack read at address 0 alone under-reads.

**Settings.** VictronConnect sends settings to each device in turn ("set setting %d for dev %d") and keeps the phase masters' flag words in sync (§12.3).

### 12.9 MK3 baud-rate change (partly decoded)

When the interface is an MK3, VictronConnect sends `02 FF 4E` ("MK3 Request Baudrate change"). It then switches the port to **115200** baud and sends a frame that starts with a run of `0x55`, the autobaud string. It logs "MK3 baudrate change successful" when an `N` reply comes back.

If the rate isn't supported it falls back to 2400 ("set baud rate to %d, rate not supported. Revert to 2400 baud"). The rest of the handshake wasn't decoded. Capture VictronConnect before trying this on a live unit.

## 13. Open Items for the Bench

1. **GetSettingInfo:** does the unit answer `0x3C`, or only `0x35`? Send both for setting 2 and compare the `0x89` replies.
2. **ReadSetting ID width:** confirm the two-byte form `05 FF <slot> 31 <lo> <hi>` gives the same values as the one-byte form.
3. **High-ID writes:** capture the reply to a write of setting 190 or 128. A `0x9B` would mean "access level required" rather than "silently ignored".
4. **WriteSetting + WriteData:** retry `0x33 <lo> <hi>` then `0x34 <lo> <hi>` with a two-byte ID. VictronConnect uses this two-step.
5. **`L` reply on the bench MK3:** log the full reply and check the length, the alarm byte and the error-code byte against §12.5. A warning can be provoked with load (overload) or a low battery.
6. **Energy counters:** send `0F 00` and confirm a `0x98` reply with 10 bytes that rise under load.
7. **Flag names not yet toggled:** Setting 0 bit 11 (reduced float) and setting 60 bits 4 and 9. Change "storage mode", "lithium batteries" and "Solar & Wind priority" in VEConfigure one at a time and diff.
