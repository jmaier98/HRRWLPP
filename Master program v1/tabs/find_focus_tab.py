# find_focus_tab.py
from __future__ import annotations
import os, time, math, numpy as np
from typing import Callable, Dict, Tuple, List, Optional
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QComboBox, QGroupBox
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from scipy.optimize import curve_fit


# ----------------------- Sigmoid Model and Fitting ----------------------- #
def sigmoid(x, x0, w, A, B):
    """Standard sigmoid: A + B / (1 + exp(-(x - x0) / w))"""
    return A + B / (1.0 + np.exp(-(np.asarray(x) - x0) / w))


def fit_sigmoid_and_extract_spot_size(x_data: np.ndarray, y_data: np.ndarray) -> Tuple[Optional[float], Optional[dict]]:
    """
    Fit a sigmoid to (x_data, y_data) and extract the beam spot size 
    as the width from 25% to 75% of the rise.
    
    Returns: (spot_size, fit_info_dict) or (None, None) if fit fails
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
        
        # Extract spot size: width from 25% to 75%
        # At 25%: A + 0.25*B = A + B / (1 + exp(-(x - x0) / w))
        # Solving: 0.25 = 1 / (1 + exp(-(x - x0) / w))
        # => 1 + exp(-(x - x0) / w) = 4
        # => exp(-(x - x0) / w) = 3
        # => -(x - x0) / w = ln(3)
        # => x - x0 = -w * ln(3)
        
        x_25 = x0 - w * np.log(3)    # 25% point
        x_75 = x0 + w * np.log(3)    # 75% point
        spot_size = x_75 - x_25
        
        fit_info = {
            'x0': x0,
            'w': w,
            'A': A,
            'B': B,
            'spot_size': spot_size,
            'x_25': x_25,
            'x_75': x_75,
        }
        
        return spot_size, fit_info
        
    except Exception as e:
        print(f"Sigmoid fit error: {e}")
        return None, None


# ----------------------------- Worker ---------------------------------- #
class _FindFocusWorker(QThread):
    """
    Runs a focus-finding scan on a background thread.
    
    Emits:
      progress: (stage, z_height, spot_size, trace_x, trace_y, fitted_y)
      height_done: (z_height, spot_size)
      stage_complete: (stage_name)
      finished: (user_abort, message)
    """
    progress = pyqtSignal(str, float, object, object, object, object)
    height_done = pyqtSignal(float, object)
    stage_complete = pyqtSignal(str)
    finished = pyqtSignal(bool, str)
    
    def __init__(
        self,
        galvo_x_start: float,
        galvo_x_end: float,
        galvo_y: float,
        n_galvo_steps: int,
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
        self.galvo_x_start = galvo_x_start
        self.galvo_x_end = galvo_x_end
        self.galvo_y = galvo_y
        self.n_galvo_steps = max(2, int(n_galvo_steps))
        self.z_start = z_start
        self.z_rough_steps = max(1, int(z_rough_steps))
        self.z_rough_step_size = float(z_rough_step_size)
        self.z_fine_steps = max(1, int(z_fine_steps))
        self.z_fine_step_size = float(z_fine_step_size)
        self.set_z = set_z_fn
        self.set_galvo = set_galvo_fn
        self.measure = measure_fn
        self.stop_flag_ref = stop_flag_ref
        self.wait_s = wait_s
        
    def run(self):
        try:
            # Generate galvo X positions
            galvo_xs = np.linspace(self.galvo_x_start, self.galvo_x_end, self.n_galvo_steps)
            
            # =============== ROUGH STEPS ===============
            rough_results = []  # list of (z_height, spot_size)
            best_rough_idx = None
            best_rough_spot_size = np.inf
            
            for rough_idx in range(self.z_rough_steps):
                if self.stop_flag_ref[0]:
                    self.finished.emit(True, "Scan stopped by user (rough steps).")
                    return
                
                z_height = self.z_start + rough_idx * self.z_rough_step_size
                
                # Perform 1D galvo scan at this height
                spot_size, trace_x, trace_y, fitted_y = self._scan_at_height(
                    z_height, galvo_xs
                )
                
                rough_results.append((z_height, spot_size))
                self.height_done.emit(z_height, spot_size)
                self.progress.emit("rough", z_height, spot_size, trace_x, trace_y, fitted_y)
                
                # Track best rough step
                if spot_size is not None and spot_size < best_rough_spot_size:
                    best_rough_spot_size = spot_size
                    best_rough_idx = rough_idx
            
            self.stage_complete.emit("Rough Steps Complete")
            
            # Return to starting Z position to compensate for hysteresis before fine scan
            try:
                self.set_z(self.z_start)
                time.sleep(self.wait_s * 2)
            except Exception as e:
                self.finished.emit(True, f"Error returning to start position before fine scan: {e}")
                return
            
            if best_rough_idx is None:
                self.finished.emit(True, "No valid rough step data obtained.")
                return
            
            # =============== FINE STEPS ===============
            best_rough_z = self.z_start + best_rough_idx * self.z_rough_step_size
            
            # Generate fine scan positions around best rough step
            fine_z_offsets = np.linspace(
                -(self.z_fine_steps - 1) // 2 * self.z_fine_step_size,
                (self.z_fine_steps - 1) // 2 * self.z_fine_step_size,
                self.z_fine_steps
            )
            fine_z_heights = best_rough_z + fine_z_offsets
            
            fine_results = []  # list of (z_height, spot_size)
            best_fine_z = best_rough_z
            best_fine_spot_size = best_rough_spot_size
            
            for fine_z in fine_z_heights:
                if self.stop_flag_ref[0]:
                    self.finished.emit(True, "Scan stopped by user (fine steps).")
                    return
                
                # Perform 1D galvo scan at this height
                spot_size, trace_x, trace_y, fitted_y = self._scan_at_height(
                    fine_z, galvo_xs
                )
                
                fine_results.append((fine_z, spot_size))
                self.height_done.emit(fine_z, spot_size)
                self.progress.emit("fine", fine_z, spot_size, trace_x, trace_y, fitted_y)
                
                # Track best fine step
                if spot_size is not None and spot_size < best_fine_spot_size:
                    best_fine_spot_size = spot_size
                    best_fine_z = fine_z
            
            self.stage_complete.emit("Fine Steps Complete")
            
            # =============== GO TO BEST FOCUS ===============
            if self.stop_flag_ref[0]:
                self.finished.emit(True, "Scan stopped by user (before final focus).")
                return
            
            # Return to starting Z position to compensate for hysteresis before moving to optimal focus
            try:
                self.set_z(self.z_start)
                time.sleep(self.wait_s * 2)
            except Exception as e:
                self.finished.emit(True, f"Error returning to start position before optimal focus: {e}")
                return
            
            try:
                self.set_z(best_fine_z)
                time.sleep(self.wait_s * 2)  # Extra settle time
            except Exception as e:
                self.finished.emit(True, f"Error moving to best focus: {e}")
                return
            
            msg = f"Focus complete. Best Z: {best_fine_z:.4f} (spot size: {best_fine_spot_size:.4f})"
            self.finished.emit(False, msg)
            
        except Exception as e:
            self.finished.emit(True, f"Unexpected error: {e}")
    
    def _scan_at_height(self, z_height: float, galvo_xs: np.ndarray) -> Tuple[Optional[float], np.ndarray, np.ndarray, np.ndarray]:
        """
        Perform one 1D galvo scan at a fixed Z height.
        Returns: (spot_size, x_positions, measured_values, fitted_values)
        """
        try:
            # Move to Z height
            self.set_z(z_height)
            time.sleep(self.wait_s * 2)
            
            # Scan galvo X
            y_data = []
            for gx in galvo_xs:
                if self.stop_flag_ref[0]:
                    return None, np.array([]), np.array([]), np.array([])
                
                # Move galvo
                self.set_galvo(gx, self.galvo_y)
                time.sleep(self.wait_s)
                
                # Measure
                try:
                    val = self.measure()
                    y_data.append(float(val))
                except Exception as e:
                    print(f"Measurement error at galvo x={gx}: {e}")
                    y_data.append(np.nan)
            
            y_data = np.array(y_data, dtype=float)
            
            # Fit sigmoid and extract spot size
            spot_size, fit_info = fit_sigmoid_and_extract_spot_size(galvo_xs, y_data)
            
            # Generate fitted curve for plotting
            if fit_info is not None:
                x_fine = np.linspace(np.min(galvo_xs), np.max(galvo_xs), 100)
                fitted_y = sigmoid(x_fine, fit_info['x0'], fit_info['w'], fit_info['A'], fit_info['B'])
            else:
                x_fine = galvo_xs
                fitted_y = np.full_like(galvo_xs, np.nan)
            
            return spot_size, galvo_xs, y_data, fitted_y
            
        except Exception as e:
            print(f"Error in _scan_at_height: {e}")
            return None, np.array([]), np.array([]), np.array([])


# ----------------------------- Main Tab -------------------------------- #
class FindFocusTab(QWidget):
    """
    Tab for finding the optimal focus by scanning galvo across electrode edge
    and fitting sigmoid to extract beam spot size.
    """
    def __init__(self, instrument_manager, state=None):
        super().__init__()
        self.IM = instrument_manager
        self.state = state
        
        # Try to fetch instruments
        self.galvo = None
        self.lockin = None
        self.btt = None
        try:
            self.galvo = self.IM.get("Galvo")
        except Exception:
            pass
        try:
            self.lockin = self.IM.get("SR830")
        except Exception:
            pass
        try:
            self.btt = self.IM.get("BTT")
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
        
        # Galvo controls
        self.galvo_x_start = QLineEdit("-0.5")
        self.galvo_x_start.setValidator(dv_norm)
        self.galvo_x_end = QLineEdit("0.5")
        self.galvo_x_end.setValidator(dv_norm)
        self.galvo_y = QLineEdit("0.0")
        self.galvo_y.setValidator(dv_norm)
        self.n_galvo_steps = QLineEdit("50")
        self.n_galvo_steps.setValidator(iv_pos)
        
        # Z height controls
        self.z_start = QLineEdit("0.0")
        self.z_start.setValidator(dv)
        
        # Rough step controls
        self.z_rough_steps = QLineEdit("5")
        self.z_rough_steps.setValidator(iv_pos)
        self.z_rough_step_size = QLineEdit("0.1")
        self.z_rough_step_size.setValidator(dv)
        
        # Fine step controls
        self.z_fine_steps = QLineEdit("5")
        self.z_fine_steps.setValidator(iv_pos)
        self.z_fine_step_size = QLineEdit("0.02")
        self.z_fine_step_size.setValidator(dv)
        
        # Wait time
        self.wait_ms = QLineEdit("50")
        self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))
        
        # Form layout
        form.addRow("Galvo X start:", self.galvo_x_start)
        form.addRow("Galvo X end:", self.galvo_x_end)
        form.addRow("Galvo Y:", self.galvo_y)
        form.addRow("# Galvo steps:", self.n_galvo_steps)
        form.addRow("", QLabel(""))  # spacer
        form.addRow("Z start height:", self.z_start)
        form.addRow("# Rough steps:", self.z_rough_steps)
        form.addRow("Rough step size:", self.z_rough_step_size)
        form.addRow("# Fine steps:", self.z_fine_steps)
        form.addRow("Fine step size:", self.z_fine_step_size)
        form.addRow("Wait (ms):", self.wait_ms)
        
        # Buttons
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
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
        
        # Top plot: Current scan trace with sigmoid fit
        self.fig_top = Figure(figsize=(6, 3), tight_layout=True)
        self.ax_top = self.fig_top.add_subplot(111)
        self.ax_top.set_title("Current Scan (Galvo X vs Lockin)")
        self.ax_top.set_xlabel("Galvo X")
        self.ax_top.set_ylabel("Lockin X")
        self.canvas_top = FigureCanvas(self.fig_top)
        
        # Bottom plot: Spot size vs height
        self.fig_bot = Figure(figsize=(6, 3), tight_layout=True)
        self.ax_bot = self.fig_bot.add_subplot(111)
        self.ax_bot.set_title("Beam Spot Size vs Z Height")
        self.ax_bot.set_xlabel("Z Height")
        self.ax_bot.set_ylabel("Spot Size (25%-75%)")
        self.canvas_bot = FigureCanvas(self.fig_bot)
        
        right.addWidget(self.canvas_top, stretch=1)
        right.addWidget(self.canvas_bot, stretch=1)
        
        outer.addLayout(ctrl_box, stretch=1)
        outer.addLayout(right, stretch=3)
        
        # Runtime state
        self._stop_flag = [False]
        self._worker: _FindFocusWorker | None = None
        self._heights: list[float] = []
        self._spot_sizes: list[Optional[float]] = []
        self._stages: list[str] = []
        self._current_stage = "rough"
        
        # Signals
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
    
    def _start(self):
        """Start the focus-finding scan."""
        try:
            galvo_x_start = float(self.galvo_x_start.text())
            galvo_x_end = float(self.galvo_x_end.text())
            galvo_y = float(self.galvo_y.text())
            n_galvo_steps = int(self.n_galvo_steps.text())
            z_start = float(self.z_start.text())
            z_rough_steps = int(self.z_rough_steps.text())
            z_rough_step_size = float(self.z_rough_step_size.text())
            z_fine_steps = int(self.z_fine_steps.text())
            z_fine_step_size = float(self.z_fine_step_size.text())
            wait_ms = int(self.wait_ms.text())
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Invalid input values.")
            return
        
        if not self.galvo or not self.lockin or not self.btt:
            QMessageBox.critical(self, "Setup Error", "Galvo, Lockin, or BTT not available.")
            return
        
        # Clear plots
        self.ax_top.clear()
        self.ax_top.set_title("Current Scan (Galvo X vs Lockin)")
        self.ax_top.set_xlabel("Galvo X")
        self.ax_top.set_ylabel("Lockin X")
        self.canvas_top.draw_idle()
        
        self.ax_bot.clear()
        self.ax_bot.set_title("Beam Spot Size vs Z Height")
        self.ax_bot.set_xlabel("Z Height")
        self.ax_bot.set_ylabel("Spot Size (25%-75%)")
        self.canvas_bot.draw_idle()
        
        # Reset runtime state
        self._stop_flag[0] = False
        self._heights.clear()
        self._spot_sizes.clear()
        self._stages.clear()
        self._current_stage = "rough"
        self.info_lbl.setText("")
        
        # Define set_z and set_galvo functions
        def set_z(z_val):
            self.btt.focus(z_val)
        
        def set_galvo(x, y):
            self.galvo.move(x, y)
        
        def measure():
            return self.lockin.read_PD()
        
        # Start worker
        self._worker = _FindFocusWorker(
            galvo_x_start=galvo_x_start,
            galvo_x_end=galvo_x_end,
            galvo_y=galvo_y,
            n_galvo_steps=n_galvo_steps,
            z_start=z_start,
            z_rough_steps=z_rough_steps,
            z_rough_step_size=z_rough_step_size,
            z_fine_steps=z_fine_steps,
            z_fine_step_size=z_fine_step_size,
            set_z_fn=set_z,
            set_galvo_fn=set_galvo,
            measure_fn=measure,
            stop_flag_ref=self._stop_flag,
            wait_s=wait_ms / 1000.0,
        )
        
        self._worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker.height_done.connect(self._on_height_done, Qt.ConnectionType.QueuedConnection)
        self._worker.stage_complete.connect(self._on_stage_complete, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._worker.start()
        
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Scanning…")
    
    def _stop(self):
        """Stop the current scan."""
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText("Stopping…")
    
    def _on_progress(self, stage: str, z_height: float, spot_size: Optional[float], 
                      trace_x: np.ndarray, trace_y: np.ndarray, fitted_y: np.ndarray):
        """Update on each height scan completion."""
        # Update top plot: current scan with fit
        self.ax_top.clear()
        self.ax_top.set_title(f"Current Scan - Z={z_height:.4f}, Stage={stage}")
        self.ax_top.set_xlabel("Galvo X")
        self.ax_top.set_ylabel("Lockin X")
        self.ax_top.plot(trace_x, trace_y, 'o-', label="Measured", alpha=0.7)
        if len(fitted_y) > 0 and not np.all(np.isnan(fitted_y)):
            x_fine = np.linspace(np.min(trace_x), np.max(trace_x), len(fitted_y))
            self.ax_top.plot(x_fine, fitted_y, 'r--', label="Sigmoid fit", linewidth=2)
        self.ax_top.legend()
        self.ax_top.grid(True, alpha=0.2)
        self.canvas_top.draw_idle()
    
    def _on_height_done(self, z_height: float, spot_size: Optional[float]):
        """Record completed height scan."""
        self._heights.append(z_height)
        self._spot_sizes.append(spot_size)
        self._stages.append(self._current_stage)
        
        # Update bottom plot
        self.ax_bot.clear()
        self.ax_bot.set_title("Beam Spot Size vs Z Height")
        self.ax_bot.set_xlabel("Z Height")
        self.ax_bot.set_ylabel("Spot Size (25%-75%)")
        
        valid_idx = [i for i, ss in enumerate(self._spot_sizes) if ss is not None]
        if valid_idx:
            valid_heights = np.array([self._heights[i] for i in valid_idx])
            valid_sizes = np.array([self._spot_sizes[i] for i in valid_idx])
            valid_stages = [self._stages[i] for i in valid_idx]
            
            # Plot rough and fine separately
            rough_idx = [j for j, s in enumerate(valid_stages) if s == "rough"]
            fine_idx = [j for j, s in enumerate(valid_stages) if s == "fine"]
            
            if rough_idx:
                self.ax_bot.plot(valid_heights[rough_idx], valid_sizes[rough_idx], 'bo-', linewidth=2, markersize=6, label="Rough")
            if fine_idx:
                self.ax_bot.plot(valid_heights[fine_idx], valid_sizes[fine_idx], 'ro-', linewidth=2, markersize=6, label="Fine")
            
            # Mark best
            best_idx = np.argmin(valid_sizes)
            best_height = valid_heights[best_idx]
            best_size = valid_sizes[best_idx]
            self.ax_bot.plot(best_height, best_size, 'k*', markersize=20, label="Best")
            self.ax_bot.legend()
        
        self.ax_bot.grid(True, alpha=0.2)
        self.canvas_bot.draw_idle()
    
    def _on_stage_complete(self, stage_name: str):
        """Update when a stage (rough or fine) completes."""
        if "Rough" in stage_name:
            self._current_stage = "fine"
        self.status_lbl.setText(f"{stage_name}")
    
    def _on_finished(self, user_abort: bool, message: str):
        """Finalize when scan completes."""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self.info_lbl.setText(message)
        self._worker = None
    
    def closeEvent(self, event):
        """Clean shutdown."""
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
