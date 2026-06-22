# tabs/two_d_scan_tab.py
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

class _Scan2DWorker(QThread):
    progress  = pyqtSignal(int, int, float, float, tuple)  # ix, iy, x, y, d6
    scan_done = pyqtSignal(int)                             # scan index (1-based)
    finished  = pyqtSignal(bool, str)                      # user_abort, message

    def __init__(
        self,
        xs: np.ndarray,
        ys: np.ndarray,
        set_xy: Callable[[float, float], None],
        measure_fn: Callable[[], Tuple[float, ...]],
        wait_s: float,
        stop_flag_ref: List[bool],
        fast_axis: str,
        n_scans: int = 1,
        align_enabled: bool = False,
        alignment_tab=None,
        align_every_n: int = 1_000_000,
        galvo_scan: bool = True,
        state=None,
        galvo_ref=None,
    ):
        super().__init__()
        self._xs            = np.asarray(xs, float)
        self._ys            = np.asarray(ys, float)
        self._set_xy        = set_xy
        self._measure       = measure_fn
        self._wait_s        = max(0.0, float(wait_s))
        self._stop          = stop_flag_ref
        self._fast          = 'X' if str(fast_axis).upper().startswith('X') else 'Y'
        self._n_scans       = max(1, int(n_scans))
        self._align_enabled = align_enabled
        self._alignment_tab = alignment_tab
        self._align_every_n = max(1, int(align_every_n))
        self._galvo_scan    = galvo_scan
        self._state         = state
        self._galvo_ref     = galvo_ref
        self.x_offset       = 0.0
        self.y_offset       = 0.0

    def _run_alignment(self, target_x: float, target_y: float):
        """Run alignment synchronously, update offsets, then return to scan position."""
        dx, dy = self._alignment_tab.run_alignment_blocking(self._stop)
        if abs(dx) > 30.0 or abs(dy) > 30.0:
            print(f'Warning: alignment offset ({dx:.4f},{dy:.4f}) exceeds ±30 — offsets ignored.')
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

    def _iter_coords(self):
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
            for s in range(1, self._n_scans + 1):
                pixel_count = 0
                for ix, iy, x, y in self._iter_coords():
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
                        self.finished.emit(True, f'Axis move error at ({x}, {y}): {e}')
                        return
                    if self._wait_s > 0:
                        time.sleep(self._wait_s)
                    if self._stop[0]:
                        self.finished.emit(True, 'Scan stopped by user.')
                        return
                    try:
                        d = self._measure()
                    except Exception as e:
                        self.finished.emit(True, f'Measurement error at ({x}, {y}): {e}')
                        return
                    if not isinstance(d, (list, tuple)):
                        d = (float(d),)
                    d6 = tuple([float(v) for v in (list(d) + [math.nan] * 6)[:6]])
                    self.progress.emit(int(ix), int(iy), float(x), float(y), d6)
                    pixel_count += 1
                self.scan_done.emit(s)
            self.finished.emit(False, '2D scan(s) complete.')
        except Exception as e:
            self.finished.emit(True, f'Unexpected error: {e}')


# ── Tab ───────────────────────────────────────────────────────────────────────

