# alignment_tab.py
from __future__ import annotations
import os, time, math, numpy as np
from typing import Callable, Dict, Tuple, List, Optional
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QComboBox, QCheckBox
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from scipy.optimize import curve_fit


# ----------------------- Sigmoid Model and Fitting ----------------------- #
def sigmoid(x, x0, w, A, B):
    """Standard sigmoid: A + B / (1 + exp(-(x - x0) / w))"""
    return A + B / (1.0 + np.exp(-(np.asarray(x) - x0) / w))


def double_sigmoid(x, x1, x2, w, A, B):
    """
    Hat (double sigmoid): rises at x1, falls at x2.
    f(x) = A + B * [sig(x, x1, w) - sig(x, x2, w)]
    Center = (x1 + x2) / 2. B can be negative for a dip.
    Assumes x1 < x2.
    """
    x = np.asarray(x)
    return A + B * (1.0 / (1.0 + np.exp(-(x - x1) / w)) - 1.0 / (1.0 + np.exp(-(x - x2) / w)))


def fit_sigmoid_and_extract_center(x_data: np.ndarray, y_data: np.ndarray) -> Tuple[Optional[float], Optional[dict]]:
    """
    Fit a sigmoid to (x_data, y_data) and extract the center position (x0).

    Returns: (center_position, fit_info_dict) or (None, None) if fit fails
    """
    try:
        # Remove NaN values
        valid = np.isfinite(y_data)
        if np.sum(valid) < 4:
            return None, None

        x_clean = x_data[valid]
        y_clean = y_data[valid]

        # Initial parameter guess
        y_min, y_max = np.min(y_clean), np.max(y_clean)
        x_mid = np.mean(x_clean)
        width_guess = (np.max(x_clean) - np.min(x_clean)) / 4.0

        # Bounds for fitting
        p0 = [x_mid, width_guess, y_min, y_max - y_min]
        bounds = (
            [np.min(x_clean) - width_guess, 1e-6, -np.inf, -np.inf],
            [np.max(x_clean) + width_guess, (np.max(x_clean) - np.min(x_clean)) * 2, np.inf, np.inf]
        )

        # Fit
        popt, pcov = curve_fit(sigmoid, x_clean, y_clean, p0=p0, bounds=bounds, maxfev=5000)
        x0, w, A, B = popt

        fit_info = {
            'model': 'sigmoid',
            'x0': x0,
            'w': w,
            'A': A,
            'B': B,
        }

        return x0, fit_info

    except Exception as e:
        print(f"Sigmoid fit error: {e}")
        return None, None


def fit_double_sigmoid_and_extract_center(x_data: np.ndarray, y_data: np.ndarray) -> Tuple[Optional[float], Optional[dict]]:
    """
    Fit a double sigmoid (hat shape) to (x_data, y_data).
    Center = midpoint between the two inflection points (x1 + x2) / 2.

    Returns: (center_position, fit_info_dict) or (None, None) if fit fails
    """
    try:
        valid = np.isfinite(y_data)
        if np.sum(valid) < 5:
            return None, None

        x_clean = x_data[valid]
        y_clean = y_data[valid]

        span = np.max(x_clean) - np.min(x_clean)
        y_min, y_max = np.min(y_clean), np.max(y_clean)

        # Estimate center from peak (works for both hat and dip)
        peak_idx = np.argmax(np.abs(y_clean - np.mean(y_clean)))
        x_peak = x_clean[peak_idx]

        # Initial guesses: x1 and x2 symmetric around peak, ~1/4 span apart each
        half_width_guess = span / 8.0
        x1_guess = x_peak - half_width_guess
        x2_guess = x_peak + half_width_guess
        w_guess = max(span / 20.0, 1e-6)  # sharp transitions

        A_guess = y_min
        B_guess = y_max - y_min  # positive for hat; allow negative via bounds

        p0 = [x1_guess, x2_guess, w_guess, A_guess, B_guess]
        x_lo, x_hi = np.min(x_clean), np.max(x_clean)
        bounds = (
            [x_lo - span * 0.5, x_lo - span * 0.5, 1e-9, -np.inf, -np.inf],
            [x_hi + span * 0.5, x_hi + span * 0.5, span,  np.inf,  np.inf],
        )

        popt, _ = curve_fit(double_sigmoid, x_clean, y_clean, p0=p0, bounds=bounds, maxfev=10000)
        x1, x2, w, A, B = popt

        # Enforce x1 <= x2 (swap if optimizer flipped them)
        if x1 > x2:
            x1, x2 = x2, x1

        center = (x1 + x2) / 2.0

        fit_info = {
            'model': 'double_sigmoid',
            'x1': x1,
            'x2': x2,
            'w': w,
            'A': A,
            'B': B,
        }

        return center, fit_info

    except Exception as e:
        print(f"Double sigmoid fit error: {e}")
        return None, None


