from __future__ import annotations
import os, time, math
from typing import Callable, Dict, Tuple, List, Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QComboBox,
    QCheckBox
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.axes_grid1 import make_axes_locatable
from helpers.power_tuners import tune_pump_power_blocking, tune_probe_power_blocking


# =============================== Worker =============================== #
class _Scan3DWorker(QThread):
    """
    Runs one or more full 2D scans on a background thread, each at a different Z.

    Emits per-point progress:
        progress(iz, ix, iy, z_val, x_val, y_val, data_tuple_of_len_6)

    Emits at the end of each full 2D pass:
        scan_done(scan_index_1based)

    Emits when finished:
        finished(user_abort: bool, message: str)
    """
    progress = pyqtSignal(int, int, int, float, float, float, tuple)
    scan_done = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(
        self,
        zs: np.ndarray,
        xs: np.ndarray,
        ys: np.ndarray,
        set_z: Callable[[float], None],
        set_xy: Callable[[float, float], None],
        measure_fn: Callable[[], Tuple[float, ...]],
        wait_s: float,
        stop_flag_ref: List[bool],
        fast_axis: str,  # 'X' or 'Y'
        align_enabled: bool = False,
        alignment_tab = None,
    ):
        super().__init__()
        self._zs = np.asarray(zs, float)
        self._xs = np.asarray(xs, float)
        self._ys = np.asarray(ys, float)
        self._set_z = set_z
        self._set_xy = set_xy
        self._measure = measure_fn
        self._wait_s = max(0.0, float(wait_s))
        self._stop_flag_ref = stop_flag_ref
        self._fast = 'X' if str(fast_axis).upper().startswith('X') else 'Y'
        self._align_enabled = align_enabled
        self._alignment_tab = alignment_tab
        self.x_offset = 0.0
        self.y_offset = 0.0


    def _iter_coords(self):
        # Iterate without serpentine; simple raster with chosen fast axis
        if self._fast == 'X':
            for iy, y in enumerate(self._ys):
                for ix, x in enumerate(self._xs):
                    yield ix, iy, x, y
        else:
            for ix, x in enumerate(self._xs):
                for iy, y in enumerate(self._ys):
                    yield ix, iy, x, y

    def run(self):
        try:
            for s, z in enumerate(self._zs):
                # Set Z before starting the 2D scan
                try:
                    self._set_z(float(z))
                except Exception as e:
                    self.finished.emit(True, f'Z axis move error at z={z}: {e}')
                    return
                if self._align_enabled and self._alignment_tab is not None:
                    self._alignment_tab._find_feature()
                    print("aligning")
                    time.sleep(15)
                    self.x_offset = self._alignment_tab.state.settings.get("x_offset", math.nan)
                    self.y_offset = self._alignment_tab.state.settings.get("y_offset", math.nan)
                    print(f"Alignment offsets at z={z}: x_offset={self.x_offset}, y_offset={self.y_offset}")
                    if abs(self.x_offset)>4e-2 or abs(self.y_offset)>4e-2:
                        self.x_offset = 0
                        self.y_offset = 0
                        print(f"Warning: large alignment offset at z={z}: x_offset={self.x_offset}, y_offset={self.y_offset}. Offsets ignored.")
                for ix, iy, x, y in self._iter_coords():
                    if self._stop_flag_ref[0]:
                        self.finished.emit(True, 'Scan stopped by user.')
                        return

                    try:
                        if ix == 0:
                            self._set_xy(float(x), float(y+.05))
                            time.sleep(0.05)
                        self._set_xy(float(x+self.x_offset), float(y+self.y_offset))
                    except Exception as e:
                        self.finished.emit(True, f'Axis move error at (x={x}, y={y}): {e}')
                        return

                    if self._wait_s > 0:
                        time.sleep(self._wait_s)

                    try:
                        d = self._measure()
                    except Exception as e:
                        self.finished.emit(True, f'Measurement error at (x={x}, y={y}): {e}')
                        return

                    if not isinstance(d, (list, tuple)):
                        d = (float(d),)
                    d6 = tuple([float(v) for v in (list(d) + [math.nan]*6)[:6]])

                    self.progress.emit(int(s), int(ix), int(iy), float(z), float(x), float(y), d6)

                self.scan_done.emit(s + 1)

            self.finished.emit(False, '3D scan complete.')
        except Exception as e:
            self.finished.emit(True, f'Unexpected error: {e}')