class TwoDScanTab(QWidget):
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
        self.AXIS_X_SETTERS: Dict[str, Callable[[float], None]] = {}
        self.AXIS_Y_SETTERS: Dict[str, Callable[[float], None]] = {}

        if stage is not None:
            self.AXIS_X_SETTERS['Newport (mm)'] = lambda x: stage.move(float(x))
            self.AXIS_Y_SETTERS['Newport (mm)'] = lambda y: stage.move(float(y))
            self.AXIS_X_SETTERS['Delay (ps)']   = lambda x: stage.move_ps(float(x))
            self.AXIS_Y_SETTERS['Delay (ps)']   = lambda y: stage.move_ps(float(y))

        if galvo is not None:
            self.AXIS_X_SETTERS['Galvo X (mu)'] = lambda x: galvo.move_x(float(x))
            self.AXIS_X_SETTERS['Galvo Y (mu)'] = lambda x: galvo.move_y(float(x))
            self.AXIS_Y_SETTERS['Galvo X (mu)'] = lambda y: galvo.move_x(float(y))
            self.AXIS_Y_SETTERS['Galvo Y (mu)'] = lambda y: galvo.move_y(float(y))

        if k2400 is not None:
            self.AXIS_X_SETTERS['Keithley V'] = lambda x: k2400.ramp_voltage(float(x))
            self.AXIS_Y_SETTERS['Keithley V'] = lambda y: k2400.ramp_voltage(float(y))

        if btt is not None and pm is not None and shutter is not None:
            self.AXIS_X_SETTERS['Pump Power (µW)']  = lambda x: tune_pump_power_blocking(float(x), pm, btt, shutter, self.state)
            self.AXIS_Y_SETTERS['Pump Power (µW)']  = lambda y: tune_pump_power_blocking(float(y), pm, btt, shutter, self.state)
            self.AXIS_X_SETTERS['Probe Power (µW)'] = lambda x: tune_probe_power_blocking(float(x), pm, btt, shutter, self.state)
            self.AXIS_Y_SETTERS['Probe Power (µW)'] = lambda y: tune_probe_power_blocking(float(y), pm, btt, shutter, self.state)

        if btt is not None:
            self.AXIS_X_SETTERS['Fast Probe Power (µW)'] = lambda x: set_fast_probe_power_blocking(float(x), btt, self.state)
            self.AXIS_Y_SETTERS['Fast Probe Power (µW)'] = lambda y: set_fast_probe_power_blocking(float(y), btt, self.state)
            self.AXIS_X_SETTERS['Pump HWP (°)'] = lambda x: btt.rot_3(float(x), 5000)
            self.AXIS_Y_SETTERS['Pump HWP (°)'] = lambda y: btt.rot_3(float(y), 5000)
            self.AXIS_X_SETTERS['Rot 1 (°)']    = lambda x: btt.rot_1(float(x), 5000)
            self.AXIS_Y_SETTERS['Rot 1 (°)']    = lambda y: btt.rot_1(float(y), 5000)

        self.AXIS_X_SETTERS['(dummy) X no-op'] = lambda x: None
        self.AXIS_Y_SETTERS['(dummy) Y no-op'] = lambda y: None

        # ── Measurement funcs ─────────────────────────────────────────────────
        self.MEASUREMENT_FUNCS: Dict[str, Callable[[], Tuple[float, ...]]] = {}

        def _safe_div(a, b, eps=1e-12):
            if not (math.isfinite(a) and math.isfinite(b)):
                return math.nan
            return a / (b if abs(b) > eps else (eps if b >= 0 else -eps))

        if lockin is not None:
            self.MEASUREMENT_FUNCS['Lock-in X only']         = lambda: (lockin.read_x(),)
            self.MEASUREMENT_FUNCS['Lock-in (X, Y, R, θ)']  = lambda: (lockin.read_x(), lockin.read_y(), lockin.read_r(), lockin.read_theta())
            self.MEASUREMENT_FUNCS['PC and Reflectivity']    = lambda: (lockin.read_x(), lockin.read_x2())
            self.MEASUREMENT_FUNCS['Double PC (X,Y of both)'] = lambda: (*lockin.read_xy(), *lockin.read_xy2())
            self.MEASUREMENT_FUNCS['PC and Offset']          = lambda: (*lockin.read_xy(), self.state.settings.get('x_offset', math.nan), self.state.settings.get('y_offset', math.nan), math.nan)
            self.MEASUREMENT_FUNCS['R (calc), I, V']         = lambda: (_safe_div(lockin.read_x2(), lockin.read_x()), lockin.read_x(), lockin.read_x2(), math.nan, math.nan, math.nan)
            self.MEASUREMENT_FUNCS['Circular Dichroism']     = lambda: (lockin.read_x(), lockin.read_x2(), _safe_div(lockin.read_x(), lockin.read_x2()), math.nan, math.nan, math.nan)

        self.MEASUREMENT_FUNCS['(dummy) all NaN'] = lambda: (math.nan,) * 6

        # ── UI ────────────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)

        ctrl = QVBoxLayout()
        form = QFormLayout()

        dv     = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_pos = QDoubleValidator(0.0, 1e300, 9, self)

        self.fast_axis_combo = QComboBox()
        self.fast_axis_combo.addItems(['X fast', 'Y fast'])

        self.x_setter_combo = QComboBox()
        self.x_setter_combo.addItems(list(self.AXIS_X_SETTERS.keys()))
        if 'Galvo X' in self.AXIS_X_SETTERS:
            self.x_setter_combo.setCurrentText('Galvo X (mu)')

        self.x_start = QLineEdit('-0.01'); self.x_start.setValidator(dv)
        self.x_end   = QLineEdit('0.01');  self.x_end.setValidator(dv)
        self.x_step  = QLineEdit('0.001'); self.x_step.setValidator(dv_pos)

        self.y_setter_combo = QComboBox()
        self.y_setter_combo.addItems(list(self.AXIS_Y_SETTERS.keys()))
        if 'Galvo Y' in self.AXIS_Y_SETTERS:
            self.y_setter_combo.setCurrentText('Galvo Y (mu)')

        self.y_start = QLineEdit('-0.01'); self.y_start.setValidator(dv)
        self.y_end   = QLineEdit('0.01');  self.y_end.setValidator(dv)
        self.y_step  = QLineEdit('0.001'); self.y_step.setValidator(dv_pos)

        self.meas_combo    = QComboBox()
        self.meas_combo.addItems(list(self.MEASUREMENT_FUNCS.keys()))
        self.display_combo = QComboBox()
        self.display_combo.addItems([f'data{i}' for i in range(1, 7)])

        self.wait_ms = QLineEdit('5');  self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))
        self.n_scans = QLineEdit('1');  self.n_scans.setValidator(QIntValidator(1, 10_000, self))

        self.show_avg_chk = QCheckBox('Show average')

        self.align_chk     = QCheckBox('Align during scan')
        self.align_every_n = QLineEdit('50')
        self.align_every_n.setValidator(QIntValidator(1, 1_000_000, self))

        self.folder   = QLineEdit(os.path.expanduser('~/two_d_scans'))
        self.filename = QLineEdit('scan_2d')
        browse_btn = QPushButton('Browse…')
        browse_btn.clicked.connect(self._browse)

        self.start_btn = QPushButton('Start')
        self.stop_btn  = QPushButton('Stop'); self.stop_btn.setEnabled(False)
        self.save_btn  = QPushButton('Save')
        self.status_lbl = QLabel('Idle.')

        form.addRow('Fast axis:',       self.fast_axis_combo)
        form.addRow('X setter:',        self.x_setter_combo)
        form.addRow('X start:',         self.x_start)
        form.addRow('X end:',           self.x_end)
        form.addRow('X step:',          self.x_step)
        form.addRow('Y setter:',        self.y_setter_combo)
        form.addRow('Y start:',         self.y_start)
        form.addRow('Y end:',           self.y_end)
        form.addRow('Y step:',          self.y_step)
        form.addRow('Measurement:',     self.meas_combo)
        form.addRow('Display channel:', self.display_combo)
        form.addRow('Wait (ms):',       self.wait_ms)
        form.addRow('Averages:',        self.n_scans)
        form.addRow('',                 self.show_avg_chk)
        form.addRow('',                 self.align_chk)
        form.addRow('Align every N px:', self.align_every_n)

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

        # Right: live image with fixed colorbar axis
        right = QVBoxLayout()
        self.fig_img = Figure(figsize=(6, 6), tight_layout=False)
        self.fig_img.set_facecolor('#f5f5f5')
        self.ax_img  = self.fig_img.add_subplot(111)
        self.ax_img.set_facecolor('#ffffff')
        divider = make_axes_locatable(self.ax_img)
        self._cax  = divider.append_axes('right', size='4%', pad=0.05)
        self._cbar = None
        self.ax_img.set_title('Live 2D Image')
        self.ax_img.set_xlabel('X')
        self.ax_img.set_ylabel('Y')
        self.canvas_img = FigureCanvas(self.fig_img)
        right.addWidget(self.canvas_img, stretch=1)

        outer.addLayout(ctrl, stretch=1)
        outer.addLayout(right, stretch=5)

        # ── Runtime state ─────────────────────────────────────────────────────
        self._stop_flag: List[bool] = [False]
        self._worker: _Scan2DWorker | None = None
        self._xs: np.ndarray | None = None
        self._ys: np.ndarray | None = None
        self.data: np.ndarray | None = None      # (n_scans, Ny, Nx, 6)
        self._scan_index: int = 0
        self._im_artist = None

        # ── Connections — manual controls ─────────────────────────────────────
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)
        self.display_combo.currentIndexChanged.connect(self._redraw_image)
        self.show_avg_chk.stateChanged.connect(self._redraw_image)

        # ── Connections — agent signals ───────────────────────────────────────
        sigs = controller.scan_signals
        sigs.scan_2d_configured.connect(self._agent_scan_configured, Qt.ConnectionType.QueuedConnection)
        sigs.scan_2d_started.connect(self._agent_scan_started,       Qt.ConnectionType.QueuedConnection)
        sigs.scan_2d_point.connect(self._agent_point_ready,          Qt.ConnectionType.QueuedConnection)
        sigs.scan_2d_done.connect(self._agent_scan_done,             Qt.ConnectionType.QueuedConnection)
        sigs.stop_all.connect(self._stop,                            Qt.ConnectionType.DirectConnection)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _selected_ch(self) -> int:
        return max(0, min(5, self.display_combo.currentIndex()))

    def _current_or_average(self) -> np.ndarray | None:
        if self.data is None:
            return None
        s = self._selected_ch()
        cur = self.data[self._scan_index, :, :, s]
        if not self.show_avg_chk.isChecked():
            return cur
        block = self.data[:self._scan_index + 1, :, :, s]
        with np.errstate(invalid='ignore'):
            return np.nanmean(block, axis=0)

    def _auto_clim(self, img: np.ndarray):
        vals = img[np.isfinite(img)]
        if vals.size == 0:
            return 0.0, 1.0
        vmin, vmax = float(vals.min()), float(vals.max())
        if vmin == vmax:
            return vmin - 0.5, vmax + 0.5
        return vmin, vmax

    def _update_image(self, img: np.ndarray, title: str = 'Live 2D Image'):
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

    # ── Manual scan actions ───────────────────────────────────────────────────

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose folder', self.folder.text())
        if path:
            self.folder.setText(path)

    def _build_axis_arrays(self) -> tuple[np.ndarray, np.ndarray] | None:
        try:
            x0, x1, dx = float(self.x_start.text()), float(self.x_end.text()), float(self.x_step.text())
            y0, y1, dy = float(self.y_start.text()), float(self.y_end.text()), float(self.y_step.text())
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

    def _start(self):
        axes = self._build_axis_arrays()
        if axes is None:
            return
        xs, ys = axes

        try:
            wait_s  = max(0.0, int(self.wait_ms.text()) / 1000.0)
            n_scans = int(self.n_scans.text())
            assert n_scans >= 1
        except Exception:
            QMessageBox.warning(self, 'Input error', 'Averages must be ≥ 1.')
            return

        meas_key   = self.meas_combo.currentText()
        measure_fn = self.MEASUREMENT_FUNCS.get(meas_key)
        set_x      = self.AXIS_X_SETTERS.get(self.x_setter_combo.currentText())
        set_y      = self.AXIS_Y_SETTERS.get(self.y_setter_combo.currentText())
        if None in (measure_fn, set_x, set_y):
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

        self._xs, self._ys = xs, ys
        self.data = np.full((n_scans, len(ys), len(xs), 6), np.nan, float)
        self._scan_index = 0
        self._stop_flag[0] = False

        blank = np.full((len(ys), len(xs)), np.nan, float)
        self._update_image(blank)

        self._worker = _Scan2DWorker(
            xs=xs, ys=ys, set_xy=_seq_xy, measure_fn=measure_fn,
            wait_s=wait_s, stop_flag_ref=self._stop_flag,
            fast_axis='X' if fast_is_x else 'Y', n_scans=n_scans,
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
        self.controller.log(f'2D scan started: X={self.x_setter_combo.currentText()} Y={self.y_setter_combo.currentText()} meas={meas_key}')

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self.status_lbl.setText('Stopping…')

    # ── Worker signal handlers ────────────────────────────────────────────────

    def _on_progress(self, ix: int, iy: int, x: float, y: float, d6: tuple):
        if self.data is None:
            return
        _, Ny, Nx, _ = self.data.shape
        if 0 <= ix < Nx and 0 <= iy < Ny:
            self.data[self._scan_index, iy, ix, :] = np.asarray(d6, float)
        img = self._current_or_average()
        if img is not None:
            self._update_image(img, f'Live 2D — last ({x:.4g}, {y:.4g})')

    def _on_scan_done(self, scan_idx: int):
        if self.data is None:
            return
        self.status_lbl.setText(f'Completed scan {scan_idx}')
        self._scan_index = min(self._scan_index + 1, self.data.shape[0] - 1)
        img = self._current_or_average()
        if img is not None:
            self._update_image(img)

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self._worker = None
        self.controller.log(f'2D scan finished: {message}')

    def _redraw_image(self):
        img = self._current_or_average()
        if img is not None:
            self._update_image(img)

    # ── Agent live-plot handlers ──────────────────────────────────────────────

    def _agent_scan_configured(self, params: dict):
        """Update GUI fields before the agent scan starts."""
        for combo, key in ((self.x_setter_combo, params.get('x_key', '')),
                           (self.y_setter_combo, params.get('y_key', '')),
                           (self.meas_combo,     params.get('meas_key', ''))):
            idx = combo.findText(key)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        self.x_start.setText(str(params.get('x_start', 0.0)))
        self.x_end.setText(str(params.get('x_end', 0.0)))
        self.y_start.setText(str(params.get('y_start', 0.0)))
        self.y_end.setText(str(params.get('y_end', 0.0)))

        # Use the exact step that was requested — no back-computation
        if 'x_step' in params and params['x_step']:
            self.x_step.setText(f"{params['x_step']:.6g}")
        if 'y_step' in params and params['y_step']:
            self.y_step.setText(f"{params['y_step']:.6g}")

        self.wait_ms.setText(str(int(params.get('wait_ms', 5))))
        self.n_scans.setText(str(int(params.get('n_scans', 1))))

    def _agent_scan_started(self, payload: dict):
        """Reset image buffers for the incoming agent scan."""
        xs = np.asarray(payload['xs'], float)
        ys = np.asarray(payload['ys'], float)
        n  = int(payload.get('n_scans', 1))
        self._xs, self._ys = xs, ys
        self.data = np.full((n, len(ys), len(xs), 6), np.nan, float)
        self._scan_index = 0
        blank = np.full((len(ys), len(xs)), np.nan, float)
        self._update_image(blank, 'Live 2D Image (agent)')
        self.status_lbl.setText('Agent scanning…')

    def _agent_point_ready(self, ix: int, iy: int, x: float, y: float, d6: tuple):
        self._on_progress(ix, iy, x, y, d6)

    def _agent_scan_done(self, scan_idx: int):
        self._on_scan_done(scan_idx)
        self.status_lbl.setText(f'Agent scan {scan_idx} complete.')

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
        if self.data is None or self._xs is None or self._ys is None:
            raise RuntimeError("No scan data to save — run a scan first.")

        folder = self.folder.text().strip()
        if filename:
            self.filename.setText(filename.strip())
        base = self.filename.text().strip() or 'scan_2d'

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"Could not create folder: {e}")

        safe_base = self._unique_name(folder, base)
        if safe_base != base:
            self.filename.setText(safe_base)

        png_path = os.path.join(folder, f'{safe_base}.png')
        txt_path = os.path.join(folder, f'{safe_base}.txt')

        self.fig_img.savefig(png_path, dpi=200, bbox_inches='tight')
        # bbox_inches='tight' calls draw() internally which can reset axis limits;
        # redraw immediately from current data to restore the display.
        self._redraw_image()

        n_s, Ny, Nx, _ = self.data.shape
        rows = []
        for s in range(n_s):
            for iy in range(Ny):
                for ix in range(Nx):
                    d6 = self.data[s, iy, ix, :]
                    if np.isfinite(d6).any():
                        rows.append([s, float(self._xs[ix]), float(self._ys[iy]),
                                     *[float(v) for v in d6]])
        if not rows:
            raise RuntimeError("No valid data points to save.")

        xs_str = f"{self._xs[0]} to {self._xs[-1]} (Nx={len(self._xs)})"
        ys_str = f"{self._ys[0]} to {self._ys[-1]} (Ny={len(self._ys)})"
        header = '\n'.join([
            '2D Scan',
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            '',
            'Parameters:',
            f'  Fast axis: {self.fast_axis_combo.currentText()}',
            f'  X setter: {self.x_setter_combo.currentText()}',
            f'  Y setter: {self.y_setter_combo.currentText()}',
            f'  Measurement: {self.meas_combo.currentText()}',
            f'  X: {self.x_start.text()} to {self.x_end.text()} step {self.x_step.text()}',
            f'  Y: {self.y_start.text()} to {self.y_end.text()} step {self.y_step.text()}',
            f'  Wait (ms): {self.wait_ms.text()}',
            f'  Averages: {self.n_scans.text()}',
            f'  Alignment enabled: {self.align_chk.isChecked()}',
            f'  Align every N pixels: {self.align_every_n.text()}',
            f'  Screenshot: {os.path.basename(png_path)}',
            '',
            f'Data shape: {self.data.shape}  (n_scans, Ny, Nx, 6)',
            f'X array: {xs_str}',
            f'Y array: {ys_str}',
            '',
            'Columns: [scan_index, x, y, data1, data2, data3, data4, data5, data6]',
            '',
            'Table begins below this header.',
        ])
        np.savetxt(txt_path, np.asarray(rows, float),
                   delimiter='\t', fmt='%.10g', header=header, comments='')

        self.controller.log(f"2D scan saved: {txt_path}")
        return txt_path

    def _save(self):
        if self.data is None or self._xs is None or self._ys is None:
            QMessageBox.information(self, 'Nothing to save', 'Run a scan first.')
            return
        try:
            txt_path = self.save_data()
        except RuntimeError as e:
            QMessageBox.critical(self, 'Save error', str(e))
            return
        folder = self.folder.text().strip()
        base   = self.filename.text().strip()
        QMessageBox.information(self, 'Saved',
            f'Saved:\n{txt_path}\n{os.path.join(folder, base + ".png")}')

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
