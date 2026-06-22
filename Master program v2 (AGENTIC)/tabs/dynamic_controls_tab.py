# tabs/dynamic_controls_tab.py
"""
Dynamic Controls Tab — direct access to every instrument axis and command.

Layout
------
  Top:    scrollable grid of (label / entry / Go) cells
  Middle: scrollable current-state display
  Bottom: button grid (homing, shutters, output toggle, calibration)
  Footer: power-tuner status bar (always visible)
"""
from __future__ import annotations
from math import ceil
from functools import partial

import threading

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QScrollArea, QGroupBox, QLabel, QLineEdit, QPushButton, QApplication,
    QSizePolicy, QFrame,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDoubleValidator

from helpers.power_tuners import ProbeTuner, PumpTuner, ProbeCalibrationScanner

DEFAULT_FEEDRATE = 1000


class DynamicControlsTab(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.state = controller.state

        # ── Instruments (graceful if absent) ─────────────────────────────────
        self.btt      = controller.get_instrument("BTT")
        self.stage    = controller.get_instrument("ESP")
        self.galvo    = controller.get_instrument("Galvo")
        self.PM       = controller.get_instrument("PM")
        self.shutter  = controller.get_instrument("Shutter")
        self.keithley = controller.get_instrument("Keithley2400")
        self.spec     = controller.get_instrument("SP2500")
        self.lakeshore= controller.get_instrument("Lakeshore331")

        self._tuner_thread = None
        self._cal_thread   = None

        # ── Entry controls (label, go-handler) ───────────────────────────────
        self._entry_defs: list[tuple[str, callable]] = [
            ("Pump waveplate (°)",         self._go_pump_polarizer),
            ("Probe waveplate (°)",        self._go_probe_polarizer),
            ("Probe polarizer (°)",        self._go_pump_hwp),
            ("Pump polarizer (°)",         self._go_probe_hwp),
            ("Pump power (µW)",            self._go_pump_power),
            ("Probe power (µW)",           self._go_probe_power),
            ("Rail 1 (mm)",                self._go_rail1),
            ("Rail 2 (mm)",                self._go_rail2),
            ("Delay stage pos (mm)",       self._go_stage_pos),
            ("Delay stage pos (ps)",       self._go_stage_ps),
            ("Delay stage speed (mm/s)",   self._go_stage_speed),
            ("Set time-zero pos (mm)",     self._go_time_zero),
            ("Galvo X",                    self._go_galvo_x),
            ("Galvo Y",                    self._go_galvo_y),
            ("Cryo X (mm)",                self._go_cryo_x),
            ("Cryo Y (mm)",                self._go_cryo_y),
            ("Cryo Z (mm)",                self._go_cryo_z),
            ("Objective focus (mm)",       self._go_focus),
            ("Gate voltage (V)",           self._go_gate_v),
            ("Temperature setpoint (K)",   self._go_temperature),
            ("Spectrometer wavelength (nm)", self._go_spec_wavelength),
            ("Spectrometer slew (nm/min)", self._go_spec_slew),
        ]

        # ── Button commands ───────────────────────────────────────────────────
        self._button_defs: list[tuple[str, callable]] = [
            ("Open pump shutter",          self._btn_open_pump_shutter),
            ("Close pump shutter",         self._btn_close_pump_shutter),
            ("Open probe shutter",         self._btn_open_probe_shutter),
            ("Close probe shutter",        self._btn_close_probe_shutter),
            ("Home rails",                 self._btn_home_rails),
            ("Home Rot 1",                 self._btn_home_pump_pol),
            ("Home Rot 2",                 self._btn_home_probe_pol),
            ("Home Rot 3",                 self._btn_home_pump_hwp),
            ("Home Rot 4",                 self._btn_home_probe_hwp),
            ("Enable Keithley output",     self._btn_keithley_on),
            ("Disable Keithley output",    self._btn_keithley_off),
            ("Set focus zero",             self._btn_focus_zero),
            ("Build probe power table",    self._btn_build_probe_table),
        ]

        self._entry_widgets: list[QLineEdit] = []

        # ── Connect to controller signals ─────────────────────────────────────
        from PyQt6.QtCore import Qt as _Qt
        sigs = controller.scan_signals
        sigs.control_set.connect(self._on_control_set, _Qt.ConnectionType.QueuedConnection)
        sigs.stop_all.connect(self._on_stop_all,       _Qt.ConnectionType.DirectConnection)

        # ── Build UI ──────────────────────────────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 4)
        outer.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(10)

        content_layout.addWidget(self._build_entry_grid())
        content_layout.addWidget(self._build_state_group())
        content_layout.addWidget(self._build_button_grid())
        content_layout.addStretch(1)

        scroll.setWidget(content)

        # Footer: tuner status bar
        self._tuner_status = QLabel("Power tuner: idle")
        self._tuner_status.setStyleSheet(
            "QLabel { background:#e8f4fd; color:#1a5276; "
            "padding:4px 8px; border-radius:3px; font-family:monospace; "
            "border:1px solid #aed6f1; }"
        )
        self._tuner_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._tuner_status.setWordWrap(True)

        outer.addWidget(scroll, stretch=1)
        outer.addWidget(self._tuner_status)

    # ── Widget builders ───────────────────────────────────────────────────────

    def _build_entry_grid(self) -> QWidget:
        box = QGroupBox("Controls")
        columns = 4
        rows = ceil(len(self._entry_defs) / columns)
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)

        dv = QDoubleValidator(-1e9, 1e9, 6)

        for idx, (label, handler) in enumerate(self._entry_defs):
            row = idx // columns
            col = idx % columns

            lbl   = QLabel(label + ":")
            lbl.setStyleSheet("font-size:11px;")
            entry = QLineEdit()
            entry.setPlaceholderText("value…")
            entry.setValidator(dv)
            entry.setFixedWidth(100)
            btn   = QPushButton("Go")
            btn.setFixedWidth(36)
            btn.clicked.connect(partial(self._on_go, idx))

            self._entry_widgets.append(entry)

            cell = QWidget()
            vbox = QVBoxLayout(cell)
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(2)
            row_inner = QHBoxLayout()
            row_inner.setSpacing(4)
            row_inner.addWidget(entry)
            row_inner.addWidget(btn)
            vbox.addWidget(lbl)
            vbox.addLayout(row_inner)

            grid.addWidget(cell, row, col)

        grid.setRowStretch(rows, 1)
        return box

    def _build_state_group(self) -> QGroupBox:
        box = QGroupBox("Current State")
        layout = QFormLayout(box)
        layout.setSpacing(2)
        self._state_labels: dict[str, QLabel] = {}
        for key, val in self.state.settings.items():
            name_lbl  = QLabel(f"{key}:")
            name_lbl.setStyleSheet("font-size:10px; color:#555;")
            value_lbl = QLabel(self._fmt(val))
            value_lbl.setStyleSheet("font-size:10px;")
            self._state_labels[key] = value_lbl
            layout.addRow(name_lbl, value_lbl)
        return box

    def _build_button_grid(self) -> QGroupBox:
        box = QGroupBox("Commands")
        columns = 4
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        for jdx, (label, handler) in enumerate(self._button_defs):
            row = jdx // columns
            col = jdx % columns
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            grid.addWidget(btn, row, col)
        return box

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt(val) -> str:
        return f"{val:.5g}" if isinstance(val, float) else str(val)

    def _read(self, idx: int) -> float | None:
        text = self._entry_widgets[idx].text().strip()
        try:
            return float(text)
        except ValueError:
            self._set_status(f"Invalid value for '{self._entry_defs[idx][0]}'")
            return None

    def _set_status(self, msg: str):
        self._tuner_status.setText(f"Power tuner: {msg}")
        self.controller.log(msg)

    def update_state_display(self):
        for key, lbl in self._state_labels.items():
            lbl.setText(self._fmt(self.state.settings.get(key, "")))
        QApplication.processEvents()

    def showEvent(self, event):
        super().showEvent(event)
        self.update_state_display()

    # ── Entry Go handlers ─────────────────────────────────────────────────────

    def _on_go(self, idx: int):
        v = self._read(idx)
        if v is None:
            return
        label, handler = self._entry_defs[idx]
        try:
            handler(v)
        except Exception as e:
            self._set_status(f"Error — {label}: {e}")
            return
        self.controller.log(f"Dynamic control: {label} → {v}")
        self.update_state_display()

    def _go_pump_polarizer(self, v):
        if self.btt: self.btt.rot_1(v, 5000)

    def _go_probe_polarizer(self, v):
        if self.btt: self.btt.rot_2(v, DEFAULT_FEEDRATE)

    def _go_pump_hwp(self, v):
        if self.btt: self.btt.rot_3(v, DEFAULT_FEEDRATE)

    def _go_probe_hwp(self, v):
        if self.btt: self.btt.rot_4(v, DEFAULT_FEEDRATE)

    def _go_pump_power(self, v):
        if not (self.btt and self.PM and self.shutter):
            raise RuntimeError("BTT, PM, or Shutter not available.")
        if self._tuner_thread and self._tuner_thread.isRunning():
            self._tuner_thread.quit(); self._tuner_thread.wait()
        self._set_status(f"Starting pump tuner → target {v:.2f} µW")
        tuner = PumpTuner(
            target=v, pm=self.PM, btt=self.btt, shutter=self.shutter,
            initial_wp=self.state.settings.get("rot_3_angle", 0.0),
        )
        self._tuner_thread = tuner
        tuner.reading_ready.connect(self._on_pump_update)
        tuner.status_message.connect(self._set_status)
        tuner.finished.connect(tuner.deleteLater)
        tuner.finished.connect(lambda: setattr(self, '_tuner_thread', None))
        tuner.finished.connect(lambda: self._set_status("idle"))
        tuner.start()

    def _go_probe_power(self, v):
        if not (self.btt and self.PM and self.shutter):
            raise RuntimeError("BTT, PM, or Shutter not available.")
        if self._tuner_thread and self._tuner_thread.isRunning():
            self._tuner_thread.quit(); self._tuner_thread.wait()
        self._set_status(f"Starting probe tuner → target {v:.2f} µW")
        tuner = ProbeTuner(
            target=v, pm=self.PM, btt=self.btt, shutter=self.shutter,
            initial_wp=self.state.settings.get("rot_4_angle", 0.0),  # rot_4 = pump polarizer
        )
        self._tuner_thread = tuner
        tuner.reading_ready.connect(self._on_probe_update)
        tuner.status_message.connect(self._set_status)
        tuner.finished.connect(tuner.deleteLater)
        tuner.finished.connect(lambda: setattr(self, '_tuner_thread', None))
        tuner.finished.connect(lambda: self._set_status("idle"))
        tuner.start()

    def _go_rail1(self, v):
        if self.btt: self.btt.rail_1(v)

    def _go_rail2(self, v):
        if self.btt: self.btt.rail_2(v)

    def _go_stage_pos(self, v):
        if self.stage: self.stage.move_and_wait(v)

    def _go_stage_ps(self, v):
        if self.stage: self.stage.move_and_wait_ps(v)

    def _go_stage_speed(self, v):
        if self.stage: self.stage.set_speed(1, v)

    def _go_time_zero(self, v):
        self.state.settings["time zero pos"] = v

    def _go_galvo_x(self, v):
        if self.galvo: self.galvo.move_x(v)
        self.state.settings["galvo_x_position"] = v

    def _go_galvo_y(self, v):
        if self.galvo: self.galvo.move_y(v)
        self.state.settings["galvo_y_position"] = v

    def _go_cryo_x(self, v):
        if self.btt: self.btt.cryoXY(v, 0, DEFAULT_FEEDRATE)

    def _go_cryo_y(self, v):
        if self.btt: self.btt.cryoXY(0, v, DEFAULT_FEEDRATE)

    def _go_cryo_z(self, v):
        if self.btt: self.btt.cryoZ(v, DEFAULT_FEEDRATE)

    def _go_focus(self, v):
        if self.btt: self.btt.focus(v)

    def _go_gate_v(self, v):
        if self.keithley: self.keithley.ramp_voltage(v)

    def _go_temperature(self, v):
        if self.lakeshore: self.lakeshore.set_temp(v)

    def _go_spec_wavelength(self, v):
        if self.spec: self.spec.set_nm(v)

    def _go_spec_slew(self, v):
        if self.spec: self.spec.set_nm_min(v)

    # ── Button handlers ───────────────────────────────────────────────────────

    def _btn_open_pump_shutter(self):
        if self.shutter:
            self.shutter.openPump()
            self.state.settings["pump_shutter_open"] = True
            self.controller.log("Pump shutter opened")
            self.update_state_display()

    def _btn_close_pump_shutter(self):
        if self.shutter:
            self.shutter.closePump()
            self.state.settings["pump_shutter_open"] = False
            self.controller.log("Pump shutter closed")
            self.update_state_display()

    def _btn_open_probe_shutter(self):
        if self.shutter:
            self.shutter.openProbe()
            self.state.settings["probe_shutter_open"] = True
            self.controller.log("Probe shutter opened")
            self.update_state_display()

    def _btn_close_probe_shutter(self):
        if self.shutter:
            self.shutter.closeProbe()
            self.state.settings["probe_shutter_open"] = False
            self.controller.log("Probe shutter closed")
            self.update_state_display()

    def _btn_home_rails(self):
        if self.btt:
            self.btt.homeRails()
            self.controller.log("Rails homed")

    def _btn_home_pump_pol(self):
        if self.btt:
            self.btt.home_rot1()
            self.controller.log("Pump waveplate (Rot 1) homed")

    def _btn_home_probe_pol(self):
        if self.btt:
            self.btt.home_rot2()
            self.controller.log("Probe waveplate (Rot 2) homed")

    def _btn_home_pump_hwp(self):
        if self.btt:
            self.btt.home_rot3()
            self.state.settings["rot_3_angle"] = 0.0
            self.controller.log("Probe polarizer (Rot 3) homed")
            self.update_state_display()

    def _btn_home_probe_hwp(self):
        if self.btt:
            self.btt.home_rot4()
            self.state.settings["rot_4_angle"] = 0.0
            self.controller.log("Pump polarizer (Rot 4) homed")
            self.update_state_display()

    def _btn_keithley_on(self):
        if self.keithley:
            self.keithley.output_on()
            self.controller.log("Keithley output ON")

    def _btn_keithley_off(self):
        if self.keithley:
            self.keithley.output_off()
            self.controller.log("Keithley output OFF")

    def _btn_focus_zero(self):
        self.state.settings["focus_position"] = 0.0
        self.controller.log("Focus position zeroed")
        self.update_state_display()

    def _btn_build_probe_table(self):
        if not (self.btt and self.PM and self.shutter):
            self._set_status("BTT, PM, or Shutter not available for calibration.")
            return
        if self._cal_thread and self._cal_thread.isRunning():
            self._set_status("Calibration already running.")
            return
        self._set_status("Building probe power table…")
        cal = ProbeCalibrationScanner(
            pm=self.PM, btt=self.btt, shutter=self.shutter, state=self.state
        )
        self._cal_thread = cal
        cal.status_message.connect(self._set_status)
        cal.calibration_done.connect(self._on_probe_cal_done)
        cal.finished.connect(cal.deleteLater)
        cal.finished.connect(lambda: setattr(self, '_cal_thread', None))
        cal.finished.connect(lambda: self._set_status("idle"))
        cal.start()

    # ── Tuner callbacks ───────────────────────────────────────────────────────

    def _on_pump_update(self, angle: float, power: float):
        self.state.settings["rot_4_angle"] = angle   # rot_4 = pump polarizer
        self.state.settings["pump_power"] = power
        self.update_state_display()

    def _on_probe_update(self, angle: float, power: float):
        self.state.settings["rot_3_angle"] = angle   # rot_3 = probe polarizer
        self.state.settings["probe_power"] = power
        self.update_state_display()

    def _on_probe_cal_done(self, C, D, E, start_angle):
        import numpy as np
        R = float(np.sqrt(D**2 + E**2))
        self._set_status(
            f"Probe table done — P_min={C-R:.2f} µW  P_max={C+R:.2f} µW"
        )
        self.update_state_display()

    # ── Signal handlers (from controller) ────────────────────────────────────

    def _on_control_set(self, label: str, value: float):
        """Update the matching entry field when the agent sets a control."""
        for idx, (lbl, _) in enumerate(self._entry_defs):
            if lbl.lower() == label.lower():
                self._entry_widgets[idx].setText(str(value))
                return

    def _on_stop_all(self):
        """Abort any running tuner or calibration thread."""
        for thread_attr in ("_tuner_thread", "_cal_thread"):
            t = getattr(self, thread_attr, None)
            if t and t.isRunning():
                t.quit()
                t.wait(2000)
                setattr(self, thread_attr, None)
        self._set_status("idle (stopped)")

    # ── Public API for agent tools ────────────────────────────────────────────

    def list_controls(self) -> list[str]:
        return [label for label, _ in self._entry_defs]

    def list_buttons(self) -> list[str]:
        return [label for label, _ in self._button_defs]

    def set_control(self, label: str, value: float):
        """Call a named entry control from outside (e.g. agent).

        For power tuning controls this blocks until the tuner thread finishes,
        so the agent cannot proceed until the power has actually been set.
        """
        for idx, (lbl, handler) in enumerate(self._entry_defs):
            if lbl.lower() == label.lower():
                handler(value)
                # If the handler launched a tuner QThread, block the agent worker
                # thread until it completes.
                #
                # We use a Python threading.Event + DirectConnection instead of
                # QThread.wait() to avoid a Qt deadlock: the tuner was created on
                # the agent worker thread (a QThread itself), so its queued signals
                # are delivered to that thread's event loop — which is frozen inside
                # QThread.wait().  DirectConnection fires the lambda in the emitting
                # (tuner) thread directly, bypassing the event queue entirely.
                t = self._tuner_thread
                if t is not None and t.isRunning():
                    _done = threading.Event()
                    t.finished.connect(
                        lambda: _done.set(),
                        Qt.ConnectionType.DirectConnection,
                    )
                    _done.wait()   # pure-Python wait, no Qt event loop needed
                self.controller.log(f"Agent set '{lbl}' -> {value}")
                self.controller.notify_control_set(lbl, value)
                self.update_state_display()
                return
        raise ValueError(f"Unknown control: '{label}'")

    def press_button(self, label: str):
        """Press a named command button from outside (e.g. agent)."""
        for lbl, handler in self._button_defs:
            if lbl.lower() == label.lower():
                handler()
                self.controller.log(f"Agent pressed '{lbl}'")
                return
        raise ValueError(f"Unknown button: '{label}'")
