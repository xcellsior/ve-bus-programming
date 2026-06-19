#!/usr/bin/env python3
"""
vebus-tui — interactive terminal UI for a Victron VE.Bus inverter via MK3-USB.

Surfaces the reverse-engineered settings coherently, auto-refreshes live
telemetry (reads only — safe), and performs guarded writes: every write is
confirmed in a modal (ID, old→new, decoded meaning, EEPROM vs RAM-only) and
verified by readback.

    python3 tui.py                 # auto: try /dev/ttyUSB0, else mock
    python3 tui.py --port /dev/inverter
    python3 tui.py --mock          # no hardware needed

Run inside the project venv:  .venv/bin/python tui.py
See README.md for the safety note about EEPROM writes.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             Static, TabbedContent, TabPane)

from vebus import protocol as p


# ─────────────────────────────────────────────────────────────────────────────
# Modal: confirm a single write
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmWrite(ModalScreen[Optional[str]]):
    """
    Confirm a single setting write. Dismisses with:
      None      → cancel
      "eeprom"  → write RAM + EEPROM (persistent, flag 0x01)
      "ram"     → write RAM only (flag 0x03, no EEPROM wear)
    """

    def __init__(self, setting_id: int, old: Optional[int], new: int,
                 warning: str = "", allow_ram_only: bool = True):
        super().__init__()
        self.setting_id = setting_id
        self.old = old
        self.new = new
        self.warning = warning
        self.allow_ram_only = allow_ram_only

    def compose(self) -> ComposeResult:
        m = p.setting_meta(self.setting_id)
        old_s = p.format_setting_value(self.setting_id, self.old)
        new_s = p.format_setting_value(self.setting_id, self.new)
        with Vertical(id="dialog"):
            yield Label("⚠  Confirm write", id="dialog-title")
            yield Static(
                f"[b]Setting {self.setting_id}[/b] — {m.name}\n\n"
                f"  raw:      0x{(self.old or 0):04X} → [b]0x{self.new:04X}[/b]\n"
                f"  decoded:  {old_s} → [b]{new_s}[/b]",
                id="dialog-body")
            if self.warning:
                yield Static(f"[yellow]⚠ {self.warning}[/yellow]", id="dialog-warn")
            yield Static("EEPROM has limited write cycles. Use RAM-only to test "
                         "without wear (value is lost on power cycle).",
                         id="dialog-note")
            with Horizontal(id="dialog-buttons"):
                yield Button("Write EEPROM", variant="error", id="eeprom")
                if self.allow_ram_only:
                    yield Button("RAM only", variant="warning", id="ram")
                yield Button("Cancel", variant="primary", id="cancel")

    @on(Button.Pressed)
    def handle(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)


# ─────────────────────────────────────────────────────────────────────────────
# Modal: confirm the multi-step LiFePO4 sequence
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmSequence(ModalScreen[Optional[str]]):
    """Preview + confirm the full LiFePO4 write sequence. Same return codes."""

    def __init__(self, steps: list[p.WriteStep]):
        super().__init__()
        self.steps = steps

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Label("⚠  Apply LiFePO4 fixed charge profile", id="dialog-title")
            yield Static(
                "This writes the following settings IN ORDER (Setting 0 first, "
                "read-modify-write to disable adaptive charging):",
                id="dialog-body")
            table = DataTable(id="seq-table", zebra_stripes=True)
            table.add_columns("#", "Setting", "New value", "What it does")
            for i, s in enumerate(self.steps, 1):
                table.add_row(str(i), str(s.setting_id),
                              p.format_setting_value(s.setting_id, s.value), s.label)
            yield table
            yield Static("[yellow]Charger settings affect battery health. "
                         "Confirm values match your battery's datasheet.[/yellow]",
                         id="dialog-warn")
            with Horizontal(id="dialog-buttons"):
                yield Button("Apply (EEPROM)", variant="error", id="eeprom")
                yield Button("Apply (RAM only)", variant="warning", id="ram")
                yield Button("Cancel", variant="primary", id="cancel")

    @on(Button.Pressed)
    def handle(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)


# ─────────────────────────────────────────────────────────────────────────────
# Main app
# ─────────────────────────────────────────────────────────────────────────────

class VEBusTUI(App):
    CSS = """
    Screen { layout: vertical; }
    TabPane { overflow-y: auto; }
    #status-bar { height: 1; background: $panel; color: $text; padding: 0 1; }
    #status-bar.mock { background: $warning; color: black; }
    #status-bar.live { background: $success; color: black; }
    .panel-title { text-style: bold; color: $accent; padding: 1 0 0 0; }
    .raw-word { color: $text-muted; }
    DataTable { height: auto; max-height: 18; }
    .field-row { height: 3; align: left middle; }
    .field-row Label { width: 22; }
    .field-row Input { width: 14; }
    .field-row Button { margin: 0 1; }
    #dialog, #dialog-wide {
        background: $surface; border: thick $accent; padding: 1 2;
        width: 70; height: auto; align: center middle;
    }
    #dialog-wide { width: 90; }
    #dialog-title { text-style: bold; color: $warning; width: 100%; }
    #dialog-buttons { height: auto; align: center middle; padding-top: 1; }
    #dialog-buttons Button { margin: 0 1; }
    .flag-on { color: $success; }
    .flag-off { color: $text-muted; }
    .actions { height: auto; padding: 1 0; }
    .actions Button { margin: 0 1; }
    """

    BINDINGS = [
        ("r", "refresh_all", "Refresh all"),
        ("y", "resync", "Resync bus"),
        ("q", "quit", "Quit"),
    ]

    # Setting IDs we read for the full picture.
    SETTING_IDS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 17, 18,
                   60, 64, 65, 72, 73, 81, 128, 190, 191]

    def __init__(self, backend: p.Backend, refresh_interval: float = 2.0):
        super().__init__()
        self.backend = backend
        self.refresh_interval = refresh_interval
        self.settings: dict[int, Optional[int]] = {}
        self.device: Optional[p.DeviceInfo] = None
        self._tele_busy = False        # guard: skip a tick if one is in flight
        self._tele_started = False      # telemetry interval starts after 1st load
        self._last_cfg: Optional[p.ConfigInfo] = None  # last shore-config snapshot

    # ── layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Connecting…", id="status-bar")
        with TabbedContent(initial="tab-dash"):
            with TabPane("Dashboard", id="tab-dash"):
                yield Static("Live telemetry (auto-refresh — reads only, EEPROM-safe)",
                             classes="panel-title")
                yield DataTable(id="telemetry", zebra_stripes=True)
                yield Static("Front panel LEDs", classes="panel-title")
                yield Static("—", id="leds")
                yield Static("Shore power (AC input) current limit", classes="panel-title")
                with Horizontal(classes="field-row"):
                    yield Label("Limit (A)")
                    yield Input(id="in-shore", placeholder="30")
                    yield Button("Write", id="set-shore", variant="warning")
                yield Static("—", id="shore-info")
                yield Static("Operating state control", classes="panel-title")
                yield Static("(read the state on the unit's display; these are "
                             "explicit commands, not auto-polled)", id="state-note")
                with Horizontal(classes="actions"):
                    yield Button("Off", id="state-off", variant="default")
                    yield Button("On (invert)", id="state-on", variant="default")
                    yield Button("Charger only", id="state-charger", variant="default")
                    yield Button("Normal (auto)", id="state-normal", variant="default")

            with TabPane("Flags", id="tab-flags"):
                yield Static("Setting 0 — primary flags", classes="panel-title")
                yield Static("", id="s0-raw", classes="raw-word")
                yield DataTable(id="s0-table", zebra_stripes=True)
                with Horizontal(classes="actions"):
                    yield Button("Toggle UPS", id="tog-ups")
                    yield Button("Toggle PowerAssist", id="tog-pa")
                    yield Button("Toggle Weak AC", id="tog-wac")
                    yield Button("Toggle charge mode", id="tog-charge")
                yield Static("Setting 1 — secondary flags", classes="panel-title")
                yield Static("", id="s1-raw", classes="raw-word")
                yield DataTable(id="s1-table", zebra_stripes=True)
                with Horizontal(classes="actions"):
                    yield Button("Toggle Wide Freq Range", id="tog-wfr")
                    yield Button("Toggle Dynamic Current Limiter", id="tog-dcl")

            with TabPane("Charge", id="tab-charge"):
                yield Static("Charge profile", classes="panel-title")
                yield DataTable(id="charge-table", zebra_stripes=True)
                yield Static("Edit individual parameters", classes="panel-title")
                with Horizontal(classes="field-row"):
                    yield Label("Absorption (V)")
                    yield Input(id="in-abs", placeholder="56.80")
                    yield Button("Write", id="set-abs", variant="warning")
                with Horizontal(classes="field-row"):
                    yield Label("Float (V)")
                    yield Input(id="in-float", placeholder="54.00")
                    yield Button("Write", id="set-float", variant="warning")
                with Horizontal(classes="field-row"):
                    yield Label("Charge current (A)")
                    yield Input(id="in-current", placeholder="35")
                    yield Button("Write", id="set-current", variant="warning")
                with Horizontal(classes="field-row"):
                    yield Label("Low-batt cutoff (V)")
                    yield Input(id="in-cutoff", placeholder="44.50")
                    yield Button("Write", id="set-cutoff", variant="warning")
                yield Static("LiFePO4 fixed profile (8-write sequence)",
                             classes="panel-title")
                yield Static("Uses the absorption / float / current fields above.",
                             id="lifepo4-note")
                with Horizontal(classes="actions"):
                    yield Button("Apply LiFePO4 Fixed Profile",
                                 id="apply-lifepo4", variant="error")

            with TabPane("Grid / LOM", id="tab-grid"):
                yield Static("Grid code / Loss-of-Mains settings", classes="panel-title")
                yield DataTable(id="grid-table", zebra_stripes=True)
                yield Static("", id="grid-note")
                with Horizontal(classes="actions"):
                    yield Button("Clean residuals (write 0xFFFF to 128 & 191)",
                                 id="clean-residuals", variant="error")

            with TabPane("All settings", id="tab-raw"):
                yield Static("All read setting IDs (raw + decoded)", classes="panel-title")
                yield DataTable(id="raw-table", zebra_stripes=True)
        yield Footer()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        # Column headers.
        self.query_one("#telemetry", DataTable).add_columns(
            "ID", "Measurement", "Raw", "Value")
        self.query_one("#s0-table", DataTable).add_columns(
            "Bit", "Name", "State", "Meaning")
        self.query_one("#s1-table", DataTable).add_columns(
            "Bit", "Name", "State", "Meaning")
        self.query_one("#charge-table", DataTable).add_columns(
            "ID", "Parameter", "Value")
        self.query_one("#grid-table", DataTable).add_columns(
            "ID", "Description", "Raw", "Decoded")
        self.query_one("#raw-table", DataTable).add_columns(
            "ID", "Name", "Raw", "Hex")
        # Telemetry auto-refresh is started only after the first full settings
        # load completes (see refresh_all_worker), so the two don't contend on
        # the slow 2400-baud link during the initial paint.
        self.refresh_all_worker()

    # ── status bar ────────────────────────────────────────────────────────────

    def update_status(self) -> None:
        bar = self.query_one("#status-bar", Static)
        d = self.device
        model = d.model if d else "…"
        fw = f"  fw {d.firmware}" if d and d.firmware else ""
        if self.backend.is_mock:
            bar.set_classes(["mock"])
            bar.update(f"● MOCK MODE — no device  |  {self.backend.description}  "
                       f"|  model: {model}{fw}")
        elif not self.backend.healthy:
            bar.set_classes(["mock"])  # reuse warning style
            bar.update("⚠ NO RESPONSE — bus silent/desynced. Press 'y' to resync, "
                       "or check no other program (VEConfigure, another tui.py) "
                       f"holds {self.backend.description}.")
        else:
            bar.set_classes(["live"])
            bar.update(f"● LIVE — {self.backend.description}  |  model: {model}{fw}")

    # ── refresh: telemetry (periodic, reads only) ───────────────────────────

    @work(thread=True, group="telemetry")
    def refresh_telemetry(self) -> None:
        # Skip (don't cancel) if a cycle is still running — a full read can take
        # longer than the interval on 2400 baud, and cancelling mid-cycle would
        # leave telemetry permanently empty.
        if self._tele_busy:
            return
        self._tele_busy = True
        try:
            rows = []
            for tv in p.TELEMETRY:
                v = self.backend.read_ramvar(tv.var_id)
                if v is None:
                    continue
                # Exact scaling from GetVariableInfo (cached after first fetch);
                # falls back to known divisors if the info query is unavailable.
                info = self.backend.read_ramvar_info(tv.var_id)
                shown = p.format_telemetry(tv.var_id, v, info)
                rows.append((tv.var_id, tv.label, f"0x{v:04X}", shown))
            # Front-panel LEDs and shore (AC input) current limit — reads only.
            leds = self.backend.read_leds()
            cfg = self.backend.read_config()
            self.call_from_thread(self._fill_telemetry, rows)
            self.call_from_thread(self._fill_dashboard_extra, leds, cfg)
        finally:
            self._tele_busy = False

    def _fill_telemetry(self, rows) -> None:
        t = self.query_one("#telemetry", DataTable)
        t.clear()
        for vid, name, raw_hex, val in rows:
            t.add_row(str(vid), name, raw_hex, val)
        self.update_status()  # reflect link health (telemetry exercises the bus)

    def _render_leds(self, led: Optional[p.LedInfo]) -> str:
        if led is None:
            return "—"
        glyph = {"on": "[b green]●[/]", "blink": "[b yellow]◐[/]", "off": "[dim]○[/]"}
        return "   ".join(f"{glyph[s]} {n}" for n, s in led.states())

    def _fill_dashboard_extra(self, leds, cfg) -> None:
        self.query_one("#leds", Static).update(self._render_leds(leds))
        if cfg is not None:
            self._last_cfg = cfg
            self.query_one("#shore-info", Static).update(
                f"actual [b]{cfg.actual_current:.1f} A[/b]   |   range "
                f"{cfg.min_current:.1f}–{cfg.max_current:.1f} A   |   switch: "
                f"{cfg.switch_state_name}   |   front switch "
                f"{'UP' if cfg.front_switch_up else 'DOWN'}")
            inp = self.query_one("#in-shore", Input)
            if not inp.value:
                inp.value = f"{cfg.actual_current:.0f}"
        else:
            self.query_one("#shore-info", Static).update("shore config unavailable")

    # ── refresh: everything (settings + model), on startup / 'r' / post-write ─

    def action_refresh_all(self) -> None:
        self.refresh_all_worker()

    @work(thread=True, group="settings")
    def action_resync(self) -> None:
        """Manually recover a silent/desynced bus, then reload everything."""
        self.backend.resync()
        self.call_from_thread(lambda: self.notify("Bus resync sent.", timeout=4))
        self.device = None  # re-identify after recovery
        self.refresh_all_worker()

    @work(thread=True, exclusive=True, group="settings")
    def refresh_all_worker(self) -> None:
        if self.device is None:
            self.device = self.backend.identify()
        snap = {sid: self.backend.read_setting(sid) for sid in self.SETTING_IDS}
        self.settings = snap
        self.call_from_thread(self._fill_settings_panels)
        self.call_from_thread(self.update_status)
        self.call_from_thread(self._start_telemetry)

    def _start_telemetry(self) -> None:
        """Begin the telemetry auto-refresh once, after the first load."""
        if self._tele_started:
            return
        self._tele_started = True
        self.refresh_telemetry()  # immediate first paint
        self.set_interval(self.refresh_interval, self.refresh_telemetry)

    def _fill_settings_panels(self) -> None:
        self._fill_flags()
        self._fill_charge()
        self._fill_grid()
        self._fill_raw()

    def _fill_flags(self) -> None:
        s0 = self.settings.get(0)
        s1 = self.settings.get(1)
        self.query_one("#s0-raw", Static).update(
            f"raw word: [b]0x{s0:04X}[/b]" if s0 is not None else "raw word: —")
        self.query_one("#s1-raw", Static).update(
            f"raw word: [b]0x{s1:04X}[/b]" if s1 is not None else "raw word: —")
        t0 = self.query_one("#s0-table", DataTable); t0.clear()
        if s0 is not None:
            for f in p.decode_setting0(s0):
                state = "● SET" if f.is_set else "○ clr"
                meaning = f.meaning if f.interpret else f"{f.meaning} (raw only)"
                if not f.confirmed:
                    meaning += "  [unconfirmed]"
                t0.add_row(str(f.bit), f.name, state, meaning)
        t1 = self.query_one("#s1-table", DataTable); t1.clear()
        if s1 is not None:
            for f in p.decode_setting1(s1):
                state = "● SET" if f.is_set else "○ clr"
                t1.add_row(str(f.bit), f.name, state, f.meaning)

    def _fill_charge(self) -> None:
        t = self.query_one("#charge-table", DataTable); t.clear()
        for sid in (2, 3, 4, 9, 10, 11):
            v = self.settings.get(sid)
            t.add_row(str(sid), p.setting_meta(sid).name,
                      p.format_setting_value(sid, v))
        # Prefill edit fields from current values (only if empty).
        self._prefill("#in-abs", self.settings.get(2), 100.0)
        self._prefill("#in-float", self.settings.get(3), 100.0)
        self._prefill("#in-current", self.settings.get(4), 1.0)
        self._prefill("#in-cutoff", self.settings.get(11), 100.0)

    def _prefill(self, sel: str, raw: Optional[int], scale: float) -> None:
        inp = self.query_one(sel, Input)
        if not inp.value and raw is not None and raw != p.UNSUPPORTED:
            inp.value = f"{raw / scale:.2f}" if scale != 1.0 else str(raw)

    def _fill_grid(self) -> None:
        t = self.query_one("#grid-table", DataTable); t.clear()
        descs = {81: "Grid code active flag", 128: "LOM config A",
                 190: "LOM config B (firmware-managed)", 191: "LOM config C"}
        for sid in (81, 128, 190, 191):
            v = self.settings.get(sid)
            raw = "—" if v is None else (f"0x{v:04X}")
            t.add_row(str(sid), descs[sid], raw, p.format_setting_value(sid, v))
        note = ("Reverting a grid code to 'None' leaves residuals in 128/190/191. "
                "Writing 0xFFFF clears 128 & 191; 190 is firmware-managed and "
                "ignores writes (its residual is harmless).")
        s128 = self.settings.get(128)
        s191 = self.settings.get(191)
        has_residual = (s128 not in (None, p.UNSUPPORTED)
                        or s191 not in (None, p.UNSUPPORTED))
        if has_residual:
            note = "[yellow]Residual LOM config present.[/yellow] " + note
        self.query_one("#grid-note", Static).update(note)

    def _fill_raw(self) -> None:
        t = self.query_one("#raw-table", DataTable); t.clear()
        for sid in self.SETTING_IDS:
            v = self.settings.get(sid)
            if v is None:
                continue
            t.add_row(str(sid), p.setting_meta(sid).name, str(v), f"0x{v:04X}")

    # ── write helpers ─────────────────────────────────────────────────────────

    async def _confirm_and_write(self, setting_id: int, new: int,
                                 warning: str = "") -> bool:
        """Show confirm modal, write if confirmed, verify, refresh. Returns ok."""
        old = self.settings.get(setting_id)
        choice = await self.push_screen_wait(
            ConfirmWrite(setting_id, old, new, warning))
        if choice is None:
            self.notify("Write cancelled.", severity="information")
            return False
        ram_only = (choice == "ram")
        result = await asyncio.to_thread(
            self.backend.write_setting, setting_id, new, ram_only, True)
        if result.ok:
            self.settings[setting_id] = result.readback
            self.notify(f"Setting {setting_id} = {p.format_setting_value(setting_id, new)} "
                        f"✓ {result.message}", severity="information", timeout=6)
        else:
            self.notify(f"Setting {setting_id} FAILED: {result.message}",
                        severity="error", timeout=8)
        self._fill_settings_panels()
        return result.ok

    def _parse_voltage(self, sel: str) -> Optional[int]:
        try:
            return p.volts_to_raw(float(self.query_one(sel, Input).value))
        except ValueError:
            self.notify("Enter a valid number.", severity="error")
            return None

    def _parse_int(self, sel: str) -> Optional[int]:
        try:
            return int(float(self.query_one(sel, Input).value))
        except ValueError:
            self.notify("Enter a valid number.", severity="error")
            return None

    # ── button handlers ───────────────────────────────────────────────────────

    @on(Button.Pressed, "#set-abs")
    @work
    async def write_abs(self) -> None:
        await self._write_validated(2, self._parse_voltage("#in-abs"))

    @on(Button.Pressed, "#set-float")
    @work
    async def write_float(self) -> None:
        await self._write_validated(3, self._parse_voltage("#in-float"))

    @on(Button.Pressed, "#set-current")
    @work
    async def write_current(self) -> None:
        await self._write_validated(4, self._parse_int("#in-current"))

    @on(Button.Pressed, "#set-cutoff")
    @work
    async def write_cutoff(self) -> None:
        await self._write_validated(11, self._parse_voltage("#in-cutoff"))

    async def _write_validated(self, setting_id: int, value: Optional[int]) -> None:
        if value is None:
            return
        vr = p.validate_setting_write(setting_id, value)
        await self._confirm_and_write(setting_id, value,
                                      warning="" if vr.ok else vr.message)

    # flag toggles (read-modify-write on the live snapshot) -------------------

    @on(Button.Pressed, "#tog-ups")
    @work
    async def toggle_ups(self) -> None:
        await self._toggle_bit(0, 3)

    @on(Button.Pressed, "#tog-pa")
    @work
    async def toggle_pa(self) -> None:
        await self._toggle_bit(0, 5)

    @on(Button.Pressed, "#tog-wac")
    @work
    async def toggle_wac(self) -> None:
        await self._toggle_bit(0, 14)

    @on(Button.Pressed, "#tog-charge")
    @work
    async def toggle_charge(self) -> None:
        await self._toggle_bit(0, 11)

    @on(Button.Pressed, "#tog-wfr")
    @work
    async def toggle_wfr(self) -> None:
        await self._toggle_bit(1, 11)

    @on(Button.Pressed, "#tog-dcl")
    @work
    async def toggle_dcl(self) -> None:
        await self._toggle_bit(1, 12)

    async def _toggle_bit(self, setting_id: int, bit: int) -> None:
        cur = self.settings.get(setting_id)
        if cur is None or cur == p.UNSUPPORTED:
            self.notify(f"Setting {setting_id} unavailable — refresh first.",
                        severity="error")
            return
        new = cur ^ (1 << bit)  # read-modify-write: flip exactly one bit
        await self._confirm_and_write(setting_id, new)

    # operating state controls ------------------------------------------------

    @on(Button.Pressed, "#state-off")
    @work
    async def state_off(self) -> None:
        await self._confirm_state(0x00)

    @on(Button.Pressed, "#state-on")
    @work
    async def state_on(self) -> None:
        await self._confirm_state(0x01)

    @on(Button.Pressed, "#state-charger")
    @work
    async def state_charger(self) -> None:
        await self._confirm_state(0x02)

    @on(Button.Pressed, "#state-normal")
    @work
    async def state_normal(self) -> None:
        await self._confirm_state(0x03)

    @on(Button.Pressed, "#set-shore")
    @work
    async def write_shore(self) -> None:
        try:
            amps = float(self.query_one("#in-shore", Input).value)
        except ValueError:
            self.notify("Enter a valid number.", severity="error")
            return
        cfg = self._last_cfg
        rng = (f"\nDevice range: {cfg.min_current:.1f}–{cfg.max_current:.1f} A "
               f"(value will be clamped)." if cfg else "")
        sw = f"\nSwitch state preserved as: {cfg.switch_state_name}." if cfg else ""
        ok = await self.push_screen_wait(_ConfirmText(
            f"Set shore power (AC input) current limit to [b]{amps:.1f} A[/b]?{rng}{sw}\n\n"
            "Uses the runtime 'S' command — not EEPROM. The value may reset to the "
            "configured default if the unit sleeps or loses VE.Bus power."))
        if not ok:
            return
        res = await asyncio.to_thread(self.backend.set_current_limit, amps, True)
        self.notify(res.message, severity="information" if res.ok else "error", timeout=8)
        # Refresh the shore-info line from the device.
        cfg2 = await asyncio.to_thread(self.backend.read_config)
        self._fill_dashboard_extra(self.backend.read_leds(), cfg2)

    async def _confirm_state(self, state: int) -> None:
        name = p.STATE_NAMES.get(state, str(state))
        ok = await self.push_screen_wait(
            _ConfirmText(f"Set operating state to:\n\n  [b]{name}[/b]\n\n"
                         f"This changes inverter operation immediately."))
        if not ok:
            return
        await asyncio.to_thread(self.backend.set_state, state)
        self.notify(f"Operating state command sent: {name}", timeout=6)

    # LiFePO4 sequence --------------------------------------------------------

    @on(Button.Pressed, "#apply-lifepo4")
    @work
    async def apply_lifepo4(self) -> None:
        abs_raw = self._parse_voltage("#in-abs")
        flt_raw = self._parse_voltage("#in-float")
        cur = self._parse_int("#in-current")
        if abs_raw is None or flt_raw is None:
            self.notify("Set absorption and float first.", severity="error")
            return
        # Cross-field + range validation before previewing.
        vo = p.validate_voltage_ordering(p.raw_to_volts(abs_raw),
                                         p.raw_to_volts(flt_raw))
        if not vo.ok:
            self.notify(vo.message, severity="error", timeout=8)
            return
        for sid, val in ((2, abs_raw), (3, flt_raw)):
            vr = p.validate_setting_write(sid, val)
            if not vr.ok:
                self.notify(vr.message, severity="error", timeout=8)
                return
        s0 = self.settings.get(0)
        if s0 is None or s0 == p.UNSUPPORTED:
            self.notify("Setting 0 unavailable — refresh first.", severity="error")
            return
        steps = p.build_lifepo4_sequence(s0, p.raw_to_volts(abs_raw),
                                         p.raw_to_volts(flt_raw), cur)
        choice = await self.push_screen_wait(ConfirmSequence(steps))
        if choice is None:
            self.notify("LiFePO4 sequence cancelled.", severity="information")
            return
        ram_only = (choice == "ram")
        await self._run_sequence(steps, ram_only)

    async def _run_sequence(self, steps: list[p.WriteStep], ram_only: bool) -> None:
        ok_count = 0
        for i, s in enumerate(steps, 1):
            result = await asyncio.to_thread(
                self.backend.write_setting, s.setting_id, s.value, ram_only, True)
            if result.ok:
                ok_count += 1
                self.settings[s.setting_id] = result.readback
                self.notify(f"[{i}/{len(steps)}] Setting {s.setting_id} ✓ {s.label}",
                            timeout=4)
            else:
                self.notify(f"[{i}/{len(steps)}] Setting {s.setting_id} FAILED: "
                            f"{result.message} — stopping.",
                            severity="error", timeout=10)
                break
            await asyncio.sleep(0.05)
        self._fill_settings_panels()
        sev = "information" if ok_count == len(steps) else "warning"
        self.notify(f"LiFePO4 sequence: {ok_count}/{len(steps)} writes confirmed.",
                    severity=sev, timeout=8)

    # grid residual cleanup ---------------------------------------------------

    @on(Button.Pressed, "#clean-residuals")
    @work
    async def clean_residuals(self) -> None:
        ok = await self.push_screen_wait(
            _ConfirmText("Write [b]0xFFFF[/b] to settings [b]128[/b] and [b]191[/b] "
                         "to clear grid-code residuals?\n\n"
                         "(Setting 190 is firmware-managed and will be left alone.)"))
        if not ok:
            return
        for sid in (128, 191):
            result = await asyncio.to_thread(
                self.backend.write_setting, sid, 0xFFFF, False, True)
            self.notify(f"Setting {sid}: {result.message}",
                        severity="information" if result.ok else "warning", timeout=6)
        self.refresh_all_worker()


class _ConfirmText(ModalScreen[bool]):
    """Generic yes/no confirmation modal."""

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("⚠  Confirm", id="dialog-title")
            yield Static(self.text, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button("Confirm", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    @on(Button.Pressed)
    def handle(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")


# ─────────────────────────────────────────────────────────────────────────────

def open_backend_with_candidates(port: str, mock: bool, fallback: bool) -> p.Backend:
    """Try the requested port, then /dev/inverter, then (optionally) mock."""
    if mock:
        return p.open_backend(mock=True)
    candidates = [port]
    if "/dev/inverter" not in candidates:
        candidates.append("/dev/inverter")
    last_err = None
    for cand in candidates:
        try:
            return p.open_backend(port=cand, mock=False, fallback_to_mock=False)
        except Exception as e:  # try next candidate
            last_err = e
    if fallback:
        b = p.MockBackend()
        b.open()
        b.description = f"MOCK (fallback — no port opened: {last_err})"
        return b
    raise SystemExit(f"Could not open any serial port {candidates}: {last_err}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Victron VE.Bus MK3 TUI")
    ap.add_argument("--port", default="/dev/ttyUSB0",
                    help="serial port (default /dev/ttyUSB0; falls back to mock)")
    ap.add_argument("--mock", action="store_true",
                    help="run with a simulated device, no hardware needed")
    ap.add_argument("--no-fallback", action="store_true",
                    help="do not fall back to mock if the port can't be opened")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="telemetry auto-refresh interval, seconds (default 2)")
    args = ap.parse_args()

    backend = open_backend_with_candidates(
        args.port, mock=args.mock, fallback=not args.no_fallback)
    try:
        VEBusTUI(backend, refresh_interval=args.interval).run()
    finally:
        backend.close()


if __name__ == "__main__":
    main()
