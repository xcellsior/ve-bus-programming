# ve-bus-programming

Programming a Victron Inverter with VE.Bus without Venus or GX device. Linux based, only using the MK3 adapter.

Also check out [victron-mk3](https://github.com/j9brown/victron-mk3) — a working library that implements many inverter functions. This repo focuses on the parts that library doesn't cover: direct setting reads/writes, charge profile configuration, and protocol details that Victron's documentation gets wrong.

## Documentation

**[FINDINGS.md](FINDINGS.md)** — Comprehensive protocol reference covering the full VE.Bus MK2/MK3 wire protocol, reverse-engineered from USB packet captures and iterative bench testing. Includes:

- Complete frame format, checksum calculation, and command/response reference
- Setting 0 and Setting 1 flag register bit maps (UPS, PowerAssist, Weak AC, adaptive charge, dynamic current limiter, wide frequency range)
- Charge profile settings and the exact 8-write sequence VEConfigure uses for LiFePO4
- Grid code / LOM setting behavior and the residual cleanup problem when reverting to "None"
- Timing, ACK quirks, cross-model differences (MultiPlus vs. Quattro), and practical gotchas
- Methodology for mapping unknown settings using sweep-and-diff

Find Victron's official (incomplete) protocol documentation here:
https://www.victronenergy.com/upload/documents/Technical-Information-Interfacing-with-VE-Bus-products-MK2-Protocol-3-14.pdf

## Tools

| Script | Purpose |
|--------|---------|
| `set_voltage.py` | Set absorption and float voltage (WriteViaID example) |
| `settings_sweeper.py` | Sweep all 256 setting IDs — dump supported settings to CSV for diffing |
| `ram_sweeper.py` | Sweep all 256 RAM variable IDs — identify live telemetry values |

The sweeper scripts are the primary tool for mapping unknown settings. Run a sweep, change one parameter in VEConfigure, sweep again, and diff the CSV outputs. See [FINDINGS.md § Methodology for Mapping Unknown Settings](FINDINGS.md#11-methodology-for-mapping-unknown-settings) for details.

## Key Protocol Corrections

Victron's documentation has several errors. The most critical:

**Use CommandWriteViaID (0x37), not CommandWriteSetting (0x33) + CommandWriteData (0x34).** The documented two-step write process does not work. The correct write frame is:

```
07 FF 58 37 01 [setting_id] [value_lo] [value_hi] [checksum]
```

- `07` — length byte
- `FF` — protocol marker
- `58` — Winmon slot ('X'; any of 0x57–0x5A works)
- `37` — CommandWriteViaID
- `01` — flags: RAM + EEPROM (use `0x03` for RAM only)
- Setting value is little-endian, voltage settings scaled ×100 (e.g., 5600 = 56.00V)

See [FINDINGS.md § Command Reference](FINDINGS.md#4-command-reference) for the full command set including ReadSetting, GetSettingInfo, ReadRAMVar, and State commands.

## Required Hardware

- Victron MultiPlus, Quattro, or compatible VE.Bus inverter/charger
- MK3-USB interface
- USB connection to host computer

## Software Requirements

- Python 3.x
- pyserial

```bash
pip install pyserial
```

## Quick Start

```python
# Set absorption and float voltage
from set_voltage import VoltageSettings

setter = VoltageSettings()
setter.set_voltage(56.0, 2)  # Absorption = 56.00V
setter.set_voltage(54.0, 3)  # Float = 54.00V
```

```bash
# Discover all settings on your inverter
python settings_sweeper.py /dev/ttyUSB0 -o baseline.csv

# Discover live RAM variables
python ram_sweeper.py /dev/ttyUSB0 -o ramvars.csv
```

**Note:** Writes go to EEPROM which has limited write cycles. These tools are for configuration and discovery, not automation loops.

## Safety

- Verify voltage settings with a multimeter after writing
- Incorrect charge parameters can damage batteries
- Never blindly copy flag register values between different models — base values differ between MultiPlus and Quattro
- Always use read-modify-write when changing individual bits in flag registers (Settings 0 and 1)

## Contributing

This project was developed through reverse engineering and practical testing. If you discover additional protocol details, setting ID mappings, or corrections, please contribute them back.
