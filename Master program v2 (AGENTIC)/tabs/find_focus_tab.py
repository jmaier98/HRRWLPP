# tabs/find_focus_tab.py
from __future__ import annotations
import time, math
from typing import Callable, List, Optional, Tuple

import numpy as np
from scipy.optimize import curve_fit

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QComboBox,
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


# ── Sigmoid fit ───────────────────────────────────────────────────────────────

def _sigmoid(x, x0, w, A, B):
    return A + B / (1.0 + np.exp(-(np.asarray(x, float) - x0) / w))


def _fit_sigmoid(x: np.ndarray, y: np.ndarray
                 ) -> Tuple[Optional[float], Optional[dict]]:
    """
    Fit a sigmoid and return the 25%–75% beam width as the spot size.
    Returns (spot_size, fit_info) or (None, None) on failure.
    """
    mask = np.isfinite(y)
    if mask.sum() < 4:
        return None, None
    xc, yc = x[mask], y[mask]
    span = xc.max() - xc.min()
    p0 = [xc.mean(), span / 4, yc.min(), yc.max() - yc.min()]
    bounds = (
        [xc.min() - span, 1e-6, -np.inf, -np.inf],
        [xc.max() + span, span * 2,  np.inf,  np.inf],
    )
    try:
        popt, _ = curve_fit(_sigmoid, xc, yc, p0=p0, bounds=bounds, maxfev=5000)
    except Exception as e:
        print(f"[FindFocus] Sigmoid fit failed: {e}")
        return None, None
    x0, w, A, B = popt
    # 25 % point: x = x0 - w·ln(3),  75 % point: x = x0 + w·ln(3)
    x25, x75 = x0 - w * math.log(3), x0 + w * math.log(3)
    return x75 - x25, {"x0": x0, "w": w, "A": A, "B": B,
                        "spot_size": x75 - x25, "x25": x25, "x75": x75}


# ── Worker ────────────────────────────────────────────────────────────────────

class _FindFocusWorker(QThread):
    """
    Rough + fine focus scan using a 1D galvo sweep to find the sigmoid edge.

    Signals
    -------
    progress(stage, z, spot_size, trace_x, trace_y, fitted_y)
    height_done(z, spot_size_or_None)
    stage_complete(label)
    finished(user_abort, message)
    """
    progress       = pyqtSignal(str, float, object, object, object, object)
    height_done    = pyqtSignal(float, object)
    stage_complete = pyqtSignal(str)
    finished       = pyqtSignal(bool, str)

    def __init__(
        self,
        galvo_xs: np.ndarray,
        galvo_ys: np.ndarray,
        z_start: float,
        z_rough_steps: int,
        z_rough_step_size: float,
        z_fine_steps: int,
        z_fine_step_size: float,
        set_z_fn: Callable[[float], None],
        set_galvo_fn: Callable[[float, float], None],
        measure_fn: Callable[[], float],
        stop_flag_ref: List[bool],
        wait_s: float = 0.05,
    ):
        super().__init__()
        self._galvo_xs        = galvo_xs
        self._galvo_ys        = galvo_ys
        # Arc-length positions along the cut (for plotting)
        dx = np.diff(galvo_xs, prepend=galvo_xs[0])
        dy = np.diff(galvo_ys, prepend=galvo_ys[0])
        self._arc             = np.cumsum(np.hypot(dx, dy))
        self._z_start         = float(z_start)
        self._z_rough_steps   = max(1, int(z_rough_steps))
        self._z_rough_step    = float(z_rough_step_size)
        self._z_fine_steps    = max(1, int(z_fine_steps))
        self._z_fine_step     = float(z_fine_step_size)
        self._set_z           = set_z_fn
        self._set_galvo       = set_galvo_fn
        self._measure         = measure_fn
        self._stop            = stop_flag_ref
        self._wait_s          = max(0.0, float(wait_s))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _scan_at(self, z: float):
        """
        Galvo sweep along the (xs, ys) line at height z.
        Returns (spot_size, arc_positions, signal_values, fitted_signal).
        The sigmoid is fitted against arc-length so it works for any cut angle.
        """
        self._set_z(z)
        time.sleep(self._wait_s * 2)

        signals = []
        for gx, gy in zip(self._galvo_xs, self._galvo_ys):
            if self._stop[0]:
                return None, self._arc, np.full(len(self._arc), np.nan), np.array([])
            self._set_galvo(gx, gy)
            time.sleep(self._wait_s)
            try:
                signals.append(float(self._measure()))
            except Exception:
                signals.append(np.nan)

        signals = np.array(signals, float)
        spot_size, fit_info = _fit_sigmoid(self._arc, signals)

        if fit_info is not None:
            af = np.linspace(self._arc.min(), self._arc.max(), 120)
            fitted = _sigmoid(af, fit_info["x0"], fit_info["w"],
                              fit_info["A"], fit_info["B"])
        else:
            af     = self._arc
            fitted = np.full_like(self._arc, np.nan)

        return spot_size, self._arc, signals, fitted

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(self):
        try:
            # ── Rough scan ────────────────────────────────────────────────────
            rough_z    = [self._z_start + i * self._z_rough_step
                          for i in range(self._z_rough_steps)]
            best_rough_z, best_rough_ss = rough_z[0], np.inf

            for z in rough_z:
                if self._stop[0]:
                    self.finished.emit(True, "Stopped by user (rough).")
                    return
                ss, xs, ys, fy = self._scan_at(z)
                self.height_done.emit(z, ss)
                self.progress.emit("rough", z, ss, xs, ys, fy)
                if ss is not None and ss < best_rough_ss:
                    best_rough_ss, best_rough_z = ss, z

            self.stage_complete.emit("Rough scan complete")

            # Return to start before fine scan to cancel hysteresis
            self._set_z(self._z_start)
            time.sleep(self._wait_s * 2)

            # ── Fine scan ─────────────────────────────────────────────────────
            half  = (self._z_fine_steps - 1) // 2
            fine_z = [best_rough_z + (i - half) * self._z_fine_step
                      for i in range(self._z_fine_steps)]
            best_fine_z, best_fine_ss = best_rough_z, best_rough_ss

            for z in fine_z:
                if self._stop[0]:
                    self.finished.emit(True, "Stopped by user (fine).")
                    return
                ss, xs, ys, fy = self._scan_at(z)
                self.height_done.emit(z, ss)
                self.progress.emit("fine", z, ss, xs, ys, fy)
                if ss is not None and ss < best_fine_ss:
                    best_fine_ss, best_fine_z = ss, z

            self.stage_complete.emit("Fine scan complete")

            if self._stop[0]:
                self.finished.emit(True, "Stopped by user (before final move).")
                return

            # Return to start then approach from below to cancel hysteresis
            self._set_z(self._z_start)
            time.sleep(self._wait_s * 2)
            self._set_z(best_fine_z)
            time.sleep(self._wait_s * 2)

            self.finished.emit(
                False,
                f"Focus found. Z = {best_fine_z:.4f}  "
                f"spot size = {best_fine_ss:.4f}"
            )

        except Exception as e:
            self.finished.emit(True, f"Unexpected error: {e}")


