# tabs/three_d_scan_tab.py
from __future__ import annotations
import os, time, math
from typing import Callable, Dict, Tuple, List, Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QComboBox,
    QCheckBox,
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.axes_grid1 import make_axes_locatable

from helpers.power_tuners import (
    tune_pump_power_blocking,
    tune_probe_power_blocking,
    set_fast_probe_power_blocking,
)


# ── Worker ────────────────────────────────────────────────────────────────────

class _Scan3DWorker(QThread):
    """
    3D scan worker.  Three fast-axis modes:
      X fast: outer=Z, middle=Y, inner=X
      Y fast: outer=Z, middle=X, inner=Y
      Z fast: outer=XY raster, inner=Z

    Signals
    -------
    progress(iz, ix, iy, z, x, y, d6_tuple)
    scan_done(count_1based)   – one Z layer done (XY-fast) or one XY point done (Z-fast)
    finished(user_abort, message)
    """
    progress  = pyqtSignal(int, int, int, float, float, float, tuple)
    scan_done = pyqtSignal(int)
    finished  = pyqtSignal(bool, str)

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
        fast_axis: str,            # 'X', 'Y', or 'Z'
        z_reset_s: float = 0.0,
        align_enabled: bool = False,
        alignment_tab=None,
        align_every_n: int = 50,
        galvo_scan: bool = True,
        state=None,
        galvo_ref=None,
    ):
        super().__init__()
        self._zs            = np.asarray(zs, float)
        self._xs            = np.asarray(xs, float)
        self._ys            = np.asarray(ys, float)
        self._set_z         = set_z
        self._set_xy        = set_xy
        self._measure       = measure_fn
        self._wait_s        = max(0.0, float(wait_s))
        self._z_reset_s     = max(0.0, float(z_reset_s))
        self._stop          = stop_flag_ref
        fa = str(fast_axis).upper()
        self._fast          = 'Z' if fa.startswith('Z') else ('X' if fa.startswith('X') else 'Y')
        self._align_enabled = align_enabled
        self._alignment_tab = alignment_tab
        self._align_every_n = max(1, int(align_every_n))
        self._galvo_scan    = galvo_scan
        self._state         = state
        self._galvo_ref     = galvo_ref
        self.x_offset       = 0.0
        self.y_offset       = 0.0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _iter_xy(self):
        """Yield (ix, iy, x, y) in raster order respecting the fast axis."""
        if self._fast == 'X' or self._fast == 'Z':
            for iy, y in enumerate(self._ys):
                for ix, x in enumerate(self._xs):
                    yield ix, iy, x, y
        else:
            for ix, x in enumerate(self._xs):
                for iy, y in enumerate(self._ys):
                    yield ix, iy, x, y

    def _run_alignment(self, target_x: float, target_y: float):
        """Blocking alignment call — waits for completion before returning."""
        dx, dy = self._alignment_tab.run_alignment_blocking(self._stop)
        if abs(dx) > 30.0 or abs(dy) > 30.0:
            print(f'Warning: alignment offset ({dx:.4f},{dy:.4f}) exceeds ±30 — ignored.')
            dx, dy = 0.0, 0.0
        self.x_offset = dx
        self.y_offset = dy

        if self._galvo_scan:
            self._set_xy(float(target_x + self.x_offset), float(target_y + self.y_offset))
        else:
            if self._galvo_ref is not None and self._state is not None:
                gx = self._state.settings.get('galvo_x_position', 0.0)
                gy = self._state.settings.get('galvo_y_position', 0.0)
                self._galvo_ref.move_xy(float(gx + self.x_offset), float(gy + self.y_offset))
            self._set_xy(float(target_x), float(target_y))

    def _measure_point(self) -> Optional[tuple]:
        try:
            d = self._measure()
        except Exception as e:
            self.finished.emit(True, f'Measurement error: {e}')
            return None
        if not isinstance(d, (list, tuple)):
            d = (float(d),)
        return tuple([float(v) for v in (list(d) + [math.nan] * 6)[:6]])

    # ── Main entry ────────────────────────────────────────────────────────────

    def run(self):
        try:
            if self._fast == 'Z':
                self._run_z_fast()
            else:
                self._run_xy_fast()
        except Exception as e:
            self.finished.emit(True, f'Unexpected error: {e}')

    # ── XY-fast (Z is outer loop) ─────────────────────────────────────────────

    def _run_xy_fast(self):
        for s, z in enumerate(self._zs):
            try:
                self._set_z(float(z))
            except Exception as e:
                self.finished.emit(True, f'Z move error at z={z}: {e}')
                return

            pixel_count = 0
            for ix, iy, x, y in self._iter_xy():
                if self._stop[0]:
                    self.finished.emit(True, 'Scan stopped by user.')
                    return

                if self._align_enabled and self._alignment_tab is not None:
                    if pixel_count % self._align_every_n == 0:
                        try:
                            self._run_alignment(x, y)
                        except Exception as e:
                            self.finished.emit(True, f'Alignment error: {e}')
                            return

                try:
                    if self._galvo_scan:
                        self._set_xy(float(x + self.x_offset), float(y + self.y_offset))
                    else:
                        self._set_xy(float(x), float(y))
                except Exception as e:
                    self.finished.emit(True, f'XY move error at ({x},{y}): {e}')
                    return

                if self._wait_s > 0:
                    time.sleep(self._wait_s)

                if self._stop[0]:
                    self.finished.emit(True, 'Scan stopped by user.')
                    return

                d6 = self._measure_point()
                if d6 is None:
                    return

                self.progress.emit(int(s), int(ix), int(iy), float(z), float(x), float(y), d6)
                pixel_count += 1

            self.scan_done.emit(s + 1)

        self.finished.emit(False, '3D scan complete.')

    # ── Z-fast (XY outer, Z inner) ────────────────────────────────────────────

    def _run_z_fast(self):
        xy_count = 0
        for ix, iy, x, y in self._iter_xy():
            if self._stop[0]:
                self.finished.emit(True, 'Scan stopped by user.')
                return

            if self._align_enabled and self._alignment_tab is not None:
                if xy_count % self._align_every_n == 0:
                    try:
                        self._run_alignment(x, y)
                    except Exception as e:
                        self.finished.emit(True, f'Alignment error: {e}')
                        return

            try:
                if self._galvo_scan:
                    self._set_xy(float(x + self.x_offset), float(y + self.y_offset))
                else:
                    self._set_xy(float(x), float(y))
            except Exception as e:
                self.finished.emit(True, f'XY move error at ({x},{y}): {e}')
                return

            for iz, z in enumerate(self._zs):
                if self._stop[0]:
                    self.finished.emit(True, 'Scan stopped by user.')
                    return

                try:
                    self._set_z(float(z))
                except Exception as e:
                    self.finished.emit(True, f'Z move error at z={z}: {e}')
                    return

                if iz == 0 and self._z_reset_s > 0:
                    time.sleep(self._z_reset_s)

                if self._wait_s > 0:
                    time.sleep(self._wait_s)

                d6 = self._measure_point()
                if d6 is None:
                    return

                self.progress.emit(int(iz), int(ix), int(iy), float(z), float(x), float(y), d6)

            xy_count += 1
            self.scan_done.emit(xy_count)

        self.finished.emit(False, '3D scan complete (Z-fast).')


