# tabs/alignment_tab.py
from __future__ import annotations
import time
from typing import Callable, List, Optional, Tuple

import numpy as np
from scipy.optimize import curve_fit

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QComboBox, QCheckBox,
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


# ── Fit models ────────────────────────────────────────────────────────────────

def _sigmoid(x, x0, w, A, B):
    return A + B / (1.0 + np.exp(-(np.asarray(x, float) - x0) / w))


def _double_sigmoid(x, x1, x2, w, A, B):
    x = np.asarray(x, float)
    return A + B * (1 / (1 + np.exp(-(x - x1) / w)) -
                    1 / (1 + np.exp(-(x - x2) / w)))


def _fit_sigmoid_center(x: np.ndarray, y: np.ndarray
                        ) -> Tuple[Optional[float], Optional[dict]]:
    mask = np.isfinite(y)
    if mask.sum() < 4:
        return None, None
    xc, yc = x[mask], y[mask]
    span = xc.max() - xc.min()
    p0 = [xc.mean(), span / 4, yc.min(), yc.max() - yc.min()]
    bounds = ([xc.min() - span, 1e-6, -np.inf, -np.inf],
              [xc.max() + span, span * 2,  np.inf,  np.inf])
    try:
        popt, _ = curve_fit(_sigmoid, xc, yc, p0=p0, bounds=bounds, maxfev=5000)
        x0, w, A, B = popt
        return x0, {"model": "sigmoid", "x0": x0, "w": w, "A": A, "B": B}
    except Exception as e:
        print(f"[Alignment] Sigmoid fit failed: {e}")
        return None, None


def _fit_double_sigmoid_center(x: np.ndarray, y: np.ndarray
                                ) -> Tuple[Optional[float], Optional[dict]]:
    mask = np.isfinite(y)
    if mask.sum() < 5:
        return None, None
    xc, yc = x[mask], y[mask]
    span = xc.max() - xc.min()
    peak_idx = np.argmax(np.abs(yc - yc.mean()))
    xp = xc[peak_idx]
    hw = span / 8
    p0 = [xp - hw, xp + hw, max(span / 20, 1e-6), yc.min(), yc.max() - yc.min()]
    bounds = ([xc.min() - span * .5, xc.min() - span * .5, 1e-9, -np.inf, -np.inf],
              [xc.max() + span * .5, xc.max() + span * .5, span,   np.inf,  np.inf])
    try:
        popt, _ = curve_fit(_double_sigmoid, xc, yc, p0=p0, bounds=bounds, maxfev=10000)
        x1, x2, w, A, B = popt
        if x1 > x2:
            x1, x2 = x2, x1
        center = (x1 + x2) / 2
        return center, {"model": "double_sigmoid", "x1": x1, "x2": x2, "w": w, "A": A, "B": B}
    except Exception as e:
        print(f"[Alignment] Double-sigmoid fit failed: {e}")
        return None, None


# ── Worker ────────────────────────────────────────────────────────────────────