# ----------------------------- Worker ---------------------------------- #
class _AlignmentWorker(QThread):
    """
    Runs feature alignment scans on a background thread.
    
    Emits:
      progress: (direction, center_pos, trace_x, trace_y, fitted_y)
      finished: (user_abort, message)
    """
    progress = pyqtSignal(str, object, object, object, object)
    finished = pyqtSignal(bool, str)
    
    def __init__(
        self,
        start_x: float,
        start_y: float,
        horiz_distance: float,
        vert_distance: float,
        n_horiz_points: int,
        n_vert_points: int,
        horiz_left_to_right: bool,
        vert_bottom_to_top: bool,
        set_galvo_fn: Callable[[float, float], None],
        measure_fn: Callable[[], float],
        stop_flag_ref: List[bool],
        wait_s: float = 0.05,
        fit_mode: str = "sigmoid",  # "sigmoid" or "double_sigmoid"
    ):
        super().__init__()
        self.start_x = start_x
        self.start_y = start_y
        self.horiz_distance = horiz_distance
        self.vert_distance = vert_distance
        self.n_horiz_points = max(2, int(n_horiz_points))
        self.n_vert_points = max(2, int(n_vert_points))
        self.horiz_left_to_right = horiz_left_to_right
        self.vert_bottom_to_top = vert_bottom_to_top
        self.set_galvo = set_galvo_fn
        self.measure = measure_fn
        self.stop_flag_ref = stop_flag_ref
        self.wait_s = wait_s
        self.fit_mode = fit_mode
        
    def run(self):
        try:
            # =============== HORIZONTAL SCAN ===============
            if self.stop_flag_ref[0]:
                self.finished.emit(True, "Scan stopped by user.")
                return
            
            # Generate horizontal scan positions
            if self.horiz_left_to_right:
                x_start = self.start_x
                x_end = self.start_x + self.horiz_distance
            else:
                x_start = self.start_x
                x_end = self.start_x - self.horiz_distance
            
            horiz_xs = np.linspace(x_start, x_end, self.n_horiz_points)
            
            # Perform horizontal scan
            x_center, trace_x, trace_y, fitted_y = self._scan_along_axis(
                horiz_xs, self.start_y, "horizontal"
            )
            
            self.progress.emit("horizontal", x_center, trace_x, trace_y, fitted_y)
            
            # Return to start position
            self.set_galvo(self.start_x, self.start_y)
            time.sleep(self.wait_s * 2)
            
            # =============== VERTICAL SCAN ===============
            if self.stop_flag_ref[0]:
                self.finished.emit(True, "Scan stopped by user.")
                return
            
            # Generate vertical scan positions
            if self.vert_bottom_to_top:
                y_start = self.start_y
                y_end = self.start_y + self.vert_distance
            else:
                y_start = self.start_y
                y_end = self.start_y - self.vert_distance
            
            vert_ys = np.linspace(y_start, y_end, self.n_vert_points)
            
            # Perform vertical scan
            y_center, trace_y_pos, trace_y_vals, fitted_y_curve = self._scan_along_axis(
                vert_ys, self.start_x, "vertical"
            )
            
            self.progress.emit("vertical", y_center, trace_y_pos, trace_y_vals, fitted_y_curve)
            
            # Return to start position
            self.set_galvo(self.start_x, self.start_y)
            time.sleep(self.wait_s * 2)
            
            msg = f"Alignment complete. X center: {x_center:.4f}, Y center: {y_center:.4f}"
            self.finished.emit(False, msg)
            self.finished_message = msg  # Store for sync access
            
        except Exception as e:
            error_msg = f"Unexpected error: {e}"
            self.finished.emit(True, error_msg)
            self.finished_message = error_msg
    
    def run_sync(self) -> Tuple[bool, str]:
        """
        Run the alignment synchronously and return (success, message).
        This is used by the synchronous find_feature_sync method.
        """
        try:
            # =============== HORIZONTAL SCAN ===============
            if self.stop_flag_ref[0]:
                return True, "Scan stopped by user."
            
            # Generate horizontal scan positions
            if self.horiz_left_to_right:
                x_start = self.start_x
                x_end = self.start_x + self.horiz_distance
            else:
                x_start = self.start_x
                x_end = self.start_x - self.horiz_distance
            
            horiz_xs = np.linspace(x_start, x_end, self.n_horiz_points)
            
            # Perform horizontal scan
            x_center, trace_x, trace_y, fitted_y = self._scan_along_axis(
                horiz_xs, self.start_y, "horizontal"
            )
            
            self.progress.emit("horizontal", x_center, trace_x, trace_y, fitted_y)
            
            # Return to start position
            self.set_galvo(self.start_x, self.start_y)
            time.sleep(self.wait_s * 2)
            
            # =============== VERTICAL SCAN ===============
            if self.stop_flag_ref[0]:
                return True, "Scan stopped by user."
            
            # Generate vertical scan positions
            if self.vert_bottom_to_top:
                y_start = self.start_y
                y_end = self.start_y + self.vert_distance
            else:
                y_start = self.start_y
                y_end = self.start_y - self.vert_distance
            
            vert_ys = np.linspace(y_start, y_end, self.n_vert_points)
            
            # Perform vertical scan
            y_center, trace_y_pos, trace_y_vals, fitted_y_curve = self._scan_along_axis(
                vert_ys, self.start_x, "vertical"
            )
            
            self.progress.emit("vertical", y_center, trace_y_pos, trace_y_vals, fitted_y_curve)
            
            # Return to start position
            self.set_galvo(self.start_x, self.start_y)
            time.sleep(self.wait_s * 2)
            
            msg = f"Alignment complete. X center: {x_center:.4f}, Y center: {y_center:.4f}"
            return True, msg
            
        except Exception as e:
            error_msg = f"Unexpected error: {e}"
            return False, error_msg
    
    def _scan_along_axis(self, positions: np.ndarray, fixed_coord: float, direction: str) -> Tuple[Optional[float], np.ndarray, np.ndarray, np.ndarray]:
        """
        Perform one 1D scan along an axis.
        Returns: (center_position, positions, measured_values, fitted_values)
        """
        try:
            # Scan along the axis
            y_data = []
            for pos in positions:
                if self.stop_flag_ref[0]:
                    return None, np.array([]), np.array([]), np.array([])
                
                # Move galvo
                if direction == "horizontal":
                    self.set_galvo(pos, fixed_coord)
                else:  # vertical
                    self.set_galvo(fixed_coord, pos)
                time.sleep(self.wait_s)
                
                # Measure
                try:
                    val = self.measure()
                    y_data.append(float(val))
                except Exception as e:
                    print(f"Measurement error at position {pos}: {e}")
                    y_data.append(np.nan)
            
            y_data = np.array(y_data, dtype=float)

            # Fit and extract center
            pos_fine = np.linspace(np.min(positions), np.max(positions), 200)
            if self.fit_mode == "double_sigmoid":
                center_pos, fit_info = fit_double_sigmoid_and_extract_center(positions, y_data)
                if fit_info is not None:
                    fitted_y = double_sigmoid(pos_fine, fit_info['x1'], fit_info['x2'],
                                              fit_info['w'], fit_info['A'], fit_info['B'])
                else:
                    fitted_y = np.full_like(pos_fine, np.nan)
            else:
                center_pos, fit_info = fit_sigmoid_and_extract_center(positions, y_data)
                if fit_info is not None:
                    fitted_y = sigmoid(pos_fine, fit_info['x0'], fit_info['w'],
                                       fit_info['A'], fit_info['B'])
                else:
                    fitted_y = np.full_like(pos_fine, np.nan)

            return center_pos, positions, y_data, fitted_y
            
        except Exception as e:
            print(f"Error in _scan_along_axis: {e}")
            return None, np.array([]), np.array([]), np.array([])


