from __future__ import annotations
import os, time, math
from typing import Callable, Dict, Tuple, List, Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QComboBox,
    QGroupBox, QCheckBox
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


# =============================== Worker =============================== #
class _Scan2DWorker(QThread):
    """
    Runs a 2D scan on a background thread.

    Emits per-point progress:
        progress(ix, iy, x_val, y_val, data_tuple_of_len_6)

    Emits when a row (fast axis line) completes:
        line_done(iy)

    Emits when finished:
        finished(user_abort: bool, message: str)
    """
    progress = pyqtSignal(int, int, float, float, tuple)
    line_done = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(
        self,
        xs: np.ndarray,
        ys: np.ndarray,
        set_xy: Callable[[float, float], None],
        measure_fn: Callable[[], Tuple[float, ...]],
        wait_s: float,
        stop_flag_ref: List[bool],
        serpentine: bool,
        fast_axis: str,  # 'X' or 'Y'
        per_pixel_avg: int = 1,
    ):
        super().__init__()
        self._xs = np.asarray(xs, float)
        self._ys = np.asarray(ys, float)
        self._set_xy = set_xy
        self._measure = measure_fn
        self._wait_s = max(0.0, float(wait_s))
        self._stop_flag_ref = stop_flag_ref
        self._serp = bool(serpentine)
        self._fast = 'X' if str(fast_axis).upper().startswith('X') else 'Y'
        self._avgN = max(1, int(per_pixel_avg))

    def _iter_coords(self):
        # Arrange iteration order based on selected fast axis and serpentine
        if self._fast == 'X':
            xs, ys = self._xs, self._ys
            for iy, y in enumerate(ys):
                if self._serp and (iy % 2 == 1):
                    xrow = self._xs[::-1]
                    xidx = range(len(self._xs)-1, -1, -1)
                else:
                    xrow = self._xs
                    xidx = range(len(self._xs))
                for local_ix, (ix, x) in enumerate(zip(xidx, xrow)):
                    yield ix, iy, x, y
                self.line_done.emit(iy)
        else:
            # Y is fast
            ys, xs = self._ys, self._xs
            for ix, x in enumerate(xs):
                if self._serp and (ix % 2 == 1):
                    ycol = self._ys[::-1]
                    yidx = range(len(self._ys)-1, -1, -1)
                else:
                    ycol = self._ys
                    yidx = range(len(self._ys))
                for local_iy, (iy, y) in enumerate(zip(yidx, ycol)):
                    yield ix, iy, x, y
                self.line_done.emit(ix)

    def run(self):
        try:
            for ix, iy, x, y in self._iter_coords():
                if self._stop_flag_ref[0]:
                    self.finished.emit(True, 'Scan stopped by user.')
                    return

                try:
                    self._set_xy(float(x), float(y))
                except Exception as e:
                    self.finished.emit(True, f'Axis move error at (x={x}, y={y}): {e}')
                    return

                if self._wait_s > 0:
                    time.sleep(self._wait_s)

                # Per-pixel averaging
                accum = np.zeros(6, float)
                for _ in range(self._avgN):
                    try:
                        d = self._measure()
                    except Exception as e:
                        self.finished.emit(True, f'Measurement error at (x={x}, y={y}): {e}')
                        return
                    if not isinstance(d, (list, tuple)):
                        d = (float(d),)
                    d6 = np.array([(float(v) if math.isfinite(float(v)) else np.nan) for v in (list(d) + [math.nan]*6)[:6]], float)
                    accum += np.nan_to_num(d6, nan=0.0)
                d6 = tuple(accum / float(self._avgN))

                self.progress.emit(int(ix), int(iy), float(x), float(y), d6)

            self.finished.emit(False, '2D scan complete.')
        except Exception as e:
            self.finished.emit(True, f'Unexpected error: {e}')


