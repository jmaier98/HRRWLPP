from __future__ import annotations
import time
from collections import deque
from typing import Optional, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QLabel,
    QDoubleSpinBox, QSpinBox, QPushButton, QComboBox, QCheckBox, QMessageBox
)

# Matplotlib Qt canvas
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


HEATER_RANGE_MAP_INT2STR = {0: "Off", 1: "Low (0.5 W)", 2: "Medium (5 W)", 3: "High (50 W)"}
HEATER_RANGE_MAP_STR2INT = {"Off": 0, "Low (0.5 W)": 1, "Medium (5 W)": 2, "High (50 W)": 3}


# ---------------------------
# Worker that polls the 331 in a thread
# ---------------------------
class TempPoller(QThread):
    new_sample = pyqtSignal(float, float, object)  # (t_sec, temp)
    status_msg = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, ls_driver, input_units: str = "K", interval_s: float = 0.5, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.ls = ls_driver
        self.units = input_units  # 'K', 'C', 'S', 'L'
        self.interval_s = max(0.05, float(interval_s))
        self._running = False

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
                # Try to read heater output if available
                if hasattr(self.ls, "read_heater_output_percent"):
                    try:
                        heater_pct = float(self.ls.read_heater_output_percent())
                    except Exception:
                        heater_pct = None
                t = time.perf_counter() - t0
                self.new_sample.emit(t, temp, heater_pct)
            except Exception as e:
                self.error.emit(f"Read failed: {e}")
                # brief backoff to avoid tight error loop
                time.sleep(min(self.interval_s, 0.5))
            finally:
                time.sleep(self.interval_s)