# ── Tab ───────────────────────────────────────────────────────────────────────

class ThreeDScanTab(QWidget):
    def __init__(self, controller, alignment_tab=None):
        super().__init__()
        self.controller    = controller
        self.state         = controller.state
        self.alignment_tab = alignment_tab

        # ── Instruments ───────────────────────────────────────────────────────
        galvo   = controller.get_instrument('Galvo')
        stage   = controller.get_instrument('ESP')
        lockin  = controller.get_instrument('SR830')
        k2400   = controller.get_instrument('Keithley2400')
        btt     = controller.get_instrument('BTT')
        pm      = controller.get_instrument('PM')
        shutter = controller.get_instrument('Shutter')

        # ── Axis setters ──────────────────────────────────────────────────────
        self.AXIS_Z_SETTERS: Dict[str, Callable[[float], None]] = {}
        self.AXIS_X_SETTERS: Dict[str, Callable[[float], None]] = {}
        self.AXIS_Y_SETTERS: Dict[str, Callable[[float], None]] = {}

        if stage is not None:
            for d in (self.AXIS_Z_SETTERS, self.AXIS_X_SETTERS, self.AXIS_Y_SETTERS):
                d['Newport (mm)'] = lambda v: stage.move(float(v))
                d['Delay (ps)']   = lambda v: stage.move_ps(float(v))

        if galvo is not None:
            for d in (self.AXIS_Z_SETTERS, self.AXIS_X_SETTERS, self.AXIS_Y_SETTERS):
                d['Galvo X (mu)'] = lambda v: galvo.move_x(float(v))
                d['Galvo Y (mu)'] = lambda v: galvo.move_y(float(v))

        if k2400 is not None:
            for d in (self.AXIS_Z_SETTERS, self.AXIS_X_SETTERS, self.AXIS_Y_SETTERS):
                d['Keithley V'] = lambda v: k2400.ramp_voltage(float(v))

        if btt is not None and pm is not None and shutter is not None:
            for d in (self.AXIS_Z_SETTERS, self.AXIS_X_SETTERS, self.AXIS_Y_SETTERS):
                d['Pump Power (µW)']  = lambda v: tune_pump_power_blocking(float(v), pm, btt, shutter, self.state)
                d['Probe Power (µW)'] = lambda v: tune_probe_power_blocking(float(v), pm, btt, shutter, self.state)

        if btt is not None:
            for d in (self.AXIS_Z_SETTERS, self.AXIS_X_SETTERS, self.AXIS_Y_SETTERS):
                d['Fast Probe Power (µW)'] = lambda v: set_fast_probe_power_blocking(float(v), btt, self.state)
                d['Pump HWP (°)']          = lambda v: btt.rot_3(float(v), 5000)
                d['Rot 1 (°)']             = lambda v: btt.rot_1(float(v), 5000)

        for d in (self.AXIS_Z_SETTERS, self.AXIS_X_SETTERS, self.AXIS_Y_SETTERS):
            d['(dummy) no-op'] = lambda v: None

        # ── Measurement funcs ─────────────────────────────────────────────────
        self.MEASUREMENT_FUNCS: Dict[str, Callable[[], Tuple[float, ...]]] = {}

        def _safe_div(a, b, eps=1e-12):
            if not (math.isfinite(a) and math.isfinite(b)):
                return math.nan
            return a / (b if abs(b) > eps else (eps if b >= 0 else -eps))

        if lockin is not None:
            self.MEASUREMENT_FUNCS['Lock-in X only']           = lambda: (lockin.read_x(),)
            self.MEASUREMENT_FUNCS['Lock-in (X, Y, R, θ)']    = lambda: (lockin.read_x(), lockin.read_y(), lockin.read_r(), lockin.read_theta())
            self.MEASUREMENT_FUNCS['PC and Reflectivity']      = lambda: (lockin.read_x(), lockin.read_x2())
            self.MEASUREMENT_FUNCS['Double PC (X,Y of both)']  = lambda: (*lockin.read_xy(), *lockin.read_xy2())
            self.MEASUREMENT_FUNCS['PC and Offset']            = lambda: (*lockin.read_xy(), self.state.settings.get('x_offset', math.nan), self.state.settings.get('y_offset', math.nan), math.nan)
            self.MEASUREMENT_FUNCS['R (calc), I, V']           = lambda: (_safe_div(lockin.read_x2(), lockin.read_x()), lockin.read_x(), lockin.read_x2(), math.nan, math.nan, math.nan)
            self.MEASUREMENT_FUNCS['Circular Dichroism']       = lambda: (lockin.read_x(), lockin.read_x2(), _safe_div(lockin.read_x(), lockin.read_x2()), math.nan, math.nan, math.nan)

        self.MEASUREMENT_FUNCS['(dummy) all NaN'] = lambda: (math.nan,) * 6

        # ── UI ────────────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)
        ctrl  = QVBoxLayout()
        form  = QFormLayout()

        dv     = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_pos = QDoubleValidator(0.0, 1e300, 9, self)

        self.fast_axis_combo = QComboBox()
        self.fast_axis_combo.addItems(['X fast', 'Y fast', 'Z fast'])

        self.z_setter_combo = QComboBox()
        self.z_setter_combo.addItems(list(self.AXIS_Z_SETTERS.keys()))
        if 'Pump HWP (°)' in self.AXIS_Z_SETTERS:
            self.z_setter_combo.setCurrentText('Pump HWP (°)')
        self.z_start = QLineEdit('0');   self.z_start.setValidator(dv)
        self.z_end   = QLineEdit('180'); self.z_end.setValidator(dv)
        self.z_step  = QLineEdit('10');  self.z_step.setValidator(dv_pos)

        self.x_setter_combo = QComboBox()
        self.x_setter_combo.addItems(list(self.AXIS_X_SETTERS.keys()))
        if 'Galvo X (mu)' in self.AXIS_X_SETTERS:
            self.x_setter_combo.setCurrentText('Galvo X (mu)')
        self.x_start = QLineEdit('-0.01'); self.x_start.setValidator(dv)
        self.x_end   = QLineEdit('0.01');  self.x_end.setValidator(dv)
        self.x_step  = QLineEdit('0.001'); self.x_step.setValidator(dv_pos)

        self.y_setter_combo = QComboBox()
        self.y_setter_combo.addItems(list(self.AXIS_Y_SETTERS.keys()))
        if 'Galvo Y (mu)' in self.AXIS_Y_SETTERS:
            self.y_setter_combo.setCurrentText('Galvo Y (mu)')
        self.y_start = QLineEdit('-0.01'); self.y_start.setValidator(dv)
        self.y_end   = QLineEdit('0.01');  self.y_end.setValidator(dv)
        self.y_step  = QLineEdit('0.001'); self.y_step.setValidator(dv_pos)

        self.meas_combo    = QComboBox()
        self.meas_combo.addItems(list(self.MEASUREMENT_FUNCS.keys()))
        self.display_combo = QComboBox()
        self.display_combo.addItems([f'data{i}' for i in range(1, 7)])

        self.wait_ms     = QLineEdit('5'); self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))
        self.z_reset_ms  = QLineEdit('0'); self.z_reset_ms.setValidator(QIntValidator(0, 1_000_000, self))

        self.align_chk     = QCheckBox('Align during scan')
        self.align_every_n = QLineEdit('50')
        self.align_every_n.setValidator(QIntValidator(1, 1_000_000, self))

        self.folder   = QLineEdit(os.path.expanduser('~/three_d_scans'))
        self.filename = QLineEdit('scan_3d')
        browse_btn = QPushButton('Browse…')
        browse_btn.clicked.connect(self._browse)

        self.start_btn  = QPushButton('Start')
        self.stop_btn   = QPushButton('Stop'); self.stop_btn.setEnabled(False)
        self.save_btn   = QPushButton('Save')
        self.status_lbl = QLabel('Idle.')

        form.addRow('Fast axis:',         self.fast_axis_combo)
        form.addRow('Z setter:',          self.z_setter_combo)
        form.addRow('Z start:',           self.z_start)
        form.addRow('Z end:',             self.z_end)
        form.addRow('Z step:',            self.z_step)
        form.addRow('X setter:',          self.x_setter_combo)
        form.addRow('X start:',           self.x_start)
        form.addRow('X end:',             self.x_end)
        form.addRow('X step:',            self.x_step)
        form.addRow('Y setter:',          self.y_setter_combo)
        form.addRow('Y start:',           self.y_start)
        form.addRow('Y end:',             self.y_end)
        form.addRow('Y step:',            self.y_step)
        form.addRow('Measurement:',       self.meas_combo)
        form.addRow('Display channel:',   self.display_combo)
        form.addRow('Wait (ms):',         self.wait_ms)
        form.addRow('Z reset wait (ms):', self.z_reset_ms)
        form.addRow('',                   self.align_chk)
        form.addRow('Align every N px:',  self.align_every_n)

        save_row = QGridLayout()
        save_row.addWidget(QLabel('Folder:'),        0, 0)
        save_row.addWidget(self.folder,              0, 1)
        save_row.addWidget(browse_btn,               0, 2)
        save_row.addWidget(QLabel('File (no ext):'), 1, 0)
        save_row.addWidget(self.filename,            1, 1)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.save_btn)

        ctrl.addLayout(form)
        ctrl.addLayout(save_row)
        ctrl.addLayout(btn_row)
        ctrl.addWidget(self.status_lbl)
        ctrl.addStretch(1)

        right = QVBoxLayout()
        self.fig_img = Figure(figsize=(6, 6), tight_layout=False)
        self.fig_img.set_facecolor('#f5f5f5')
        self.ax_img  = self.fig_img.add_subplot(111)
        self.ax_img.set_facecolor('#ffffff')
        divider = make_axes_locatable(self.ax_img)
        self._cax  = divider.append_axes('right', size='4%', pad=0.05)
        self._cbar = None
        self.ax_img.set_title('Live 3D Scan')
        self.canvas_img = FigureCanvas(self.fig_img)
        right.addWidget(self.canvas_img, stretch=1)

        outer.addLayout(ctrl, stretch=1)
        outer.addLayout(right, stretch=5)

        # ── Runtime state ─────────────────────────────────────────────────────
        self._stop_flag: List[bool] = [False]
        self._worker: _Scan3DWorker | None = None
        self._zs: np.ndarray | None = None
        self._xs: np.ndarray | None = None
        self._ys: np.ndarray | None = None
        self.data: np.ndarray | None = None   # (Nz, Ny, Nx, 6)
        self._scan_index: int = 0
        self._im_artist = None

        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)
        self.display_combo.currentIndexChanged.connect(self._redraw_image)

        # ── Agent signal connections ───────────────────────────────────────────
        sigs = controller.scan_signals
        sigs.scan_3d_configured.connect(self._agent_scan_configured, Qt.ConnectionType.QueuedConnection)
        sigs.scan_3d_point.connect(self._agent_point_ready,          Qt.ConnectionType.QueuedConnection)
        sigs.scan_3d_layer_done.connect(self._agent_layer_done,      Qt.ConnectionType.QueuedConnection)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_z_fast(self) -> bool:
        return self.fast_axis_combo.currentText().startswith('Z')

    def _selected_ch(self) -> int:
        return max(0, min(5, self.display_combo.currentIndex()))

    def _current_image(self) -> np.ndarray | None:
        if self.data is None:
            return None
        s = self._selected_ch()
        if self._is_z_fast():
            with np.errstate(all='ignore'):
                return np.nanmean(self.data[:, :, :, s], axis=0)
        return self.data[self._scan_index, :, :, s]

    def _auto_clim(self, img: np.ndarray):
        vals = img[np.isfinite(img)]
        if vals.size == 0:
            return 0.0, 1.0
        vmin, vmax = float(vals.min()), float(vals.max())
        if vmin == vmax:
            return vmin - 0.5, vmax + 0.5
        return vmin, vmax

    def _update_image(self, img: np.ndarray, title: str = 'Live 3D Scan'):
        if self._xs is None or self._ys is None:
            return
        extent = [self._xs.min(), self._xs.max(), self._ys.min(), self._ys.max()]
        if self._im_artist is None:
            self._im_artist = self.ax_img.imshow(
                img, origin='lower', extent=extent, aspect='auto'
            )
            self.ax_img.set_aspect('auto')
            if self._cbar is None:
                self._cbar = self.fig_img.colorbar(self._im_artist, cax=self._cax)
        else:
            self._im_artist.set_data(img)
            self._im_artist.set_extent(extent)
        self._im_artist.set_clim(*self._auto_clim(img))
        self._cbar.set_label(self.display_combo.currentText())
        self.ax_img.set_title(title)
        self.canvas_img.draw_idle()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose folder', self.folder.text())
        if path:
            self.folder.setText(path)

    def _build_axis_arrays(self):
        try:
            z0, z1, dz = float(self.z_start.text()), float(self.z_end.text()), float(self.z_step.text())
            x0, x1, dx = float(self.x_start.text()), float(self.x_end.text()), float(self.x_step.text())
            y0, y1, dy = float(self.y_start.text()), float(self.y_end.text()), float(self.y_step.text())
            if dz <= 0 or dx <= 0 or dy <= 0:
                raise ValueError
        except Exception:
            QMessageBox.warning(self, 'Input error', 'Start/end/step values invalid.')
            return None
        for a0, a1, label in ((z0, z1, 'Z'), (x0, x1, 'X'), (y0, y1, 'Y')):
            if a0 == a1:
                QMessageBox.warning(self, 'Input error', f'{label} range must be non-zero.')
                return None
        nz = int(np.floor(abs((z1 - z0) / dz)) + 1)
        nx = int(np.floor(abs((x1 - x0) / dx)) + 1)
        ny = int(np.floor(abs((y1 - y0) / dy)) + 1)
        zs = np.linspace(z0, z0 + (nz - 1) * dz * np.sign(z1 - z0), nz)
        xs = np.linspace(x0, x0 + (nx - 1) * dx * np.sign(x1 - x0), nx)
        ys = np.linspace(y0, y0 + (ny - 1) * dy * np.sign(y1 - y0), ny)
        return zs, xs, ys

    def _start(self):
        axes = self._build_axis_arrays()
        if axes is None:
            return
        zs, xs, ys = axes

        try:
            wait_s    = max(0.0, int(self.wait_ms.text()) / 1000.0)
            z_reset_s = max(0.0, int(self.z_reset_ms.text()) / 1000.0)
        except Exception:
            QMessageBox.warning(self, 'Input error', 'Wait times invalid.')
            return

        meas_key   = self.meas_combo.currentText()
        measure_fn = self.MEASUREMENT_FUNCS.get(meas_key)
        set_z      = self.AXIS_Z_SETTERS.get(self.z_setter_combo.currentText())
        set_x      = self.AXIS_X_SETTERS.get(self.x_setter_combo.currentText())
        set_y      = self.AXIS_Y_SETTERS.get(self.y_setter_combo.currentText())
        if None in (measure_fn, set_z, set_x, set_y):
            QMessageBox.critical(self, 'Config error', 'Axis or measurement not found.')
            return

        fast_is_x = self.fast_axis_combo.currentText().startswith('X')
        _last = [float('nan'), float('nan')]

        def _seq_xy(x: float, y: float):
            x, y = float(x), float(y)
            xc, yc = (x != _last[0]), (y != _last[1])
            _last[0], _last[1] = x, y
            if fast_is_x:
                if yc: set_y(y)
                if xc: set_x(x)
            else:
                if xc: set_x(x)
                if yc: set_y(y)

        galvo_scan = (self.x_setter_combo.currentText().startswith('Galvo') and
                      self.y_setter_combo.currentText().startswith('Galvo'))

        align_enabled = self.align_chk.isChecked()
        if align_enabled and self.alignment_tab is None:
            QMessageBox.warning(self, 'Alignment', 'Alignment tab not available.')
            align_enabled = False
        if align_enabled:
            self.alignment_tab._cached_alignment_params = self.alignment_tab.get_alignment_params()

        try:
            align_every_n = max(1, int(self.align_every_n.text()))
        except Exception:
            align_every_n = 50

        self._zs, self._xs, self._ys = zs, xs, ys
        Nz, Ny, Nx = len(zs), len(ys), len(xs)
        self.data = np.full((Nz, Ny, Nx, 6), np.nan, float)
        self._scan_index = 0
        self._stop_flag[0] = False

        blank = np.full((Ny, Nx), np.nan, float)
        self._update_image(blank)

        fast_text = self.fast_axis_combo.currentText()
        fast_key  = 'Z' if fast_text.startswith('Z') else ('X' if fast_text.startswith('X') else 'Y')

        self._worker = _Scan3DWorker(
            zs=zs, xs=xs, ys=ys,
            set_z=set_z, set_xy=_seq_xy,
            measure_fn=measure_fn,
            wait_s=wait_s, stop_flag_ref=self._stop_flag,
            fast_axis=fast_key,
            z_reset_s=z_reset_s,
            align_enabled=align_enabled,
            alignment_tab=self.alignment_tab,
            align_every_n=align_every_n,
            galvo_scan=galvo_scan,
            state=self.state,
            galvo_ref=self.controller.get_instrument('Galvo'),
        )
        self._worker.progress.connect(self._on_progress,  Qt.ConnectionType.QueuedConnection)
        self._worker.scan_done.connect(self._on_scan_done, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished,  Qt.ConnectionType.QueuedConnection)
        self._worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText('Scanning…')
        self.controller.log(
            f'3D scan started: Z={self.z_setter_combo.currentText()} '
            f'X={self.x_setter_combo.currentText()} Y={self.y_setter_combo.currentText()} '
            f'meas={meas_key}'
        )

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText('Stopping…')

    # ── Worker signal handlers ────────────────────────────────────────────────

    def _on_progress(self, iz: int, ix: int, iy: int,
                     z: float, x: float, y: float, d6: tuple):
        if self.data is None:
            return
        Nz, Ny, Nx, _ = self.data.shape
        if 0 <= iz < Nz and 0 <= ix < Nx and 0 <= iy < Ny:
            self.data[iz, iy, ix, :] = np.asarray(d6, float)
        img = self._current_image()
        if img is not None:
            label = (f'Z-fast XY map — z={z:.4g}, xy=({x:.4g},{y:.4g})'
                     if self._is_z_fast()
                     else f'Layer z={z:.4g} — last ({x:.4g},{y:.4g})')
            self._update_image(img, label)

    def _on_scan_done(self, count: int):
        if self.data is None:
            return
        if self._is_z_fast():
            total = self.data.shape[1] * self.data.shape[2]
            self.status_lbl.setText(f'XY point {count} / {total}')
        else:
            self.status_lbl.setText(f'Completed Z layer {count}')
            self._scan_index = min(self._scan_index + 1, self.data.shape[0] - 1)
        img = self._current_image()
        if img is not None:
            self._update_image(img)

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self._worker = None
        self.controller.log(f'3D scan finished: {message}')

    def _redraw_image(self):
        img = self._current_image()
        if img is not None:
            self._update_image(img)

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self):
        if self.data is None or self._zs is None:
            QMessageBox.information(self, 'Nothing to save', 'Run a scan first.')
            return

        folder = self.folder.text().strip()
        base   = self.filename.text().strip() or 'scan_3d'
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, 'Folder error', str(e))
            return

        # avoid overwriting existing files
        used = {os.path.splitext(f)[0] for f in os.listdir(folder)} if os.path.isdir(folder) else set()
        safe = base
        if safe in used:
            i = 1
            while f'{base}_{i}' in used:
                i += 1
            safe = f'{base}_{i}'
            self.filename.setText(safe)

        png_path = os.path.join(folder, f'{safe}.png')
        txt_path = os.path.join(folder, f'{safe}.txt')

        try:
            self.fig_img.savefig(png_path, dpi=200, bbox_inches='tight')
        except Exception as e:
            QMessageBox.critical(self, 'Save error', f'PNG failed: {e}')
            return

        Nz, Ny, Nx, _ = self.data.shape
        rows = []
        for iz in range(Nz):
            for iy in range(Ny):
                for ix in range(Nx):
                    d6 = self.data[iz, iy, ix, :]
                    if np.isfinite(d6).any():
                        rows.append([float(self._zs[iz]), float(self._xs[ix]), float(self._ys[iy]),
                                     *[float(v) for v in d6]])
        if not rows:
            QMessageBox.information(self, 'Nothing to save', 'No valid data points yet.')
            return

        zs_str = f'{self._zs[0]} to {self._zs[-1]} (Nz={len(self._zs)})'
        xs_str = f'{self._xs[0]} to {self._xs[-1]} (Nx={len(self._xs)})'
        ys_str = f'{self._ys[0]} to {self._ys[-1]} (Ny={len(self._ys)})'
        header = '\n'.join([
            '3D Scan',
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            '',
            'Parameters:',
            f'  Fast axis: {self.fast_axis_combo.currentText()}',
            f'  Z setter: {self.z_setter_combo.currentText()}',
            f'  X setter: {self.x_setter_combo.currentText()}',
            f'  Y setter: {self.y_setter_combo.currentText()}',
            f'  Measurement: {self.meas_combo.currentText()}',
            f'  Z: {self.z_start.text()} to {self.z_end.text()} step {self.z_step.text()}',
            f'  X: {self.x_start.text()} to {self.x_end.text()} step {self.x_step.text()}',
            f'  Y: {self.y_start.text()} to {self.y_end.text()} step {self.y_step.text()}',
            f'  Wait (ms): {self.wait_ms.text()}',
            f'  Z reset wait (ms): {self.z_reset_ms.text()}',
            f'  Alignment enabled: {self.align_chk.isChecked()}',
            f'  Align every N pixels: {self.align_every_n.text()}',
            f'  Screenshot: {os.path.basename(png_path)}',
            '',
            f'Data shape: {self.data.shape}  (Nz, Ny, Nx, 6)',
            f'Z array: {zs_str}',
            f'X array: {xs_str}',
            f'Y array: {ys_str}',
            '',
            'Columns: [z, x, y, data1, data2, data3, data4, data5, data6]',
            '',
            'Table begins below this header.',
        ])
        try:
            np.savetxt(txt_path, np.asarray(rows, float),
                       delimiter='\t', fmt='%.10g', header=header, comments='')
        except Exception as e:
            QMessageBox.critical(self, 'Save error', f'TXT failed: {e}')
            return

        self.controller.log(f'3D scan saved: {txt_path}')
        QMessageBox.information(self, 'Saved', f'Saved:\n{txt_path}\n{png_path}')

    # ── Agent live-plot handlers ──────────────────────────────────────────────

    def _agent_scan_configured(self, params: dict):
        """Update GUI fields and allocate data array for an agent-driven 3D scan."""
        self.apply_settings(params)
        # Re-derive axis arrays so live plotting works
        axes = self._build_axis_arrays()
        if axes is not None:
            self._zs, self._xs, self._ys = axes
            Nz, Ny, Nx = len(self._zs), len(self._ys), len(self._xs)
            self.data = np.full((Nz, Ny, Nx, 6), np.nan, float)
            self._scan_index = 0
            self._im_artist = None
            self.ax_img.cla()
            if self._cbar is not None:
                self._cbar.remove()
                self._cbar = None
            divider = make_axes_locatable(self.ax_img)
            self._cax = divider.append_axes('right', size='4%', pad=0.05)
            self.status_lbl.setText('Agent scan running…')

    def _agent_point_ready(self, iz: int, ix: int, iy: int,
                           z: float, x: float, y: float, d6: tuple):
        """Store and plot an agent-driven measurement point."""
        if self.data is None:
            return
        Nz, Ny, Nx, _ = self.data.shape
        if 0 <= iz < Nz and 0 <= ix < Nx and 0 <= iy < Ny:
            self.data[iz, iy, ix, :] = np.asarray(d6, float)
        img = self._current_image()
        if img is not None:
            self._update_image(img, f'Agent 3D scan — z={z:.4g} xy=({x:.4g},{y:.4g})')

    def _agent_layer_done(self, count: int):
        if self.data is None:
            return
        self.status_lbl.setText(f'Agent: layer/point {count} done')
        img = self._current_image()
        if img is not None:
            self._update_image(img)

    # ── Agent API ─────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        """Return current UI settings as a plain dict (for the agent)."""
        return {
            "fast_axis":      self.fast_axis_combo.currentText(),
            "z_setter":       self.z_setter_combo.currentText(),
            "z_start":        self.z_start.text(),
            "z_end":          self.z_end.text(),
            "z_step":         self.z_step.text(),
            "x_setter":       self.x_setter_combo.currentText(),
            "x_start":        self.x_start.text(),
            "x_end":          self.x_end.text(),
            "x_step":         self.x_step.text(),
            "y_setter":       self.y_setter_combo.currentText(),
            "y_start":        self.y_start.text(),
            "y_end":          self.y_end.text(),
            "y_step":         self.y_step.text(),
            "measurement":    self.meas_combo.currentText(),
            "display_channel": self.display_combo.currentText(),
            "wait_ms":        self.wait_ms.text(),
            "z_reset_ms":     self.z_reset_ms.text(),
            "align_enabled":  self.align_chk.isChecked(),
            "align_every_n":  self.align_every_n.text(),
            "folder":         self.folder.text(),
            "filename":       self.filename.text(),
            "z_setters_available":  list(self.AXIS_Z_SETTERS.keys()),
            "x_setters_available":  list(self.AXIS_X_SETTERS.keys()),
            "y_setters_available":  list(self.AXIS_Y_SETTERS.keys()),
            "measurements_available": list(self.MEASUREMENT_FUNCS.keys()),
        }

    def apply_settings(self, settings: dict) -> list[str]:
        """
        Apply a dict of settings from the agent.  Returns a list of
        warnings for any keys that were unrecognised or had invalid values.
        """
        warnings: list[str] = []

        def _set_combo(combo, key):
            if key in settings:
                v = str(settings[key])
                idx = combo.findText(v)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    warnings.append(f"'{v}' is not a valid choice for '{key}'")

        def _set_text(widget, key):
            if key in settings:
                widget.setText(str(settings[key]))

        _set_combo(self.fast_axis_combo, "fast_axis")
        _set_combo(self.z_setter_combo,  "z_setter")
        _set_combo(self.x_setter_combo,  "x_setter")
        _set_combo(self.y_setter_combo,  "y_setter")
        _set_combo(self.meas_combo,      "measurement")
        _set_combo(self.display_combo,   "display_channel")

        _set_text(self.z_start,      "z_start")
        _set_text(self.z_end,        "z_end")
        _set_text(self.z_step,       "z_step")
        _set_text(self.x_start,      "x_start")
        _set_text(self.x_end,        "x_end")
        _set_text(self.x_step,       "x_step")
        _set_text(self.y_start,      "y_start")
        _set_text(self.y_end,        "y_end")
        _set_text(self.y_step,       "y_step")
        _set_text(self.wait_ms,      "wait_ms")
        _set_text(self.z_reset_ms,   "z_reset_ms")
        _set_text(self.align_every_n,"align_every_n")
        _set_text(self.folder,       "folder")
        _set_text(self.filename,     "filename")

        if "align_enabled" in settings:
            self.align_chk.setChecked(bool(settings["align_enabled"]))

        return warnings

    def set_filename(self, name: str):
        self.filename.setText(name.strip())

    def save_data(self, filename: str | None = None) -> str:
        """Save current 3D scan data. Returns the .txt path or raises RuntimeError."""
        if self.data is None or self._zs is None:
            raise RuntimeError("No scan data to save — run a scan first.")
        if filename:
            self.filename.setText(filename.strip())
        self._save()
        folder = self.folder.text().strip()
        base   = self.filename.text().strip() or "scan_3d"
        return os.path.join(folder, f"{base}.txt")

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
