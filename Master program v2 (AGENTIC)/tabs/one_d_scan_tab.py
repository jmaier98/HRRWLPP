# tabs/one_d_scan_tab.py
from __future__ import annotations
import os, time, math
import numpy as np
from typing import Callable, Dict, Tuple, List

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QComboBox,
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


# ── Worker ────────────────────────────────────────────────────────────────────

class _Scan1DWorker(QThread):
    progress  = pyqtSignal(int, int, float, tuple)   # (scan_idx, ix, x_val, d6)
    scan_done = pyqtSignal(int)
    finished  = pyqtSignal(bool, str)                # (user_abort, message)

    def __init__(
        self,
        x_values: np.ndarray,
        n_scans: int,
        wait_s: float,
        set_x_fn: Callable[[float], None],
        measure_fn: Callable[[], Tuple],
        stop_flag_ref: List[bool],
        wait_before_scan_s: float = 0.0,
    ):
        super().__init__()
        self._xs = x_values
        self._n_scans = max(1, int(n_scans))
        self._wait_s = max(0.0, float(wait_s))
        self._wait_before_scan_s = max(0.0, float(wait_before_scan_s))
        self._set_x = set_x_fn
        self._measure = measure_fn
        self._stop = stop_flag_ref

    def run(self):
        try:
            for scan_idx in range(1, self._n_scans + 1):
                for ix, x in enumerate(self._xs):
                    if self._stop[0]:
                        self.finished.emit(True, "Scan stopped by user.")
                        return
                    try:
                        self._set_x(float(x))
                    except Exception as e:
                        self.finished.emit(True, f"Axis move error at x={x}: {e}")
                        return
                    if self._wait_s > 0:
                        time.sleep(self._wait_s)
                    if ix == 0 and self._wait_before_scan_s > 0:
                        time.sleep(self._wait_before_scan_s)
                    try:
                        d = self._measure()
                    except Exception as e:
                        self.finished.emit(True, f"Measurement error at x={x}: {e}")
                        return
                    if not isinstance(d, (list, tuple)):
                        d = (float(d),)
                    d6 = tuple([float(v) for v in (list(d) + [math.nan] * 6)][:6])
                    self.progress.emit(scan_idx, ix, float(x), d6)
                self.scan_done.emit(scan_idx)
            self.finished.emit(False, "All scans complete.")
        except Exception as e:
            self.finished.emit(True, f"Unexpected error: {e}")


# ── Tab ───────────────────────────────────────────────────────────────────────