# ---------------------------
# The Tab widget
# ---------------------------
class TemperatureControlTab(QWidget):
    """
    Drop-in tab to control & plot temperature from a LakeShore 331 via your Instrument Manager.

    Constructor:
        TemperatureControlTab(instrument_manager, state)

    Assumptions:
        - IM provides a LakeShore-331-like driver with methods:
            read_temperature(units='K'|'C'|'S'|'L') -> float
            set_setpoint(value: float) / set_temp(value)
            get_setpoint() -> float
            set_pid(p, i, d) / set_pid_vals(p,i,d)
            get_pid() -> (p,i,d)
            set_heater_range(code_or_name)
            get_heater_range() -> (code:int, name:str)
            read_heater_output_percent() -> float    # optional
        - If your method names differ slightly, adjust in _call_* helpers below.
    """

    def __init__(self, instrument_manager, state, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.IM = instrument_manager
        self.state = state

        # Find the LakeShore in the Instrument Manager
        self.LS = self._resolve_lakeshore()

        # --- UI ---
        main = QHBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(12)

        # Left controls
        controls_box = QGroupBox("Temperature Controller")
        controls_layout = QGridLayout()
        controls_layout.setVerticalSpacing(8)
        controls_layout.setHorizontalSpacing(8)
        controls_box.setLayout(controls_layout)
        row = 0

        # Setpoint
        controls_layout.addWidget(QLabel("Setpoint (K)"), row, 0)
        self.sp_setpoint = QDoubleSpinBox()
        self.sp_setpoint.setDecimals(3)
        self.sp_setpoint.setRange(-1000.0, 1000.0)
        self.sp_setpoint.setSingleStep(0.1)
        controls_layout.addWidget(self.sp_setpoint, row, 1)
        self.btn_apply_setpoint = QPushButton("Apply")
        controls_layout.addWidget(self.btn_apply_setpoint, row, 2)
        row += 1

        # PID
        controls_layout.addWidget(QLabel("P"), row, 0)
        self.sp_p = QDoubleSpinBox(); self.sp_p.setRange(0.1, 1000.0); self.sp_p.setDecimals(2); self.sp_p.setSingleStep(0.1)
        controls_layout.addWidget(self.sp_p, row, 1)
        self.btn_apply_pid = QPushButton("Apply PID")
        controls_layout.addWidget(self.btn_apply_pid, row, 2)
        row += 1

        controls_layout.addWidget(QLabel("I"), row, 0)
        self.sp_i = QDoubleSpinBox(); self.sp_i.setRange(0.1, 1000.0); self.sp_i.setDecimals(2); self.sp_i.setSingleStep(0.1)
        controls_layout.addWidget(self.sp_i, row, 1)
        row += 1

        controls_layout.addWidget(QLabel("D"), row, 0)
        self.sp_d = QDoubleSpinBox(); self.sp_d.setRange(0.0, 200.0); self.sp_d.setDecimals(2); self.sp_d.setSingleStep(0.1)
        controls_layout.addWidget(self.sp_d, row, 1)
        row += 1

        # Heater enable/disable and range
        self.chk_heater_enable = QCheckBox("Heater Enabled")
        controls_layout.addWidget(self.chk_heater_enable, row, 0, 1, 2)
        row += 1

        controls_layout.addWidget(QLabel("Heater Range"), row, 0)
        self.cb_range = QComboBox()
        for name in ("Off", "Low (0.5 W)", "Medium (5 W)", "High (50 W)"):
            self.cb_range.addItem(name)
        controls_layout.addWidget(self.cb_range, row, 1, 1, 2)
        row += 1

        self.btn_apply_heater = QPushButton("Apply Heater")
        controls_layout.addWidget(self.btn_apply_heater, row, 0, 1, 3)
        row += 1

        # Plot controls: window + interval
        plot_ctrl_box = QGroupBox("Plot Settings")
        plot_ctrl_layout = QGridLayout()
        plot_ctrl_box.setLayout(plot_ctrl_layout)

        plot_ctrl_layout.addWidget(QLabel("Plot Window (s)"), 0, 0)
        self.sp_window = QDoubleSpinBox()
        self.sp_window.setRange(5.0, 36000.0)
        self.sp_window.setSingleStep(5.0)
        self.sp_window.setValue(600.0)  # default 10 min
        plot_ctrl_layout.addWidget(self.sp_window, 0, 1)

        plot_ctrl_layout.addWidget(QLabel("Time Between Points (s)"), 1, 0)
        self.sp_interval = QDoubleSpinBox()
        self.sp_interval.setRange(0.05, 60.0)
        self.sp_interval.setSingleStep(0.05)
        self.sp_interval.setValue(0.5)
        plot_ctrl_layout.addWidget(self.sp_interval, 1, 1)

        # Start/Stop buttons
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        plot_ctrl_layout.addLayout(btn_row, 2, 0, 1, 2)

        # Stack left side
        left_col = QVBoxLayout()
        left_col.addWidget(controls_box)
        left_col.addWidget(plot_ctrl_box)
        left_col.addStretch(1)

        left_wrap = QWidget()
        left_wrap.setLayout(left_col)
        main.addWidget(left_wrap, stretch=0)

        # Right plot
        self.fig = Figure(figsize=(5, 3), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Temperature (K)")
        self.ax.grid(True, alpha=0.3)

        main.addWidget(self.canvas, stretch=1)

        # Data buffer
        self.xbuf = deque()
        self.ybuf = deque()
        self.hbuf = deque() 

        # Plot line
        (self.line,) = self.ax.plot([], [], lw=1.5)

        # NEW: second y-axis for heater output (%)
        self.ax2 = self.ax.twinx()
        self.ax2.set_ylabel("Heater Output (%)")
        (self.hline,) = self.ax2.plot([], [], ls="--", lw=1.2, alpha=0.9, label="Heater (%)")

        # Optional: start with 0–100% bounds for clarity
        self.ax2.set_ylim(0, 100)

        # NEW: combined legend (handles both axes)
        lines = [self.line, self.hline]
        labels = [l.get_label() for l in lines]
        self.ax.legend(lines, labels, loc="upper left")

        # Poller thread
        self.poller: Optional[TempPoller] = None
        # Hook up signals
        self.btn_apply_setpoint.clicked.connect(self._apply_setpoint)
        self.btn_apply_pid.clicked.connect(self._apply_pid)
        self.chk_heater_enable.toggled.connect(self._heater_enable_toggled)
        self.btn_apply_heater.clicked.connect(self._apply_heater)
        self.cb_range.currentTextChanged.connect(self._range_changed_ui)

        self.btn_start.clicked.connect(self._start_polling)
        self.btn_stop.clicked.connect(self._stop_polling)
        self.sp_interval.valueChanged.connect(self._interval_changed)

        # Initialize from instrument
        self._init_from_instrument()

    # -------- Instrument Manager resolution --------
    def _resolve_lakeshore(self):
        for key in ("LS331", "Lakeshore331", "TempController"):
            try:
                inst = self.IM.get(key)
                if inst is not None:
                    return inst
            except Exception:
                pass
        raise RuntimeError(
            "Could not find LakeShore 331 in Instrument Manager. "
            "Tried keys: 'LS331', 'LakeShore331', 'TempController'."
        )

    # -------- UI initial state from instrument --------
    def _init_from_instrument(self):
        # Setpoint
        try:
            sp = self._call_get_setpoint()
            self.sp_setpoint.setValue(float(sp))
        except Exception:
            pass

        # PID
        try:
            p, i, d = self._call_get_pid()
            self.sp_p.setValue(float(p))
            self.sp_i.setValue(float(i))
            self.sp_d.setValue(float(d))
        except Exception:
            pass

        # Heater range
        try:
            code, name = self._call_get_heater_range()
            nice = HEATER_RANGE_MAP_INT2STR.get(code, name)
            idx = self.cb_range.findText(nice)
            if idx >= 0:
                self.cb_range.setCurrentIndex(idx)
            self.chk_heater_enable.setChecked(code != 0)
        except Exception:
            pass

    # -------- Worker lifecycle --------
    def _start_polling(self):
        if self.poller and self.poller.isRunning():
            return
        self.xbuf.clear()
        self.ybuf.clear()
        self.hbuf.clear()
        self.line.set_data([], [])
        self.hline.set_data([], [])
        self.ax.set_xlim(0, 1)
        self.canvas.draw_idle()

        self.poller = TempPoller(self.LS, input_units="K", interval_s=self.sp_interval.value())
        self.poller.new_sample.connect(self._handle_sample)
        self.poller.error.connect(self._handle_error)
        self.poller.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def _stop_polling(self):
        if self.poller:
            self.poller.stop()
            self.poller.wait(1500)
            self.poller = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _interval_changed(self, val: float):
        if self.poller:
            self.poller.set_interval(self.sp_interval.value())

    # -------- Sample & plotting --------
    def _handle_sample(self, t: float, temp: float, heater_pct):
        window = float(self.sp_window.value())
        self.xbuf.append(t)
        self.ybuf.append(temp)
        # Only append heater if present
        if heater_pct is not None:
            self.hbuf.append(float(heater_pct))
        else:
            # keep buffers aligned; append None as placeholder
            self.hbuf.append(None)

        # drop old points from all buffers
        t_cut = t - window
        while self.xbuf and self.xbuf[0] < t_cut:
            self.xbuf.popleft()
            self.ybuf.popleft()
            self.hbuf.popleft()

        self._redraw()

    def _redraw(self):
        # Temperature line
        self.line.set_data(self.xbuf, self.ybuf)

        # Heater line: strip out None placeholders for plotting
        hx = [x for x, h in zip(self.xbuf, self.hbuf) if h is not None]
        hy = [h for h in self.hbuf if h is not None]
        self.hline.set_data(hx, hy)

        if self.xbuf:
            # X window
            xmin = max(0.0, self.xbuf[-1] - float(self.sp_window.value()))
            xmax = self.xbuf[-1] if self.xbuf[-1] > xmin else xmin + 1.0
            self.ax.set_xlim(xmin, xmax)

            # Temp Y autoscale with light padding
            if self.ybuf:
                ymin = min(self.ybuf)
                ymax = max(self.ybuf)
                pad = max(1e-6, 0.05 * (ymax - ymin if ymax > ymin else 1.0))
                self.ax.set_ylim(ymin - pad, ymax + pad)

            # Heater Y: default 0–100 if we have any valid points; otherwise keep as-is
            if hy:
                hmin = max(0.0, min(hy))
                hmax = min(100.0, max(hy))
                # make sure there's visible range
                if hmax - hmin < 5:
                    hmax = min(100.0, hmin + 5)
                self.ax2.set_ylim(hmin - 2, hmax + 2)
            # If no hy, leave ax2 limits untouched (initialized to 0–100 above)

        self.canvas.draw_idle()

    def _handle_error(self, msg: str):
        # Avoid spamming modal dialogs; use a non-blocking toast-ish messagebox once
        QMessageBox.warning(self, "LakeShore Read Error", msg)

    # -------- Apply actions --------
    def _apply_setpoint(self):
        try:
            self._call_set_setpoint(self.sp_setpoint.value())
        except Exception as e:
            QMessageBox.critical(self, "Setpoint Error", str(e))

    def _apply_pid(self):
        try:
            self._call_set_pid(self.sp_p.value(), self.sp_i.value(), self.sp_d.value())
        except Exception as e:
            QMessageBox.critical(self, "PID Error", str(e))

    def _heater_enable_toggled(self, enabled: bool):
        try:
            if enabled:
                # Ensure nonzero range in UI
                if self.cb_range.currentText().startswith("Off"):
                    self.cb_range.setCurrentText("Low (0.5 W)")
                self._apply_heater()

                # NEW: put controller into a closed-loop mode so it actually drives output
                if hasattr(self.LS, "engage_closed_loop"):
                    self.LS.engage_closed_loop(desired_mode=1)  # Manual PID
                else:
                    # Fallback if using a simpler driver: try CMODE 1 explicitly
                    if hasattr(self.LS, "set_control_mode"):
                        self.LS.set_control_mode(1)
            else:
                self._call_set_heater_range(0)  # Off
        except Exception as e:
            QMessageBox.critical(self, "Heater Error", str(e))

    def _range_changed_ui(self, _txt: str):
        # Keep the checkbox in sync with explicit range changes
        code = HEATER_RANGE_MAP_STR2INT.get(self.cb_range.currentText(), 0)
        self.chk_heater_enable.blockSignals(True)
        self.chk_heater_enable.setChecked(code != 0)
        self.chk_heater_enable.blockSignals(False)

    def _apply_heater(self):
        try:
            code = HEATER_RANGE_MAP_STR2INT.get(self.cb_range.currentText(), 0)
            self._call_set_heater_range(code)
            self.chk_heater_enable.setChecked(code != 0)
        except Exception as e:
            QMessageBox.critical(self, "Heater Error", str(e))

    # -------- Instrument call helpers (tolerate naming differences) --------
    def _call_get_setpoint(self) -> float:
        if hasattr(self.LS, "get_setpoint"):
            return float(self.LS.get_setpoint())
        # fallback?
        raise AttributeError("LakeShore driver missing get_setpoint()")

    def _call_set_setpoint(self, val: float):
        if hasattr(self.LS, "set_setpoint"):
            return self.LS.set_setpoint(val)
        if hasattr(self.LS, "set_temp"):
            return self.LS.set_temp(val)
        raise AttributeError("LakeShore driver missing set_setpoint()/set_temp()")

    def _call_get_pid(self) -> Tuple[float, float, float]:
        if hasattr(self.LS, "get_pid"):
            return self.LS.get_pid()
        raise AttributeError("LakeShore driver missing get_pid()")

    def _call_set_pid(self, p: float, i: float, d: float):
        if hasattr(self.LS, "set_pid"):
            return self.LS.set_pid(p, i, d)
        if hasattr(self.LS, "set_pid_vals"):
            return self.LS.set_pid_vals(p, i, d)
        raise AttributeError("LakeShore driver missing set_pid()/set_pid_vals()")

    def _call_get_heater_range(self) -> Tuple[int, str]:
        if hasattr(self.LS, "get_heater_range"):
            return self.LS.get_heater_range()
        raise AttributeError("LakeShore driver missing get_heater_range()")

    def _call_set_heater_range(self, code_or_name):
        if hasattr(self.LS, "set_heater_range"):
            return self.LS.set_heater_range(code_or_name)
        if hasattr(self.LS, "set_heater"):
            return self.LS.set_heater(code_or_name)
        raise AttributeError("LakeShore driver missing set_heater_range()/set_heater()")

    # -------- Cleanup --------
    def closeEvent(self, event):
        # ensure thread stops if the tab is closed
        try:
            self._stop_polling()
        except Exception:
            pass
        super().closeEvent(event)
