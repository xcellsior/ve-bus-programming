Please check out https://github.com/j9brown/victron-mk3 as this has a working script for performing many of the inverter's functions.

This repo's example script performs the setting of absorption and float voltage which is a feature not in the above repo.

# ve-bus-programming
Programming a Victron Inverter with VE.Bus without Venus or GX device. Raspberry Pi and MK3 only.

# Programming a Victron Inverter with VE.Bus
This repository aims to add valuable information about VE.Bus that the technical protocol documentation lacks or is incorrect on. Find the documentation from Victron here:
https://www.victronenergy.com/upload/documents/Technical-Information-Interfacing-with-VE-Bus-products-MK2-Protocol-3-14.pdf
Within this repo contains tools for directly programming Victron inverters via VE.Bus using the MK3 USB interface. While Victron provides documentation for the VE.Bus protocol, several key details are either incorrect or missing. This guide aims to fill those gaps based on actual reverse engineering and testing.

## Key Findings and Documentation Corrections

### Command Structure
The documentation suggests using commands 0x33 (CommandWriteSetting) followed by 0x34 (CommandWriteData) for changing settings. However, this does not work. Instead, use CommandWriteViaID (0x37) with the following structure:

```
07 FF 58 37 01 [setting_id] [value_lo] [value_hi] [checksum]
```
Where:
- 07: Correct length byte (documentation wasn't clear about including all bytes)
- FF: Protocol marker
- 58: 'X' command (not mentioned in documentation)
- 37: CommandWriteViaID
- 01: Flags for RAM only
- setting_id: 2 for absorption voltage, 3 for float voltage
- value: Little endian, scaled by 0.01 (e.g., 5600 = 56.00V)

### Scaling Factors
While the documentation mentions various scaling factors, for voltage settings:
- Multiply desired voltage by 100 to get the internal value
- For example: 56.0V → 5600 (0x15E0 in little endian)

### Setting IDs
Important setting IDs:
- 2: Absorption voltage
- 3: Float voltage

### Common Issues and Solutions
1. **Command Response**: Many commands won't generate responses. This is normal and differs from what the documentation suggests about waiting for responses.

2. **Byte Order**: All values are little endian, but the documentation isn't clear about where this applies.

3. **MK3 vs MK2**: The documentation primarily focuses on MK2, but MK3 USB behaves differently. Notably:
   - No need for DTR signal handling
   - Different timing requirements
   - Some commands that work with MK2 don't work with MK3

### Working with the Device
1. Always set the address (04 FF 41 01 00 BB) before sending commands
2. Use proper delays between commands (100ms minimum recommended)
3. Baud rate should be 2400 (this part of documentation is correct)

## Required Hardware
- Victron MultiPlus II Inverter (or compatible)
- MK3 USB interface
- USB connection to host computer

## Software Requirements
- Python 3.x
- pyserial library

```bash
pip install pyserial
```

## Usage
See the included Python script for implementation details. Basic usage:

```python
setter = VoltageSettings()
# Set absorption voltage to 56.0V
setter.set_voltage(56.0, 2)
# Set float voltage to 54.0V
setter.set_voltage(54.0, 3)
```

## Debugging Tips
1. Use a tool like cutecom or Wireshark with USB capture to verify commands
2. Monitor voltage settings with a multimeter to confirm changes
3. The device's LED status can be used to verify operation mode

## Known Limitations
1. Some settings may require specific device states to be changed
2. Not all settings documented in the manual are accessible
3. Some changes may require device restart to take effect

## Safety Notes
- Always verify voltage settings with appropriate tools
- Incorrect settings can damage batteries
- Some settings may interact with BMS or other protection systems

## Contributing
This project was developed through reverse engineering and practical testing. If you discover additional protocol details or corrections, please contribute them back to the community.