class _AlignmentWorker(QThread):
    """
    Runs horizontal then vertical galvo scans, fits the selected model,
    and extracts the feature centre in each axis.

    Signals
    -------
    progress(direction, center, trace_pos, trace_vals, fitted_vals)
    finished(user_abort, x_center, y_center, message)
    """
    progress = pyqtSignal(str, object, object, object, object)
    finished = pyqtSignal(bool, object, object, str)

    def __init__(
        self,
        h_start_x: float, h_start_y: float, h_distance: float, h_left_right: bool,
        v_start_x: float, v_start_y: float, v_distance: float, v_bottom_top: bool,
        n_h: int, n_v: int,
        set_galvo_fn: Callable[[float, float], None],
        measure_fn: Callable[[], float],
        stop_flag_ref: List[bool],
        wait_s: float,
        h_fit_mode: str,
        v_fit_mode: str,
    ):
        super().__init__()
        self._hx0, self._hy0  = h_start_x, h_start_y
        self._h_dist          = h_distance
        self._h_sign          = 1.0 if h_left_right else -1.0
        self._vx0, self._vy0  = v_start_x, v_start_y
        self._v_dist          = v_distance
        self._v_sign          = 1.0 if v_bottom_top else -1.0
        self._n_h, self._n_v  = max(2, n_h), max(2, n_v)
        self._set_galvo       = set_galvo_fn
        self._measure         = measure_fn
        self._stop            = stop_flag_ref
        self._wait_s          = max(0.0, wait_s)
        self._h_fit           = h_fit_mode
        self._v_fit           = v_fit_mode
        self.x_result: Optional[float] = None
        self.y_result: Optional[float] = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _scan(self, positions: np.ndarray, fixed: float,
              direction: str, fit_mode: str):
        """
        1D scan along `positions`.  direction='horizontal' keeps Y=fixed;
        direction='vertical' keeps X=fixed.  Returns (center, pos, vals, fitted).
        """
        vals = []
        for p in positions:
            if self._stop[0]:
                return None, positions, np.full(len(positions), np.nan), np.array([])
            if direction == "horizontal":
                self._set_galvo(p, fixed)
            else:
                self._set_galvo(fixed, p)
            time.sleep(self._wait_s)
            try:
                vals.append(float(self._measure()))
            except Exception:
                vals.append(np.nan)

        vals = np.array(vals, float)
        pf   = np.linspace(positions.min(), positions.max(), 200)

        if fit_mode == "double_sigmoid":
            center, info = _fit_double_sigmoid_center(positions, vals)
            if info is not None:
                fitted = _double_sigmoid(pf, info["x1"], info["x2"],
                                         info["w"], info["A"], info["B"])
            else:
                fitted = np.full_like(pf, np.nan)
        else:
            center, info = _fit_sigmoid_center(positions, vals)
            if info is not None:
                fitted = _sigmoid(pf, info["x0"], info["w"], info["A"], info["B"])
            else:
                fitted = np.full_like(pf, np.nan)

        return center, positions, vals, fitted

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(self):
        try:
            # ── Horizontal scan ───────────────────────────────────────────────
            if self._stop[0]:
                self.finished.emit(True, None, None, "Stopped by user.")
                return
            h_pos = np.linspace(self._hx0,
                                self._hx0 + self._h_sign * self._h_dist,
                                self._n_h)
            x_center, tp, tv, tf = self._scan(h_pos, self._hy0,
                                               "horizontal", self._h_fit)
            self.progress.emit("horizontal", x_center, tp, tv, tf)
            self._set_galvo(self._hx0, self._hy0)
            time.sleep(self._wait_s * 2)

            # ── Vertical scan ─────────────────────────────────────────────────
            if self._stop[0]:
                self.finished.emit(True, None, None, "Stopped by user.")
                return
            v_pos = np.linspace(self._vy0,
                                self._vy0 + self._v_sign * self._v_dist,
                                self._n_v)
            y_center, tp, tv, tf = self._scan(v_pos, self._vx0,
                                               "vertical", self._v_fit)
            self.progress.emit("vertical", y_center, tp, tv, tf)
            self._set_galvo(self._vx0, self._vy0)
            time.sleep(self._wait_s * 2)

            self.x_result = x_center
            self.y_result = y_center
            xc_str = f"{x_center:.4f}" if x_center is not None else "N/A"
            yc_str = f"{y_center:.4f}" if y_center is not None else "N/A"
            self.finished.emit(False, x_center, y_center,
                               f"Done. X centre = {xc_str},  Y centre = {yc_str}")

        except Exception as e:
            self.finished.emit(True, None, None, f"Unexpected error: {e}")


# ── Tab ───────────────────────────────────────────────────────────────────────

