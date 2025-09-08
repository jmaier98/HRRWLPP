# one_d_scan_tab.py
from __future__ import annotations
import os, time, math, numpy as np
from typing import Callable, Dict, Tuple, List
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QComboBox, QGroupBox
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import math
import numpy as np


# --------------------------- Worker ---------------------------------- #
class _Scan1DWorker(QThread):
    """
    Runs repeated 1D scans on a background thread.

    Produces per-point results:
      emits progress(scan_idx, ix, x_val, data_tuple_of_len_6)

    After each full scan is done:
      emits scan_done(scan_idx)
    """
    progress = pyqtSignal(int, int, float, tuple)
    scan_done = pyqtSignal(int)
    finished = pyqtSignal(bool, str)  # user_abort, message

    def __init__(
        self,
        x_values: np.ndarray,
        n_scans: int,
        wait_s: float,
        set_x_fn: Callable[[float], None],
        measure_fn: Callable[[], Tuple[float, float, float, float, float, float]],
        stop_flag_ref: List[bool],
    ):
        super().__init__()
        self._xs = x_values
        self._n_scans = max(1, int(n_scans))
        self._wait_s = max(0.0, float(wait_s))
        self._set_x = set_x_fn
        self._measure = measure_fn
        self._stop_flag_ref = stop_flag_ref

    def run(self):
        try:
            for scan_idx in range(1, self._n_scans + 1):
                # forward (you can add serpentine or reverse if desired)
                for ix, x in enumerate(self._xs):
                    if self._stop_flag_ref[0]:
                        self.finished.emit(True, "Scan stopped by user.")
                        return

                    # Move axis
                    try:
                        self._set_x(float(x))
                    except Exception as e:
                        self.finished.emit(True, f"Axis move error at x={x}: {e}")
                        return

                    # Wait to settle
                    if self._wait_s > 0:
                        time.sleep(self._wait_s)

                    # Measure (returns 6-tuple)
                    try:
                        d = self._measure()
                    except Exception as e:
                        self.finished.emit(True, f"Measurement error at x={x}: {e}")
                        return

                    # Normalize to 6 floats
                    if not isinstance(d, (list, tuple)):
                        d = (float(d),)
                    d6 = tuple([float(v) for v in (list(d) + [math.nan]*6)][:6])

                    self.progress.emit(scan_idx, ix, float(x), d6)

                # one full pass finished
                self.scan_done.emit(scan_idx)

            self.finished.emit(False, "All scans complete.")
        except Exception as e:
            self.finished.emit(True, f"Unexpected error: {e}")