# ── Tab ───────────────────────────────────────────────────────────────────────

class FindFocusTab(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.state   = controller.state
        self.galvo   = controller.get_instrument("Galvo")
        self.lockin  = controller.get_instrument("SR830")
        self.btt     = controller.get_instrument("BTT")

        # ── UI ────────────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)
        ctrl  = QVBoxLayout()
        form  = QFormLayout()

        dv      = QDoubleValidator(-1e9, 1e9, 3, self)
        dv_norm = QDoubleValidator(-500, 500, 1, self)
        iv_pos  = QIntValidator(1, 1_000_000, self)

        self.galvo_x_start    = QLineEdit("-300"); self.galvo_x_start.setValidator(dv_norm)
        self.galvo_y_start    = QLineEdit("0");    self.galvo_y_start.setValidator(dv_norm)
        self.galvo_x_end      = QLineEdit("300");  self.galvo_x_end.setValidator(dv_norm)
        self.galvo_y_end      = QLineEdit("0");    self.galvo_y_end.setValidator(dv_norm)
        self.n_galvo_steps    = QLineEdit("50");   self.n_galvo_steps.setValidator(iv_pos)
        self.z_start          = QLineEdit("0.0");  self.z_start.setValidator(dv)
        self.z_rough_steps    = QLineEdit("5");    self.z_rough_steps.setValidator(iv_pos)
        self.z_rough_step_size= QLineEdit("0.1");  self.z_rough_step_size.setValidator(dv)
        self.z_fine_steps     = QLineEdit("5");    self.z_fine_steps.setValidator(iv_pos)
        self.z_fine_step_size = QLineEdit("0.02"); self.z_fine_step_size.setValidator(dv)
        self.wait_ms          = QLineEdit("50");   self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))

        # Measurement selector
        self.meas_combo = QComboBox()
        meas_opts = []
        if self.lockin is not None:
            meas_opts += ["Lock-in X", "Lock-in R", "Lock-in X2", "Lock-in Aux1"]
        meas_opts += ["(dummy) zero"]
        self.meas_combo.addItems(meas_opts)

        form.addRow("Measurement:",     self.meas_combo)
        form.addRow("Start X:",         self.galvo_x_start)
        form.addRow("Start Y:",         self.galvo_y_start)
        form.addRow("End X:",           self.galvo_x_end)
        form.addRow("End Y:",           self.galvo_y_end)
        form.addRow("# Galvo steps:",   self.n_galvo_steps)
        form.addRow("Z start:",         self.z_start)
        form.addRow("# Rough steps:",   self.z_rough_steps)
        form.addRow("Rough step size:", self.z_rough_step_size)
        form.addRow("# Fine steps:",    self.z_fine_steps)
        form.addRow("Fine step size:",  self.z_fine_step_size)
        form.addRow("Wait (ms):",       self.wait_ms)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn  = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)

        self.status_lbl = QLabel("Idle.")
        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)

        ctrl.addLayout(form)
        ctrl.addLayout(btn_row)
        ctrl.addWidget(self.status_lbl)
        ctrl.addWidget(self.result_lbl)
        ctrl.addStretch(1)

        # Plots
        right = QVBoxLayout()
        self.fig_top = Figure(figsize=(6, 3), tight_layout=True, facecolor="#f5f5f5")
        self.ax_top  = self.fig_top.add_subplot(111)
        self.ax_top.set_facecolor("#ffffff")
        self.ax_top.set_title("Current galvo sweep + sigmoid fit")
        self.ax_top.set_xlabel("Distance along cut")
        self.ax_top.set_ylabel("Signal")
        self.canvas_top = FigureCanvas(self.fig_top)

        self.fig_bot = Figure(figsize=(6, 3), tight_layout=True, facecolor="#f5f5f5")
        self.ax_bot  = self.fig_bot.add_subplot(111)
        self.ax_bot.set_facecolor("#ffffff")
        self.ax_bot.set_title("Spot size vs Z")
        self.ax_bot.set_xlabel("Z height")
        self.ax_bot.set_ylabel("Spot size (25%–75%)")
        self.canvas_bot = FigureCanvas(self.fig_bot)

        right.addWidget(self.canvas_top, stretch=1)
        right.addWidget(self.canvas_bot, stretch=1)

        outer.addLayout(ctrl, stretch=1)
        outer.addLayout(right, stretch=3)

        # Runtime state
        self._stop_flag: List[bool] = [False]
        self._worker: _FindFocusWorker | None = None
        self._heights:    list[float]          = []
        self._spot_sizes: list[Optional[float]] = []
        self._scan_stage: list[str]             = []
        self._current_stage = "rough"

        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

        # Stop-all from agent panel
        controller.scan_signals.stop_all.connect(
            self._stop, Qt.ConnectionType.DirectConnection
        )

    # ── Actions ───────────────────────────────────────────────────────────────

    def _measure_fn(self) -> float:
        meas = self.meas_combo.currentText()
        if meas == "Lock-in X":
            return self.lockin.read_x()
        elif meas == "Lock-in R":
            return self.lockin.read_r()
        elif meas == "Lock-in X2":
            return self.lockin.read_x2()
        elif meas == "Lock-in Aux1":
            return self.lockin.read_PD()
        return 0.0

    def _start(self):
        if not self.galvo or not self.btt:
            QMessageBox.critical(self, "Setup Error",
                                 "Galvo and BTT are required for focus finding.")
            return
        meas = self.meas_combo.currentText()
        if meas != "(dummy) zero" and not self.lockin:
            QMessageBox.critical(self, "Setup Error", "Lock-in not available.")
            return

        try:
            gx_start = float(self.galvo_x_start.text())
            gy_start = float(self.galvo_y_start.text())
            gx_end   = float(self.galvo_x_end.text())
            gy_end   = float(self.galvo_y_end.text())
            n_gsteps = int(self.n_galvo_steps.text())
            z_start  = float(self.z_start.text())
            n_rough  = int(self.z_rough_steps.text())
            rough_dz = float(self.z_rough_step_size.text())
            n_fine   = int(self.z_fine_steps.text())
            fine_dz  = float(self.z_fine_step_size.text())
            wait_s   = int(self.wait_ms.text()) / 1000.0
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Invalid parameter values.")
            return

        # Clear plots and runtime state
        for ax, canvas in ((self.ax_top, self.canvas_top),
                           (self.ax_bot, self.canvas_bot)):
            ax.clear()
            ax.grid(True, alpha=0.3)
        self.canvas_top.draw_idle()
        self.canvas_bot.draw_idle()

        self._stop_flag[0] = False
        self._heights.clear()
        self._spot_sizes.clear()
        self._scan_stage.clear()
        self._current_stage = "rough"
        self.result_lbl.setText("")

        n = max(2, n_gsteps)
        galvo_xs = np.linspace(gx_start, gx_end, n)
        galvo_ys = np.linspace(gy_start, gy_end, n)

        self._worker = _FindFocusWorker(
            galvo_xs=galvo_xs,
            galvo_ys=galvo_ys,
            z_start=z_start,
            z_rough_steps=n_rough,
            z_rough_step_size=rough_dz,
            z_fine_steps=n_fine,
            z_fine_step_size=fine_dz,
            set_z_fn=lambda z: self.btt.focus(z),
            set_galvo_fn=lambda x, y: self.galvo.move_xy(x, y),
            measure_fn=self._measure_fn,
            stop_flag_ref=self._stop_flag,
            wait_s=wait_s,
        )
        self._worker.progress.connect(self._on_progress,     Qt.ConnectionType.QueuedConnection)
        self._worker.height_done.connect(self._on_height_done, Qt.ConnectionType.QueuedConnection)
        self._worker.stage_complete.connect(self._on_stage_complete, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished,     Qt.ConnectionType.QueuedConnection)
        self._worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")
        self.controller.log("Find focus started")

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText("Stopping…")

    # ── Worker signal handlers ────────────────────────────────────────────────

    def _on_progress(self, stage: str, z: float, spot_size,
                     trace_x, trace_y, fitted_y):
        self.ax_top.clear()
        self.ax_top.set_facecolor("#ffffff")
        ss_str = f"{spot_size:.4f}" if spot_size is not None else "fit failed"
        self.ax_top.set_title(f"{stage.capitalize()} — Z = {z:.4f},  spot = {ss_str}")
        self.ax_top.set_xlabel("Distance along cut")
        self.ax_top.set_ylabel("Signal")
        if len(trace_x) > 0:
            self.ax_top.plot(trace_x, trace_y, "o-", markersize=3,
                             linewidth=1.2, label="Measured", alpha=0.8)
        if len(fitted_y) > 0 and np.isfinite(fitted_y).any():
            af = np.linspace(float(trace_x.min()), float(trace_x.max()), len(fitted_y))
            self.ax_top.plot(af, fitted_y, "r--", linewidth=2, label="Sigmoid fit")
        self.ax_top.legend(fontsize=8)
        self.ax_top.grid(True, alpha=0.3)
        self.canvas_top.draw_idle()

    def _on_height_done(self, z: float, spot_size):
        self._heights.append(z)
        self._spot_sizes.append(spot_size)
        self._scan_stage.append(self._current_stage)

        valid = [(h, ss, st) for h, ss, st in
                 zip(self._heights, self._spot_sizes, self._scan_stage)
                 if ss is not None]

        self.ax_bot.clear()
        self.ax_bot.set_facecolor("#ffffff")
        self.ax_bot.set_title("Spot size vs Z")
        self.ax_bot.set_xlabel("Z height")
        self.ax_bot.set_ylabel("Spot size (25%–75%)")

        if valid:
            rh = [h for h, ss, st in valid if st == "rough"]
            rs = [ss for h, ss, st in valid if st == "rough"]
            fh = [h for h, ss, st in valid if st == "fine"]
            fs = [ss for h, ss, st in valid if st == "fine"]
            if rh:
                self.ax_bot.plot(rh, rs, "bo-", linewidth=2, markersize=5, label="Rough")
            if fh:
                self.ax_bot.plot(fh, fs, "ro-", linewidth=2, markersize=5, label="Fine")
            all_h = [h for h, _, _ in valid]
            all_s = [ss for _, ss, _ in valid]
            bi = int(np.argmin(all_s))
            self.ax_bot.plot(all_h[bi], all_s[bi], "k*", markersize=16, label="Best")
            self.ax_bot.legend(fontsize=8)

        self.ax_bot.grid(True, alpha=0.3)
        self.canvas_bot.draw_idle()
        self.status_lbl.setText(f"Z = {z:.4f}")

    def _on_stage_complete(self, label: str):
        if "Rough" in label:
            self._current_stage = "fine"
        self.status_lbl.setText(label)

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText("Done." if not user_abort else "Stopped.")
        self.result_lbl.setText(message)
        self._worker = None
        self.controller.log(f"Find focus: {message}")

    # ── Agent API ─────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        return {
            "measurement":       self.meas_combo.currentText(),
            "galvo_x_start":     self.galvo_x_start.text(),
            "galvo_y_start":     self.galvo_y_start.text(),
            "galvo_x_end":       self.galvo_x_end.text(),
            "galvo_y_end":       self.galvo_y_end.text(),
            "n_galvo_steps":     self.n_galvo_steps.text(),
            "z_start":           self.z_start.text(),
            "z_rough_steps":     self.z_rough_steps.text(),
            "z_rough_step_size": self.z_rough_step_size.text(),
            "z_fine_steps":      self.z_fine_steps.text(),
            "z_fine_step_size":  self.z_fine_step_size.text(),
            "wait_ms":           self.wait_ms.text(),
            "measurements_available": list(self.meas_combo.itemText(i)
                                           for i in range(self.meas_combo.count())),
        }

    def apply_settings(self, settings: dict) -> list[str]:
        warnings: list[str] = []

        def _set_text(widget, key):
            if key in settings:
                widget.setText(str(settings[key]))

        if "measurement" in settings:
            idx = self.meas_combo.findText(settings["measurement"])
            if idx >= 0:
                self.meas_combo.setCurrentIndex(idx)
            else:
                warnings.append(f"Unknown measurement: {settings['measurement']}")

        _set_text(self.galvo_x_start,     "galvo_x_start")
        _set_text(self.galvo_y_start,     "galvo_y_start")
        _set_text(self.galvo_x_end,       "galvo_x_end")
        _set_text(self.galvo_y_end,       "galvo_y_end")
        _set_text(self.n_galvo_steps,     "n_galvo_steps")
        _set_text(self.z_start,           "z_start")
        _set_text(self.z_rough_steps,     "z_rough_steps")
        _set_text(self.z_rough_step_size, "z_rough_step_size")
        _set_text(self.z_fine_steps,      "z_fine_steps")
        _set_text(self.z_fine_step_size,  "z_fine_step_size")
        _set_text(self.wait_ms,           "wait_ms")

        return warnings

    def run_find_focus_blocking(self) -> dict:
        """Run the focus scan in the calling thread (for agent use)."""
        if not self.galvo or not self.btt:
            return {"error": "Galvo and BTT are required for focus finding"}
        try:
            gx_start = float(self.galvo_x_start.text())
            gy_start = float(self.galvo_y_start.text())
            gx_end   = float(self.galvo_x_end.text())
            gy_end   = float(self.galvo_y_end.text())
            n_gsteps = int(self.n_galvo_steps.text())
            z_start  = float(self.z_start.text())
            n_rough  = int(self.z_rough_steps.text())
            rough_dz = float(self.z_rough_step_size.text())
            n_fine   = int(self.z_fine_steps.text())
            fine_dz  = float(self.z_fine_step_size.text())
            wait_s   = int(self.wait_ms.text()) / 1000.0
        except ValueError as e:
            return {"error": f"Invalid parameter: {e}"}

        n = max(2, n_gsteps)
        galvo_xs = np.linspace(gx_start, gx_end, n)
        galvo_ys = np.linspace(gy_start, gy_end, n)
        stop_flag = [False]

        worker = _FindFocusWorker(
            galvo_xs=galvo_xs, galvo_ys=galvo_ys,
            z_start=z_start,
            z_rough_steps=n_rough, z_rough_step_size=rough_dz,
            z_fine_steps=n_fine,   z_fine_step_size=fine_dz,
            set_z_fn=lambda z: self.btt.focus(z),
            set_galvo_fn=lambda x, y: self.galvo.move_xy(x, y),
            measure_fn=self._measure_fn,
            stop_flag_ref=stop_flag,
            wait_s=wait_s,
        )

        result_holder: dict = {}

        def _on_finished(user_abort: bool, message: str):
            result_holder["user_abort"] = user_abort
            result_holder["message"]    = message

        worker.finished.connect(_on_finished, Qt.ConnectionType.DirectConnection)
        worker.start()
        worker.wait()

        if result_holder.get("user_abort"):
            return {"error": result_holder.get("message", "Stopped")}

        msg = result_holder.get("message", "")
        self.controller.log(f"Agent find focus: {msg}")
        # Parse best Z and spot size from the message
        import re
        z_match  = re.search(r"Z\s*=\s*([\d.eE+\-]+)", msg)
        ss_match = re.search(r"spot size\s*=\s*([\d.eE+\-]+)", msg)
        return {
            "message":   msg,
            "best_z":    float(z_match.group(1))  if z_match  else None,
            "spot_size": float(ss_match.group(1)) if ss_match else None,
        }

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