# ============================== Main Tab ============================= #
class TwoDScanTab(QWidget):
    """
    General 2D scan tab with:
      - X and Y axis selectors (drop-downs)
      - Start/End/Step for both axes
      - Measurement selector (returns up to 6 channels)
      - Display selector (choose which channel to show as the live image)
      - Fast-axis selector (X or Y) and serpentine toggle
      - Per-pixel wait time and averaging
      - Live 2D image (imshow) updated as data arrives
      - Saves per-point table: [ix, iy, x, y, d1..d6] and an optional data cube (.npy)

    Expected IM drivers (same style as your 1D tab):
      - Galvo with movexy(x, y)
      - Newport stage with move_and_wait(mm) or move_and_wait_ps(ps)
      - SR830 lock-in for measurement (read_x, read_y, read_r, read_theta)
    """
    def __init__(self, instrument_manager, state=None):
        super().__init__()
        self.IM = instrument_manager
        self.state = state

        # Instruments (optional; only needed for defaults)
        self.galvo = self._try_get('Galvo')
        self.stage = self._try_get('ESP')
        self.lockin = self._try_get('SR830')
        self.k2400 = self._try_get('Keithley2400')

        # ---------------- Registry: Axis setters and measurement ---------------- #
        # Individual axis setters: functions that move *one* axis and block until settled
        self.AXIS_X_SETTERS: Dict[str, Callable[[float], None]] = {}
        self.AXIS_Y_SETTERS: Dict[str, Callable[[float], None]] = {}

        # Combined XY setter: functions that move both axes in one call
        self.AXIS_XY_SETTERS: Dict[str, Callable[[float, float], None]] = {}

        # Fill with sensible defaults if instruments exist
        if self.stage is not None:
            self.AXIS_X_SETTERS['Newport X (mm)'] = lambda x: self.stage.move_and_wait(float(x))
            self.AXIS_Y_SETTERS['Newport Y (mm)'] = lambda y: self.stage.move_and_wait(float(y), axis='Y') if hasattr(self.stage, 'move_and_wait') else None
            # If stage also supports picoseconds axis (delay line)
            if hasattr(self.stage, 'move_and_wait_ps'):
                self.AXIS_X_SETTERS['Delay X (ps)'] = lambda x: self.stage.move_and_wait_ps(float(x))
                self.AXIS_Y_SETTERS['Delay Y (ps)'] = lambda y: self.stage.move_and_wait_ps(float(y))

        if self.galvo is not None:
            # Prefer a *combined* move for galvo, to avoid time skew between axes
            self.AXIS_XY_SETTERS['Galvo (unitless)'] = lambda x, y: self.galvo.movexy(float(x), float(y))
            # Fallback individual setters (if needed)
            self.AXIS_X_SETTERS['Galvo X'] = lambda x: self.galvo.movexy(float(x), 0.0)
            self.AXIS_Y_SETTERS['Galvo Y'] = lambda y: self.galvo.movexy(0.0, float(y))

        if self.k2400 is not None:
            self.AXIS_X_SETTERS['Keithley V (X)'] = lambda x: self.k2400.ramp_voltage(float(x))
            self.AXIS_Y_SETTERS['Keithley V (Y)'] = lambda y: self.k2400.ramp_voltage(float(y))

        # Measurements: return up to 6 channels
        self.MEASUREMENT_FUNCS: Dict[str, Callable[[], Tuple[float, ...]]] = {}

        def _safe_div(a: float, b: float, eps: float = 1e-12) -> float:
            if not (math.isfinite(a) and math.isfinite(b)):
                return math.nan
            denom = b if abs(b) > eps else (eps if b >= 0 else -eps)
            return a / denom

        if self.lockin is not None:
            self.MEASUREMENT_FUNCS['Lock-in X only'] = lambda: (self.lockin.read_x(),)
            self.MEASUREMENT_FUNCS['Lock-in (X, Y, R, θ)'] = (
                lambda: (
                    self.lockin.read_x(),
                    self.lockin.read_y(),
                    self.lockin.read_r(),
                    self.lockin.read_theta(),
                )
            )
            def _photocurrent_R_read():
                V = self.lockin.read_x2() if hasattr(self.lockin, 'read_x2') else math.nan
                I = self.lockin.read_x()
                R = _safe_div(V, I)
                return (R, I, V, math.nan, math.nan, math.nan)
            def _CD_read():
                R = self.lockin.read_x()
                CD = self.lockin.read_x2()
                cd_R = _safe_div(CD, R)
                return (R, CD, cd_R, math.nan, math.nan, math.nan)
            self.MEASUREMENT_FUNCS['Circular Dichroism R, CD, CD/R (calc)'] = _CD_read
            self.MEASUREMENT_FUNCS['R (calc), I, V'] = _photocurrent_R_read

        # Always provide dummies
        self.MEASUREMENT_FUNCS['(dummy) all NaN'] = lambda: (math.nan,)*6
        self.AXIS_X_SETTERS['(dummy) X no-op'] = lambda x: None
        self.AXIS_Y_SETTERS['(dummy) Y no-op'] = lambda y: None
        self.AXIS_XY_SETTERS['(dummy) XY no-op'] = lambda x, y: None

        # ------------------------------- UI ------------------------------- #
        outer = QHBoxLayout(self)

        # Left controls
        ctrl = QVBoxLayout()
        form = QFormLayout()

        # Axis selection
        self.fast_axis_combo = QComboBox(); self.fast_axis_combo.addItems(['X fast', 'Y fast'])
        self.serpentine_chk = QCheckBox('Serpentine scan')
        self.xy_mode_combo = QComboBox(); self.xy_mode_combo.addItems(['Use combined XY setter (if available)', 'Use separate X & Y setters'])

        self.xy_setter_combo = QComboBox(); self.xy_setter_combo.addItems(list(self.AXIS_XY_SETTERS.keys()))
        self.x_setter_combo  = QComboBox(); self.x_setter_combo.addItems(list(self.AXIS_X_SETTERS.keys()))
        self.y_setter_combo  = QComboBox(); self.y_setter_combo.addItems(list(self.AXIS_Y_SETTERS.keys()))

        # Measurement and display
        self.meas_combo = QComboBox(); self.meas_combo.addItems(list(self.MEASUREMENT_FUNCS.keys()))
        self.display_combo = QComboBox(); self.display_combo.addItems([f'data{i}' for i in range(1,7)])

        # Ranges & steps
        dv = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_pos = QDoubleValidator(0.0, 1e300, 9, self)
        iv_pos = QIntValidator(1, 10_000_000, self)

        self.x_start = QLineEdit('-0.01'); self.x_start.setValidator(dv)
        self.x_end   = QLineEdit('0.01');  self.x_end.setValidator(dv)
        self.x_step  = QLineEdit('0.001'); self.x_step.setValidator(dv_pos)

        self.y_start = QLineEdit('-0.01'); self.y_start.setValidator(dv)
        self.y_end   = QLineEdit('0.01');  self.y_end.setValidator(dv)
        self.y_step  = QLineEdit('0.001'); self.y_step.setValidator(dv_pos)

        self.wait_ms = QLineEdit('5'); self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))
        self.pix_avg = QLineEdit('1'); self.pix_avg.setValidator(iv_pos)

        # Save controls
        self.folder = QLineEdit(os.path.expanduser('~/two_d_scans'))
        self.filename = QLineEdit('scan_2d')
        browse_btn = QPushButton('Browse…'); browse_btn.clicked.connect(self._browse)

        # Buttons
        self.start_btn = QPushButton('Start')
        self.stop_btn  = QPushButton('Stop'); self.stop_btn.setEnabled(False)
        self.save_btn  = QPushButton('Save')

        # Status
        self.status_lbl = QLabel('Idle.')

        # Lay out form
        form.addRow('Fast axis:', self.fast_axis_combo)
        form.addRow('', self.serpentine_chk)
        form.addRow('XY mode:', self.xy_mode_combo)
        form.addRow('Combined XY setter:', self.xy_setter_combo)
        form.addRow('X setter:', self.x_setter_combo)
        form.addRow('Y setter:', self.y_setter_combo)
        form.addRow('Measurement:', self.meas_combo)
        form.addRow('Display channel:', self.display_combo)

        form.addRow('X start:', self.x_start)
        form.addRow('X end:',   self.x_end)
        form.addRow('X step:',  self.x_step)
        form.addRow('Y start:', self.y_start)
        form.addRow('Y end:',   self.y_end)
        form.addRow('Y step:',  self.y_step)
        form.addRow('Wait (ms):', self.wait_ms)
        form.addRow('Averages / pixel:', self.pix_avg)

        save_row = QGridLayout()
        save_row.addWidget(QLabel('Folder:'), 0, 0)
        save_row.addWidget(self.folder,       0, 1)
        save_row.addWidget(browse_btn,        0, 2)
        save_row.addWidget(QLabel('File (no ext):'), 1, 0)
        save_row.addWidget(self.filename,         1, 1)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.save_btn)

        ctrl.addLayout(form)
        ctrl.addLayout(save_row)
        ctrl.addLayout(btn_row)
        ctrl.addWidget(self.status_lbl)
        ctrl.addStretch(1)

        # Right: live image
        right = QVBoxLayout()
        self.fig_img = Figure(figsize=(6, 6), tight_layout=True)
        self.ax_img = self.fig_img.add_subplot(111)
        self.ax_img.set_title('Live 2D Image')
        self.ax_img.set_xlabel('X')
        self.ax_img.set_ylabel('Y')
        self.canvas_img = FigureCanvas(self.fig_img)
        right.addWidget(self.canvas_img, stretch=1)

        outer.addLayout(ctrl, stretch=1)
        outer.addLayout(right, stretch=3)

        # Runtime
        self._stop_flag = [False]
        self._worker: Optional[_Scan2DWorker] = None
        self._xs: Optional[np.ndarray] = None
        self._ys: Optional[np.ndarray] = None
        self._img6: Optional[np.ndarray] = None   # (Ny, Nx, 6)
        self._im_artist = None

        # Signals
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)
        self.display_combo.currentIndexChanged.connect(self._redraw_image)
        self.xy_mode_combo.currentIndexChanged.connect(self._update_xy_controls_visibility)

        self._update_xy_controls_visibility()

    # ----------------------------- Helpers ----------------------------- #
    def _try_get(self, name: str):
        try:
            return self.IM.get(name)
        except Exception:
            return None

    def _update_xy_controls_visibility(self):
        use_combined = (self.xy_mode_combo.currentIndex() == 0)
        self.xy_setter_combo.setEnabled(use_combined)
        self.x_setter_combo.setEnabled(not use_combined)
        self.y_setter_combo.setEnabled(not use_combined)

    # ----------------------------- Actions ----------------------------- #
    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose folder', self.folder.text())
        if path:
            self.folder.setText(path)

    def _build_axis_arrays(self) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        try:
            x0 = float(self.x_start.text()); x1 = float(self.x_end.text()); dx = float(self.x_step.text())
            y0 = float(self.y_start.text()); y1 = float(self.y_end.text()); dy = float(self.y_step.text())
            if dx <= 0 or dy <= 0:
                raise ValueError
        except Exception:
            QMessageBox.warning(self, 'Input error', 'Start/end/step values invalid.')
            return None

        if (x1 - x0) == 0 or (y1 - y0) == 0:
            QMessageBox.warning(self, 'Input error', 'Ranges must be non-zero.')
            return None

        if abs(dx) > abs(x1 - x0) or abs(dy) > abs(y1 - y0):
            QMessageBox.warning(self, 'Input error', 'Step too large for range.')
            return None

        nx = int(np.floor(abs((x1 - x0) / dx)) + 1)
        ny = int(np.floor(abs((y1 - y0) / dy)) + 1)
        xs = np.linspace(x0, x0 + (nx - 1) * dx * np.sign(x1 - x0), nx)
        ys = np.linspace(y0, y0 + (ny - 1) * dy * np.sign(y1 - y0), ny)
        return xs, ys

    def _selected_channel_index(self) -> int:
        return max(0, min(5, self.display_combo.currentIndex()))

    def _start(self):
        axes = self._build_axis_arrays()
        if axes is None:
            return
        xs, ys = axes
        self._xs, self._ys = xs, ys

        try:
            wait_s = max(0.0, int(self.wait_ms.text()) / 1000.0)
            avgN = int(self.pix_avg.text()); assert avgN >= 1
        except Exception:
            QMessageBox.warning(self, 'Input error', 'Averages/pixel must be ≥ 1.')
            return

        # Resolve measurement
        meas_key = self.meas_combo.currentText()
        measure_fn = self.MEASUREMENT_FUNCS.get(meas_key)
        if measure_fn is None:
            QMessageBox.critical(self, 'Configuration error', 'Measurement not found.')
            return

        # Resolve set_xy wrapper
        use_combined = (self.xy_mode_combo.currentIndex() == 0)
        set_xy: Optional[Callable[[float, float], None]] = None
        if use_combined and self.xy_setter_combo.currentText() in self.AXIS_XY_SETTERS:
            set_xy = self.AXIS_XY_SETTERS[self.xy_setter_combo.currentText()]
        else:
            set_x = self.AXIS_X_SETTERS.get(self.x_setter_combo.currentText())
            set_y = self.AXIS_Y_SETTERS.get(self.y_setter_combo.currentText())
            if (set_x is None) or (set_y is None):
                QMessageBox.critical(self, 'Configuration error', 'X or Y setter not found.')
                return
            # Compose sequential XY move (move slow axis first to minimize dwell error on fast axis)
            def _seq_xy(x: float, y: float, set_x=set_x, set_y=set_y):
                # Move slow axis first depending on fast axis choice
                if self.fast_axis_combo.currentText().startswith('X'):
                    set_y(float(y)); set_x(float(x))
                else:
                    set_x(float(x)); set_y(float(y))
            set_xy = _seq_xy

        # Reset image buffers
        Ny, Nx = len(ys), len(xs)
        self._img6 = np.full((Ny, Nx, 6), np.nan, float)
        self._stop_flag[0] = False

        # Prepare image axes
        self.ax_img.clear()
        s = self._selected_channel_index()
        self.ax_img.set_title('Live 2D Image')
        self.ax_img.set_xlabel('X')
        self.ax_img.set_ylabel('Y')
        extent = [xs.min(), xs.max(), ys.min(), ys.max()]
        img = self._img6[:, :, s]
        self._im_artist = self.ax_img.imshow(img, origin='lower', extent=extent, aspect='auto')
        self.fig_img.colorbar(self._im_artist, ax=self.ax_img, label=self.display_combo.currentText())
        self.canvas_img.draw_idle()

        # Start worker
        self._worker = _Scan2DWorker(
            xs=xs,
            ys=ys,
            set_xy=set_xy,
            measure_fn=measure_fn,
            wait_s=wait_s,
            stop_flag_ref=self._stop_flag,
            serpentine=self.serpentine_chk.isChecked(),
            fast_axis=('X' if self.fast_axis_combo.currentText().startswith('X') else 'Y'),
            per_pixel_avg=avgN,
        )
        self._worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText('Scanning…')

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText('Stopping…')

    def _on_progress(self, ix: int, iy: int, x: float, y: float, d6: tuple):
        # Update cube
        if self._img6 is None:
            return
        Ny, Nx, _ = self._img6.shape
        if 0 <= ix < Nx and 0 <= iy < Ny:
            self._img6[iy, ix, :] = np.asarray(d6, float)

        # Update image
        s = self._selected_channel_index()
        self._im_artist.set_data(self._img6[:, :, s])
        self.ax_img.set_title(f'Live 2D Image — last (x={x:.6g}, y={y:.6g})')
        self._im_artist.set_clim(*self._auto_clim(self._img6[:, :, s]))
        self.canvas_img.draw_idle()

    def _auto_clim(self, img: np.ndarray, lo=2, hi=98):
        # robust auto-scaling using percentiles
        vals = img[np.isfinite(img)]
        if vals.size < 8:
            return (np.nanmin(img) if np.isfinite(img).any() else 0.0,
                    np.nanmax(img) if np.isfinite(img).any() else 1.0)
        p1, p2 = np.percentile(vals, [lo, hi])
        if not np.isfinite(p1) or not np.isfinite(p2) or p2 <= p1:
            return (float(np.min(vals)), float(np.max(vals)))
        return float(p1), float(p2)

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self._worker = None

    def _redraw_image(self):
        if self._img6 is None or self._xs is None or self._ys is None:
            return
        self.ax_img.clear()
        s = self._selected_channel_index()
        extent = [self._xs.min(), self._xs.max(), self._ys.min(), self._ys.max()]
        self._im_artist = self.ax_img.imshow(self._img6[:, :, s], origin='lower', extent=extent, aspect='auto')
        self.fig_img.colorbar(self._im_artist, ax=self.ax_img, label=self.display_combo.currentText())
        self.canvas_img.draw_idle()

    # ------------------------------ Saving ------------------------------ #
    def _save(self):
        if self._img6 is None or self._xs is None or self._ys is None:
            QMessageBox.information(self, 'Nothing to save', 'Run a scan first.')
            return

        folder = self.folder.text().strip()
        fname  = (self.filename.text().strip() or 'scan_2d')
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, 'Folder error', f'Could not create folder:\n{e}')
            return

        # Flatten per-point table like the 1D tab style
        Ny, Nx, _ = self._img6.shape
        rows = []
        for iy, y in enumerate(self._ys):
            for ix, x in enumerate(self._xs):
                d6 = self._img6[iy, ix, :]
                rows.append([ix, iy, float(x), float(y)] + [float(v) for v in d6])
        arr = np.asarray(rows, float)
        txt_path = os.path.join(folder, f'{fname}.txt')
        header = self._build_header()
        try:
            np.savetxt(txt_path, arr, delimiter='\t', fmt='%.10g', header=header, comments='')
        except Exception as e:
            QMessageBox.critical(self, 'Save error', f'Failed to save TXT:\n{e}')
            return

        # Save raw cube as .npy (Ny, Nx, 6) and axes
        try:
            np.save(os.path.join(folder, f'{fname}_cube.npy'), self._img6)
            np.save(os.path.join(folder, f'{fname}_xs.npy'), self._xs)
            np.save(os.path.join(folder, f'{fname}_ys.npy'), self._ys)
        except Exception:
            pass  # optional

        QMessageBox.information(self, 'Saved', f'Saved:\n{txt_path}\n(and *_cube.npy, *_xs.npy, *_ys.npy)')

    def _build_header(self) -> str:
        xs_str = 'None' if self._xs is None else f'{self._xs[0]} … {self._xs[-1]} (Nx={len(self._xs)})'
        ys_str = 'None' if self._ys is None else f'{self._ys[0]} … {self._ys[-1]} (Ny={len(self._ys)})'
        lines = [
            '2D Scan',
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            '',
            'Parameters:',
            f'  Fast axis: {self.fast_axis_combo.currentText()}',
            f'  Serpentine: {self.serpentine_chk.isChecked()}',
            f'  XY mode: {self.xy_mode_combo.currentText()}',
            f'  Combined XY setter: {self.xy_setter_combo.currentText()}',
            f'  X setter: {self.x_setter_combo.currentText()}',
            f'  Y setter: {self.y_setter_combo.currentText()}',
            f'  Measurement: {self.meas_combo.currentText()}',
            f'  Display channel: {self.display_combo.currentText()}',
            f'  X start: {self.x_start.text()}',
            f'  X end:   {self.x_end.text()}',
            f'  X step:  {self.x_step.text()}',
            f'  Y start: {self.y_start.text()}',
            f'  Y end:   {self.y_end.text()}',
            f'  Y step:  {self.y_step.text()}',
            f'  Wait (ms): {self.wait_ms.text()}',
            f'  Averages / pixel: {self.pix_avg.text()}',
            '',
            'Data format (10 columns):',
            '  [ix, iy, x, y, data1, data2, data3, data4, data5, data6]',
            '',
            f'X array summary: {xs_str}',
            f'Y array summary: {ys_str}',
            '',
            'Table begins below this header.'
        ]
        return '\n'.join(lines)

    # ------------------------------ Cleanup ------------------------------ #
    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