class AlignmentTab(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.state  = controller.state
        self.galvo  = controller.get_instrument("Galvo")
        self.lockin = controller.get_instrument("SR830")

        # ── UI ────────────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)
        ctrl  = QVBoxLayout()
        form  = QFormLayout()

        dv     = QDoubleValidator(-500, 500, 1, self)
        dv_pos = QDoubleValidator(0.0, 1000, 1, self)
        iv_pos = QIntValidator(2, 1_000_000, self)

        # Horizontal scan
        self.hx0   = QLineEdit("0");    self.hx0.setValidator(dv)
        self.hy0   = QLineEdit("0");    self.hy0.setValidator(dv)
        self.h_dist= QLineEdit("100");  self.h_dist.setValidator(dv_pos)
        self.n_h   = QLineEdit("50");   self.n_h.setValidator(iv_pos)
        self.h_dir = QCheckBox("Left → Right");  self.h_dir.setChecked(True)
        self.h_fit = QComboBox()
        self.h_fit.addItem("Single sigmoid (edge)",          "sigmoid")
        self.h_fit.addItem("Double sigmoid / hat (wire)",    "double_sigmoid")

        # Vertical scan
        self.vx0   = QLineEdit("0");    self.vx0.setValidator(dv)
        self.vy0   = QLineEdit("0");    self.vy0.setValidator(dv)
        self.v_dist= QLineEdit("100");  self.v_dist.setValidator(dv_pos)
        self.n_v   = QLineEdit("50");   self.n_v.setValidator(iv_pos)
        self.v_dir = QCheckBox("Bottom → Top");  self.v_dir.setChecked(True)
        self.v_fit = QComboBox()
        self.v_fit.addItem("Single sigmoid (edge)",          "sigmoid")
        self.v_fit.addItem("Double sigmoid / hat (wire)",    "double_sigmoid")

        # Measurement
        self.meas_combo = QComboBox()
        meas_opts = []
        if self.lockin:
            meas_opts += ["Lock-in X", "Lock-in R", "Lock-in X2", "Lock-in Aux1"]
        meas_opts += ["(dummy) zero"]
        self.meas_combo.addItems(meas_opts)

        # Wait
        self.wait_ms = QLineEdit("50")
        self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))

        form.addRow(QLabel("── Measurement ──────────────────"))
        form.addRow("Signal:",            self.meas_combo)
        form.addRow("Wait (ms):",         self.wait_ms)
        form.addRow(QLabel("── Horizontal scan ──────────────"))
        form.addRow("Start X:",           self.hx0)
        form.addRow("Start Y:",           self.hy0)
        form.addRow("Distance:",          self.h_dist)
        form.addRow("# points:",          self.n_h)
        form.addRow("Direction:",         self.h_dir)
        form.addRow("Fit model:",         self.h_fit)
        form.addRow(QLabel("── Vertical scan ────────────────"))
        form.addRow("Start X:",           self.vx0)
        form.addRow("Start Y:",           self.vy0)
        form.addRow("Distance:",          self.v_dist)
        form.addRow("# points:",          self.n_v)
        form.addRow("Direction:",         self.v_dir)
        form.addRow("Fit model:",         self.v_fit)

        btn_row = QHBoxLayout()
        self.set_orig_btn  = QPushButton("Set original position")
        self.find_btn      = QPushButton("Find feature")
        self.stop_btn      = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.set_orig_btn)
        btn_row.addWidget(self.find_btn)
        btn_row.addWidget(self.stop_btn)

        self.status_lbl = QLabel("Idle.")
        self.info_lbl   = QLabel("")
        self.info_lbl.setWordWrap(True)

        ctrl.addLayout(form)
        ctrl.addLayout(btn_row)
        ctrl.addWidget(self.status_lbl)
        ctrl.addWidget(self.info_lbl)
        ctrl.addStretch(1)

        # Plots
        right = QVBoxLayout()
        self.fig_h = Figure(figsize=(6, 3), tight_layout=True, facecolor="#f5f5f5")
        self.ax_h  = self.fig_h.add_subplot(111)
        self.ax_h.set_facecolor("#ffffff")
        self.ax_h.set_title("Horizontal scan")
        self.canvas_h = FigureCanvas(self.fig_h)

        self.fig_v = Figure(figsize=(6, 3), tight_layout=True, facecolor="#f5f5f5")
        self.ax_v  = self.fig_v.add_subplot(111)
        self.ax_v.set_facecolor("#ffffff")
        self.ax_v.set_title("Vertical scan")
        self.canvas_v = FigureCanvas(self.fig_v)

        right.addWidget(self.canvas_h, stretch=1)
        right.addWidget(self.canvas_v, stretch=1)

        outer.addLayout(ctrl, stretch=1)
        outer.addLayout(right, stretch=3)

        # Runtime state
        self._stop_flag: List[bool] = [False]
        self._worker: _AlignmentWorker | None = None
        self._update_original = False

        # Connections
        self.set_orig_btn.clicked.connect(self._set_original)
        self.find_btn.clicked.connect(self._find_feature)
        self.stop_btn.clicked.connect(self._stop)
        controller.scan_signals.stop_all.connect(
            self._stop, Qt.ConnectionType.DirectConnection
        )

    # ── Measurement function ──────────────────────────────────────────────────

    def _measure(self) -> float:
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

    # ── Actions ───────────────────────────────────────────────────────────────

    def _set_original(self):
        self._run(update_original=True)

    def _find_feature(self):
        self._run(update_original=False)

    def _run(self, update_original: bool):
        if not self.galvo:
            QMessageBox.critical(self, "Setup Error", "Galvo not available.")
            return
        meas = self.meas_combo.currentText()
        if meas != "(dummy) zero" and not self.lockin:
            QMessageBox.critical(self, "Setup Error", "Lock-in not available.")
            return

        try:
            hx0    = float(self.hx0.text())
            hy0    = float(self.hy0.text())
            h_dist = float(self.h_dist.text())
            n_h    = int(self.n_h.text())
            vx0    = float(self.vx0.text())
            vy0    = float(self.vy0.text())
            v_dist = float(self.v_dist.text())
            n_v    = int(self.n_v.text())
            wait_s = int(self.wait_ms.text()) / 1000.0
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Invalid parameter values.")
            return

        # Clear plots
        for ax, canvas, title in (
            (self.ax_h, self.canvas_h, "Horizontal scan"),
            (self.ax_v, self.canvas_v, "Vertical scan"),
        ):
            ax.clear()
            ax.set_facecolor("#ffffff")
            ax.set_title(title)
            ax.grid(True, alpha=0.3)
            canvas.draw_idle()

        self._stop_flag[0] = False
        self._update_original = update_original
        self.info_lbl.setText("")

        self._worker = _AlignmentWorker(
            h_start_x=hx0, h_start_y=hy0,
            h_distance=h_dist, h_left_right=self.h_dir.isChecked(),
            v_start_x=vx0, v_start_y=vy0,
            v_distance=v_dist, v_bottom_top=self.v_dir.isChecked(),
            n_h=n_h, n_v=n_v,
            set_galvo_fn=lambda x, y: self.galvo.move_xy(x, y),
            measure_fn=self._measure,
            stop_flag_ref=self._stop_flag,
            wait_s=wait_s,
            h_fit_mode=self.h_fit.currentData(),
            v_fit_mode=self.v_fit.currentData(),
        )
        self._worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

        self.set_orig_btn.setEnabled(False)
        self.find_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")
        self.controller.log(
            f"Alignment started ({'set original' if update_original else 'find feature'})"
        )

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText("Stopping…")

    # ── Worker signal handlers ────────────────────────────────────────────────

    def _on_progress(self, direction: str, center,
                     trace_pos, trace_vals, fitted_vals):
        if direction == "horizontal":
            ax, canvas = self.ax_h, self.canvas_h
            xlabel, title_base = "Galvo X", "Horizontal scan"
            fit_mode = self.h_fit.currentData()
        else:
            ax, canvas = self.ax_v, self.canvas_v
            xlabel, title_base = "Galvo Y", "Vertical scan"
            fit_mode = self.v_fit.currentData()

        ax.clear()
        ax.set_facecolor("#ffffff")
        cstr  = f"{center:.4f}" if center is not None else "fit failed"
        ax.set_title(f"{title_base}  —  centre = {cstr}")
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Signal")

        if len(trace_pos) > 0:
            ax.plot(trace_pos, trace_vals, "o-", markersize=3,
                    linewidth=1.2, alpha=0.8, label="Measured")

        if len(fitted_vals) > 0 and np.isfinite(fitted_vals).any():
            pf = np.linspace(float(trace_pos.min()), float(trace_pos.max()), len(fitted_vals))
            lbl = "Hat fit" if fit_mode == "double_sigmoid" else "Sigmoid fit"
            ax.plot(pf, fitted_vals, "r--", linewidth=2, label=lbl)
            if center is not None:
                ax.axvline(center, color="green", linestyle=":", linewidth=1.5)
                ax.plot(center, np.interp(center, pf, fitted_vals),
                        "g*", markersize=14, label="Centre")

        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        canvas.draw_idle()

    def _on_finished(self, user_abort: bool, x_center, y_center, message: str):
        self.set_orig_btn.setEnabled(True)
        self.find_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText("Stopped." if user_abort else "Done.")
        self._worker = None

        if user_abort or x_center is None or y_center is None:
            self.info_lbl.setText(message)
            return

        if self._update_original:
            self.state.settings["x_original_feature_pos"] = x_center
            self.state.settings["y_original_feature_pos"] = y_center
            self.state.settings["x_found_feature_pos"]    = x_center
            self.state.settings["y_found_feature_pos"]    = y_center
            self.state.settings["x_offset"] = 0.0
            self.state.settings["y_offset"] = 0.0
            self.state.save()
            self.info_lbl.setText(
                f"Original set — X = {x_center:.4f},  Y = {y_center:.4f}"
            )
            self.controller.log(
                f"Alignment original set: X={x_center:.4f} Y={y_center:.4f}"
            )
        else:
            orig_x = float(self.state.settings.get("x_original_feature_pos", 0.0))
            orig_y = float(self.state.settings.get("y_original_feature_pos", 0.0))
            dx = x_center - orig_x
            dy = y_center - orig_y
            self.state.settings["x_found_feature_pos"] = x_center
            self.state.settings["y_found_feature_pos"] = y_center
            self.state.settings["x_offset"] = dx
            self.state.settings["y_offset"] = dy
            self.state.save()
            self.info_lbl.setText(
                f"Found — X = {x_center:.4f},  Y = {y_center:.4f}  |  "
                f"Offset Δx = {dx:.4f},  Δy = {dy:.4f}"
            )
            self.controller.log(
                f"Alignment found: X={x_center:.4f} Y={y_center:.4f} "
                f"offset=({dx:.4f},{dy:.4f})"
            )

    def get_alignment_params(self) -> dict:
        """Capture all UI parameters. Call from the main thread before starting a scan."""
        return {
            "hx0":        float(self.hx0.text()),
            "hy0":        float(self.hy0.text()),
            "h_dist":     float(self.h_dist.text()),
            "h_left_right": self.h_dir.isChecked(),
            "vx0":        float(self.vx0.text()),
            "vy0":        float(self.vy0.text()),
            "v_dist":     float(self.v_dist.text()),
            "v_bottom_top": self.v_dir.isChecked(),
            "n_h":        int(self.n_h.text()),
            "n_v":        int(self.n_v.text()),
            "wait_s":     int(self.wait_ms.text()) / 1000.0,
            "h_fit_mode": self.h_fit.currentData(),
            "v_fit_mode": self.v_fit.currentData(),
        }

    def run_alignment_blocking(self, stop_flag_ref: list) -> tuple:
        """
        Run alignment synchronously from a background thread.
        Blocks until the alignment worker finishes, then updates state and returns
        (x_offset, y_offset).  Params must have been captured with get_alignment_params()
        beforehand and stored in self._cached_alignment_params.
        """
        p = getattr(self, '_cached_alignment_params', None)
        if p is None:
            print("[Alignment] No cached params — skipping.")
            return 0.0, 0.0

        worker = _AlignmentWorker(
            h_start_x=p["hx0"], h_start_y=p["hy0"],
            h_distance=p["h_dist"], h_left_right=p["h_left_right"],
            v_start_x=p["vx0"], v_start_y=p["vy0"],
            v_distance=p["v_dist"], v_bottom_top=p["v_bottom_top"],
            n_h=p["n_h"], n_v=p["n_v"],
            set_galvo_fn=lambda x, y: self.galvo.move_xy(x, y),
            measure_fn=self._measure,
            stop_flag_ref=stop_flag_ref,
            wait_s=p["wait_s"],
            h_fit_mode=p["h_fit_mode"],
            v_fit_mode=p["v_fit_mode"],
        )
        worker.start()
        worker.wait()  # blocks the calling thread until alignment is done

        x_center = worker.x_result
        y_center = worker.y_result

        if x_center is None or y_center is None:
            print("[Alignment] Fit failed — offsets unchanged.")
            return (self.state.settings.get("x_offset", 0.0),
                    self.state.settings.get("y_offset", 0.0))

        orig_x = float(self.state.settings.get("x_original_feature_pos", x_center))
        orig_y = float(self.state.settings.get("y_original_feature_pos", y_center))
        dx = x_center - orig_x
        dy = y_center - orig_y
        self.state.settings["x_found_feature_pos"] = x_center
        self.state.settings["y_found_feature_pos"] = y_center
        self.state.settings["x_offset"] = dx
        self.state.settings["y_offset"] = dy
        self.state.save()
        print(f"[Alignment] x={x_center:.4f} y={y_center:.4f}  offset=({dx:.4f},{dy:.4f})")
        return dx, dy

    # ── Agent API ─────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        return {
            "measurement":     self.meas_combo.currentText(),
            "wait_ms":         self.wait_ms.text(),
            "h_start_x":       self.hx0.text(),
            "h_start_y":       self.hy0.text(),
            "h_distance":      self.h_dist.text(),
            "h_n_points":      self.n_h.text(),
            "h_left_right":    self.h_dir.isChecked(),
            "h_fit_model":     self.h_fit.currentData(),
            "v_start_x":       self.vx0.text(),
            "v_start_y":       self.vy0.text(),
            "v_distance":      self.v_dist.text(),
            "v_n_points":      self.n_v.text(),
            "v_bottom_top":    self.v_dir.isChecked(),
            "v_fit_model":     self.v_fit.currentData(),
            "x_original":      self.state.settings.get("x_original_feature_pos"),
            "y_original":      self.state.settings.get("y_original_feature_pos"),
            "x_found":         self.state.settings.get("x_found_feature_pos"),
            "y_found":         self.state.settings.get("y_found_feature_pos"),
            "x_offset":        self.state.settings.get("x_offset", 0.0),
            "y_offset":        self.state.settings.get("y_offset", 0.0),
            "measurements_available": list(self.meas_combo.itemText(i)
                                           for i in range(self.meas_combo.count())),
            "fit_models_available": ["sigmoid", "double_sigmoid"],
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

        _set_text(self.wait_ms,   "wait_ms")
        _set_text(self.hx0,       "h_start_x")
        _set_text(self.hy0,       "h_start_y")
        _set_text(self.h_dist,    "h_distance")
        _set_text(self.n_h,       "h_n_points")
        _set_text(self.vx0,       "v_start_x")
        _set_text(self.vy0,       "v_start_y")
        _set_text(self.v_dist,    "v_distance")
        _set_text(self.n_v,       "v_n_points")

        if "h_left_right" in settings:
            self.h_dir.setChecked(bool(settings["h_left_right"]))
        if "v_bottom_top" in settings:
            self.v_dir.setChecked(bool(settings["v_bottom_top"]))

        for combo, key in ((self.h_fit, "h_fit_model"), (self.v_fit, "v_fit_model")):
            if key in settings:
                for i in range(combo.count()):
                    if combo.itemData(i) == settings[key]:
                        combo.setCurrentIndex(i)
                        break
                else:
                    warnings.append(f"Unknown fit model for {key}: {settings[key]}")

        return warnings

    def run_find_feature_blocking(self, set_origin: bool = False) -> dict:
        """
        Run the alignment scan in the calling thread (for agent use).
        If set_origin=True, saves the found position as the new origin.
        Returns a dict with x_center, y_center, x_offset, y_offset.
        """
        if not self.galvo:
            return {"error": "Galvo not available"}
        p = self.get_alignment_params()
        stop_flag = [False]
        worker = _AlignmentWorker(
            h_start_x=p["hx0"],     h_start_y=p["hy0"],
            h_distance=p["h_dist"], h_left_right=p["h_left_right"],
            v_start_x=p["vx0"],     v_start_y=p["vy0"],
            v_distance=p["v_dist"], v_bottom_top=p["v_bottom_top"],
            n_h=p["n_h"],           n_v=p["n_v"],
            set_galvo_fn=lambda x, y: self.galvo.move_xy(x, y),
            measure_fn=self._measure,
            stop_flag_ref=stop_flag,
            wait_s=p["wait_s"],
            h_fit_mode=p["h_fit_mode"],
            v_fit_mode=p["v_fit_mode"],
        )
        worker.start()
        worker.wait()

        x_center = worker.x_result
        y_center = worker.y_result
        if x_center is None or y_center is None:
            return {"error": "Fit failed — could not locate feature center"}

        if set_origin:
            self.state.settings["x_original_feature_pos"] = x_center
            self.state.settings["y_original_feature_pos"] = y_center
            self.state.settings["x_found_feature_pos"]    = x_center
            self.state.settings["y_found_feature_pos"]    = y_center
            self.state.settings["x_offset"] = 0.0
            self.state.settings["y_offset"] = 0.0
            self.state.save()
            self.controller.log(f"Agent alignment origin set: X={x_center:.4f} Y={y_center:.4f}")
            return {"x_center": x_center, "y_center": y_center,
                    "x_offset": 0.0, "y_offset": 0.0, "origin_updated": True}
        else:
            orig_x = float(self.state.settings.get("x_original_feature_pos", x_center))
            orig_y = float(self.state.settings.get("y_original_feature_pos", y_center))
            dx = x_center - orig_x
            dy = y_center - orig_y
            self.state.settings["x_found_feature_pos"] = x_center
            self.state.settings["y_found_feature_pos"] = y_center
            self.state.settings["x_offset"] = dx
            self.state.settings["y_offset"] = dy
            self.state.save()
            self.controller.log(
                f"Agent alignment: X={x_center:.4f} Y={y_center:.4f} "
                f"offset=({dx:.4f},{dy:.4f})"
            )
            return {"x_center": x_center, "y_center": y_center,
                    "x_offset": dx, "y_offset": dy, "origin_updated": False}

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