# ============================== Main Tab ============================= #
class ThreeDScanTab(QWidget):
    """
    3D scan tab with:
      - Z, X and Y axis selectors (drop-downs)
      - Z start/end/step directly under Z setter, then X setter with X start/end/step, then Y setter with Y start/end/step
      - Measurement selector (returns up to 6 channels)
      - Display selector (choose which channel to show as the image)
      - Fast-axis selector (X or Y)
      - Live 2D image (imshow) updated as data arrives
      - Saves a per-point table and a cube
    """
    def __init__(self, instrument_manager, state=None, alignment_tab=None):
        super().__init__()
        self.IM = instrument_manager
        self.state = state
        self.alignment_tab = alignment_tab

        # Instruments (optional; only needed for defaults)
        self.galvo = self._try_get('Galvo')
        self.stage = self._try_get('ESP')
        self.lockin = self._try_get('SR830')
        self.k2400 = self._try_get('Keithley2400')
        self.BTT = self._try_get('BTT')
        self.PM = self._try_get('PM')
        self.shutter = self._try_get('Shutter')

        # ---------------- Registry: Axis setters and measurement ---------------- #
        self.AXIS_Z_SETTERS: Dict[str, Callable[[float], None]] = {}
        self.AXIS_X_SETTERS: Dict[str, Callable[[float], None]] = {}
        self.AXIS_Y_SETTERS: Dict[str, Callable[[float], None]] = {}


        if self.stage is not None:
            # Adjust these lambdas if your stage API differs for Y
            self.AXIS_Z_SETTERS['Newport Z (mm)'] = lambda z: self.stage.move_and_wait(float(z))
            self.AXIS_X_SETTERS['Newport X (mm)'] = lambda x: self.stage.move_and_wait(float(x))
            self.AXIS_Y_SETTERS['Newport Y (mm)'] = lambda y: self.stage.move_and_wait(float(y))
            if hasattr(self.stage, 'move_and_wait_ps'):
                self.AXIS_Z_SETTERS['Delay Z (ps)'] = lambda z: self.stage.move_and_wait_ps(float(z))
                self.AXIS_X_SETTERS['Delay X (ps)'] = lambda x: self.stage.move_and_wait_ps(float(x))
                self.AXIS_Y_SETTERS['Delay Y (ps)'] = lambda y: self.stage.move_and_wait_ps(float(y))

        if self.galvo is not None:
            self.AXIS_Z_SETTERS['Galvo Z'] = lambda z: self.galvo.move_y(float(z))  # assuming y is z or something
            self.AXIS_X_SETTERS['Galvo X'] = lambda x: self.galvo.move_x(float(x))
            self.AXIS_Y_SETTERS['Galvo Y'] = lambda y: self.galvo.move_y(float(y))

        if self.k2400 is not None:
            self.AXIS_Z_SETTERS['Keithley Z V'] = lambda z: self.k2400.ramp_voltage(float(z))
            self.AXIS_X_SETTERS['Keithley X V'] = lambda x: self.k2400.ramp_voltage(float(x))
            self.AXIS_Y_SETTERS['Keithley Y V'] = lambda y: self.k2400.ramp_voltage(float(y))

        if self.BTT is not None:
            self.AXIS_Z_SETTERS['Pump Half Wave Plate'] = lambda z: self.BTT.rot_3(float(z), 5000)
            self.AXIS_X_SETTERS['Pump Half Wave Plate'] = lambda x: self.BTT.rot_3(float(x), 5000)
            self.AXIS_Y_SETTERS['Pump Half Wave Plate'] = lambda y: self.BTT.rot_3(float(y), 5000)

        if self.BTT is not None and self.PM is not None and self.shutter is not None:
            self.AXIS_Z_SETTERS['Pump Power (µW)'] = lambda z: tune_pump_power_blocking(
                float(z), self.PM, self.BTT, self.shutter, self.state
            )
            self.AXIS_Z_SETTERS['Probe Power (µW)'] = lambda z: tune_probe_power_blocking(
                float(z), self.PM, self.BTT, self.shutter, self.state
            )
            self.AXIS_X_SETTERS['Pump Power (µW)'] = lambda x: tune_pump_power_blocking(
                float(x), self.PM, self.BTT, self.shutter, self.state
            )
            self.AXIS_X_SETTERS['Probe Power (µW)'] = lambda x: tune_probe_power_blocking(
                float(x), self.PM, self.BTT, self.shutter, self.state
            )
            self.AXIS_Y_SETTERS['Pump Power (µW)'] = lambda y: tune_pump_power_blocking(
                float(y), self.PM, self.BTT, self.shutter, self.state
            )
            self.AXIS_Y_SETTERS['Probe Power (µW)'] = lambda y: tune_probe_power_blocking(
                float(y), self.PM, self.BTT, self.shutter, self.state
            )

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
            self.MEASUREMENT_FUNCS['PC and Reflectivity'] = (
                lambda: (
                    self.lockin.read_x(),
                    self.lockin.read_x2(),
                )
            )
            def _photocurrent_readxy():
                R = self.lockin.read_x2()
                Vx, Vy = self.lockin.read_xy()
                return (R, Vx, Vy, math.nan, math.nan, math.nan)
            def _double_pc():
                Vx1, Vy1, PC = self.lockin.read_xy()
                Vx2, Vy2, PC2= self.lockin.read_xy2() 
                return (Vx1, Vy1, Vx2, Vy2, PC, math.nan)
            self.MEASUREMENT_FUNCS['PC and reflect (both channels)'] = _photocurrent_readxy
            self.MEASUREMENT_FUNCS['Double PC (X,Y of both)'] = _double_pc
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
            def _CD_with_R():
                x_ch,a_ch = self.lockin.readX_A()
                return (x_ch, a_ch, _safe_div(x_ch, a_ch), math.nan, math.nan, math.nan)
            self.MEASUREMENT_FUNCS['R (calc), I, V'] = _photocurrent_R_read
            self.MEASUREMENT_FUNCS['X, A, X/A (calc)'] = _CD_with_R

        self.MEASUREMENT_FUNCS['(dummy) all NaN'] = lambda: (math.nan,)*6
        self.AXIS_Z_SETTERS['(dummy) Z no-op'] = lambda z: None
        self.AXIS_X_SETTERS['(dummy) X no-op'] = lambda x: None
        self.AXIS_Y_SETTERS['(dummy) Y no-op'] = lambda y: None

        # ------------------------------- UI ------------------------------- #
        outer = QHBoxLayout(self)

        # Left controls
        ctrl = QVBoxLayout()
        form = QFormLayout()

        # Fast axis selection
        self.fast_axis_combo = QComboBox(); self.fast_axis_combo.addItems(['X fast', 'Y fast'])

        # Z setter and Z range (Z start/end/step directly below Z setter)
        self.z_setter_combo  = QComboBox(); self.z_setter_combo.addItems(list(self.AXIS_Z_SETTERS.keys()))
        self.z_setter_combo.setCurrentText("Pump Half Wave Plate")
        dv = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_pos = QDoubleValidator(0.0, 1e300, 9, self)
        self.z_start = QLineEdit('0'); self.z_start.setValidator(dv)
        self.z_end   = QLineEdit('180');  self.z_end.setValidator(dv)
        self.z_step  = QLineEdit('10'); self.z_step.setValidator(dv_pos)

        # X setter and X range (X start/end/step directly below X setter)
        self.x_setter_combo  = QComboBox(); self.x_setter_combo.addItems(list(self.AXIS_X_SETTERS.keys()))
        self.x_setter_combo.setCurrentText("Galvo X")
        self.x_start = QLineEdit('-0.01'); self.x_start.setValidator(dv)
        self.x_end   = QLineEdit('0.01');  self.x_end.setValidator(dv)
        self.x_step  = QLineEdit('0.001'); self.x_step.setValidator(dv_pos)

        # Y setter and Y range
        self.y_setter_combo  = QComboBox(); self.y_setter_combo.addItems(list(self.AXIS_Y_SETTERS.keys()))
        self.y_setter_combo.setCurrentText("Galvo Y")
        self.y_start = QLineEdit('-0.01'); self.y_start.setValidator(dv)
        self.y_end   = QLineEdit('0.01');  self.y_end.setValidator(dv)
        self.y_step  = QLineEdit('0.001'); self.y_step.setValidator(dv_pos)

        # Measurement & display
        self.meas_combo = QComboBox(); self.meas_combo.addItems(list(self.MEASUREMENT_FUNCS.keys()))
        self.display_combo = QComboBox(); self.display_combo.addItems([f'data{i}' for i in range(1,7)])

        # Timing
        self.wait_ms = QLineEdit('5'); self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))
        self.align_checkbox = QCheckBox('Align between each scan')
        self.align_checkbox.setChecked(False)
        form.addRow('', self.align_checkbox)
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

        # Lay out form in requested order
        form.addRow('Fast axis:', self.fast_axis_combo)

        form.addRow('Z setter:', self.z_setter_combo)
        form.addRow('Z start:', self.z_start)
        form.addRow('Z end:',   self.z_end)
        form.addRow('Z step:',  self.z_step)

        form.addRow('X setter:', self.x_setter_combo)
        form.addRow('X start:', self.x_start)
        form.addRow('X end:',   self.x_end)
        form.addRow('X step:',  self.x_step)

        form.addRow('Y setter:', self.y_setter_combo)
        form.addRow('Y start:', self.y_start)
        form.addRow('Y end:',   self.y_end)
        form.addRow('Y step:',  self.y_step)

        form.addRow('Measurement:', self.meas_combo)
        form.addRow('Display channel:', self.display_combo)
        form.addRow('Wait (ms):', self.wait_ms)

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
        self.fig_img = Figure(figsize=(6, 6), tight_layout=False)
        self.ax_img = self.fig_img.add_subplot(111)
        self.fig_img.set_tight_layout(False)  # prevent re-tightening
        divider = make_axes_locatable(self.ax_img)
        self._cax = divider.append_axes("right", size="4%", pad=0.05)
        self._cbar = None
        self.ax_img.set_title('Live 2D Image')
        self.ax_img.set_xlabel('X')
        self.ax_img.set_ylabel('Y')
        self.canvas_img = FigureCanvas(self.fig_img)
        right.addWidget(self.canvas_img, stretch=1)

        outer.addLayout(ctrl, stretch=1)
        outer.addLayout(right, stretch=5)

        # Runtime
        self._stop_flag = [False]
        self._worker: Optional[_Scan3DWorker] = None
        self._zs: Optional[np.ndarray] = None
        self._xs: Optional[np.ndarray] = None
        self._ys: Optional[np.ndarray] = None
        # Legacy fields kept for compatibility; not used after refactor
        self._img6_current: Optional[np.ndarray] = None
        self._img6_sum: Optional[np.ndarray] = None
        self._scan_count: int = 0

        # New data store: (Nz, Ny, Nx, 6)
        self.data: Optional[np.ndarray] = None
        self._scan_index: int = 0  # 0-based index of the current z
        self._im_artist = None
        self._cbar = None

        # Signals
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)
        self.display_combo.currentIndexChanged.connect(self._redraw_image)

    # ----------------------------- Helpers ----------------------------- #
    def _try_get(self, name: str):
        try:
            return self.IM.get(name)
        except Exception:
            return None

    # ----------------------------- Actions ----------------------------- #
    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose folder', self.folder.text())
        if path:
            self.folder.setText(path)

    def _build_axis_arrays(self) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        try:
            z0 = float(self.z_start.text()); z1 = float(self.z_end.text()); dz = float(self.z_step.text())
            x0 = float(self.x_start.text()); x1 = float(self.x_end.text()); dx = float(self.x_step.text())
            y0 = float(self.y_start.text()); y1 = float(self.y_end.text()); dy = float(self.y_step.text())
            if dx <= 0 or dy <= 0 or dz <= 0:
                raise ValueError
        except Exception:
            QMessageBox.warning(self, 'Input error', 'Start/end/step values invalid.')
            return None

        if (z1 - z0) == 0 or (x1 - x0) == 0 or (y1 - y0) == 0:
            QMessageBox.warning(self, 'Input error', 'Ranges must be non-zero.')
            return None

        if abs(dz) > abs(z1 - z0) or abs(dx) > abs(x1 - x0) or abs(dy) > abs(y1 - y0):
            QMessageBox.warning(self, 'Input error', 'Step too large for range.')
            return None

        nz = int(np.floor(abs((z1 - z0) / dz)) + 1)
        nx = int(np.floor(abs((x1 - x0) / dx)) + 1)
        ny = int(np.floor(abs((y1 - y0) / dy)) + 1)
        zs = np.linspace(z0, z0 + (nz - 1) * dz * np.sign(z1 - z0), nz)
        xs = np.linspace(x0, x0 + (nx - 1) * dx * np.sign(x1 - x0), nx)
        ys = np.linspace(y0, y0 + (ny - 1) * dy * np.sign(y1 - y0), ny)
        return zs, xs, ys

    def _selected_channel_index(self) -> int:
        return max(0, min(5, self.display_combo.currentIndex()))

    def _start(self):
        axes = self._build_axis_arrays()
        if axes is None:
            return
        zs, xs, ys = axes
        self._zs, self._xs, self._ys = zs, xs, ys

        try:
            wait_s = max(0.0, int(self.wait_ms.text()) / 1000.0)
        except Exception:
            QMessageBox.warning(self, 'Input error', 'Wait time must be ≥ 0.')
            return

        meas_key = self.meas_combo.currentText()
        measure_fn = self.MEASUREMENT_FUNCS.get(meas_key)
        if measure_fn is None:
            QMessageBox.critical(self, 'Configuration error', 'Measurement not found.')
            return

        # Build set_z, set_xy from separate setters; move slow axis first
        set_z = self.AXIS_Z_SETTERS.get(self.z_setter_combo.currentText())
        set_x = self.AXIS_X_SETTERS.get(self.x_setter_combo.currentText())
        set_y = self.AXIS_Y_SETTERS.get(self.y_setter_combo.currentText())
        if (set_z is None) or (set_x is None) or (set_y is None):
            QMessageBox.critical(self, 'Configuration error', 'Z, X or Y setter not found.')
            return
        def _seq_xy(x: float, y: float, set_x=set_x, set_y=set_y):
            if self.fast_axis_combo.currentText().startswith('X'):
                set_y(float(y)); set_x(float(x))
            else:
                set_x(float(x)); set_y(float(y))

        # Reset images/buffers
        Nz, Ny, Nx = len(zs), len(ys), len(xs)
        self.data = np.full((Nz, Ny, Nx, 6), np.nan, float)
        self._scan_index = 0
        self._stop_flag[0] = False

        # Prep image
        self.ax_img.set_title('Live 2D Image')
        self.ax_img.set_xlabel('X')
        self.ax_img.set_ylabel('Y')

        extent = [xs.min(), xs.max(), ys.min(), ys.max()]
        # Use the new 4-D store
        img = self._current_or_average_image()
        if img is None:
            # On the very first draw, before any progress arrives, make an empty frame
            img = np.full((len(ys), len(xs)), np.nan, float)

        if self._im_artist is None:
            # First time only: create the artist and colorbar
            self._im_artist = self.ax_img.imshow(img, origin='lower', extent=extent, aspect='auto')
            self.ax_img.set_aspect('equal', 'box')
            if self._cbar is None:
                self._cbar = self.fig_img.colorbar(self._im_artist, cax=self._cax)
        else:
            # DO NOT clear axes here; just update the existing artist
            self._im_artist.set_data(img)
            self._im_artist.set_extent(extent)
            self.ax_img.set_aspect('equal', 'box')
            self._cbar.update_normal(self._im_artist)

        self._cbar.set_label(self.display_combo.currentText())
        self.canvas_img.draw_idle()

        # Start worker
        self._worker = _Scan3DWorker(
            zs=zs,
            xs=xs,
            ys=ys,
            set_z=set_z,
            set_xy=_seq_xy,
            measure_fn=measure_fn,
            wait_s=wait_s,
            stop_flag_ref=self._stop_flag,
            fast_axis=('X' if self.fast_axis_combo.currentText().startswith('X') else 'Y'),
            align_enabled=self.align_checkbox.isChecked(),
            alignment_tab=self.alignment_tab,
        )
        self._worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker.scan_done.connect(self._on_scan_done, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText('Scanning…')

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText('Stopping…')

    def _current_or_average_image(self) -> Optional[np.ndarray]:
        if self.data is None:
            return None
        s = self._selected_channel_index()

        # Current z's image
        cur = self.data[self._scan_index, :, :, s]

        return cur

    def _on_progress(self, iz: int, ix: int, iy: int, z: float, x: float, y: float, d6: tuple):
        if self.data is None:
            return
        Nz, Ny, Nx, _ = self.data.shape
        if 0 <= iz < Nz and 0 <= ix < Nx and 0 <= iy < Ny:
            self.data[iz, iy, ix, :] = np.asarray(d6, float)

        img = self._current_or_average_image()
        if img is not None:
            self._im_artist.set_data(img)
            self.ax_img.set_title(f'Live 2D Image — z={z:.6g}, last (x={x:.6g}, y={y:.6g})')
            self._im_artist.set_clim(*self._auto_clim(img))
            self.canvas_img.draw_idle()

    def _auto_clim(self, img: np.ndarray, lo=2, hi=98):
        vals = img[np.isfinite(img)]
        if vals.size < 8:
            lo_v = float(np.nanmin(img)) if np.isfinite(img).any() else 0.0
            hi_v = float(np.nanmax(img)) if np.isfinite(img).any() else 1.0
            return lo_v, hi_v
        p1, p2 = np.percentile(vals, [lo, hi])
        if not np.isfinite(p1) or not np.isfinite(p2) or p2 <= p1:
            return float(np.min(vals)), float(np.max(vals))
        return float(p1), float(p2)

    def _on_scan_done(self, scan_idx: int):
        if self.data is None:
            return
        self.status_lbl.setText(f'Completed z layer {scan_idx}')

        # Move to next z (cap at last index to avoid overflow if extra signals appear)
        self._scan_index = min(self._scan_index + 1, self.data.shape[0] - 1)

        img = self._current_or_average_image()
        if img is not None:
            self._im_artist.set_data(img)
            self._im_artist.set_clim(*self._auto_clim(img))
            self.canvas_img.draw_idle()

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self._worker = None

    def _redraw_image(self):
        if self._zs is None or self._xs is None or self._ys is None:
            return
        img = self._current_or_average_image()
        if img is None:
            return

        extent = [self._xs.min(), self._xs.max(), self._ys.min(), self._ys.max()]

        if self._im_artist is None:
            # First-time draw from a redraw path
            self._im_artist = self.ax_img.imshow(img, origin='lower', extent=extent, aspect='auto')
            self.ax_img.set_aspect('equal', 'box')
            if self._cbar is None:
                self._cbar = self.fig_img.colorbar(self._im_artist, cax=self._cax)
        else:
            # Normal path: just update
            self._im_artist.set_data(img)
            self._im_artist.set_extent(extent)
            self.ax_img.set_aspect('equal', 'box')
            self._cbar.update_normal(self._im_artist)

        self._cbar.set_label(self.display_combo.currentText())
        self.canvas_img.draw_idle()
    # ------------------------------ Saving ------------------------------ #
    def _save(self):
        # Validate that we have axes and data
        if self._zs is None or self._xs is None or self._ys is None or not hasattr(self, "data") or self.data is None:
            QMessageBox.information(self, 'Nothing to save', 'Run a scan first.')
            return

        folder = self.folder.text().strip()
        fname  = (self.filename.text().strip() or 'scan_3d')
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, 'Folder error', f'Could not create folder:\n{e}')
            return

        # ---- 1) Save a screenshot of the current scan image ----
        png_path = os.path.join(folder, f'{fname}.png')
        try:
            # Save the live figure exactly as shown (includes colorbar, labels, etc.)
            self.fig_img.savefig(png_path, dpi=200, bbox_inches='tight')
        except Exception as e:
            QMessageBox.critical(self, 'Save error', f'Failed to save PNG:\n{e}')
            return

        # ---- 2) Build one flat table: z, Xvalue, Yvalue, data1..data6 ----
        # data shape: (Nz, Ny, Nx, 6)
        data = self.data
        Nz, Ny, Nx, n_ch = data.shape
        zs = self._zs
        xs = self._xs
        ys = self._ys

        # We’ll include rows where at least one channel is finite
        rows = []
        for iz in range(Nz):
            zv = float(zs[iz])
            for iy in range(Ny):
                yv = float(ys[iy])
                for ix in range(Nx):
                    xv = float(xs[ix])
                    d6 = data[iz, iy, ix, :]
                    # keep row if any channel is finite (handles partial scans)
                    if np.isfinite(d6).any():
                        rows.append([zv, xv, yv,
                                    float(d6[0]), float(d6[1]), float(d6[2]),
                                    float(d6[3]), float(d6[4]), float(d6[5])])

        if not rows:
            QMessageBox.information(self, 'Nothing to save', 'No valid data points yet.')
            return

        arr = np.asarray(rows, float)

        # ---- Header for the TXT file ----
        # Use the current UI state + basic scan metadata
        zs_str = f"{zs[0]} … {zs[-1]} (Nz={len(zs)})" if zs.size else 'None'
        xs_str = f"{xs[0]} … {xs[-1]} (Nx={len(xs)})" if xs.size else 'None'
        ys_str = f"{ys[0]} … {ys[-1]} (Ny={len(ys)})" if ys.size else 'None'
        header_lines = [
            '3D Scan',
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            '',
            'Parameters:',
            f'  Fast axis: {self.fast_axis_combo.currentText()}',
            f'  Z setter: {self.z_setter_combo.currentText()}',
            f'  X setter: {self.x_setter_combo.currentText()}',
            f'  Y setter: {self.y_setter_combo.currentText()}',
            f'  Measurement: {self.meas_combo.currentText()}',
            f'  Display channel: {self.display_combo.currentText()}',
            f'  Z start: {self.z_start.text()}',
            f'  Z end:   {self.z_end.text()}',
            f'  Z step:  {self.z_step.text()}',
            f'  X start: {self.x_start.text()}',
            f'  X end:   {self.x_end.text()}',
            f'  X step:  {self.x_step.text()}',
            f'  Y start: {self.y_start.text()}',
            f'  Y end:   {self.y_end.text()}',
            f'  Y step:  {self.y_step.text()}',
            f'  Wait (ms): {self.wait_ms.text()}',
            f'  Screenshot file: {os.path.basename(png_path)}',
            '',
            f'In-memory data shape: {data.shape}  (Nz, Ny, Nx, 6)',
            f'Z array summary: {zs_str}',
            f'X array summary: {xs_str}',
            f'Y array summary: {ys_str}',
            '',
            'Data format (9 columns):',
            '  [z, x, y, data1, data2, data3, data4, data5, data6]',
            '',
            'Table begins below this header.'
        ]
        header = '\n'.join(header_lines)

        # ---- Write the single TXT file (tab-delimited) ----
        txt_path = os.path.join(folder, f'{fname}.txt')
        try:
            np.savetxt(txt_path, arr, delimiter='\t', fmt='%.10g', header=header, comments='')
        except Exception as e:
            QMessageBox.critical(self, 'Save error', f'Failed to save TXT:\n{e}')
            return

        QMessageBox.information(
            self, 'Saved',
            f"Saved:\n{txt_path}\n{png_path}"
        )


    def _build_header(self) -> str:
        zs_str = 'None' if self._zs is None else f"{self._zs[0]} … {self._zs[-1]} (Nz={len(self._zs)})"
        xs_str = 'None' if self._xs is None else f"{self._xs[0]} … {self._xs[-1]} (Nx={len(self._xs)})"
        ys_str = 'None' if self._ys is None else f"{self._ys[0]} … {self._ys[-1]} (Ny={len(self._ys)})"
        lines = [
            '3D Scan',
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            '',
            'Parameters:',
            f'  Fast axis: {self.fast_axis_combo.currentText()}',
            f'  Z setter: {self.z_setter_combo.currentText()}',
            f'  X setter: {self.x_setter_combo.currentText()}',
            f'  Y setter: {self.y_setter_combo.currentText()}',
            f'  Measurement: {self.meas_combo.currentText()}',
            f'  Display channel: {self.display_combo.currentText()}',
            f'  Z start: {self.z_start.text()}',
            f'  Z end:   {self.z_end.text()}',
            f'  Z step:  {self.z_step.text()}',
            f'  X start: {self.x_start.text()}',
            f'  X end:   {self.x_end.text()}',
            f'  X step:  {self.x_step.text()}',
            f'  Y start: {self.y_start.text()}',
            f'  Y end:   {self.y_end.text()}',
            f'  Y step:  {self.y_step.text()}',
            f'  Wait (ms): {self.wait_ms.text()}',
            '',
            'Data format (10 columns):',
            '  [iz, iy, x, y, data1, data2, data3, data4, data5, data6]',
            '',
            f'Z array summary: {zs_str}',
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
