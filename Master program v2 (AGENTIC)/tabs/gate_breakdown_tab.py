# tabs/gate_breakdown_tab.py
"""
Gate breakdown tester — ramps one Keithley 2400 gate and watches the leakage
current, stopping the moment the gate starts to conduct.

Every path out of a sweep (finished, breakdown, user stop, instrument error)
ramps the gate back to 0 V before releasing it, so the device is never left
biased after an abort.
"""
from __future__ import annotations
import base64
import io
import math
import os
import threading
import time

import numpy as np

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout, QGroupBox,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QCheckBox,
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator, QFont
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from drivers.Keithley2400 import RAMP_DOWN_RATE_V_PER_S_DEFAULT

TAB_NAME = "Gate Breakdown"


# ── Workers ───────────────────────────────────────────────────────────────────

class _RampWorker(QThread):
    """Ramps the gate to a target voltage off the GUI thread."""
    finished = pyqtSignal(bool, str)          # (aborted, message)

    def __init__(self, k, target_v: float, step_v: float, dwell_s: float,
                 stop_flag_ref: list[bool]):
        super().__init__()
        self._k      = k
        self._target = float(target_v)
        self._step   = abs(float(step_v))
        self._dwell  = max(0.0, float(dwell_s))
        self._stop   = stop_flag_ref

    def run(self):
        try:
            self._k.ramp_voltage(self._target, step_V=self._step,
                                 dwell_s=self._dwell,
                                 abort_flag=lambda: self._stop[0])
        except Exception as e:
            self.finished.emit(True, f"Ramp failed: {e}")
            return
        if self._stop[0]:
            self.finished.emit(True, "Ramp stopped by user.")
        else:
            self.finished.emit(False, f"Gate at {self._target:g} V.")


class _GateSweepWorker(QThread):
    """Ramps to the sweep start, steps to the end, and always walks back to 0 V.

    The sweep ends early when |I| crosses the abort threshold or the source
    trips its compliance limit — either counts as the gate breaking down.
    """
    point    = pyqtSignal(float, float)       # (V applied, I measured)
    status   = pyqtSignal(str)
    finished = pyqtSignal(bool, str, float)   # (aborted, message, breakdown V or NaN)

    def __init__(self, k, v_start: float, v_end: float, v_step: float,
                 dwell_s: float, i_abort: float, ramp_down_rate: float,
                 stop_flag_ref: list[bool]):
        super().__init__()
        self._k         = k
        self._v_start   = float(v_start)
        self._v_end     = float(v_end)
        self._step      = abs(float(v_step))
        self._dwell     = max(0.0, float(dwell_s))
        self._i_abort   = abs(float(i_abort))
        self._down_rate = abs(float(ramp_down_rate))
        self._stop      = stop_flag_ref

        # The worker owns the authoritative copy of the data: reading these
        # after wait() returns avoids racing the queued GUI updates.
        self.vs: list[float]       = []
        self.currents: list[float] = []
        self.breakdown_v: float    = math.nan

    def run(self):
        aborted = False
        message = "Sweep complete — no breakdown detected."
        try:
            span   = self._v_end - self._v_start
            n      = int(np.floor(abs(span / self._step)) + 1)
            volts  = np.linspace(self._v_start,
                                 self._v_start + (n - 1) * self._step * np.sign(span),
                                 n)
            compliance = float(self._k.get_current_compliance())

            self._k.set_source_voltage_mode()
            self._k.output_on()

            self.status.emit(f"Ramping to {self._v_start:g} V…")
            self._k.ramp_voltage(self._v_start, step_V=self._step,
                                 dwell_s=self._dwell,
                                 abort_flag=lambda: self._stop[0])

            if self._stop[0]:
                aborted, message = True, "Stopped by user before the sweep started."
            else:
                self.status.emit("Sweeping…")
                for v in volts:
                    if self._stop[0]:
                        aborted = True
                        message = f"Stopped by user at {float(v):g} V."
                        break

                    # The sweep is the one place that must not ramp: it is
                    # already stepping by its own v_step and dwell, and a
                    # nested ramp would re-walk every point.
                    self._k.set_voltage_immediate(float(v))
                    if self._dwell > 0:
                        time.sleep(self._dwell)

                    iv = self._k.read_iv()
                    if iv is None:
                        aborted = True
                        message = f"Measurement failed at {float(v):g} V."
                        break
                    i_meas = float(iv[0])

                    self.vs.append(float(v))
                    self.currents.append(i_meas)
                    self.point.emit(float(v), i_meas)

                    if abs(i_meas) >= self._i_abort:
                        self.breakdown_v = float(v)
                        message = (f"Breakdown at {float(v):g} V — |I| = {abs(i_meas):.3e} A "
                                   f"crossed the {self._i_abort:.3e} A limit.")
                        break

                    # Only ask the meter about its compliance bit once the
                    # reading is near the limit — it is one GPIB query per point.
                    if abs(i_meas) >= 0.95 * compliance and self._k.in_compliance():
                        self.breakdown_v = float(v)
                        message = (f"Breakdown at {float(v):g} V — source hit its "
                                   f"{compliance:.3e} A compliance limit.")
                        break
        except Exception as e:
            aborted = True
            message = f"Sweep error: {e}"
        finally:
            # Unconditional: a stopped or failed sweep must still leave the
            # gate at 0 V. Uses the fast measurement-free ramp — walking back
            # down at the sweep's own step/dwell would keep a gate that has
            # just broken down sitting at full bias for tens of seconds.
            self.status.emit(f"Ramping back to 0 V at {self._down_rate:g} V/s…")
            try:
                self._k.ramp_voltage_fast(0.0, rate_V_per_s=self._down_rate)
            except Exception as e:
                message += f"  (Ramp back to 0 V failed: {e})"
            self.finished.emit(aborted, message, self.breakdown_v)