# ----------------------------- Main Tab -------------------------------- #
class AlignmentTab(QWidget):
    """
    Tab for feature alignment by scanning horizontal and vertical lines
    and fitting sigmoids to extract feature center positions using lock-in measurements.
    """
    def __init__(self, instrument_manager, state=None):
        super().__init__()
        self.IM = instrument_manager
        self.state = state
        
        # Try to fetch instruments
        self.galvo = None
        self.lockin = None
        try:
            self.galvo = self.IM.get("Galvo")
        except Exception:
            pass
        try:
            self.lockin = self.IM.get("SR830")
        except Exception:
            pass
        
        # UI Setup
        outer = QHBoxLayout(self)
        
        # Left: Controls
        ctrl_box = QVBoxLayout()
        form = QFormLayout()
        
        dv = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_norm = QDoubleValidator(-1.0, 1.0, 9, self)
        iv_pos = QIntValidator(1, 10_000_000, self)
        
        # Starting position
        self.start_x = QLineEdit("0.0")
        self.start_x.setValidator(dv_norm)
        self.start_y = QLineEdit("0.0")
        self.start_y.setValidator(dv_norm)
        
        # Scan distances
        self.horiz_distance = QLineEdit("0.1")
        self.horiz_distance.setValidator(dv)
        self.vert_distance = QLineEdit("0.1")
        self.vert_distance.setValidator(dv)
        
        # Number of points
        self.n_horiz_points = QLineEdit("50")
        self.n_horiz_points.setValidator(iv_pos)
        self.n_vert_points = QLineEdit("50")
        self.n_vert_points.setValidator(iv_pos)
        
        # Direction toggles
        self.horiz_left_to_right = QCheckBox("Horizontal: Left to Right")
        self.horiz_left_to_right.setChecked(True)
        self.vert_bottom_to_top = QCheckBox("Vertical: Bottom to Top")
        self.vert_bottom_to_top.setChecked(True)
        
        # Wait time
        self.wait_ms = QLineEdit("50")
        self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))

        # Fit mode selector
        self.fit_mode_combo = QComboBox()
        self.fit_mode_combo.addItem("Single Sigmoid (edge)", "sigmoid")
        self.fit_mode_combo.addItem("Double Sigmoid / Hat (thin electrode)", "double_sigmoid")

        # Form layout
        form.addRow("Start X (galvo):", self.start_x)
        form.addRow("Start Y (galvo):", self.start_y)
        form.addRow("", QLabel(""))  # spacer
        form.addRow("Horizontal distance:", self.horiz_distance)
        form.addRow("Vertical distance:", self.vert_distance)
        form.addRow("", QLabel(""))  # spacer
        form.addRow("# Horizontal points:", self.n_horiz_points)
        form.addRow("# Vertical points:", self.n_vert_points)
        form.addRow("", QLabel(""))  # spacer
        form.addRow(self.horiz_left_to_right)
        form.addRow(self.vert_bottom_to_top)
        form.addRow("", QLabel(""))  # spacer
        form.addRow("Wait (ms):", self.wait_ms)
        form.addRow("", QLabel(""))  # spacer
        form.addRow("Fit mode:", self.fit_mode_combo)
        
        # Buttons
        btn_row = QHBoxLayout()
        self.set_original_btn = QPushButton("Set Original Feature Pos")
        self.find_feature_btn = QPushButton("Find Feature Pos")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.set_original_btn)
        btn_row.addWidget(self.find_feature_btn)
        btn_row.addWidget(self.stop_btn)
        
        # Status
        self.status_lbl = QLabel("Idle.")
        self.info_lbl = QLabel("")
        
        ctrl_box.addLayout(form)
        ctrl_box.addLayout(btn_row)
        ctrl_box.addWidget(self.status_lbl)
        ctrl_box.addWidget(self.info_lbl)
        ctrl_box.addStretch(1)
        
        # Right: Plots
        right = QVBoxLayout()
        
        # Top plot: Horizontal scan
        self.fig_top = Figure(figsize=(8, 4), tight_layout=True)
        self.ax_top = self.fig_top.add_subplot(111)
        self.ax_top.set_title("Horizontal Scan")
        self.ax_top.set_xlabel("Galvo X")
        self.ax_top.set_ylabel("Lock-in X")
        self.canvas_top = FigureCanvas(self.fig_top)
        
        # Bottom plot: Vertical scan
        self.fig_bottom = Figure(figsize=(8, 4), tight_layout=True)
        self.ax_bottom = self.fig_bottom.add_subplot(111)
        self.ax_bottom.set_title("Vertical Scan")
        self.ax_bottom.set_xlabel("Galvo Y")
        self.ax_bottom.set_ylabel("Lock-in X")
        self.canvas_bottom = FigureCanvas(self.fig_bottom)
        
        right.addWidget(self.canvas_top, stretch=1)
        right.addWidget(self.canvas_bottom, stretch=1)
        
        outer.addLayout(ctrl_box, stretch=1)
        outer.addLayout(right, stretch=3)
        
        # Runtime state
        self._stop_flag = [False]
        self._worker: _AlignmentWorker | None = None
        self._horizontal_data = None  # (center_pos, trace_pos, trace_vals, fitted_vals)
        self._vertical_data = None    # (center_pos, trace_pos, trace_vals, fitted_vals)
        self._current_start_x = 0.0   # Store start position for state updates
        self._current_start_y = 0.0   # Store start position for state updates
        self._update_original = False # Store whether this is an original or find scan
        
        # Signals
        self.set_original_btn.clicked.connect(self._set_original)
        self.find_feature_btn.clicked.connect(self._find_feature)
        self.stop_btn.clicked.connect(self._stop)
    
    def _set_original(self):
        """Set the original feature position."""
        self._run_alignment_scan(update_original=True)
    
    def _find_feature(self):
        """Find the current feature position."""
        try:
            start_x = float(self.start_x.text())
            start_y = float(self.start_y.text())
            horiz_distance = float(self.horiz_distance.text())
            vert_distance = float(self.vert_distance.text())
            n_horiz_points = int(self.n_horiz_points.text())
            n_vert_points = int(self.n_vert_points.text())
            horiz_left_to_right = self.horiz_left_to_right.isChecked()
            vert_bottom_to_top = self.vert_bottom_to_top.isChecked()
            wait_ms = int(self.wait_ms.text())
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Invalid input values.")
            return
        
        if not self.galvo or not self.lockin:
            QMessageBox.critical(self, "Setup Error", "Galvo or Lock-in not available.")
            return
        
        # Clear plots
        self.ax_top.clear()
        self.ax_top.set_title("Horizontal Scan")
        self.ax_top.set_xlabel("Galvo X")
        self.ax_top.set_ylabel("Lock-in X")
        self.canvas_top.draw_idle()
        
        self.ax_bottom.clear()
        self.ax_bottom.set_title("Vertical Scan")
        self.ax_bottom.set_xlabel("Galvo Y")
        self.ax_bottom.set_ylabel("Lock-in X")
        self.canvas_bottom.draw_idle()
        
        # Reset runtime state
        self._stop_flag[0] = False
        self._horizontal_data = None
        self._vertical_data = None
        self._current_start_x = start_x
        self._current_start_y = start_y
        self._update_original = False  # Always false for find feature
        
        # Define functions
        def set_galvo(x, y):
            self.galvo.move(x, y)
        
        def measure():
            return self.lockin.read_PD()
        
        # Create worker
        self._worker = _AlignmentWorker(
            start_x=start_x,
            start_y=start_y,
            horiz_distance=horiz_distance,
            vert_distance=vert_distance,
            n_horiz_points=n_horiz_points,
            n_vert_points=n_vert_points,
            horiz_left_to_right=horiz_left_to_right,
            vert_bottom_to_top=vert_bottom_to_top,
            set_galvo_fn=set_galvo,
            measure_fn=measure,
            stop_flag_ref=self._stop_flag,
            wait_s=wait_ms / 1000.0,
            fit_mode=self.fit_mode_combo.currentData(),
        )

        # Disable buttons during scan
        self.set_original_btn.setEnabled(False)
        self.find_feature_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")

        # Connect progress signals for real-time plot updates
        self._worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)

        # Run synchronously
        success, message = self._worker.run_sync()
        
        # Disconnect progress signal
        self._worker.progress.disconnect(self._on_progress)
        
        # Set offsets in state variables if scan completed successfully
        if success and message.startswith("Alignment complete"):
            try:
                msg_parts = message.split()
                if len(msg_parts) >= 8:
                    x_center = float(msg_parts[4].rstrip(','))
                    y_center = float(msg_parts[7])
                    
                    # Update found positions and calculate offsets
                    self.state.settings["x_found_feature_pos"] = x_center
                    self.state.settings["y_found_feature_pos"] = y_center
                    
                    # Calculate offsets (found - original)
                    orig_x = self.state.settings.get("x_original_feature_pos", 0.0)
                    orig_y = self.state.settings.get("y_original_feature_pos", 0.0)
                    self.state.settings["x_offset"] = x_center - orig_x
                    self.state.settings["y_offset"] = y_center - orig_y
                    
                    self.state.save()
            except (ValueError, IndexError) as e:
                print(f"Error parsing alignment result: {e}")
        
        # Update UI (same as _on_finished would do)
        self._on_finished(success, message, False)
    
    def _run_alignment_scan(self, update_original: bool):
        """Run the alignment scan."""
        try:
            start_x = float(self.start_x.text())
            start_y = float(self.start_y.text())
            horiz_distance = float(self.horiz_distance.text())
            vert_distance = float(self.vert_distance.text())
            n_horiz_points = int(self.n_horiz_points.text())
            n_vert_points = int(self.n_vert_points.text())
            horiz_left_to_right = self.horiz_left_to_right.isChecked()
            vert_bottom_to_top = self.vert_bottom_to_top.isChecked()
            wait_ms = int(self.wait_ms.text())
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Invalid input values.")
            return
        
        if not self.galvo or not self.lockin:
            QMessageBox.critical(self, "Setup Error", "Galvo or Lock-in not available.")
            return
        
        # Clear plots
        self.ax_top.clear()
        self.ax_top.set_title("Horizontal Scan")
        self.ax_top.set_xlabel("Galvo X")
        self.ax_top.set_ylabel("Lock-in X")
        self.canvas_top.draw_idle()
        
        self.ax_bottom.clear()
        self.ax_bottom.set_title("Vertical Scan")
        self.ax_bottom.set_xlabel("Galvo Y")
        self.ax_bottom.set_ylabel("Lock-in X")
        self.canvas_bottom.draw_idle()
        
        # Reset runtime state
        self._stop_flag[0] = False
        self._horizontal_data = None
        self._vertical_data = None
        self._current_start_x = start_x
        self._current_start_y = start_y
        self._update_original = update_original
        self.info_lbl.setText("")
        
        # Define functions
        def set_galvo(x, y):
            self.galvo.move(x, y)
        
        def measure():
            return self.lockin.read_PD()
        
        # Start worker
        self._worker = _AlignmentWorker(
            start_x=start_x,
            start_y=start_y,
            horiz_distance=horiz_distance,
            vert_distance=vert_distance,
            n_horiz_points=n_horiz_points,
            n_vert_points=n_vert_points,
            horiz_left_to_right=horiz_left_to_right,
            vert_bottom_to_top=vert_bottom_to_top,
            set_galvo_fn=set_galvo,
            measure_fn=measure,
            stop_flag_ref=self._stop_flag,
            wait_s=wait_ms / 1000.0,
            fit_mode=self.fit_mode_combo.currentData(),
        )
        
        self._worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(lambda abort, msg: self._on_finished(abort, msg, update_original), Qt.ConnectionType.QueuedConnection)
        self._worker.start()
        
        self.set_original_btn.setEnabled(False)
        self.find_feature_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")
    
    def _stop(self):
        """Stop the current scan."""
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText("Stopping…")
    
    def _on_progress(self, direction: str, center_pos: Optional[float], 
                      trace_pos: np.ndarray, trace_vals: np.ndarray, fitted_vals: np.ndarray):
        """Update the appropriate plot with scan data."""
        # Store the data
        scan_data = (center_pos, trace_pos, trace_vals, fitted_vals)
        if direction == "horizontal":
            self._horizontal_data = scan_data
            self._update_horizontal_plot()
        else:  # vertical
            self._vertical_data = scan_data
            self._update_vertical_plot()
    
    def _fit_label(self):
        mode = self.fit_mode_combo.currentData()
        return "Hat fit" if mode == "double_sigmoid" else "Sigmoid fit"

    def _plot_scan(self, ax, canvas, x_label, title_base, center_pos, trace_pos, trace_vals, fitted_vals):
        """Shared helper for rendering a scan result onto an axis."""
        ax.clear()
        title = f"{title_base} - Center: {center_pos:.4f}" if center_pos is not None else title_base
        ax.set_title(title)
        ax.set_xlabel(x_label)
        ax.set_ylabel("Lock-in X")

        if len(trace_pos) > 0 and len(trace_vals) > 0:
            ax.plot(trace_pos, trace_vals, 'o-', label="Measured", alpha=0.7)

        if len(fitted_vals) > 0 and not np.all(np.isnan(fitted_vals)):
            pos_fine = np.linspace(np.min(trace_pos), np.max(trace_pos), len(fitted_vals))
            ax.plot(pos_fine, fitted_vals, 'r--', label=self._fit_label(), linewidth=2)

            if center_pos is not None:
                # Mark center at the interpolated fit value
                y_at_center = np.interp(center_pos, pos_fine, fitted_vals)
                ax.axvline(center_pos, color='g', linestyle=':', linewidth=1.5)
                ax.plot(center_pos, y_at_center, 'g*', markersize=15, label="Center")

        ax.legend()
        ax.grid(True, alpha=0.2)
        canvas.draw_idle()

    def _update_horizontal_plot(self):
        """Update the horizontal scan plot."""
        if self._horizontal_data is None:
            return
        center_pos, trace_pos, trace_vals, fitted_vals = self._horizontal_data
        self._plot_scan(self.ax_top, self.canvas_top, "Galvo X", "Horizontal Scan",
                        center_pos, trace_pos, trace_vals, fitted_vals)

    def _update_vertical_plot(self):
        """Update the vertical scan plot."""
        if self._vertical_data is None:
            return
        center_pos, trace_pos, trace_vals, fitted_vals = self._vertical_data
        self._plot_scan(self.ax_bottom, self.canvas_bottom, "Galvo Y", "Vertical Scan",
                        center_pos, trace_pos, trace_vals, fitted_vals)
    
    def _on_finished(self, user_abort: bool, message: str, update_original: bool):
        """Finalize when scan completes."""
        self.set_original_btn.setEnabled(True)
        self.find_feature_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self.info_lbl.setText(message)
        
        # Update state variables if scan completed successfully
        if not user_abort and self._worker:
            # Parse the message to extract positions
            try:
                msg_parts = message.split()
                if len(msg_parts) >= 8:
                    x_center = float(msg_parts[4].rstrip(','))
                    y_center = float(msg_parts[7])
                    
                    if self._update_original:
                        # Set original feature pos: save start positions and set both original and found
                        self.state.settings["x_start_position"] = self._current_start_x
                        self.state.settings["y_start_position"] = self._current_start_y
                        self.state.settings["x_original_feature_pos"] = x_center
                        self.state.settings["y_original_feature_pos"] = y_center
                        self.state.settings["x_found_feature_pos"] = x_center
                        self.state.settings["y_found_feature_pos"] = y_center
                        # Reset offsets to 0 when setting original position
                        print("saving zeros")
                        self.state.settings["x_offset"] = 0.0
                        self.state.settings["y_offset"] = 0.0
                        self.state.save()
                        self.info_lbl.setText(f"Original position set: X={x_center:.4f}, Y={y_center:.4f}")
                    else:
                        # Find feature pos: only update found positions and calculate offsets
                        self.state.settings["x_found_feature_pos"] = x_center
                        self.state.settings["y_found_feature_pos"] = y_center
                        
                        # Calculate offsets (found - original)
                        orig_x = self.state.settings.get("x_original_feature_pos", 0.0)
                        orig_y = self.state.settings.get("y_original_feature_pos", 0.0)
                        self.state.settings["x_offset"] = x_center - orig_x
                        self.state.settings["y_offset"] = y_center - orig_y
                        print("saved offsets")
                        self.state.save()
                        offset_x = self.state.settings["x_offset"]
                        offset_y = self.state.settings["y_offset"]

                        self.info_lbl.setText(f"Found position: X={x_center:.4f}, Y={y_center:.4f} | Offset: X={offset_x:.4f}, Y={offset_y:.4f}")
            except (ValueError, IndexError) as e:
                print(f"Error parsing positions from message: {e}")
        
        self._worker = None
    
    def closeEvent(self, event):
        """Clean shutdown."""
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()