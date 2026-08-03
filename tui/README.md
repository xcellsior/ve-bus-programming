# Interactive TUI

> ⚠️ **This TUI is vibe++ coded (I mostly paid attention) and should not be used in production without manual
> verification.** Power systems can be dangerous. It sits on top of a partially
> reverse-engineered protocol — verify every value (multimeter / VEConfigure)
> before trusting it, and remember that wrong charge parameters can damage batteries.

A reactive terminal dashboard ([Textual](https://textual.textualize.io/)) that exposes the
reverse-engineered features in one place:

- **Dashboard** — live telemetry (battery V/I, AC, inverter) on an auto-refresh, plus
  explicit operating-state controls (Off / On / Charger-only / Normal).
- **Flags** — Setting 0 and Setting 1 decoded bit-by-bit (UPS, PowerAssist, Weak AC,
  adaptive vs. fixed charge, Wide Frequency Range, Dynamic Current Limiter) with the raw
  word shown; model-dependent/unknown bits are displayed but not interpreted. One-click
  read-modify-write toggles for the confirmed flags.
- **Charge** — absorption / float / charge-current / low-battery-cutoff values, individual
  editing, and a one-button **LiFePO4 fixed-profile** wizard that previews and applies the
  exact 8-write VEConfigure sequence in order.
- **Grid / LOM** — settings 81/128/190/191 with the grid-code residual situation explained
  and a guarded residual-cleanup action.
- **All settings** — every supported setting ID, raw + decoded.

The TUI sits on top of `vebus/protocol.py`, the reusable backend (serial/checksum/frame
protocol, `SerialBackend`, `MockBackend`, and all decoders) — importable by scripts and
tests with no UI dependencies.

## Running

All commands are run from the repository root:

```bash
# One-time setup (keeps system Python clean):
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Real device (auto-detects /dev/ttyUSB0, then /dev/inverter):
.venv/bin/python tui/tui.py

# Specific port:
.venv/bin/python tui/tui.py --port /dev/inverter

# Mock mode — no hardware needed (also the automatic fallback if no port opens):
.venv/bin/python tui/tui.py --mock

# Telemetry refresh interval (seconds, default 2):
.venv/bin/python tui/tui.py --interval 3
```

Keys: `r` refresh all settings · `q` quit. The status bar shows LIVE vs MOCK, the detected
model, and the port.

## Safety

- **Every write is confirmed in a modal** showing the setting ID, old→new value, and decoded
  meaning before anything is sent.
- **Writes are verified by readback** (high IDs often skip the `0x88` ACK even on success, so
  the readback is authoritative).
- **Flag changes use read-modify-write** — a single bit is flipped on the live value; whole
  flag words are never copied between units/models (base values differ — see
  [FINDINGS.md](../FINDINGS.md) §9).
- **Telemetry reads are EEPROM-safe** and auto-refresh freely. **Writes hit EEPROM**, which has
  limited write cycles — they are never auto-looped, and every confirm dialog offers a
  **RAM-only** option (flag `0x03`) to test without wear (value is lost on power cycle).
- Inputs are range-checked (e.g. absorption/float within a plausible 48V band, float < absorption)
  and out-of-range values raise a warning in the confirm dialog before you commit.

## Tests

```bash
python3 tui/test_protocol.py    # unit tests, no hardware required
```