# ── Headless plot renderer (for the agent) ───────────────────────────────────

def _render_iv_png(vs, currents, breakdown_v: float, log_scale: bool) -> str:
    """Render an I-V curve to a base64 PNG without touching the GUI figure."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = Figure(figsize=(5, 3.5), dpi=90, facecolor="#f5f5f5")
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_facecolor("#ffffff")

    ys = np.abs(np.asarray(currents, float)) if log_scale else np.asarray(currents, float)
    if log_scale:
        ax.set_yscale("log")
    ax.plot(np.asarray(vs, float), ys, "o-", markersize=3, linewidth=1.3,
            color="#2980b9")

    if math.isfinite(breakdown_v) and len(vs):
        ax.axvline(breakdown_v, color="#c0392b", linestyle="--", linewidth=1.2)
        ax.plot([breakdown_v], [ys[-1]], "x", color="#c0392b", markersize=10,
                markeredgewidth=2)
        ax.set_title(f"Breakdown at {breakdown_v:g} V", fontsize=9, color="#c0392b")

    ax.set_xlabel("Gate voltage (V)", fontsize=8)
    ax.set_ylabel("|I| (A)" if log_scale else "Current (A)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode()


# ── Tab ───────────────────────────────────────────────────────────────────────

class GateBreakdownTab(QWidget):
    """Single-gate breakdown tester driven by the Keithley 2400."""

    # Emitted from the agent thread once a blocking sweep has finished, so the
    # plot ends up holding exactly the worker's data (queued → lands after any
    # still-pending point updates).
    _finalize_sig = pyqtSignal(object, object, float)

    # Runs a callable on the GUI thread on behalf of the agent thread. Drawing
    # or saving a figure from another thread trips matplotlib's mathtext parser
    # (the log-scale tick labels), which is not thread-safe.
    _gui_call_sig = pyqtSignal(object)

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.state = controller.state
        self.k     = controller.get_instrument("Keithley2400")

        outer = QHBoxLayout(self)
        ctrl  = QVBoxLayout()

        dv     = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_pos = QDoubleValidator(0.0, 1e300, 12, self)

        # ── Gate control ──────────────────────────────────────────────────────
        gate_box = QGroupBox("Gate")
        gate_form = QFormLayout(gate_box)

        self.compliance_entry = QLineEdit("1e-6")
        self.compliance_entry.setValidator(dv_pos)
        self.compliance_entry.setToolTip(
            "Current compliance pushed to the Keithley — the hardware limit that "
            "protects the device.")
        compliance_btn = QPushButton("Set")
        compliance_btn.clicked.connect(self._set_compliance)
        comp_row = QHBoxLayout()
        comp_row.addWidget(self.compliance_entry)
        comp_row.addWidget(compliance_btn)

        self.set_v_entry = QLineEdit("0")
        self.set_v_entry.setValidator(dv)
        self.set_v_entry.setToolTip("Ramp the gate to this voltage (V).")
        set_v_btn = QPushButton("Go")
        set_v_btn.clicked.connect(self._set_gate_voltage)
        set_v_row = QHBoxLayout()
        set_v_row.addWidget(self.set_v_entry)
        set_v_row.addWidget(set_v_btn)

        out_row = QHBoxLayout()
        out_on_btn  = QPushButton("Output ON")
        out_off_btn = QPushButton("Output OFF")
        out_on_btn.clicked.connect(self._output_on)
        out_off_btn.clicked.connect(self._output_off)
        out_row.addWidget(out_on_btn)
        out_row.addWidget(out_off_btn)

        # Global ramp parameters — these live in the shared state and govern
        # EVERY Keithley voltage change in the program (scan axes, dynamic
        # controls, the agent), not just this tab. The breakdown sweep below
        # is the one exception: it steps by its own Step V / Dwell.
        self.ramp_step_entry = QLineEdit(
            f"{float(self.state.settings.get('keithley_ramp_step_v', 0.05)):g}")
        self.ramp_step_entry.setValidator(dv_pos)
        self.ramp_step_entry.setToolTip(
            "Voltage increment per step for every Keithley voltage change "
            "(state: keithley_ramp_step_v).")
        self.ramp_dwell_entry = QLineEdit(
            f"{float(self.state.settings.get('keithley_ramp_dwell_s', 0.02)) * 1000:g}")
        self.ramp_dwell_entry.setValidator(dv_pos)
        self.ramp_dwell_entry.setToolTip(
            "Wait between ramp steps (state: keithley_ramp_dwell_s).")
        self.ramp_step_entry.editingFinished.connect(self._apply_ramp_params)
        self.ramp_dwell_entry.editingFinished.connect(self._apply_ramp_params)

        gate_form.addRow("Compliance (A):", comp_row)
        gate_form.addRow("Set gate (V):",   set_v_row)
        gate_form.addRow("Ramp step (V):",  self.ramp_step_entry)
        gate_form.addRow("Ramp wait (ms):", self.ramp_dwell_entry)
        gate_form.addRow(out_row)

        # ── Sweep parameters ──────────────────────────────────────────────────
        sweep_box  = QGroupBox("Breakdown sweep")
        sweep_form = QFormLayout(sweep_box)

        self.v_start  = QLineEdit("0");    self.v_start.setValidator(dv)
        self.v_end    = QLineEdit("5");    self.v_end.setValidator(dv)
        self.v_step   = QLineEdit("0.01"); self.v_step.setValidator(dv_pos)
        self.dwell_ms = QLineEdit("20");   self.dwell_ms.setValidator(QIntValidator(0, 600_000, self))
        self.i_abort  = QLineEdit("1e-7"); self.i_abort.setValidator(dv_pos)
        self.i_abort.setToolTip(
            "Sweep stops and records a breakdown when |I| reaches this value.")

        self.ramp_down_rate = QLineEdit(f"{RAMP_DOWN_RATE_V_PER_S_DEFAULT:g}")
        self.ramp_down_rate.setValidator(dv_pos)
        self.ramp_down_rate.setToolTip(
            "How fast the gate is walked back to 0 V when the sweep ends, breaks "
            "down or is stopped. Measurement-free, so the rate is honoured exactly: "
            f"{RAMP_DOWN_RATE_V_PER_S_DEFAULT:g} V/s ≈ "
            f"{10.0 / RAMP_DOWN_RATE_V_PER_S_DEFAULT:.1f} s from 10 V.")

        sweep_form.addRow("Start V:",          self.v_start)
        sweep_form.addRow("End V:",            self.v_end)
        sweep_form.addRow("Step V:",           self.v_step)
        sweep_form.addRow("Dwell (ms):",       self.dwell_ms)
        sweep_form.addRow("Abort at |I| (A):", self.i_abort)
        sweep_form.addRow("Ramp down (V/s):",  self.ramp_down_rate)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start sweep")
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        sweep_form.addRow(btn_row)

        # ── Save ──────────────────────────────────────────────────────────────
        save_box = QGroupBox("Save")
        save_grid = QGridLayout(save_box)
        self.folder   = QLineEdit(os.path.expanduser("~/gate_breakdown"))
        self.filename = QLineEdit("gate_sweep")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        save_grid.addWidget(QLabel("Folder:"),        0, 0)
        save_grid.addWidget(self.folder,              0, 1)
        save_grid.addWidget(browse_btn,               0, 2)
        save_grid.addWidget(QLabel("File (no ext):"), 1, 0)
        save_grid.addWidget(self.filename,            1, 1)
        save_grid.addWidget(self.save_btn,            1, 2)

        # ── Readout ───────────────────────────────────────────────────────────
        big = QFont()
        big.setPointSize(20)
        big.setBold(True)
        self.readout_lbl = QLabel("–")
        self.readout_lbl.setFont(big)
        self.readout_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setStyleSheet("color:#c0392b; font-weight:bold;")

        self.status_lbl = QLabel("Idle.")
        self.status_lbl.setStyleSheet("color:#555;")

        ctrl.addWidget(gate_box)
        ctrl.addWidget(sweep_box)
        ctrl.addWidget(save_box)
        ctrl.addWidget(self.readout_lbl)
        ctrl.addWidget(self.result_lbl)
        ctrl.addWidget(self.status_lbl)
        ctrl.addStretch(1)

        # ── Plot ──────────────────────────────────────────────────────────────
        right = QVBoxLayout()
        self.log_chk = QCheckBox("Log |I|")
        self.log_chk.setChecked(True)
        self.log_chk.stateChanged.connect(self._refresh_plot)
        right.addWidget(self.log_chk)

        self.fig = Figure(figsize=(6, 5), tight_layout=True, facecolor="#f5f5f5")
        self.ax  = self.fig.add_subplot(111)
        self.ax.set_facecolor("#ffffff")
        self.ax.set_xlabel("Gate voltage (V)")
        self.ax.set_ylabel("|I| (A)")
        self.ax.grid(True, alpha=0.3)
        self._line,     = self.ax.plot([], [], "o-", markersize=3, linewidth=1.3,
                                       color="#2980b9")
        self._bd_marker, = self.ax.plot([], [], "x", color="#c0392b", markersize=12,
                                        markeredgewidth=2, label="Breakdown")
        self.canvas = FigureCanvas(self.fig)
        right.addWidget(self.canvas, stretch=1)

        outer.addLayout(ctrl, stretch=1)
        outer.addLayout(right, stretch=2)

        # ── Runtime state ─────────────────────────────────────────────────────
        self._stop_flag: list[bool] = [False]
        self._worker: _GateSweepWorker | None = None
        self._ramp_worker: _RampWorker | None = None
        self._vs: list[float] = []
        self._is: list[float] = []
        self._breakdown_v: float = math.nan

        self._finalize_sig.connect(self._on_finalize, Qt.ConnectionType.QueuedConnection)
        self._gui_call_sig.connect(self._on_gui_call, Qt.ConnectionType.QueuedConnection)
        # DirectConnection so an agent-panel stop sets the flag immediately
        # rather than waiting for the next GUI event-loop cycle.
        controller.scan_signals.stop_all.connect(
            self._stop, Qt.ConnectionType.DirectConnection)

    # ── GUI-thread marshalling ────────────────────────────────────────────────

    def _run_on_gui(self, fn):
        """Run fn() on the GUI thread and return its result (blocks the caller)."""
        if QThread.currentThread() is self.thread():
            return fn()
        req = {"fn": fn, "done": threading.Event()}
        self._gui_call_sig.emit(req)
        if not req["done"].wait(120.0):
            raise RuntimeError("Timed out waiting for the GUI thread.")
        if "error" in req:
            raise RuntimeError(req["error"])
        return req.get("result")

    @pyqtSlot(object)
    def _on_gui_call(self, req: dict):
        try:
            req["result"] = req["fn"]()
        except Exception as e:
            req["error"] = str(e)
        finally:
            req["done"].set()

    # ── Gate controls ─────────────────────────────────────────────────────────

    def _require_keithley(self) -> bool:
        if self.k is None:
            QMessageBox.warning(self, "No Keithley", "Keithley 2400 is not connected.")
            return False
        return True

    def _output_on(self):
        if not self._require_keithley():
            return
        self.k.output_on()
        self.controller.log("Gate breakdown: Keithley output ON")
        self.status_lbl.setText("Output ON.")

    def _output_off(self):
        if not self._require_keithley():
            return
        self.k.output_off()
        self.controller.log("Gate breakdown: Keithley output OFF")
        self.status_lbl.setText("Output OFF.")

    def _set_compliance(self):
        if not self._require_keithley():
            return
        try:
            amps = float(self.compliance_entry.text())
            if amps <= 0:
                raise ValueError("compliance must be > 0")
        except ValueError as e:
            QMessageBox.warning(self, "Input error", f"Invalid compliance: {e}")
            return
        self.k.set_current_compliance(amps)
        self.controller.log(f"Gate breakdown: compliance set to {amps:.3e} A")
        self.status_lbl.setText(f"Compliance {amps:.3e} A.")

    def showEvent(self, event):
        # The same two state values are editable from the Dynamic Controls tab,
        # so re-read them on every show rather than trusting the fields.
        super().showEvent(event)
        self._refresh_ramp_fields()

    def _refresh_ramp_fields(self):
        self.ramp_step_entry.setText(
            f"{float(self.state.settings.get('keithley_ramp_step_v', 0.05)):g}")
        self.ramp_dwell_entry.setText(
            f"{float(self.state.settings.get('keithley_ramp_dwell_s', 0.02)) * 1000:g}")

    def _apply_ramp_params(self):
        """Push the ramp step/wait fields into the shared state."""
        try:
            step = abs(float(self.ramp_step_entry.text()))
            wait = abs(float(self.ramp_dwell_entry.text())) / 1000.0
        except ValueError:
            return
        if step <= 0:
            QMessageBox.warning(self, "Input error", "Ramp step must be > 0 V.")
            self._refresh_ramp_fields()
            return
        self.state.settings["keithley_ramp_step_v"]  = step
        self.state.settings["keithley_ramp_dwell_s"] = wait
        self.state.save()
        self.controller.log(
            f"Keithley ramp: {step:g} V steps, {wait * 1000:g} ms wait")

    def _set_gate_voltage(self):
        """Ramp the gate to the entered voltage (off the GUI thread)."""
        if not self._require_keithley():
            return
        if self._busy():
            return
        try:
            target = float(self.set_v_entry.text())
            # Global ramp parameters, not the sweep's Step V / Dwell.
            step   = self.k.get_ramp_step_v()
            dwell  = self.k.get_ramp_dwell_s()
        except ValueError as e:
            QMessageBox.warning(self, "Input error", f"Invalid ramp settings: {e}")
            return

        self._stop_flag[0] = False
        self._ramp_worker = _RampWorker(self.k, target, step, dwell, self._stop_flag)
        self._ramp_worker.finished.connect(self._on_ramp_finished,
                                           Qt.ConnectionType.QueuedConnection)
        self._set_running(True)
        self.status_lbl.setText(f"Ramping to {target:g} V…")
        self._ramp_worker.start()

    def _on_ramp_finished(self, aborted: bool, message: str):
        self._set_running(False)
        self.status_lbl.setText(message)
        # Wait for the thread to exit before dropping the last reference
        # (finished is emitted from inside run()), else Qt aborts the process.
        worker, self._ramp_worker = self._ramp_worker, None
        if worker is not None:
            worker.wait()
        if not aborted:
            try:
                self.state.settings["gate_voltage"] = float(self.set_v_entry.text())
            except ValueError:
                pass
        self.controller.log(f"Gate breakdown: {message}")

    # ── Sweep ─────────────────────────────────────────────────────────────────

    def _busy(self) -> bool:
        if (self._worker and self._worker.isRunning()) or \
           (self._ramp_worker and self._ramp_worker.isRunning()):
            QMessageBox.information(self, "Busy",
                                    "The gate is already moving — stop it first.")
            return True
        return False

    def _read_sweep_params(self) -> dict | None:
        """Parse the sweep fields, or return None (after warning) if invalid."""
        try:
            p = self._parse_params()
        except ValueError as e:
            QMessageBox.warning(self, "Input error", str(e))
            return None
        return p

    def _parse_params(self) -> dict:
        """Parse and validate the sweep fields. Raises ValueError if unusable."""
        try:
            p = {
                "v_start":    float(self.v_start.text()),
                "v_end":      float(self.v_end.text()),
                "v_step":     float(self.v_step.text()),
                "dwell_s":    int(self.dwell_ms.text()) / 1000.0,
                "i_abort":    float(self.i_abort.text()),
                "compliance": float(self.compliance_entry.text()),
                "down_rate":  float(self.ramp_down_rate.text()),
            }
        except ValueError:
            raise ValueError("Sweep parameters are invalid.")
        if p["v_step"] <= 0:
            raise ValueError("Step V must be > 0.")
        if p["v_start"] == p["v_end"]:
            raise ValueError("Start and end voltage are equal.")
        if p["i_abort"] <= 0 or p["compliance"] <= 0:
            raise ValueError("Compliance and abort current must be > 0.")
        if p["down_rate"] <= 0:
            raise ValueError("Ramp-down rate must be > 0 V/s.")
        return p

    def _start(self):
        if not self._require_keithley() or self._busy():
            return
        p = self._read_sweep_params()
        if p is None:
            return

        self.k.set_current_compliance(p["compliance"])
        self._reset_plot()
        self._stop_flag[0] = False
        self.controller.log(
            f"Gate sweep: {p['v_start']:g} → {p['v_end']:g} V, step {p['v_step']:g} V, "
            f"abort |I| ≥ {p['i_abort']:.3e} A")

        self._worker = self._make_worker(p)
        self._worker.point.connect(self._on_point,       Qt.ConnectionType.QueuedConnection)
        self._worker.status.connect(self.status_lbl.setText, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._set_running(True)
        self._worker.start()

    def _make_worker(self, p: dict) -> _GateSweepWorker:
        return _GateSweepWorker(self.k, p["v_start"], p["v_end"], p["v_step"],
                                p["dwell_s"], p["i_abort"], p["down_rate"],
                                self._stop_flag)

    def _stop(self):
        self._stop_flag[0] = True
        if (self._worker and self._worker.isRunning()) or \
           (self._ramp_worker and self._ramp_worker.isRunning()):
            self.status_lbl.setText("Stopping — ramping the gate back to 0 V…")

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    # ── Worker handlers ───────────────────────────────────────────────────────

    def _on_point(self, v: float, i: float):
        self._vs.append(v)
        self._is.append(i)
        self.readout_lbl.setText(f"{v:g} V   ·   {i:.3e} A")
        self._refresh_plot()

    def _on_finished(self, aborted: bool, message: str, breakdown_v: float):
        self._breakdown_v = breakdown_v
        self._set_running(False)
        self.status_lbl.setText(message)
        self.result_lbl.setText(
            f"Breakdown: {breakdown_v:g} V" if math.isfinite(breakdown_v) else "")
        self._refresh_plot()
        # Wait for the thread to exit before dropping the last reference
        # (finished is emitted from inside run()), else Qt aborts the process.
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.wait()
        self.controller.log(f"Gate sweep: {message}")
        if math.isfinite(breakdown_v):
            self.state.settings["gate_breakdown_voltage"] = float(breakdown_v)
        self.state.settings["gate_voltage"] = 0.0   # every exit ramps back to 0 V

    def _on_finalize(self, vs, currents, breakdown_v: float):
        """Replace the plotted data with the worker's own copy (agent runs)."""
        self._vs = list(vs)
        self._is = list(currents)
        self._breakdown_v = breakdown_v
        self._refresh_plot()

    # ── Plot ──────────────────────────────────────────────────────────────────

    def _reset_plot(self):
        self._vs.clear()
        self._is.clear()
        self._breakdown_v = math.nan
        self.result_lbl.setText("")
        self.readout_lbl.setText("–")
        self._refresh_plot()

    def _refresh_plot(self):
        log = self.log_chk.isChecked()
        vs  = np.asarray(self._vs, float)
        ys  = np.abs(np.asarray(self._is, float)) if log else np.asarray(self._is, float)

        self.ax.set_yscale("log" if log else "linear")
        self.ax.set_ylabel("|I| (A)" if log else "Current (A)")
        self._line.set_data(vs, ys)

        if math.isfinite(self._breakdown_v) and len(ys):
            self._bd_marker.set_data([self._breakdown_v], [ys[-1]])
        else:
            self._bd_marker.set_data([], [])

        if len(vs):
            self.ax.relim()
            self.ax.autoscale_view()
        self.canvas.draw_idle()

    # ── Save ──────────────────────────────────────────────────────────────────

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder.text())
        if path:
            self.folder.setText(path)

    @staticmethod
    def _unique_name(folder: str, base: str) -> str:
        """Return a base name that does not collide with any existing file."""
        used = {os.path.splitext(f)[0] for f in os.listdir(folder)} if os.path.isdir(folder) else set()
        if base not in used:
            return base
        i = 1
        while f"{base}_{i}" in used:
            i += 1
        return f"{base}_{i}"

    def set_filename(self, name: str):
        self.filename.setText(name.strip())

    def save_data(self, filename: str | None = None) -> str:
        """Save the current sweep as .txt + .png. Returns the .txt path.

        Always performed on the GUI thread — it writes to the widgets and
        renders the live figure, neither of which is safe from the agent thread.
        """
        return self._run_on_gui(lambda: self._save_data_gui(filename))

    def _save_data_gui(self, filename: str | None = None) -> str:
        if not self._vs:
            raise RuntimeError("No sweep data to save — run a sweep first.")

        folder = self.folder.text().strip()
        if filename:
            self.filename.setText(filename.strip())
        base = self.filename.text().strip() or "gate_sweep"

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"Could not create folder: {e}")

        safe_base = self._unique_name(folder, base)
        if safe_base != base:
            self.filename.setText(safe_base)

        png_path = os.path.join(folder, f"{safe_base}.png")
        txt_path = os.path.join(folder, f"{safe_base}.txt")

        self.fig.savefig(png_path, dpi=200, bbox_inches="tight")

        arr = np.column_stack([np.asarray(self._vs, float), np.asarray(self._is, float)])
        breakdown = (f"{self._breakdown_v:g}" if math.isfinite(self._breakdown_v)
                     else "none detected")
        header = "\n".join([
            "Gate breakdown sweep",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Start V: {self.v_start.text()}   End V: {self.v_end.text()}   "
            f"Step V: {self.v_step.text()}",
            f"Dwell (ms): {self.dwell_ms.text()}   Compliance (A): {self.compliance_entry.text()}   "
            f"Abort |I| (A): {self.i_abort.text()}",
            f"Ramp-down rate (V/s): {self.ramp_down_rate.text()}",
            f"Breakdown voltage (V): {breakdown}",
            f"Plot: {png_path}",
            "",
            "V(V)\tI(A)",
        ])
        np.savetxt(txt_path, arr, delimiter="\t", fmt="%.10g", header=header, comments="")

        self.controller.log(f"Gate sweep saved: {txt_path}")
        return txt_path

    def _save(self):
        try:
            txt_path = self.save_data()
        except RuntimeError as e:
            QMessageBox.information(self, "Nothing to save", str(e))
            return
        QMessageBox.information(self, "Saved", f"Saved:\n{txt_path}")

    # ── Agent API ─────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        setpoint = self.k.get_voltage_setpoint() if self.k is not None else None
        output   = self.k.is_output_on() if self.k is not None else None
        limits   = self.k.get_voltage_limits() if self.k is not None else (None, None)
        return {
            "keithley_connected":  self.k is not None,
            "output_on":           output,
            "gate_voltage_now":    setpoint,
            "compliance_a":        self.compliance_entry.text(),
            "set_gate_v":          self.set_v_entry.text(),
            "v_start":             self.v_start.text(),
            "v_end":               self.v_end.text(),
            "v_step":              self.v_step.text(),
            "dwell_ms":            self.dwell_ms.text(),
            "abort_current_a":     self.i_abort.text(),
            "ramp_down_v_per_s":   self.ramp_down_rate.text(),
            "log_scale":           self.log_chk.isChecked(),
            "folder":              self.folder.text(),
            "filename":            self.filename.text(),
            "sweep_running":       bool(self._worker and self._worker.isRunning()),
            "last_breakdown_v":    None if not math.isfinite(self._breakdown_v)
                                   else self._breakdown_v,
            "n_points_collected":  len(self._vs),
            "voltage_soft_limit":  limits[0],
            "voltage_hard_limit":  limits[1],
        }

    def apply_settings(self, settings: dict) -> list[str]:
        warnings: list[str] = []

        def _set_text(widget, key):
            if key in settings:
                widget.setText(str(settings[key]))

        _set_text(self.v_start,          "v_start")
        _set_text(self.v_end,            "v_end")
        _set_text(self.v_step,           "v_step")
        _set_text(self.dwell_ms,         "dwell_ms")
        _set_text(self.i_abort,          "abort_current_a")
        _set_text(self.ramp_down_rate,   "ramp_down_v_per_s")
        _set_text(self.compliance_entry, "compliance_a")
        _set_text(self.set_v_entry,      "set_gate_v")
        _set_text(self.folder,           "folder")
        _set_text(self.filename,         "filename")

        if "log_scale" in settings:
            self.log_chk.setChecked(bool(settings["log_scale"]))

        if "compliance_a" in settings:
            if self.k is None:
                warnings.append("Keithley not available — compliance not applied")
            else:
                try:
                    self.k.set_current_compliance(float(settings["compliance_a"]))
                except Exception as e:
                    warnings.append(f"compliance_a: {e}")

        if "output_on" in settings:
            if self.k is None:
                warnings.append("Keithley not available — output unchanged")
            else:
                (self.k.output_on if bool(settings["output_on"]) else self.k.output_off)()

        self.controller.scan_signals.activate_tab.emit(TAB_NAME)
        return warnings

    def set_gate_voltage_blocking(self, volts: float) -> dict:
        """Ramp the gate to a voltage and block until it is there (agent use)."""
        if self.k is None:
            return {"error": "Keithley 2400 is not connected"}
        if self._worker and self._worker.isRunning():
            return {"error": "A sweep is already running"}
        step  = self.k.get_ramp_step_v()
        dwell = self.k.get_ramp_dwell_s()

        self.controller.scan_signals.activate_tab.emit(TAB_NAME)
        self._stop_flag[0] = False
        try:
            self.k.ramp_voltage(float(volts), step_V=step, dwell_s=dwell,
                                abort_flag=lambda: self._stop_flag[0])
        except Exception as e:
            return {"error": f"Ramp failed: {e}"}
        if self._stop_flag[0]:
            return {"error": "Ramp stopped by user"}

        self.state.settings["gate_voltage"] = float(volts)
        self.controller.log(f"Agent ramped gate to {float(volts):g} V")
        return {"status": "ok", "gate_voltage": float(volts)}

    def run_sweep_blocking(self) -> dict:
        """Run the sweep with live GUI plotting and block until it finishes."""
        if self.k is None:
            return {"error": "Keithley 2400 is not connected"}
        if self._worker and self._worker.isRunning():
            return {"error": "A sweep is already running"}
        try:
            p = self._parse_params()
        except ValueError as e:
            return {"error": f"Invalid sweep parameter: {e}"}

        self.controller.scan_signals.activate_tab.emit(TAB_NAME)
        self.k.set_current_compliance(p["compliance"])
        self._finalize_sig.emit([], [], math.nan)      # clear the plot on the GUI thread
        self._stop_flag[0] = False
        self.controller.log(
            f"Agent gate sweep: {p['v_start']:g} → {p['v_end']:g} V, "
            f"step {p['v_step']:g} V, abort |I| ≥ {p['i_abort']:.3e} A")

        worker = self._make_worker(p)
        worker.point.connect(self._on_point,           Qt.ConnectionType.QueuedConnection)
        worker.status.connect(self.status_lbl.setText, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_finished,     Qt.ConnectionType.QueuedConnection)

        result: dict = {}
        worker.finished.connect(
            lambda aborted, msg, bd: result.update(
                aborted=aborted, message=msg, breakdown_v=bd),
            Qt.ConnectionType.DirectConnection)

        self._worker = worker
        worker.start()
        worker.wait()

        vs, currents = list(worker.vs), list(worker.currents)
        breakdown_v  = worker.breakdown_v
        # Hand the authoritative data to the GUI; queued, so it lands after any
        # point updates still sitting in the event queue.
        self._finalize_sig.emit(vs, currents, breakdown_v)

        if result.get("aborted"):
            return {"error": result.get("message", "Sweep aborted")}
        if not vs:
            return {"error": "Sweep produced no data"}

        i_abs = np.abs(np.asarray(currents, float))
        stats = {
            "n_points":          len(vs),
            "v_first":           round(vs[0], 6),
            "v_last":            round(vs[-1], 6),
            "breakdown_v":       None if not math.isfinite(breakdown_v) else round(breakdown_v, 6),
            "max_abs_current_a": float(f"{i_abs.max():.3e}"),
            "final_current_a":   float(f"{currents[-1]:.3e}"),
            "message":           result.get("message", ""),
        }
        png_b64 = self._run_on_gui(
            lambda: _render_iv_png(vs, currents, breakdown_v, self.log_chk.isChecked()))
        return {"stats": stats, "png_b64": png_b64}

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._stop_flag[0] = True
        for w in (self._worker, self._ramp_worker):
            if w and w.isRunning():
                w.wait(5000)
        event.accept()