class OneDScanTab(QWidget):
    """
    1D scan tab. Receives the shared MicroscopeController and gets its
    axis setters and measurement funcs from it.
    """

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

        axis_setters    = controller.AXIS_SETTERS
        meas_funcs      = controller.MEASUREMENT_FUNCS

        # ── UI ────────────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)

        # Left controls
        ctrl_box = QVBoxLayout()
        form = QFormLayout()

        self.axis_combo = QComboBox()
        self.axis_combo.addItems(list(axis_setters.keys()))

        self.meas_combo = QComboBox()
        self.meas_combo.addItems(list(meas_funcs.keys()))

        self.display_combo = QComboBox()
        self.display_combo.addItems(["data1", "data2", "data3", "data4", "data5", "data6"])

        dv     = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_pos = QDoubleValidator(0.0, 1e300, 9, self)

        self.x_start              = QLineEdit("-0.01"); self.x_start.setValidator(dv)
        self.x_end                = QLineEdit("0.01");  self.x_end.setValidator(dv)
        self.x_step               = QLineEdit("0.001"); self.x_step.setValidator(dv_pos)
        self.wait_ms              = QLineEdit("10");    self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))
        self.wait_before_scan_ms  = QLineEdit("0");     self.wait_before_scan_ms.setValidator(QIntValidator(0, 1_000_000, self))
        self.n_avg                = QLineEdit("4");     self.n_avg.setValidator(QIntValidator(1, 10_000_000, self))

        form.addRow("X axis:",                    self.axis_combo)
        form.addRow("Measurement:",               self.meas_combo)
        form.addRow("Display variable:",          self.display_combo)
        form.addRow("X start:",                   self.x_start)
        form.addRow("X end:",                     self.x_end)
        form.addRow("X step:",                    self.x_step)
        form.addRow("Wait (ms):",                 self.wait_ms)
        form.addRow("Wait before each scan (ms):", self.wait_before_scan_ms)
        form.addRow("# of averages (scans):",     self.n_avg)

        # Save controls
        self.folder   = QLineEdit(os.path.expanduser("~/one_d_scans"))
        self.filename = QLineEdit("scan_1d")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)

        save_row = QGridLayout()
        save_row.addWidget(QLabel("Folder:"),       0, 0)
        save_row.addWidget(self.folder,             0, 1)
        save_row.addWidget(browse_btn,              0, 2)
        save_row.addWidget(QLabel("File (no ext):"), 1, 0)
        save_row.addWidget(self.filename,           1, 1)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn  = QPushButton("Stop");  self.stop_btn.setEnabled(False)
        self.save_btn  = QPushButton("Save")
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.save_btn)

        self.status_lbl = QLabel("Idle.")

        ctrl_box.addLayout(form)
        ctrl_box.addLayout(save_row)
        ctrl_box.addLayout(btn_row)
        ctrl_box.addWidget(self.status_lbl)
        ctrl_box.addStretch(1)

        # Right plots — light style
        right = QVBoxLayout()
        self.fig_top = Figure(figsize=(6, 3), tight_layout=True, facecolor="#f5f5f5")
        self.ax_top  = self.fig_top.add_subplot(111)
        self.ax_top.set_facecolor("#ffffff")
        self.ax_top.set_title("Incoming Traces")
        self.canvas_top = FigureCanvas(self.fig_top)

        self.fig_bot = Figure(figsize=(6, 3), tight_layout=True, facecolor="#f5f5f5")
        self.ax_bot  = self.fig_bot.add_subplot(111)
        self.ax_bot.set_facecolor("#ffffff")
        self.ax_bot.set_title("Live Average")
        self.canvas_bot = FigureCanvas(self.fig_bot)

        right.addWidget(self.canvas_top, stretch=1)
        right.addWidget(self.canvas_bot, stretch=1)

        outer.addLayout(ctrl_box, stretch=1)
        outer.addLayout(right, stretch=3)

        # Runtime state
        self._stop_flag: List[bool] = [False]
        self._worker: _Scan1DWorker | None = None
        self._xs: np.ndarray | None = None
        self._current_scan6: np.ndarray | None = None
        self._scan_traces6: list[np.ndarray] = []
        self._data_rows: List[List[float]] = []

        # Connections — manual controls
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)
        self.display_combo.currentIndexChanged.connect(self._refresh_plots)

        # Connections — live feed from agent scans (queued so they arrive on the GUI thread)
        from PyQt6.QtCore import Qt as _Qt
        sigs = controller.scan_signals
        sigs.scan_started.connect(self._agent_scan_started,   _Qt.ConnectionType.QueuedConnection)
        sigs.point_ready.connect(self._agent_point_ready,     _Qt.ConnectionType.QueuedConnection)
        sigs.scan_complete.connect(self._agent_scan_complete,  _Qt.ConnectionType.QueuedConnection)
        sigs.scan_configured.connect(self._agent_scan_configured, _Qt.ConnectionType.QueuedConnection)
        # DirectConnection so the stop flag is set immediately in the calling thread,
        # not deferred to the next event-loop cycle.
        sigs.stop_all.connect(self._stop, _Qt.ConnectionType.DirectConnection)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder.text())
        if path:
            self.folder.setText(path)

    def _start(self):
        try:
            x0 = float(self.x_start.text())
            x1 = float(self.x_end.text())
            dx = float(self.x_step.text())
            if dx <= 0:
                raise ValueError
        except Exception:
            QMessageBox.warning(self, "Input error", "X start/end/step invalid.")
            return

        if (x1 - x0) == 0 or (abs(dx) > abs(x1 - x0) and abs(x1 - x0) > 0):
            QMessageBox.warning(self, "Input error", "X step too large for range.")
            return

        n_steps = int(np.floor(abs((x1 - x0) / dx)) + 1)
        xs = np.linspace(x0, x0 + (n_steps - 1) * dx * np.sign(x1 - x0), n_steps)
        self._xs = xs

        try:
            wait_s            = max(0.0, int(self.wait_ms.text()) / 1000.0)
            wait_before_scan_s = max(0.0, int(self.wait_before_scan_ms.text()) / 1000.0)
            n_avg             = int(self.n_avg.text())
            if n_avg < 1:
                raise ValueError
        except Exception:
            QMessageBox.warning(self, "Input error", "# of averages must be ≥ 1.")
            return

        axis_key = self.axis_combo.currentText()
        meas_key = self.meas_combo.currentText()
        set_x_fn  = self.controller.AXIS_SETTERS.get(axis_key)
        measure_fn = self.controller.MEASUREMENT_FUNCS.get(meas_key)
        if set_x_fn is None or measure_fn is None:
            QMessageBox.critical(self, "Config error", "Axis or measurement not found.")
            return

        self._stop_flag[0] = False
        self._scan_traces6.clear()
        self._data_rows.clear()
        self._current_scan6 = np.full((len(xs), 6), np.nan)

        ylabel = self.display_combo.currentText()
        for ax, title in ((self.ax_top, "Incoming Traces"), (self.ax_bot, "Live Average")):
            ax.clear(); ax.set_title(title)
            ax.set_xlabel("X"); ax.set_ylabel(ylabel); ax.grid(True, alpha=0.2)
        self.canvas_top.draw_idle()
        self.canvas_bot.draw_idle()

        self._worker = _Scan1DWorker(
            x_values=xs, n_scans=n_avg, wait_s=wait_s,
            set_x_fn=set_x_fn, measure_fn=measure_fn,
            stop_flag_ref=self._stop_flag,
            wait_before_scan_s=wait_before_scan_s,
        )
        self._worker.progress.connect(self._on_progress,  Qt.ConnectionType.QueuedConnection)
        self._worker.scan_done.connect(self._on_scan_done, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished,  Qt.ConnectionType.QueuedConnection)
        self._worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")
        self.controller.log(f"1D scan started: axis={axis_key} meas={meas_key}")

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText("Stopping…")

    # ── Progress / done signals ───────────────────────────────────────────────

    def _on_progress(self, scan_idx: int, ix: int, x_val: float, d6: tuple):
        row = [float(scan_idx), float(x_val)] + [float(v) for v in d6]
        row = (row + [math.nan] * 8)[:8]
        self._data_rows.append(row)

        if self._current_scan6 is not None and 0 <= ix < self._current_scan6.shape[0]:
            self._current_scan6[ix, :] = np.asarray(d6)

        self._redraw_top(ix)
        self._update_avg_and_draw()

    def _on_scan_done(self, scan_idx: int):
        if self._current_scan6 is None or self._xs is None:
            return
        self._scan_traces6.append(np.array(self._current_scan6, copy=True))
        self._current_scan6 = np.full((len(self._xs), 6), np.nan)
        self._redraw_top(None)
        self._update_avg_and_draw()
        self.status_lbl.setText(f"Completed scan {scan_idx}")

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self._worker = None
        self.controller.log(f"1D scan finished: {message}")

    # ── Plot helpers ──────────────────────────────────────────────────────────

    def _selected_ch(self) -> int:
        return max(0, min(5, self.display_combo.currentIndex()))

    def _redraw_top(self, current_ix: int | None):
        if self._xs is None:
            return
        s = self._selected_ch()
        self.ax_top.clear()
        self.ax_top.set_title("Incoming Traces")
        self.ax_top.set_xlabel("X")
        self.ax_top.set_ylabel(self.display_combo.currentText())
        self.ax_top.grid(True, alpha=0.2)
        for tr6 in self._scan_traces6[-5:]:
            self.ax_top.plot(self._xs, tr6[:, s], alpha=0.25)
        if self._current_scan6 is not None:
            upto = (current_ix + 1) if current_ix is not None else len(self._xs)
            self.ax_top.plot(self._xs[:upto], self._current_scan6[:upto, s], linewidth=2.0)
        self.canvas_top.draw_idle()

    def _update_avg_and_draw(self):
        self.ax_bot.clear()
        self.ax_bot.set_title("Live Average")
        self.ax_bot.set_xlabel("X")
        self.ax_bot.set_ylabel(self.display_combo.currentText())
        self.ax_bot.grid(True, alpha=0.2)
        if self._xs is not None and self._scan_traces6:
            s = self._selected_ch()
            stack = np.vstack([tr[:, s] for tr in self._scan_traces6])
            avg = np.nanmean(stack, axis=0)
            self.ax_bot.plot(self._xs, avg, linewidth=2.0)
        self.canvas_bot.draw_idle()

    def _refresh_plots(self):
        self._redraw_top(None)
        self._update_avg_and_draw()

    # ── Save ──────────────────────────────────────────────────────────────────

    # ── Save helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _unique_name(folder: str, base: str) -> str:
        """Return a base name that does not collide with any existing file in folder."""
        used = {os.path.splitext(f)[0] for f in os.listdir(folder)} if os.path.isdir(folder) else set()
        if base not in used:
            return base
        i = 1
        while f"{base}_{i}" in used:
            i += 1
        return f"{base}_{i}"

    def set_filename(self, name: str):
        """Set the filename field (used by agent before saving)."""
        self.filename.setText(name.strip())

    def save_data(self, filename: str | None = None) -> str:
        """
        Save current scan data.  If filename is provided it overrides the GUI field.
        Returns the path of the saved .txt file, or raises RuntimeError on failure.
        """
        if not self._data_rows or self._xs is None:
            raise RuntimeError("No scan data to save — run a scan first.")

        folder = self.folder.text().strip()
        if filename:
            self.filename.setText(filename.strip())
        base = self.filename.text().strip() or "scan_1d"

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"Could not create folder: {e}")

        safe_base  = self._unique_name(folder, base)
        if safe_base != base:
            self.filename.setText(safe_base)

        png_traces = os.path.join(folder, f"{safe_base}_traces.png")
        png_avg    = os.path.join(folder, f"{safe_base}_average.png")
        txt_path   = os.path.join(folder, f"{safe_base}.txt")

        self.fig_top.savefig(png_traces, dpi=200, bbox_inches="tight")
        self.fig_bot.savefig(png_avg,    dpi=200, bbox_inches="tight")

        arr = np.asarray(self._data_rows, dtype=float)
        header = self._build_header(png_traces, png_avg)
        np.savetxt(txt_path, arr, delimiter="\t", fmt="%.10g", header=header, comments="")

        self.controller.log(f"1D scan saved: {txt_path}")
        return txt_path

    def _save(self):
        if not self._data_rows or self._xs is None:
            QMessageBox.information(self, "Nothing to save", "Run a scan first.")
            return
        try:
            txt_path = self.save_data()
        except RuntimeError as e:
            QMessageBox.critical(self, "Save error", str(e))
            return
        folder = self.folder.text().strip()
        base   = self.filename.text().strip()
        QMessageBox.information(self, "Saved",
            f"Saved:\n{txt_path}\n"
            f"{os.path.join(folder, base + '_traces.png')}\n"
            f"{os.path.join(folder, base + '_average.png')}")

    def _build_header(self, png_traces="", png_avg="") -> str:
        xs_str = "None" if self._xs is None else f"{self._xs[0]} to {self._xs[-1]} (N={len(self._xs)})"
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
        ]
        if png_traces:
            lines.append(f"  Traces screenshot: {os.path.basename(png_traces)}")
        if png_avg:
            lines.append(f"  Average screenshot: {os.path.basename(png_avg)}")
        lines += [
            "",
            "Data format (8 columns):",
            "  [scan_index, x_value, data1, data2, data3, data4, data5, data6]",
            "",
            f"X array: {xs_str}",
            "",
            "Table begins below this header.",
        ]
        return "\n".join(lines)

    # ── Agent live-plot handlers ──────────────────────────────────────────────

    def _agent_scan_configured(self, params: dict):
        """Update GUI fields to match the scan the agent is about to run."""
        axis_key = params.get("axis_key", "")
        meas_key = params.get("meas_key", "")
        x_start  = params.get("x_start",  0.0)
        x_end    = params.get("x_end",    0.0)
        n_points = params.get("n_points", 2)
        n_scans  = params.get("n_scans",  1)
        wait_ms  = params.get("wait_ms",  10)

        # Combo boxes — only set if the key exists in the list
        idx = self.axis_combo.findText(axis_key)
        if idx >= 0:
            self.axis_combo.setCurrentIndex(idx)

        idx = self.meas_combo.findText(meas_key)
        if idx >= 0:
            self.meas_combo.setCurrentIndex(idx)

        # Numeric fields
        self.x_start.setText(str(x_start))
        self.x_end.setText(str(x_end))
        # Derive step from n_points
        if n_points > 1 and x_end != x_start:
            step = abs(x_end - x_start) / (n_points - 1)
            self.x_step.setText(f"{step:.6g}")
        self.wait_ms.setText(str(int(wait_ms)))
        self.n_avg.setText(str(int(n_scans)))

    def _agent_scan_started(self, xs):
        """Agent started a new scan — reset plot buffers exactly as _start() does."""
        self._xs = xs
        self._scan_traces6.clear()
        self._data_rows.clear()
        self._current_scan6 = np.full((len(xs), 6), np.nan)
        ylabel = self.display_combo.currentText()
        for ax, title in ((self.ax_top, "Incoming Traces (agent)"),
                          (self.ax_bot, "Live Average (agent)")):
            ax.clear(); ax.set_title(title)
            ax.set_xlabel("X"); ax.set_ylabel(ylabel); ax.grid(True, alpha=0.3)
        self.canvas_top.draw_idle()
        self.canvas_bot.draw_idle()
        self.status_lbl.setText("Agent scanning…")

    def _agent_point_ready(self, ix: int, x_val: float, d6: tuple):
        """Reuse the same progress handler as the manual worker."""
        # agent scans are always scan_idx=1 within a single call; track with incrementing idx
        self._on_progress(1, ix, x_val, d6)

    def _agent_scan_complete(self, scan_idx: int):
        """Reuse the same scan-done handler as the manual worker."""
        self._on_scan_done(scan_idx)
        self.status_lbl.setText(f"Agent scan {scan_idx} complete.")

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
