# tabs/temperature_control_tab.py
"""Lake Shore 331 control & live temperature plot.

Ported from Master program v1, adapted to the v2 shared-controller convention:
instruments come from the controller, actions are logged through it, and the
setpoint box stays in sync when a scan axis or the agent changes it.
"""
from __future__ import annotations
import time
from collections import deque
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QLabel,
    QDoubleSpinBox, QPushButton, QComboBox, QCheckBox, QMessageBox,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


HEATER_RANGE_MAP_INT2STR = {0: "Off", 1: "Low (0.5 W)", 2: "Medium (5 W)", 3: "High (50 W)"}
HEATER_RANGE_MAP_STR2INT = {v: k for k, v in HEATER_RANGE_MAP_INT2STR.items()}


# ── Worker ────────────────────────────────────────────────────────────────────

class TempPoller(QThread):
    """Polls the 331 for temperature (and heater output) off the GUI thread."""
    new_sample = pyqtSignal(float, float, object)   # (t_sec, temp_K, heater_pct|None)
    error      = pyqtSignal(str)

    def __init__(self, ls_driver, input_units: str = "K", interval_s: float = 0.5,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.ls         = ls_driver
        self.units      = input_units
        self.interval_s = max(0.05, float(interval_s))
        self._running   = False

    def set_interval(self, interval_s: float):
        self.interval_s = max(0.05, float(interval_s))

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        t0 = time.perf_counter()
        while self._running:
            try:
                temp = float(self.ls.read_temperature(self.units))
                heater_pct = None
                if hasattr(self.ls, "read_heater_output_percent"):
                    try:
                        heater_pct = float(self.ls.read_heater_output_percent())
                    except Exception:
                        heater_pct = None
                self.new_sample.emit(time.perf_counter() - t0, temp, heater_pct)
            except Exception as e:
                self.error.emit(f"Read failed: {e}")
            time.sleep(self.interval_s)


# ── Tab ───────────────────────────────────────────────────────────────────────

class TemperatureControlTab(QWidget):
    def __init__(self, controller, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.controller = controller
        self.state      = controller.state
        self.LS         = controller.get_instrument("Lakeshore331")

        sigs = controller.scan_signals
        sigs.temperature_setpoint_set.connect(
            self._on_setpoint_set, Qt.ConnectionType.QueuedConnection)
        sigs.stop_all.connect(self._on_stop_all, Qt.ConnectionType.QueuedConnection)

        main = QHBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(12)

        # ── Controller settings ───────────────────────────────────────────────
        controls_box = QGroupBox("Temperature Controller")
        cl = QGridLayout(controls_box)
        cl.setVerticalSpacing(8)
        cl.setHorizontalSpacing(8)
        row = 0

        cl.addWidget(QLabel("Setpoint (K)"), row, 0)
        self.sp_setpoint = QDoubleSpinBox()
        self.sp_setpoint.setDecimals(3)
        self.sp_setpoint.setRange(0.0, 400.0)
        self.sp_setpoint.setSingleStep(0.1)
        self.sp_setpoint.setValue(float(self.state.settings.get("temp_setpoint", 300.0)))
        cl.addWidget(self.sp_setpoint, row, 1)
        self.btn_apply_setpoint = QPushButton("Apply")
        cl.addWidget(self.btn_apply_setpoint, row, 2)
        row += 1

        cl.addWidget(QLabel("P"), row, 0)
        self.sp_p = QDoubleSpinBox()
        self.sp_p.setRange(0.1, 1000.0); self.sp_p.setDecimals(2); self.sp_p.setSingleStep(0.1)
        cl.addWidget(self.sp_p, row, 1)
        self.btn_apply_pid = QPushButton("Apply PID")
        cl.addWidget(self.btn_apply_pid, row, 2)
        row += 1

        cl.addWidget(QLabel("I"), row, 0)
        self.sp_i = QDoubleSpinBox()
        self.sp_i.setRange(0.1, 1000.0); self.sp_i.setDecimals(2); self.sp_i.setSingleStep(0.1)
        cl.addWidget(self.sp_i, row, 1)
        row += 1

        cl.addWidget(QLabel("D"), row, 0)
        self.sp_d = QDoubleSpinBox()
        self.sp_d.setRange(0.0, 200.0); self.sp_d.setDecimals(2); self.sp_d.setSingleStep(0.1)
        cl.addWidget(self.sp_d, row, 1)
        row += 1

        self.chk_heater_enable = QCheckBox("Heater Enabled")
        cl.addWidget(self.chk_heater_enable, row, 0, 1, 2)
        row += 1

        cl.addWidget(QLabel("Heater Range"), row, 0)
        self.cb_range = QComboBox()
        for code in sorted(HEATER_RANGE_MAP_INT2STR):
            self.cb_range.addItem(HEATER_RANGE_MAP_INT2STR[code])
        cl.addWidget(self.cb_range, row, 1, 1, 2)
        row += 1

        self.btn_apply_heater = QPushButton("Apply Heater")
        cl.addWidget(self.btn_apply_heater, row, 0, 1, 3)
        row += 1

        # ── Scan-axis settle settings (persisted) ─────────────────────────────
        settle_box = QGroupBox("Scan Axis Settling")
        sl = QGridLayout(settle_box)
        settle_box.setToolTip(
            "Used when Temperature (K) is a scan axis: after each setpoint the "
            "scan waits until the reading holds inside the tolerance band."
        )

        sl.addWidget(QLabel("Tolerance (K)"), 0, 0)
        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setRange(0.001, 100.0); self.sp_tol.setDecimals(3); self.sp_tol.setSingleStep(0.1)
        self.sp_tol.setValue(float(self.state.settings.get("temp_tolerance_k", 0.5)))
        sl.addWidget(self.sp_tol, 0, 1)

        sl.addWidget(QLabel("Hold time (s)"), 1, 0)
        self.sp_settle = QDoubleSpinBox()
        self.sp_settle.setRange(0.0, 3600.0); self.sp_settle.setDecimals(1); self.sp_settle.setSingleStep(1.0)
        self.sp_settle.setValue(float(self.state.settings.get("temp_settle_s", 10.0)))
        sl.addWidget(self.sp_settle, 1, 1)

        sl.addWidget(QLabel("Timeout (s)"), 2, 0)
        self.sp_timeout = QDoubleSpinBox()
        self.sp_timeout.setRange(1.0, 86400.0); self.sp_timeout.setDecimals(0); self.sp_timeout.setSingleStep(30.0)
        self.sp_timeout.setValue(float(self.state.settings.get("temp_timeout_s", 900.0)))
        sl.addWidget(self.sp_timeout, 2, 1)

        # ── Plot settings ─────────────────────────────────────────────────────
        plot_ctrl_box = QGroupBox("Plot Settings")
        pl = QGridLayout(plot_ctrl_box)

        pl.addWidget(QLabel("Plot Window (s)"), 0, 0)
        self.sp_window = QDoubleSpinBox()
        self.sp_window.setRange(5.0, 36000.0)
        self.sp_window.setSingleStep(5.0)
        self.sp_window.setValue(600.0)
        pl.addWidget(self.sp_window, 0, 1)

        pl.addWidget(QLabel("Time Between Points (s)"), 1, 0)
        self.sp_interval = QDoubleSpinBox()
        self.sp_interval.setRange(0.05, 60.0)
        self.sp_interval.setSingleStep(0.05)
        self.sp_interval.setValue(0.5)
        pl.addWidget(self.sp_interval, 1, 1)

        self.btn_start = QPushButton("Start")
        self.btn_stop  = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        pl.addLayout(btn_row, 2, 0, 1, 2)

        self.status_lbl = QLabel("idle")
        self.status_lbl.setStyleSheet(
            "QLabel { background:#e8f4fd; color:#1a5276; padding:3px 6px; border-radius:3px; }")

        left_col = QVBoxLayout()
        left_col.addWidget(controls_box)
        left_col.addWidget(settle_box)
        left_col.addWidget(plot_ctrl_box)
        left_col.addWidget(self.status_lbl)
        left_col.addStretch(1)

        left_wrap = QWidget()
        left_wrap.setLayout(left_col)
        main.addWidget(left_wrap, stretch=0)

        # ── Plot ──────────────────────────────────────────────────────────────
        self.fig    = Figure(figsize=(5, 3), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax     = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Temperature (K)")
        self.ax.grid(True, alpha=0.3)
        main.addWidget(self.canvas, stretch=1)

        self.xbuf, self.ybuf, self.hbuf = deque(), deque(), deque()

        (self.line,) = self.ax.plot([], [], lw=1.5, label="Temperature (K)")
        self.ax2 = self.ax.twinx()
        self.ax2.set_ylabel("Heater Output (%)")
        (self.hline,) = self.ax2.plot([], [], ls="--", lw=1.2, alpha=0.9, label="Heater (%)")
        self.ax2.set_ylim(0, 100)
        self.ax.legend([self.line, self.hline],
                       [l.get_label() for l in (self.line, self.hline)], loc="upper left")

        self.poller: Optional[TempPoller] = None
        self._error_shown = False

        # ── Signals ───────────────────────────────────────────────────────────
        self.btn_apply_setpoint.clicked.connect(self._apply_setpoint)
        self.btn_apply_pid.clicked.connect(self._apply_pid)
        self.chk_heater_enable.toggled.connect(self._heater_enable_toggled)
        self.btn_apply_heater.clicked.connect(self._apply_heater)
        self.cb_range.currentTextChanged.connect(self._range_changed_ui)
        self.btn_start.clicked.connect(self._start_polling)
        self.btn_stop.clicked.connect(self._stop_polling)
        self.sp_interval.valueChanged.connect(self._interval_changed)

        for spin, key in ((self.sp_tol, "temp_tolerance_k"),
                          (self.sp_settle, "temp_settle_s"),
                          (self.sp_timeout, "temp_timeout_s")):
            spin.valueChanged.connect(
                lambda v, k=key: self.state.settings.__setitem__(k, float(v)))

        if self.LS is None:
            self._set_status("Lakeshore331 not connected — controls disabled.")
            controls_box.setEnabled(False)
            plot_ctrl_box.setEnabled(False)
        else:
            self._init_from_instrument()

    # ── Status ────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str):
        self.status_lbl.setText(msg)

    # ── Initial state from the instrument ─────────────────────────────────────

    def _init_from_instrument(self):
        try:
            self.sp_setpoint.setValue(float(self.LS.get_setpoint()))
        except Exception:
            pass
        try:
            p, i, d = self.LS.get_pid()
            self.sp_p.setValue(float(p))
            self.sp_i.setValue(float(i))
            self.sp_d.setValue(float(d))
        except Exception:
            pass
        try:
            code, name = self.LS.get_heater_range()
            idx = self.cb_range.findText(HEATER_RANGE_MAP_INT2STR.get(code, name))
            if idx >= 0:
                self.cb_range.setCurrentIndex(idx)
            self.chk_heater_enable.blockSignals(True)
            self.chk_heater_enable.setChecked(code != 0)
            self.chk_heater_enable.blockSignals(False)
        except Exception:
            pass

    # ── Poller lifecycle ──────────────────────────────────────────────────────

    def _start_polling(self):
        if self.LS is None or (self.poller and self.poller.isRunning()):
            return
        self.xbuf.clear(); self.ybuf.clear(); self.hbuf.clear()
        self.line.set_data([], [])
        self.hline.set_data([], [])
        self.ax.set_xlim(0, 1)
        self.canvas.draw_idle()
        self._error_shown = False

        self.poller = TempPoller(self.LS, "K", self.sp_interval.value())
        self.poller.new_sample.connect(self._handle_sample)
        self.poller.error.connect(self._handle_error)
        self.poller.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_status("logging…")
        self.controller.log("Temperature logging started")

    def _stop_polling(self):
        if self.poller:
            self.poller.stop()
            self.poller.wait(2000)
            self.poller = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_status("idle")

    def _on_stop_all(self):
        self._stop_polling()

    def _interval_changed(self, _val: float):
        if self.poller:
            self.poller.set_interval(self.sp_interval.value())

    # ── Sampling & plotting ───────────────────────────────────────────────────

    def _handle_sample(self, t: float, temp: float, heater_pct):
        self.xbuf.append(t)
        self.ybuf.append(temp)
        self.hbuf.append(None if heater_pct is None else float(heater_pct))

        t_cut = t - float(self.sp_window.value())
        while self.xbuf and self.xbuf[0] < t_cut:
            self.xbuf.popleft(); self.ybuf.popleft(); self.hbuf.popleft()

        self._set_status(f"T = {temp:.3f} K")
        self._redraw()

    def _redraw(self):
        self.line.set_data(self.xbuf, self.ybuf)

        hx = [x for x, h in zip(self.xbuf, self.hbuf) if h is not None]
        hy = [h for h in self.hbuf if h is not None]
        self.hline.set_data(hx, hy)

        if self.xbuf:
            xmin = max(0.0, self.xbuf[-1] - float(self.sp_window.value()))
            xmax = self.xbuf[-1] if self.xbuf[-1] > xmin else xmin + 1.0
            self.ax.set_xlim(xmin, xmax)

            if self.ybuf:
                ymin, ymax = min(self.ybuf), max(self.ybuf)
                pad = max(1e-6, 0.05 * (ymax - ymin if ymax > ymin else 1.0))
                self.ax.set_ylim(ymin - pad, ymax + pad)

            if hy:
                hmin, hmax = max(0.0, min(hy)), min(100.0, max(hy))
                if hmax - hmin < 5:
                    hmax = min(100.0, hmin + 5)
                self.ax2.set_ylim(hmin - 2, hmax + 2)

        self.canvas.draw_idle()

    def _handle_error(self, msg: str):
        # The poller can fail every cycle; warn once rather than stacking modals.
        self._set_status(msg)
        if not self._error_shown:
            self._error_shown = True
            self.controller.log(f"Lakeshore read error: {msg}")
            QMessageBox.warning(self, "LakeShore Read Error", msg)

    # ── Apply actions ─────────────────────────────────────────────────────────

    def _apply_setpoint(self):
        try:
            val = self.sp_setpoint.value()
            self.LS.set_setpoint(val)
            self.state.settings["temp_setpoint"] = float(val)
            self.controller.log(f"Temperature setpoint set to {val:.3f} K")
            self._set_status(f"setpoint → {val:.3f} K")
        except Exception as e:
            QMessageBox.critical(self, "Setpoint Error", str(e))

    def _apply_pid(self):
        try:
            self.LS.set_pid(self.sp_p.value(), self.sp_i.value(), self.sp_d.value())
            self.controller.log(
                f"PID set to P={self.sp_p.value()} I={self.sp_i.value()} D={self.sp_d.value()}")
        except Exception as e:
            QMessageBox.critical(self, "PID Error", str(e))

    def _heater_enable_toggled(self, enabled: bool):
        try:
            if enabled:
                if self.cb_range.currentText().startswith("Off"):
                    self.cb_range.setCurrentText("Low (0.5 W)")
                self._apply_heater()
                if hasattr(self.LS, "engage_closed_loop"):
                    self.LS.engage_closed_loop(desired_mode=1)
                elif hasattr(self.LS, "set_control_mode"):
                    self.LS.set_control_mode(1)
                self.controller.log("Heater enabled")
            else:
                self.LS.set_heater_range(0)
                self.controller.log("Heater disabled")
        except Exception as e:
            QMessageBox.critical(self, "Heater Error", str(e))

    def _range_changed_ui(self, _txt: str):
        code = HEATER_RANGE_MAP_STR2INT.get(self.cb_range.currentText(), 0)
        self.chk_heater_enable.blockSignals(True)
        self.chk_heater_enable.setChecked(code != 0)
        self.chk_heater_enable.blockSignals(False)

    def _apply_heater(self):
        try:
            code = HEATER_RANGE_MAP_STR2INT.get(self.cb_range.currentText(), 0)
            self.LS.set_heater_range(code)
            self.chk_heater_enable.blockSignals(True)
            self.chk_heater_enable.setChecked(code != 0)
            self.chk_heater_enable.blockSignals(False)
            self.controller.log(f"Heater range set to {HEATER_RANGE_MAP_INT2STR.get(code)}")
        except Exception as e:
            QMessageBox.critical(self, "Heater Error", str(e))

    # ── Agent / scan-axis sync ────────────────────────────────────────────────

    def _on_setpoint_set(self, value: float):
        """A scan axis or the agent moved the setpoint — reflect it in the box."""
        self.sp_setpoint.blockSignals(True)
        self.sp_setpoint.setValue(float(value))
        self.sp_setpoint.blockSignals(False)
        self._set_status(f"setpoint → {float(value):.3f} K")

    def get_settings(self) -> dict:
        return {
            "setpoint_K":   self.sp_setpoint.value(),
            "P":            self.sp_p.value(),
            "I":            self.sp_i.value(),
            "D":            self.sp_d.value(),
            "heater_range": self.cb_range.currentText(),
            "tolerance_K":  self.sp_tol.value(),
            "settle_s":     self.sp_settle.value(),
            "timeout_s":    self.sp_timeout.value(),
            "logging":      bool(self.poller and self.poller.isRunning()),
        }

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        try:
            self._stop_polling()
        except Exception:
            pass
        super().closeEvent(event)