# --------------------------- Main Tab -------------------------------- #
class OneDScanTab(QWidget):
    """
    General 1D scan tab with:
      - Axis selector (how we set X)
      - Measurement selector (how we read up to 6 channels)
      - Display selector (which of the 6 channels to plot)
      - Top plot: per-scan traces (overlaid)
      - Bottom plot: live running average over scans
      - Saves to 8 columns: [scan#, x, d1..d6]

    Expected instruments (defaults; edit AXIS_SETTERS and MEASUREMENT_FUNCS):
      - IM.get("Galvo").movexy(x, y)
      - IM.get("SR830").read_x(), read_y(), read_r(), read_theta()
    """
    def __init__(self, instrument_manager, state=None):
        super().__init__()
        self.IM = instrument_manager
        self.state = state

        # Try to fetch common instruments (optional; only needed by defaults)
        self.galvo = None
        self.lockin = None
        try:
            self.galvo = self.IM.get("Galvo")
        except Exception:
            pass
        try:
            self.lockin = self.IM.get("SR830")
            self.stage  = self.IM.get("ESP")
        except Exception:
            pass
        # --- Keithley 2400 safe-voltage axis (≤ 50 mV ramp sub-steps) ---
        try:
            k2400 = self.IM.get("Keithley2400")
        except Exception:
            k2400 = None
        # ---------- Registry: Add your axis/measurement entries here ---------- #
        # Each setter: fn(x: float) -> None
        self.AXIS_SETTERS: Dict[str, Callable[[float], None]] = {}

        
        self.AXIS_SETTERS["Newport Stage mm"] = lambda x: self.stage.move_and_wait(float(x))
        self.AXIS_SETTERS["Newport Stage ps"] = lambda x: self.stage.move_and_wait_ps(float(x))
        self.AXIS_SETTERS["Galvo X "] = lambda x: self.galvo.movexy(float(x), 0.5)
        self.AXIS_SETTERS["Keithley V "] = lambda x: k2400.ramp_voltage(float(x))
        

        # Placeholder example for a voltage source (uncomment and adapt):
        # vs = self.IM.get("VoltageSource")  # must exist
        # self.AXIS_SETTERS["Bias Voltage"] = lambda x: vs.set_voltage(float(x))

        # Each measurement: fn() -> tuple up to 6 floats
        self.MEASUREMENT_FUNCS: Dict[str, Callable[[], Tuple[float, ...]]] = {}
        def _safe_div(a: float, b: float, eps: float = 1e-12) -> float:
            """Dummy safe divide used to compute R; edit freely."""
            if not (math.isfinite(a) and math.isfinite(b)):
                return math.nan
            denom = b if abs(b) > eps else (eps if b >= 0 else -eps)
            return a / denom

        def _photocurrent_R_read():
            V = self.lockin.read_x2()
            I = self.lockin.read_x()
            R = _safe_div(V, I)
            return (R, I, V, math.nan, math.nan, math.nan)
        
        if self.lockin:
            # Provide a few ready-made options if SR830 is present
            self.MEASUREMENT_FUNCS["Lock-in X only"] = lambda: (self.lockin.read_x(),)
            self.MEASUREMENT_FUNCS["Lock-in (X, Y, R, θ)"] = lambda: (
                self.lockin.read_x(),
                self.lockin.read_y(),
                self.lockin.read_r(),
                self.lockin.read_theta(),
            )
            self.MEASUREMENT_FUNCS["R (calculated), I, V"] = _photocurrent_R_read

        self.MEASUREMENT_FUNCS["(dummy) all NaN"] = lambda: (math.nan,)*6

        self.AXIS_SETTERS["(dummy) no-op axis"] = lambda x: None

        # ----------------------------- UI ------------------------------------ #
        outer = QHBoxLayout(self)

        # Left controls
        ctrl_box = QVBoxLayout()
        form = QFormLayout()

        self.axis_combo = QComboBox()
        self.axis_combo.addItems(list(self.AXIS_SETTERS.keys()))

        self.meas_combo = QComboBox()
        self.meas_combo.addItems(list(self.MEASUREMENT_FUNCS.keys()))

        self.display_combo = QComboBox()
        # choices map to data columns 0..5 (data1..data6)
        self.display_combo.addItems([
            "data1", "data2", "data3", "data4", "data5", "data6"
        ])

        dv = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_pos = QDoubleValidator(0.0, 1e300, 9, self)
        iv_pos = QIntValidator(1, 10_000_000, self)

        self.x_start = QLineEdit("-0.01"); self.x_start.setValidator(dv)
        self.x_end   = QLineEdit("0.01");  self.x_end.setValidator(dv)
        self.x_step  = QLineEdit("0.001"); self.x_step.setValidator(dv_pos)
        self.wait_ms = QLineEdit("10");    self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))
        self.n_avg   = QLineEdit("4");     self.n_avg.setValidator(iv_pos)

        form.addRow("X axis:", self.axis_combo)
        form.addRow("Measurement:", self.meas_combo)
        form.addRow("Display variable:", self.display_combo)
        form.addRow("X start:", self.x_start)
        form.addRow("X end:",   self.x_end)
        form.addRow("X step:",  self.x_step)
        form.addRow("Wait (ms):", self.wait_ms)
        form.addRow("# of averages (scans):", self.n_avg)

        # Save controls
        self.folder = QLineEdit(os.path.expanduser("~/one_d_scans"))
        self.filename = QLineEdit("scan_1d")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)

        save_row = QGridLayout()
        save_row.addWidget(QLabel("Folder:"), 0, 0)
        save_row.addWidget(self.folder,       0, 1)
        save_row.addWidget(browse_btn,        0, 2)
        save_row.addWidget(QLabel("File (no ext):"), 1, 0)
        save_row.addWidget(self.filename,         1, 1)

        # Start/Stop/Save
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn  = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.save_btn  = QPushButton("Save")
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.save_btn)

        # Status
        self.status_lbl = QLabel("Idle.")

        ctrl_box.addLayout(form)
        ctrl_box.addLayout(save_row)
        ctrl_box.addLayout(btn_row)
        ctrl_box.addWidget(self.status_lbl)
        ctrl_box.addStretch(1)

        # Right plots
        right = QVBoxLayout()
        self.fig_top = Figure(figsize=(6, 3), tight_layout=True)
        self.ax_top = self.fig_top.add_subplot(111)
        self.ax_top.set_title("Incoming Traces")
        self.ax_top.set_xlabel("X")
        self.ax_top.set_ylabel("Display")
        self.canvas_top = FigureCanvas(self.fig_top)

        self.fig_bot = Figure(figsize=(6, 3), tight_layout=True)
        self.ax_bot = self.fig_bot.add_subplot(111)
        self.ax_bot.set_title("Live Average")
        self.ax_bot.set_xlabel("X")
        self.ax_bot.set_ylabel("Display")
        self.canvas_bot = FigureCanvas(self.fig_bot)

        right.addWidget(self.canvas_top, stretch=1)
        right.addWidget(self.canvas_bot, stretch=1)

        outer.addLayout(ctrl_box, stretch=1)
        outer.addLayout(right, stretch=3)

        # Runtime storage
        self._stop_flag = [False]
        self._worker: _Scan1DWorker | None = None
        self._xs: np.ndarray | None = None
        # Store ALL 6 channels per point so we can re-render any display later
        self._current_scan6: np.ndarray | None = None          # shape (N, 6) for the active scan
        self._scan_traces6: list[np.ndarray] = []              # list of arrays, each (N, 6), for completed scans
        self._avg: np.ndarray | None = None                    # running average for selected display (len=N)
        self._data_rows: List[List[float]] = []  # for saving

        # Connect
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)
        self.display_combo.currentIndexChanged.connect(self._refresh_plots)

    # --------------------------- Actions ------------------------------ #
    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder.text())
        if path:
            self.folder.setText(path)

    def _start(self):
        # Build X array
        try:
            x0 = float(self.x_start.text())
            x1 = float(self.x_end.text())
            dx = float(self.x_step.text())
            if dx <= 0:
                raise ValueError
        except Exception:
            QMessageBox.warning(self, "Input error", "X start/end/step invalid.")
            return
        if (x1 - x0) == 0 or abs(dx) > abs(x1 - x0) and abs(x1 - x0) > 0:
            QMessageBox.warning(self, "Input error", "X step too large for range.")
            return

        n_steps = int(np.floor(abs((x1 - x0) / dx)) + 1)
        xs = np.linspace(x0, x0 + (n_steps - 1) * dx * np.sign(x1 - x0), n_steps)
        self._xs = xs
        # Reset runtime arrays
        self._stop_flag[0] = False
        self._scan_traces6.clear()
        self._avg = None
        self._data_rows.clear()

        # Allocate current scan buffer: (N points, 6 channels), fill with NaN
        self._current_scan6 = np.full((len(xs), 6), np.nan, dtype=float)
        ylabel = self.display_combo.currentText()
        self.ax_top.clear(); self.ax_top.set_title("Incoming Traces")
        self.ax_top.set_xlabel("X"); self.ax_top.set_ylabel(ylabel); self.ax_top.grid(True, alpha=0.2)
        self.canvas_top.draw_idle()

        self.ax_bot.clear(); self.ax_bot.set_title("Live Average")
        self.ax_bot.set_xlabel("X"); self.ax_bot.set_ylabel(ylabel); self.ax_bot.grid(True, alpha=0.2)
        self.canvas_bot.draw_idle()

        # Wait & averages
        try:
            wait_s = max(0.0, int(self.wait_ms.text()) / 1000.0)
            n_avg = int(self.n_avg.text())
            if n_avg < 1:
                raise ValueError
        except Exception:
            QMessageBox.warning(self, "Input error", "# of averages must be ≥ 1.")
            return

        # Resolve selected axis setter and measurement function
        axis_key = self.axis_combo.currentText()
        meas_key = self.meas_combo.currentText()
        set_x_fn = self.AXIS_SETTERS.get(axis_key)
        measure_fn = self.MEASUREMENT_FUNCS.get(meas_key)
        if set_x_fn is None or measure_fn is None:
            QMessageBox.critical(self, "Configuration error", "Axis or measurement not found.")
            return



        # Clear plots
        self.ax_top.clear()
        self.ax_top.set_title("Incoming Traces")
        self.ax_top.set_xlabel("X")
        self.ax_top.set_ylabel(self.display_combo.currentText())
        self.ax_top.grid(True, alpha=0.2)
        self.canvas_top.draw_idle()

        self.ax_bot.clear()
        self.ax_bot.set_title("Live Average")
        self.ax_bot.set_xlabel("X")
        self.ax_bot.set_ylabel(self.display_combo.currentText())
        self.ax_bot.grid(True, alpha=0.2)
        self.canvas_bot.draw_idle()

        # Start worker
        self._worker = _Scan1DWorker(
            x_values=xs,
            n_scans=n_avg,
            wait_s=wait_s,
            set_x_fn=set_x_fn,
            measure_fn=measure_fn,
            stop_flag_ref=self._stop_flag
        )
        self._worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker.scan_done.connect(self._on_scan_done, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText("Stopping…")

    def _on_progress(self, scan_idx: int, ix: int, x_val: float, d6: tuple):
        # Save 8-column row for file
        row = [float(scan_idx), float(x_val)] + [float(v) for v in d6]
        row = row[:8] if len(row) > 8 else (row + [math.nan] * (8 - len(row)))
        self._data_rows.append(row)

        # Update the full 6-channel buffer for the current scan
        if self._current_scan6 is not None and 0 <= ix < self._current_scan6.shape[0]:
            self._current_scan6[ix, :] = np.asarray(d6, dtype=float)

        # Redraw top (incoming traces) using the currently selected display channel
        self._redraw_top(ix)

        # Update running average from completed scans only (don’t include the in-progress scan)
        self._update_avg_and_draw()
    def _selected_channel_index(self) -> int:
        # display_combo: "data1".. "data6" -> index 0..5
        return max(0, min(5, self.display_combo.currentIndex()))

    def _redraw_top(self, current_ix: int | None = None):
        """Redraw the top plot: last few completed traces + current partial trace for selected channel."""
        if self._xs is None:
            return
        s = self._selected_channel_index()
        ylabel = self.display_combo.currentText()

        self.ax_top.clear()
        self.ax_top.set_title("Incoming Traces")
        self.ax_top.set_xlabel("X")
        self.ax_top.set_ylabel(ylabel)
        self.ax_top.grid(True, alpha=0.2)

        # Plot up to last 5 completed scans faintly
        for tr6 in self._scan_traces6[-5:]:
            y = tr6[:, s]
            self.ax_top.plot(self._xs, y, alpha=0.25)

        # Plot in-progress trace (partial)
        if self._current_scan6 is not None:
            if current_ix is None:
                # use all non-NaN leading portion
                valid = np.isfinite(self._current_scan6[:, s])
                upto = int(np.argmax(~valid)) if (~valid).any() else len(valid)
            else:
                upto = current_ix + 1
            ycur = self._current_scan6[:upto, s]
            xcur = self._xs[:upto]
            self.ax_top.plot(xcur, ycur, linewidth=2.0)

        self.canvas_top.draw_idle()

    def _update_avg_and_draw(self):
        """Compute and draw the average for the selected channel from completed scans."""
        if self._xs is None or not self._scan_traces6:
            # clear avg plot if nothing to average
            self.ax_bot.clear()
            self.ax_bot.set_title("Live Average")
            self.ax_bot.set_xlabel("X")
            self.ax_bot.set_ylabel(self.display_combo.currentText())
            self.ax_bot.grid(True, alpha=0.2)
            self.canvas_bot.draw_idle()
            return

        s = self._selected_channel_index()
        stack = np.vstack([tr6[:, s] for tr6 in self._scan_traces6])
        self._avg = np.nanmean(stack, axis=0)

        self.ax_bot.clear()
        self.ax_bot.set_title("Live Average")
        self.ax_bot.set_xlabel("X")
        self.ax_bot.set_ylabel(self.display_combo.currentText())
        self.ax_bot.grid(True, alpha=0.2)
        self.ax_bot.plot(self._xs, self._avg, linewidth=2.0)
        self.canvas_bot.draw_idle()

    def _on_scan_done(self, scan_idx: int):
        if self._current_scan6 is None or self._xs is None:
            return
        # Ensure full length (it should be); copy and store
        cur = np.array(self._current_scan6, dtype=float, copy=True)
        self._scan_traces6.append(cur)

        # Start a new in-progress buffer for the next scan
        self._current_scan6 = np.full((len(self._xs), 6), np.nan, dtype=float)

        # Redraw with the newly completed trace added
        self._redraw_top(current_ix=None)
        self._update_avg_and_draw()
        self.status_lbl.setText(f"Completed scan {scan_idx}")

    def _draw_avg(self):
        if self._avg is None or self._xs is None:
            return
        self.ax_bot.clear()
        self.ax_bot.set_title("Live Average")
        self.ax_bot.set_xlabel("X")
        self.ax_bot.set_ylabel(self.display_combo.currentText())
        self.ax_bot.grid(True, alpha=0.2)
        self.ax_bot.plot(self._xs, self._avg, linewidth=2.0)
        self.canvas_bot.draw_idle()
    def _refresh_plots(self):
        """Repaint both plots for the newly selected display variable using stored data."""
        self._redraw_top(current_ix=None)
        self._update_avg_and_draw()
    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self._worker = None

    # ---------------------------- Saving ------------------------------ #
    def _save(self):
        if not self._data_rows or self._xs is None:
            QMessageBox.information(self, "Nothing to save", "Run a scan first.")
            return

        folder = self.folder.text().strip()
        fname  = (self.filename.text().strip() or "scan_1d")

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Folder error", f"Could not create folder:\n{e}")
            return

        arr = np.asarray(self._data_rows, dtype=float)
        path = os.path.join(folder, f"{fname}.txt")

        header = self._build_header()
        try:
            np.savetxt(path, arr, delimiter="\t", fmt="%.10g", header=header, comments="")
        except Exception as e:
            QMessageBox.critical(self, "Save error", f"Failed to save TXT:\n{e}")
            return

        QMessageBox.information(self, "Saved", f"Saved data to:\n{path}")

    def _build_header(self) -> str:
        xs_str = "None" if self._xs is None else f"{self._xs[0]} … {self._xs[-1]} (N={len(self._xs)})"
        lines = [
            "1D Scan",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "Parameters:",
            f"  X axis: {self.axis_combo.currentText()}",
            f"  Measurement: {self.meas_combo.currentText()}",
            f"  Display variable: {self.display_combo.currentText()}",
            f"  X start: {self.x_start.text()}",
            f"  X end:   {self.x_end.text()}",
            f"  X step:  {self.x_step.text()}",
            f"  Wait (ms): {self.wait_ms.text()}",
            f"  # of averages (scans): {self.n_avg.text()}",
            "",
            "Data format (8 columns):",
            "  [scan_index, x_value, data1, data2, data3, data4, data5, data6]",
            "",
            f"X array summary: {xs_str}",
            "",
            "Table begins below this header."
        ]
        return "\n".join(lines)

    # Clean stop
    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
