# tabs/four_d_scan_tab.py
"""
4D scan: a spatial map where every spatial pixel contains a full inner 2D scan.

  outer loops : X, Y   (usually Galvo X / Galvo Y — the spatial map)
  inner loops : A, B   (any two setters — e.g. Delay (ps) and Pump waveplate (°))

Data cube shape: (Ny, Nx, Nb, Na, 6)

The display shows two panels:
  left  — spatial map, each pixel reduced from its inner 2D scan
           (mean / max / min / peak-to-peak / std / value at one (A,B) index)
  right — the inner 2D scan of one spatial pixel (the live one, or click the
          spatial map to inspect any finished pixel)
"""
from __future__ import annotations
import os, time, math, warnings
from typing import Callable, Dict, Tuple, List, Optional

import numpy as np
from helpers.state_header import instrument_state_lines
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QFileDialog, QComboBox,
    QCheckBox, QScrollArea, QFrame, QSizePolicy,
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


# ── Positioning helpers ───────────────────────────────────────────────────────

class _XYPositioner:
    """Spatial positioner that carries the running alignment offset."""

    def __init__(self, set_x, set_y, fast_is_x: bool, galvo_scan: bool,
                 alignment_tab=None):
        self._set_x         = set_x
        self._set_y         = set_y
        self._fast_is_x     = bool(fast_is_x)
        self._galvo_scan    = bool(galvo_scan)
        self._alignment_tab = alignment_tab
        self._last          = [float('nan'), float('nan')]
        self.x_offset       = 0.0
        self.y_offset       = 0.0

    def _seq(self, x: float, y: float):
        x, y = float(x), float(y)
        xc, yc = (x != self._last[0]), (y != self._last[1])
        self._last[0], self._last[1] = x, y
        if self._fast_is_x:
            if yc: self._set_y(y)
            if xc: self._set_x(x)
        else:
            if xc: self._set_x(x)
            if yc: self._set_y(y)

    def move(self, x: float, y: float):
        if self._galvo_scan:
            self._seq(float(x) + self.x_offset, float(y) + self.y_offset)
        else:
            self._seq(float(x), float(y))

    def align(self, x: float, y: float, stop_ref: List[bool]):
        """Blocking alignment, then return to (x, y) with the new offset applied."""
        dx, dy = self._alignment_tab.run_alignment_blocking(stop_ref)
        if abs(dx) > 30.0 or abs(dy) > 30.0:
            print(f'Warning: alignment offset ({dx:.4f},{dy:.4f}) exceeds ±30 — ignored.')
            dx, dy = 0.0, 0.0
        self.x_offset, self.y_offset = dx, dy

        if self._galvo_scan:
            # The outer axes are the galvo, which alignment moved: re-issue
            # both even when (x, y) hasn't changed (e.g. a mid-pixel alignment).
            self._last = [float('nan'), float('nan')]
        # Non-galvo outer axes (voltage, power, …) aren't touched by alignment,
        # so only an axis whose value actually changed gets moved; the
        # Alignment tab's "Return to target" has already put the galvo back.
        self.move(x, y)


def _make_ab_sequencer(set_a, set_b, fast_is_a: bool):
    """Return set_ab(a, b) that only moves an axis when its value changed."""
    last = [float('nan'), float('nan')]

    def _set_ab(a: float, b: float):
        a, b = float(a), float(b)
        ac, bc = (a != last[0]), (b != last[1])
        last[0], last[1] = a, b
        if fast_is_a:
            if bc: set_b(b)
            if ac: set_a(a)
        else:
            if ac: set_a(a)
            if bc: set_b(b)

    return _set_ab


def _interleave_order(n: int) -> List[int]:
    """
    Visit every other index on the way out, then the skipped ones on the way back.

    For a 7-point axis (-3…3 step 1) this gives indices [0,2,4,6,5,3,1], i.e.
    values -3,-1,1,3,2,0,-2 — a coarse pass over the whole range first, and the
    axis ends up one step from where it started instead of at the far end.
    """
    return list(range(0, n, 2)) + list(range(1, n, 2))[::-1]


def _normalize6(d) -> Tuple[float, ...]:
    if not isinstance(d, (list, tuple)):
        d = (float(d),)
    return tuple(float(v) for v in (list(d) + [math.nan] * 6)[:6])


# ── Scan engine (shared by the GUI worker thread and the agent path) ──────────

def _run_4d(
    *,
    xs: np.ndarray,
    ys: np.ndarray,
    avals: np.ndarray,
    bvals: np.ndarray,
    positioner: _XYPositioner,
    set_ab: Callable[[float, float], None],
    measure_fn: Callable[[], Tuple[float, ...]],
    wait_s: float,
    inner_reset_s: float,
    spatial_fast_is_x: bool,
    inner_fast_is_a: bool,
    interleave_a: bool = False,
    interleave_b: bool = False,
    should_stop: Callable[[], bool],
    on_point: Callable[[int, int, int, int, float, float, float, float, tuple], None],
    on_pixel_done: Callable[[int], None],
    on_scan_done: Callable[[int], None] = lambda n: None,
    n_scans: int = 1,
    align_enabled: bool = False,
    align_every_n: int = 10 ** 9,
    align_on_inner: bool = False,
    stop_ref: Optional[List[bool]] = None,
) -> Tuple[bool, str]:
    """
    Run the full 4D scan (repeated n_scans times for averaging) in the calling thread.
    align_on_inner=False → align every align_every_n spatial pixels (outer loop).
    align_on_inner=True  → align every align_every_n inner points, counted across
                           the whole repeat, so alignment can happen mid-pixel.
    Returns (user_abort, message).
    """
    def _iter_xy():
        if spatial_fast_is_x:
            for iy, y in enumerate(ys):
                for ix, x in enumerate(xs):
                    yield ix, iy, float(x), float(y)
        else:
            for ix, x in enumerate(xs):
                for iy, y in enumerate(ys):
                    yield ix, iy, float(x), float(y)

    # Visit order of the inner axes — interleaved or straight through.  Only the
    # order changes; every point still reports its natural index, so the stored
    # cube and the plots stay in axis order.
    a_order = _interleave_order(len(avals)) if interleave_a else list(range(len(avals)))
    b_order = _interleave_order(len(bvals)) if interleave_b else list(range(len(bvals)))

    def _iter_ab():
        if inner_fast_is_a:
            for ib in b_order:
                for ia in a_order:
                    yield ia, ib, float(avals[ia]), float(bvals[ib])
        else:
            for ia in a_order:
                for ib in b_order:
                    yield ia, ib, float(avals[ia]), float(bvals[ib])

    for scan_idx in range(1, max(1, int(n_scans)) + 1):
        pixel = 0
        inner_count = 0
        for ix, iy, x, y in _iter_xy():
            if should_stop():
                return True, 'Scan stopped by user.'

            if align_enabled and not align_on_inner and pixel % max(1, align_every_n) == 0:
                try:
                    positioner.align(x, y, stop_ref if stop_ref is not None else [False])
                except Exception as e:
                    return True, f'Alignment error: {e}'
            else:
                try:
                    positioner.move(x, y)
                except Exception as e:
                    return True, f'XY move error at ({x},{y}): {e}'

            first_inner = True
            for ia, ib, a, b in _iter_ab():
                if should_stop():
                    return True, 'Scan stopped by user.'

                if align_enabled and align_on_inner and inner_count % max(1, align_every_n) == 0:
                    try:
                        positioner.align(x, y, stop_ref if stop_ref is not None else [False])
                    except Exception as e:
                        return True, f'Alignment error: {e}'
                inner_count += 1

                try:
                    set_ab(a, b)
                except Exception as e:
                    return True, f'Inner axis move error at (A={a}, B={b}): {e}'

                if first_inner and inner_reset_s > 0:
                    time.sleep(inner_reset_s)
                first_inner = False

                if wait_s > 0:
                    time.sleep(wait_s)

                if should_stop():
                    return True, 'Scan stopped by user.'

                try:
                    d6 = _normalize6(measure_fn())
                except Exception as e:
                    return True, f'Measurement error at ({x},{y},{a},{b}): {e}'

                on_point(ix, iy, ia, ib, x, y, a, b, d6)

            pixel += 1
            on_pixel_done(pixel)

        on_scan_done(scan_idx)

    return False, '4D scan(s) complete.'


# ── Worker ────────────────────────────────────────────────────────────────────

class _Scan4DWorker(QThread):
    """
    Signals
    -------
    progress(ix, iy, ia, ib, x, y, a, b, d6_tuple)
    pixel_done(count_1based)   – one spatial pixel (a whole inner 2D scan) done
    scan_done(scan_idx_1based) – one full repeat of the 4D scan done
    finished(user_abort, message)
    """
    progress   = pyqtSignal(int, int, int, int, float, float, float, float, tuple)
    pixel_done = pyqtSignal(int)
    scan_done  = pyqtSignal(int)
    finished   = pyqtSignal(bool, str)

    def __init__(self, **kw):
        super().__init__()
        self._kw = kw

    def run(self):
        try:
            abort, msg = _run_4d(
                on_point=lambda ix, iy, ia, ib, x, y, a, b, d6:
                    self.progress.emit(int(ix), int(iy), int(ia), int(ib),
                                       float(x), float(y), float(a), float(b), d6),
                on_pixel_done=lambda n: self.pixel_done.emit(int(n)),
                on_scan_done=lambda n: self.scan_done.emit(int(n)),
                **self._kw,
            )
            self.finished.emit(abort, msg)
        except Exception as e:
            self.finished.emit(True, f'Unexpected error: {e}')


# ── Tab ───────────────────────────────────────────────────────────────────────

class FourDScanTab(QWidget):
    REDUCTIONS = ['mean', 'max', 'min', 'peak-to-peak', 'std', 'at (A idx, B idx)']

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
        mirrors = controller.get_instrument('Mirrors')

        # ── Axis setters (one shared table used by all four axes) ─────────────
        self.AXIS_SETTERS: Dict[str, Callable[[float], None]] = {}

        if stage is not None:
            self.AXIS_SETTERS['Newport (mm)'] = lambda v: stage.move(float(v))
            self.AXIS_SETTERS['Delay (ps)']   = lambda v: stage.move_ps(float(v))

        if galvo is not None:
            self.AXIS_SETTERS['Galvo X (mu)'] = lambda v: galvo.move_x(float(v))
            self.AXIS_SETTERS['Galvo Y (mu)'] = lambda v: galvo.move_y(float(v))

        if mirrors is not None:
            self.AXIS_SETTERS['Mirror X (µsteps)'] = lambda v: mirrors.mirror1_x(float(v))
            self.AXIS_SETTERS['Mirror Y (µsteps)'] = lambda v: mirrors.mirror1_y(float(v))

        if k2400 is not None:
            self.AXIS_SETTERS['Keithley V'] = lambda v: k2400.ramp_voltage(float(v))

        if btt is not None and pm is not None and shutter is not None:
            self.AXIS_SETTERS['Pump Power (µW)']  = lambda v: tune_pump_power_blocking(float(v), pm, btt, shutter, self.state)
            self.AXIS_SETTERS['Probe Power (µW)'] = lambda v: tune_probe_power_blocking(float(v), pm, btt, shutter, self.state)

        if btt is not None:
            self.AXIS_SETTERS['Fast Probe Power (µW)'] = lambda v: set_fast_probe_power_blocking(float(v), btt, self.state)
            self.AXIS_SETTERS['Pump waveplate (°)']   = lambda v: btt.rot_1(float(v), 5000)
            self.AXIS_SETTERS['Probe polarizer (°)']  = lambda v: btt.rot_2(float(v), 5000)
            self.AXIS_SETTERS['Probe waveplate (°)']  = lambda v: btt.rot_3(float(v), 5000)
            self.AXIS_SETTERS['Pump polarizer (°)']   = lambda v: btt.rot_4(float(v), 5000)
            self.AXIS_SETTERS['Objective Z (mm)']      = lambda v: controller.set_objective_z(float(v))

        if controller.get_instrument('Lakeshore331') is not None:
            self.AXIS_SETTERS['Temperature (K)'] = lambda v: controller.set_temperature_blocking(float(v))

        self.AXIS_SETTERS['(dummy) no-op'] = lambda v: None

        # ── Measurement funcs ─────────────────────────────────────────────────
        self.MEASUREMENT_FUNCS: Dict[str, Callable[[], Tuple[float, ...]]] = {}

        def _safe_div(a, b, eps=1e-12):
            if not (math.isfinite(a) and math.isfinite(b)):
                return math.nan
            return a / (b if abs(b) > eps else (eps if b >= 0 else -eps))

        if lockin is not None:
            self.MEASUREMENT_FUNCS['Lock-in X only']          = lambda: (lockin.read_x(),)
            self.MEASUREMENT_FUNCS['Lock-in (X, Y, R, θ)']    = lambda: (lockin.read_x(), lockin.read_y(), lockin.read_r(), lockin.read_theta())
            self.MEASUREMENT_FUNCS['PC and Reflectivity']     = lambda: (lockin.read_x(), lockin.read_x2())
            def _double_pc():
                # data1..3 = lockin 1 (X, Y, Aux1) = current; data4..5 = lockin 2 (X, Y) = voltage
                # data6 = calculated resistance |V| / |I|
                ix, iy, aux1 = lockin.read_xy()
                vx, vy, _ = lockin.read_xy2()
                return (ix, iy, aux1, vx, vy, _safe_div(math.hypot(vx, vy), math.hypot(ix, iy)))

            self.MEASUREMENT_FUNCS['Double PC (X,Y of both)'] = _double_pc
            self.MEASUREMENT_FUNCS['PC and Offset']           = lambda: (*lockin.read_xy(), self.state.settings.get('x_offset', math.nan), self.state.settings.get('y_offset', math.nan), math.nan)
            self.MEASUREMENT_FUNCS['R (calc), I, V']          = lambda: (_safe_div(lockin.read_x2(), lockin.read_x()), lockin.read_x(), lockin.read_x2(), math.nan, math.nan, math.nan)
            self.MEASUREMENT_FUNCS['Circular Dichroism']      = lambda: (lockin.read_x(), lockin.read_x2(), _safe_div(lockin.read_x(), lockin.read_x2()), math.nan, math.nan, math.nan)

        self.MEASUREMENT_FUNCS['(dummy) all NaN'] = lambda: (math.nan,) * 6

        # ── UI ────────────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)
        ctrl  = QVBoxLayout()
        form  = QFormLayout()
        # In a narrow control column, let fields use the full width and drop
        # below their label rather than being squeezed to nothing.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        dv     = QDoubleValidator(-1e300, 1e300, 9, self)
        dv_pos = QDoubleValidator(0.0, 1e300, 9, self)
        setter_keys = list(self.AXIS_SETTERS.keys())

        def _mk_combo(default: str) -> QComboBox:
            c = QComboBox()
            c.addItems(setter_keys)
            if default in self.AXIS_SETTERS:
                c.setCurrentText(default)
            return c

        # Spatial axes
        self.spatial_fast_combo = QComboBox()
        self.spatial_fast_combo.addItems(['X fast', 'Y fast'])

        self.x_setter_combo = _mk_combo('Galvo X (mu)')
        self.x_start = QLineEdit('-0.01'); self.x_start.setValidator(dv)
        self.x_end   = QLineEdit('0.01');  self.x_end.setValidator(dv)
        self.x_step  = QLineEdit('0.005'); self.x_step.setValidator(dv_pos)

        self.y_setter_combo = _mk_combo('Galvo Y (mu)')
        self.y_start = QLineEdit('-0.01'); self.y_start.setValidator(dv)
        self.y_end   = QLineEdit('0.01');  self.y_end.setValidator(dv)
        self.y_step  = QLineEdit('0.005'); self.y_step.setValidator(dv_pos)

        # Inner 2D axes
        self.inner_fast_combo = QComboBox()
        self.inner_fast_combo.addItems(['A fast', 'B fast'])

        _INTERLEAVE_TIP = (
            'Take every other point on the way out, then fill in the skipped ones on '
            'the way back.\nE.g. -3 to 3 step 1 is measured as '
            '-3, -1, 1, 3, 2, 0, -2 — a coarse pass over the whole range first, '
            'and the axis finishes one step from its start.'
        )

        self.a_setter_combo = _mk_combo('Delay (ps)')
        self.a_start = QLineEdit('0');   self.a_start.setValidator(dv)
        self.a_end   = QLineEdit('10');  self.a_end.setValidator(dv)
        self.a_step  = QLineEdit('1');   self.a_step.setValidator(dv_pos)
        self.interleave_a_chk = QCheckBox('Interleave A')
        self.interleave_a_chk.setToolTip(_INTERLEAVE_TIP)

        self.b_setter_combo = _mk_combo('Pump waveplate (°)')
        self.b_start = QLineEdit('0');   self.b_start.setValidator(dv)
        self.b_end   = QLineEdit('180'); self.b_end.setValidator(dv)
        self.b_step  = QLineEdit('45');  self.b_step.setValidator(dv_pos)
        self.interleave_b_chk = QCheckBox('Interleave B')
        self.interleave_b_chk.setToolTip(_INTERLEAVE_TIP)

        self.meas_combo    = QComboBox()
        self.meas_combo.addItems(list(self.MEASUREMENT_FUNCS.keys()))
        self.display_combo = QComboBox()
        self.display_combo.addItems([f'data{i}' for i in range(1, 7)])

        self.reduce_combo = QComboBox()
        self.reduce_combo.addItems(self.REDUCTIONS)
        self.pick_ia = QLineEdit('0'); self.pick_ia.setValidator(QIntValidator(0, 1_000_000, self))
        self.pick_ib = QLineEdit('0'); self.pick_ib.setValidator(QIntValidator(0, 1_000_000, self))

        self.wait_ms        = QLineEdit('5'); self.wait_ms.setValidator(QIntValidator(0, 1_000_000, self))
        self.inner_reset_ms = QLineEdit('0'); self.inner_reset_ms.setValidator(QIntValidator(0, 1_000_000, self))

        self.n_scans = QLineEdit('1'); self.n_scans.setValidator(QIntValidator(1, 10_000, self))
        self.show_avg_chk = QCheckBox('Show average of repeats')

        self.follow_chk = QCheckBox('Follow live pixel')
        self.follow_chk.setChecked(True)

        self.align_chk     = QCheckBox('Align during scan')
        self.align_every_n = QLineEdit('10')
        self.align_every_n.setValidator(QIntValidator(1, 1_000_000, self))
        self.align_basis_combo = QComboBox()
        self.align_basis_combo.addItems(['spatial pixels', 'inner points'])
        self.align_basis_combo.setToolTip(
            'spatial pixels: align every N outer (X, Y) pixels.\n'
            'inner points: align every N inner (A, B) points, counted across the '
            'whole scan, so alignment can happen partway through a pixel.'
        )

        self.folder   = QLineEdit(os.path.expanduser('~/four_d_scans'))
        self.filename = QLineEdit('scan_4d')
        browse_btn = QPushButton('Browse…')
        browse_btn.clicked.connect(self._browse)

        self.start_btn  = QPushButton('Start')
        self.stop_btn   = QPushButton('Stop'); self.stop_btn.setEnabled(False)
        self.save_btn   = QPushButton('Save')
        self.status_lbl = QLabel('Idle.')
        self.size_lbl   = QLabel('')
        self.size_lbl.setStyleSheet('color:#666666; font-size:10px;')

        form.addRow(QLabel('<b>Spatial map (outer)</b>'))
        form.addRow('Spatial fast axis:', self.spatial_fast_combo)
        form.addRow('X setter:',          self.x_setter_combo)
        form.addRow('X start:',           self.x_start)
        form.addRow('X end:',             self.x_end)
        form.addRow('X step:',            self.x_step)
        form.addRow('Y setter:',          self.y_setter_combo)
        form.addRow('Y start:',           self.y_start)
        form.addRow('Y end:',             self.y_end)
        form.addRow('Y step:',            self.y_step)
        form.addRow(QLabel('<b>Inner 2D scan</b>'))
        form.addRow('Inner fast axis:',   self.inner_fast_combo)
        form.addRow('A setter:',          self.a_setter_combo)
        form.addRow('A start:',           self.a_start)
        form.addRow('A end:',             self.a_end)
        form.addRow('A step:',            self.a_step)
        form.addRow('',                   self.interleave_a_chk)
        form.addRow('B setter:',          self.b_setter_combo)
        form.addRow('B start:',           self.b_start)
        form.addRow('B end:',             self.b_end)
        form.addRow('B step:',            self.b_step)
        form.addRow('',                   self.interleave_b_chk)
        form.addRow(QLabel('<b>Measurement & display</b>'))
        form.addRow('Measurement:',       self.meas_combo)
        form.addRow('Display channel:',   self.display_combo)
        form.addRow('Map reduction:',     self.reduce_combo)
        form.addRow('Pick A index:',      self.pick_ia)
        form.addRow('Pick B index:',      self.pick_ib)
        form.addRow('Wait (ms):',         self.wait_ms)
        form.addRow('Inner reset (ms):',  self.inner_reset_ms)
        form.addRow('Averages:',          self.n_scans)
        form.addRow('',                   self.show_avg_chk)
        form.addRow('',                   self.follow_chk)
        form.addRow('',                   self.align_chk)
        form.addRow('Align every N:',     self.align_every_n)
        form.addRow('Count N in:',        self.align_basis_combo)

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
        ctrl.addWidget(self.size_lbl)
        ctrl.addStretch(1)

        # The control column is taller than most screens — put it in a scroll area
        # so it never dictates the window's minimum height.
        ctrl_panel = QWidget()
        ctrl_panel.setLayout(ctrl)
        ctrl_scroll = QScrollArea()
        ctrl_scroll.setWidget(ctrl_panel)
        ctrl_scroll.setWidgetResizable(True)
        ctrl_scroll.setFrameShape(QFrame.Shape.NoFrame)
        ctrl_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ctrl_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        ctrl_scroll.setMinimumWidth(340)
        ctrl_scroll.setMaximumWidth(460)
        ctrl_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)

        # ── Figures: spatial map (left) + inner 2D map (right) ────────────────
        right = QVBoxLayout()
        self.fig = Figure(figsize=(11, 5.5), tight_layout=True)
        self.fig.set_facecolor('#f5f5f5')

        self.ax_map = self.fig.add_subplot(1, 2, 1)
        self.ax_map.set_facecolor('#ffffff')
        self.ax_map.set_title('Spatial map')
        self._cax_map = make_axes_locatable(self.ax_map).append_axes('right', size='4%', pad=0.05)

        self.ax_inner = self.fig.add_subplot(1, 2, 2)
        self.ax_inner.set_facecolor('#ffffff')
        self.ax_inner.set_title('Inner 2D scan')
        self._cax_inner = make_axes_locatable(self.ax_inner).append_axes('right', size='4%', pad=0.05)

        self._cbar_map   = None
        self._cbar_inner = None
        self._im_map     = None
        self._im_inner   = None

        self.canvas = FigureCanvas(self.fig)
        self.canvas.setMinimumSize(320, 240)   # let the window shrink freely
        self.canvas.mpl_connect('button_press_event', self._on_canvas_click)
        right.addWidget(self.canvas, stretch=1)

        outer.addWidget(ctrl_scroll, stretch=1)
        outer.addLayout(right, stretch=5)

        # ── Runtime state ─────────────────────────────────────────────────────
        self._stop_flag: List[bool] = [False]
        self._worker: _Scan4DWorker | None = None
        self._xs: np.ndarray | None = None
        self._ys: np.ndarray | None = None
        self._as: np.ndarray | None = None
        self._bs: np.ndarray | None = None
        self.data: np.ndarray | None = None    # (n_scans, Ny, Nx, Nb, Na, 6)
        self._scan_index: int = 0              # repeat currently being filled
        self._sel_ix: int = 0
        self._sel_iy: int = 0
        self._live_ix: int = 0
        self._live_iy: int = 0
        self._agent_running: bool = False

        # ── Connections ───────────────────────────────────────────────────────
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.save_btn.clicked.connect(self._save)
        self.display_combo.currentIndexChanged.connect(self._redraw_now)
        self.reduce_combo.currentIndexChanged.connect(self._redraw_now)
        self.pick_ia.editingFinished.connect(self._redraw_now)
        self.pick_ib.editingFinished.connect(self._redraw_now)
        self.show_avg_chk.stateChanged.connect(self._redraw_now)
        self.follow_chk.stateChanged.connect(self._on_follow_toggled)
        for w in (self.x_start, self.x_end, self.x_step, self.y_start, self.y_end, self.y_step,
                  self.a_start, self.a_end, self.a_step, self.b_start, self.b_end, self.b_step,
                  self.n_scans):
            w.editingFinished.connect(self._update_size_label)
        self._update_size_label()

        # ── Agent signal connections ──────────────────────────────────────────
        sigs = controller.scan_signals
        sigs.scan_4d_configured.connect(self._agent_scan_configured, Qt.ConnectionType.QueuedConnection)
        sigs.scan_4d_started.connect(self._agent_scan_started,       Qt.ConnectionType.QueuedConnection)
        sigs.scan_4d_point.connect(self._agent_point_ready,          Qt.ConnectionType.QueuedConnection)
        sigs.scan_4d_pixel_done.connect(self._agent_pixel_done,      Qt.ConnectionType.QueuedConnection)
        sigs.scan_4d_scan_done.connect(self._agent_scan_done,        Qt.ConnectionType.QueuedConnection)
        sigs.stop_all.connect(self._flag_stop,                       Qt.ConnectionType.DirectConnection)

    # ── Display helpers ───────────────────────────────────────────────────────

    def _selected_ch(self) -> int:
        return max(0, min(5, self.display_combo.currentIndex()))

    def _cube(self) -> np.ndarray | None:
        """The (Ny, Nx, Nb, Na) cube being displayed for the selected channel.

        Either the repeat currently being filled, or the average over every
        repeat run so far when 'Show average of repeats' is ticked.
        """
        if self.data is None:
            return None
        ch  = self._selected_ch()
        idx = max(0, min(self.data.shape[0] - 1, self._scan_index))
        if not self.show_avg_chk.isChecked():
            return self.data[idx, :, :, :, :, ch]
        with warnings.catch_warnings(), np.errstate(all='ignore'):
            warnings.simplefilter('ignore')
            return np.nanmean(self.data[:idx + 1, :, :, :, :, ch], axis=0)

    def _spatial_image(self) -> np.ndarray | None:
        """Reduce every pixel's inner 2D scan to one number → (Ny, Nx)."""
        cube = self._cube()
        if cube is None:
            return None
        mode = self.reduce_combo.currentText()
        with warnings.catch_warnings(), np.errstate(all='ignore'):
            warnings.simplefilter('ignore')
            if mode == 'mean':
                return np.nanmean(cube, axis=(2, 3))
            if mode == 'max':
                return np.nanmax(cube, axis=(2, 3))
            if mode == 'min':
                return np.nanmin(cube, axis=(2, 3))
            if mode == 'peak-to-peak':
                return np.nanmax(cube, axis=(2, 3)) - np.nanmin(cube, axis=(2, 3))
            if mode == 'std':
                return np.nanstd(cube, axis=(2, 3))
            # 'at (A idx, B idx)'
            Nb, Na = cube.shape[2], cube.shape[3]
            try:
                ia = max(0, min(Na - 1, int(self.pick_ia.text())))
                ib = max(0, min(Nb - 1, int(self.pick_ib.text())))
            except Exception:
                ia = ib = 0
            return cube[:, :, ib, ia]

    def _inner_image(self) -> np.ndarray | None:
        """Inner 2D scan of the selected spatial pixel → (Nb, Na)."""
        cube = self._cube()
        if cube is None:
            return None
        Ny, Nx = cube.shape[0], cube.shape[1]
        iy = max(0, min(Ny - 1, self._sel_iy))
        ix = max(0, min(Nx - 1, self._sel_ix))
        return cube[iy, ix, :, :]

    @staticmethod
    def _auto_clim(img: np.ndarray):
        vals = img[np.isfinite(img)]
        if vals.size == 0:
            return 0.0, 1.0
        vmin, vmax = float(vals.min()), float(vals.max())
        if vmin == vmax:
            return vmin - 0.5, vmax + 0.5
        return vmin, vmax

    @staticmethod
    def _extent(xs: np.ndarray, ys: np.ndarray):
        x0, x1 = float(np.min(xs)), float(np.max(xs))
        y0, y1 = float(np.min(ys)), float(np.max(ys))
        if x0 == x1: x0, x1 = x0 - 0.5, x1 + 0.5
        if y0 == y1: y0, y1 = y0 - 0.5, y1 + 0.5
        return [x0, x1, y0, y1]

    def _redraw(self, title_map: str | None = None, title_inner: str | None = None,
                spatial: bool = True, inner: bool = True):
        """Redraw one or both panels.

        The spatial panel reduces the whole cube, so during a scan it is only
        refreshed when a pixel finishes (spatial=False for per-point updates).
        """
        img = self._spatial_image() if spatial else None
        if img is not None and self._xs is not None and self._ys is not None:
            extent = self._extent(self._xs, self._ys)
            if self._im_map is None:
                self._im_map = self.ax_map.imshow(img, origin='lower', extent=extent, aspect='auto')
                self._cbar_map = self.fig.colorbar(self._im_map, cax=self._cax_map)
                self.ax_map.set_xlabel(self.x_setter_combo.currentText())
                self.ax_map.set_ylabel(self.y_setter_combo.currentText())
            else:
                self._im_map.set_data(img)
                self._im_map.set_extent(extent)
            self._im_map.set_clim(*self._auto_clim(img))
            self._cbar_map.set_label(f'{self.display_combo.currentText()} '
                                     f'({self.reduce_combo.currentText()})')
            self.ax_map.set_title(title_map or 'Spatial map')

        inner_img = self._inner_image() if inner else None
        if inner_img is not None and self._as is not None and self._bs is not None:
            extent = self._extent(self._as, self._bs)
            if self._im_inner is None:
                self._im_inner = self.ax_inner.imshow(inner_img, origin='lower', extent=extent, aspect='auto')
                self._cbar_inner = self.fig.colorbar(self._im_inner, cax=self._cax_inner)
                self.ax_inner.set_xlabel(self.a_setter_combo.currentText())
                self.ax_inner.set_ylabel(self.b_setter_combo.currentText())
            else:
                self._im_inner.set_data(inner_img)
                self._im_inner.set_extent(extent)
            self._im_inner.set_clim(*self._auto_clim(inner_img))
            self._cbar_inner.set_label(self.display_combo.currentText())
            if title_inner is None and self._xs is not None and self._ys is not None:
                iy = max(0, min(len(self._ys) - 1, self._sel_iy))
                ix = max(0, min(len(self._xs) - 1, self._sel_ix))
                title_inner = (f'Inner 2D @ ({self._xs[ix]:.4g}, {self._ys[iy]:.4g})')
            self.ax_inner.set_title(title_inner or 'Inner 2D scan')

        self.canvas.draw_idle()

    def _redraw_now(self, *_args):
        """Slot wrapper — Qt signals pass arguments that _redraw must not receive."""
        self._redraw()

    def _on_follow_toggled(self, *_args):
        """Jump back to the pixel currently being scanned when 'follow' is re-enabled."""
        if self.follow_chk.isChecked():
            self._sel_ix, self._sel_iy = self._live_ix, self._live_iy
        self._redraw()

    def _reset_axes(self):
        """Clear both panels so a new scan starts from a clean figure."""
        # Colorbars must go before the axes are cleared, otherwise their own
        # (divider-appended) axes are already detached and remove() blows up.
        for cb in (self._cbar_map, self._cbar_inner):
            if cb is not None:
                try:
                    cb.remove()
                except Exception:
                    pass
        for cax in (self._cax_map, self._cax_inner):
            try:
                cax.remove()
            except Exception:
                pass
        self._cbar_map = self._cbar_inner = None
        self._im_map = self._im_inner = None
        for ax in (self.ax_map, self.ax_inner):
            ax.cla()
            ax.set_facecolor('#ffffff')
        self._cax_map   = make_axes_locatable(self.ax_map).append_axes('right', size='4%', pad=0.05)
        self._cax_inner = make_axes_locatable(self.ax_inner).append_axes('right', size='4%', pad=0.05)

    def _on_canvas_click(self, event):
        """Click the spatial map to inspect that pixel's inner 2D scan."""
        if event.inaxes is not self.ax_map or self.data is None:
            return
        if self._xs is None or self._ys is None or event.xdata is None:
            return
        self._sel_ix = int(np.argmin(np.abs(self._xs - event.xdata)))
        self._sel_iy = int(np.argmin(np.abs(self._ys - event.ydata)))
        self.follow_chk.setChecked(False)
        self._redraw()

    def _update_size_label(self):
        dims = self._axis_lengths()
        if dims is None:
            self.size_lbl.setText('')
            return
        Nx, Ny, Na, Nb = dims
        reps = self._n_scans_value()
        rep_txt = f' × {reps} averages' if reps > 1 else ''
        self.size_lbl.setText(f'{Nx}×{Ny} spatial × {Na}×{Nb} inner{rep_txt} = '
                              f'{Nx * Ny * Na * Nb * reps:,} points')

    def _n_scans_value(self) -> int:
        try:
            return max(1, int(self.n_scans.text()))
        except Exception:
            return 1

    def _axis_lengths(self) -> tuple[int, int, int, int] | None:
        try:
            def _n(s, e, st):
                s, e, st = float(s.text()), float(e.text()), float(st.text())
                if st <= 0 or s == e:
                    raise ValueError
                return int(np.floor(abs((e - s) / st)) + 1)
            return (_n(self.x_start, self.x_end, self.x_step),
                    _n(self.y_start, self.y_end, self.y_step),
                    _n(self.a_start, self.a_end, self.a_step),
                    _n(self.b_start, self.b_end, self.b_step))
        except Exception:
            return None

    # ── Actions ───────────────────────────────────────────────────────────────

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose folder', self.folder.text())
        if path:
            self.folder.setText(path)

    def _build_axis_arrays(self, quiet: bool = False):
        """Return (xs, ys, avals, bvals), or None on bad input.

        quiet=True suppresses the message boxes — required when the agent calls
        this from its own thread, where GUI dialogs are not safe.
        """
        def _warn(title, text):
            if not quiet:
                QMessageBox.warning(self, title, text)

        try:
            x0, x1, dx = float(self.x_start.text()), float(self.x_end.text()), float(self.x_step.text())
            y0, y1, dy = float(self.y_start.text()), float(self.y_end.text()), float(self.y_step.text())
            a0, a1, da = float(self.a_start.text()), float(self.a_end.text()), float(self.a_step.text())
            b0, b1, db = float(self.b_start.text()), float(self.b_end.text()), float(self.b_step.text())
            if min(dx, dy, da, db) <= 0:
                raise ValueError
        except Exception:
            _warn('Input error', 'Start/end/step values invalid.')
            return None

        arrays = []
        for v0, v1, dv, label in ((x0, x1, dx, 'X'), (y0, y1, dy, 'Y'),
                                  (a0, a1, da, 'A'), (b0, b1, db, 'B')):
            if v0 == v1:
                _warn('Input error', f'{label} range must be non-zero.')
                return None
            n = int(np.floor(abs((v1 - v0) / dv)) + 1)
            arrays.append(np.linspace(v0, v0 + (n - 1) * dv * np.sign(v1 - v0), n))
        return tuple(arrays)

    def _gather_run_config(self, quiet: bool = False):
        """Validate everything and return the kwargs for _run_4d, or None."""
        def _warn(title, text, critical=False):
            if quiet:
                return
            if critical:
                QMessageBox.critical(self, title, text)
            else:
                QMessageBox.warning(self, title, text)

        axes = self._build_axis_arrays(quiet=quiet)
        if axes is None:
            return None
        xs, ys, avals, bvals = axes

        try:
            wait_s        = max(0.0, int(self.wait_ms.text()) / 1000.0)
            inner_reset_s = max(0.0, int(self.inner_reset_ms.text()) / 1000.0)
        except Exception:
            _warn('Input error', 'Wait times invalid.')
            return None

        measure_fn = self.MEASUREMENT_FUNCS.get(self.meas_combo.currentText())
        set_x = self.AXIS_SETTERS.get(self.x_setter_combo.currentText())
        set_y = self.AXIS_SETTERS.get(self.y_setter_combo.currentText())
        set_a = self.AXIS_SETTERS.get(self.a_setter_combo.currentText())
        set_b = self.AXIS_SETTERS.get(self.b_setter_combo.currentText())
        if None in (measure_fn, set_x, set_y, set_a, set_b):
            _warn('Config error', 'Axis or measurement not found.', critical=True)
            return None

        spatial_fast_is_x = self.spatial_fast_combo.currentText().startswith('X')
        inner_fast_is_a   = self.inner_fast_combo.currentText().startswith('A')

        galvo_scan = (self.x_setter_combo.currentText().startswith('Galvo') and
                      self.y_setter_combo.currentText().startswith('Galvo'))

        align_enabled = self.align_chk.isChecked()
        if align_enabled and self.alignment_tab is None:
            _warn('Alignment', 'Alignment tab not available.')
            align_enabled = False
        if align_enabled:
            self.alignment_tab._cached_alignment_params = self.alignment_tab.get_alignment_params()
            if not galvo_scan and not self.alignment_tab._cached_alignment_params.get('move_to_target'):
                self.controller.log(
                    "Warning: aligning during the scan without 'Return to target "
                    "after alignment' - the galvo will stay where each alignment ends.")

        try:
            align_every_n = max(1, int(self.align_every_n.text()))
        except Exception:
            align_every_n = 10

        positioner = _XYPositioner(
            set_x, set_y, spatial_fast_is_x, galvo_scan,
            alignment_tab=self.alignment_tab,
        )

        return dict(
            xs=xs, ys=ys, avals=avals, bvals=bvals,
            positioner=positioner,
            set_ab=_make_ab_sequencer(set_a, set_b, inner_fast_is_a),
            measure_fn=measure_fn,
            wait_s=wait_s, inner_reset_s=inner_reset_s,
            spatial_fast_is_x=spatial_fast_is_x,
            inner_fast_is_a=inner_fast_is_a,
            interleave_a=self.interleave_a_chk.isChecked(),
            interleave_b=self.interleave_b_chk.isChecked(),
            n_scans=self._n_scans_value(),
            align_enabled=align_enabled,
            align_every_n=align_every_n,
            align_on_inner=self.align_basis_combo.currentText() == 'inner points',
            stop_ref=self._stop_flag,
        )

    def _allocate(self, xs, ys, avals, bvals, n_scans: int = 1):
        self._xs, self._ys, self._as, self._bs = xs, ys, avals, bvals
        self.data = np.full((max(1, int(n_scans)), len(ys), len(xs),
                             len(bvals), len(avals), 6), np.nan, float)
        self._scan_index = 0
        self._sel_ix = self._sel_iy = 0
        self._live_ix = self._live_iy = 0
        self._reset_axes()
        self._redraw()

    def _start(self):
        cfg = self._gather_run_config()
        if cfg is None:
            return

        self._allocate(cfg['xs'], cfg['ys'], cfg['avals'], cfg['bvals'], cfg['n_scans'])
        self._stop_flag[0] = False

        self._worker = _Scan4DWorker(
            should_stop=lambda: self._stop_flag[0],
            **cfg,
        )
        self._worker.progress.connect(self._on_progress,     Qt.ConnectionType.QueuedConnection)
        self._worker.pixel_done.connect(self._on_pixel_done, Qt.ConnectionType.QueuedConnection)
        self._worker.scan_done.connect(self._on_scan_done,   Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished,     Qt.ConnectionType.QueuedConnection)
        self._worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText('Scanning…')
        self.controller.log(
            f'4D scan started: X={self.x_setter_combo.currentText()} '
            f'Y={self.y_setter_combo.currentText()} '
            f'A={self.a_setter_combo.currentText()} '
            f'B={self.b_setter_combo.currentText()} '
            f'meas={self.meas_combo.currentText()} '
            f'averages={cfg["n_scans"]}'
        )

    def _stop(self):
        if (self._worker and self._worker.isRunning()) or self._agent_running:
            self._stop_flag[0] = True
            self.status_lbl.setText('Stopping…')

    def _flag_stop(self):
        """Thread-safe abort flag (connected to stop_all with a direct connection)."""
        self._stop_flag[0] = True

    # ── Worker signal handlers ────────────────────────────────────────────────

    def _store_point(self, ix, iy, ia, ib, d6):
        if self.data is None:
            return False
        n_s, Ny, Nx, Nb, Na, _ = self.data.shape
        s = max(0, min(n_s - 1, self._scan_index))
        if 0 <= iy < Ny and 0 <= ix < Nx and 0 <= ib < Nb and 0 <= ia < Na:
            self.data[s, iy, ix, ib, ia, :] = np.asarray(d6, float)
            return True
        return False

    def _on_progress(self, ix: int, iy: int, ia: int, ib: int,
                     x: float, y: float, a: float, b: float, d6: tuple):
        if not self._store_point(ix, iy, ia, ib, d6):
            return
        self._live_ix, self._live_iy = ix, iy
        if self.follow_chk.isChecked():
            self._sel_ix, self._sel_iy = ix, iy
        # Only the inner panel changes point-to-point; the spatial map is a
        # reduction of the whole cube and is refreshed once a pixel is finished.
        live = (self._sel_ix, self._sel_iy) == (ix, iy)
        self._redraw(
            spatial=False,
            title_inner=(f'Inner 2D @ ({x:.4g}, {y:.4g}) — A={a:.4g} B={b:.4g}'
                         if live else None),
        )

    def _on_pixel_done(self, count: int):
        if self.data is None:
            return
        n_s, Ny, Nx = self.data.shape[0], self.data.shape[1], self.data.shape[2]
        total = Ny * Nx
        rep = f'Average {self._scan_index + 1}/{n_s} — ' if n_s > 1 else ''
        self.status_lbl.setText(f'{rep}spatial pixel {count} / {total}')
        self._redraw(title_map=f'Spatial map — {rep.lower() or ""}{count}/{total} pixels')

    def _on_scan_done(self, scan_idx: int):
        """One complete repeat of the 4D scan finished."""
        if self.data is None:
            return
        n_s = self.data.shape[0]
        self.status_lbl.setText(f'Completed average {scan_idx} / {n_s}')
        self._scan_index = min(self._scan_index + 1, n_s - 1)
        self._redraw()

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        # Wait for the thread to exit before dropping the last reference
        # (finished is emitted from inside run()), else Qt aborts the process.
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.wait()
        self.controller.log(f'4D scan finished: {message}')

    # ── Save ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _unique_name(folder: str, base: str) -> str:
        used = {os.path.splitext(f)[0] for f in os.listdir(folder)} if os.path.isdir(folder) else set()
        if base not in used:
            return base
        i = 1
        while f'{base}_{i}' in used:
            i += 1
        return f'{base}_{i}'

    def save_data(self, filename: str | None = None) -> str:
        """
        Save the current 4D cube.  Writes <base>.png, <base>.txt and <base>.npz
        (the .npz keeps the full cube plus the four axis arrays).
        Returns the .txt path, or raises RuntimeError.
        """
        if self.data is None or self._xs is None:
            raise RuntimeError('No scan data to save — run a scan first.')

        folder = self.folder.text().strip()
        if filename:
            self.filename.setText(filename.strip())
        base = self.filename.text().strip() or 'scan_4d'

        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f'Could not create folder: {e}')

        safe = self._unique_name(folder, base)
        if safe != base:
            self.filename.setText(safe)

        png_path = os.path.join(folder, f'{safe}.png')
        txt_path = os.path.join(folder, f'{safe}.txt')
        npz_path = os.path.join(folder, f'{safe}.npz')

        self.fig.savefig(png_path, dpi=200, bbox_inches='tight')
        # bbox_inches='tight' redraws and can disturb the axes — restore the view
        self._redraw()

        np.savez_compressed(
            npz_path, data=self.data,
            xs=self._xs, ys=self._ys, a=self._as, b=self._bs,
        )

        n_s, Ny, Nx, Nb, Na, _ = self.data.shape
        rows = []
        for s in range(n_s):
            for iy in range(Ny):
                for ix in range(Nx):
                    for ib in range(Nb):
                        for ia in range(Na):
                            d6 = self.data[s, iy, ix, ib, ia, :]
                            if np.isfinite(d6).any():
                                rows.append([s, float(self._xs[ix]), float(self._ys[iy]),
                                             float(self._as[ia]), float(self._bs[ib]),
                                             *[float(v) for v in d6]])
        if not rows:
            raise RuntimeError('No valid data points to save.')

        def _rng(arr, name):
            return f'{arr[0]} to {arr[-1]} (N{name}={len(arr)})'

        header = '\n'.join([
            '4D Scan (spatial map with an inner 2D scan at every pixel)',
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            '',
            'Parameters:',
            f'  Spatial fast axis: {self.spatial_fast_combo.currentText()}',
            f'  X setter: {self.x_setter_combo.currentText()}',
            f'  Y setter: {self.y_setter_combo.currentText()}',
            f'  Inner fast axis: {self.inner_fast_combo.currentText()}',
            f'  A setter: {self.a_setter_combo.currentText()}',
            f'  B setter: {self.b_setter_combo.currentText()}',
            f'  Measurement: {self.meas_combo.currentText()}',
            f'  X: {self.x_start.text()} to {self.x_end.text()} step {self.x_step.text()}',
            f'  Y: {self.y_start.text()} to {self.y_end.text()} step {self.y_step.text()}',
            f'  A: {self.a_start.text()} to {self.a_end.text()} step {self.a_step.text()}'
            f'{"  [interleaved]" if self.interleave_a_chk.isChecked() else ""}',
            f'  B: {self.b_start.text()} to {self.b_end.text()} step {self.b_step.text()}'
            f'{"  [interleaved]" if self.interleave_b_chk.isChecked() else ""}',
            f'  Wait (ms): {self.wait_ms.text()}',
            f'  Inner reset wait (ms): {self.inner_reset_ms.text()}',
            f'  Averages (repeats): {n_s}',
            f'  Map reduction: {self.reduce_combo.currentText()}',
            f'  Alignment enabled: {self.align_chk.isChecked()}',
            f'  Align every N {self.align_basis_combo.currentText()}: {self.align_every_n.text()}',
            f'  Screenshot: {os.path.basename(png_path)}',
            f'  Raw cube: {os.path.basename(npz_path)}  (keys: data, xs, ys, a, b)',
            '',
            f'Data shape: {self.data.shape}  (n_scans, Ny, Nx, Nb, Na, 6)',
            f'X array: {_rng(self._xs, "x")}',
            f'Y array: {_rng(self._ys, "y")}',
            f'A array: {_rng(self._as, "a")}',
            f'B array: {_rng(self._bs, "b")}',
            '',
            'Columns: [scan_index, x, y, a, b, data1, data2, data3, data4, data5, data6]',
            '',
            *instrument_state_lines(self.controller),
            '',
            'Table begins below this header.',
        ])
        np.savetxt(txt_path, np.asarray(rows, float),
                   delimiter='\t', fmt='%.10g', header=header, comments='')

        self.controller.log(f'4D scan saved: {txt_path}')
        return txt_path

    def _save(self):
        if self.data is None:
            QMessageBox.information(self, 'Nothing to save', 'Run a scan first.')
            return
        try:
            txt_path = self.save_data()
        except RuntimeError as e:
            QMessageBox.critical(self, 'Save error', str(e))
            return
        QMessageBox.information(self, 'Saved', f'Saved:\n{txt_path}\n'
                                               f'{os.path.splitext(txt_path)[0]}.png\n'
                                               f'{os.path.splitext(txt_path)[0]}.npz')

    # ── Agent-driven scan (runs in the agent thread, plots live via signals) ──

    def run_scan_blocking(self) -> dict:
        """
        Run a 4D scan with the current tab settings in the *calling* thread.
        Used by the agent; the GUI is updated through controller.scan_signals.
        Returns {'stats': …, 'png_b64': …} or {'error': …}.
        """
        from controller import _render_2d_png

        if self._worker is not None and self._worker.isRunning():
            return {'error': 'A 4D scan is already running.'}

        sigs = self.controller.scan_signals
        sigs.activate_tab.emit('4D Scan')
        sigs.scan_4d_configured.emit(self.get_settings())

        cfg = self._gather_run_config(quiet=True)
        if cfg is None:
            return {'error': 'Invalid 4D scan settings — check axis ranges and steps.'}

        xs, ys       = cfg['xs'], cfg['ys']
        avals, bvals = cfg['avals'], cfg['bvals']
        n_scans      = int(cfg['n_scans'])
        data = np.full((n_scans, len(ys), len(xs), len(bvals), len(avals), 6),
                       np.nan, float)

        sigs.scan_4d_started.emit({
            'xs': xs.tolist(), 'ys': ys.tolist(),
            'a':  avals.tolist(), 'b': bvals.tolist(),
            'n_scans': n_scans,
        })

        self._stop_flag[0] = False
        self.controller._agent_stop_requested = False
        self._agent_running = True
        scan_slot = [0]

        def _should_stop():
            return self._stop_flag[0] or self.controller._agent_stop_requested

        def _on_point(ix, iy, ia, ib, x, y, a, b, d6):
            data[scan_slot[0], iy, ix, ib, ia, :] = np.asarray(d6, float)
            sigs.scan_4d_point.emit(int(ix), int(iy), int(ia), int(ib),
                                    float(x), float(y), float(a), float(b), d6)

        def _on_scan_done(n):
            scan_slot[0] = min(n, n_scans - 1)
            sigs.scan_4d_scan_done.emit(int(n))

        self.controller.log(
            f'Agent 4D scan: X={self.x_setter_combo.currentText()} '
            f'Y={self.y_setter_combo.currentText()} '
            f'A={self.a_setter_combo.currentText()} '
            f'B={self.b_setter_combo.currentText()} '
            f'meas={self.meas_combo.currentText()} '
            f'Nx={len(xs)} Ny={len(ys)} Na={len(avals)} Nb={len(bvals)} '
            f'averages={n_scans}'
        )

        try:
            abort, message = _run_4d(
                should_stop=_should_stop,
                on_point=_on_point,
                on_pixel_done=lambda n: sigs.scan_4d_pixel_done.emit(int(n)),
                on_scan_done=_on_scan_done,
                **cfg,
            )
        except Exception as e:
            return {'error': f'4D scan failed: {e}'}
        finally:
            self._agent_running = False
            self.controller._agent_stop_requested = False
            # sentinel — lets the GUI thread re-enable the buttons either way
            sigs.scan_4d_pixel_done.emit(0)

        if abort:
            return {'error': message}

        ch = self._selected_ch()
        with warnings.catch_warnings(), np.errstate(all='ignore'):
            warnings.simplefilter('ignore')
            # average the repeats, then reduce each pixel's inner 2D scan
            avg = np.nanmean(data[:, :, :, :, :, ch], axis=0)   # (Ny, Nx, Nb, Na)
            img = np.nanmean(avg, axis=(2, 3))
        vals = avg.ravel()
        vals = vals[np.isfinite(vals)]
        stats = {
            'nx': len(xs), 'ny': len(ys), 'na': len(avals), 'nb': len(bvals),
            'averages': n_scans,
            'channel': self.display_combo.currentText(),
            'min':  round(float(vals.min()), 6) if vals.size else None,
            'max':  round(float(vals.max()), 6) if vals.size else None,
            'mean': round(float(vals.mean()), 6) if vals.size else None,
        }
        png_b64 = _render_2d_png(xs, ys, img,
                                 self.x_setter_combo.currentText(),
                                 self.y_setter_combo.currentText())
        self.controller.log(f"Agent 4D scan done. range: {stats['min']} – {stats['max']}")
        return {'stats': stats, 'png_b64': png_b64}

    # ── Agent live-plot handlers ──────────────────────────────────────────────

    def _agent_scan_configured(self, params: dict):
        self.apply_settings(params)

    def _agent_scan_started(self, payload: dict):
        xs    = np.asarray(payload['xs'], float)
        ys    = np.asarray(payload['ys'], float)
        avals = np.asarray(payload['a'],  float)
        bvals = np.asarray(payload['b'],  float)
        self._allocate(xs, ys, avals, bvals, int(payload.get('n_scans', 1)))
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText('Agent scanning…')

    def _agent_point_ready(self, ix: int, iy: int, ia: int, ib: int,
                           x: float, y: float, a: float, b: float, d6: tuple):
        self._on_progress(ix, iy, ia, ib, x, y, a, b, d6)

    def _agent_pixel_done(self, count: int):
        if count <= 0:      # sentinel: the agent scan ended (finished or aborted)
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.status_lbl.setText('Agent 4D scan ended.')
            self._redraw()
            return
        self._on_pixel_done(count)

    def _agent_scan_done(self, scan_idx: int):
        self._on_scan_done(scan_idx)

    # ── Agent API ─────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        return {
            'spatial_fast_axis': self.spatial_fast_combo.currentText(),
            'x_setter':          self.x_setter_combo.currentText(),
            'x_start':           self.x_start.text(),
            'x_end':             self.x_end.text(),
            'x_step':            self.x_step.text(),
            'y_setter':          self.y_setter_combo.currentText(),
            'y_start':           self.y_start.text(),
            'y_end':             self.y_end.text(),
            'y_step':            self.y_step.text(),
            'inner_fast_axis':   self.inner_fast_combo.currentText(),
            'a_setter':          self.a_setter_combo.currentText(),
            'a_start':           self.a_start.text(),
            'a_end':             self.a_end.text(),
            'a_step':            self.a_step.text(),
            'interleave_a':      self.interleave_a_chk.isChecked(),
            'b_setter':          self.b_setter_combo.currentText(),
            'b_start':           self.b_start.text(),
            'b_end':             self.b_end.text(),
            'b_step':            self.b_step.text(),
            'interleave_b':      self.interleave_b_chk.isChecked(),
            'measurement':       self.meas_combo.currentText(),
            'display_channel':   self.display_combo.currentText(),
            'map_reduction':     self.reduce_combo.currentText(),
            'pick_a_index':      self.pick_ia.text(),
            'pick_b_index':      self.pick_ib.text(),
            'wait_ms':           self.wait_ms.text(),
            'inner_reset_ms':    self.inner_reset_ms.text(),
            'averages':          self.n_scans.text(),
            'show_average':      self.show_avg_chk.isChecked(),
            'align_enabled':     self.align_chk.isChecked(),
            'align_every_n':     self.align_every_n.text(),
            'align_basis':       self.align_basis_combo.currentText(),
            'folder':            self.folder.text(),
            'filename':          self.filename.text(),
            'setters_available':      list(self.AXIS_SETTERS.keys()),
            'measurements_available': list(self.MEASUREMENT_FUNCS.keys()),
            'map_reductions_available': list(self.REDUCTIONS),
            'display_channels_available': [self.display_combo.itemText(i)
                                           for i in range(self.display_combo.count())],
        }

    def apply_settings(self, settings: dict) -> list[str]:
        """Apply any subset of settings from the agent. Returns warnings."""
        warnings_out: list[str] = []

        def _set_combo(combo, key):
            if key in settings:
                v = str(settings[key])
                idx = combo.findText(v)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    opts = [combo.itemText(i) for i in range(combo.count())]
                    warnings_out.append(f"'{v}' is not a valid choice for '{key}'. "
                                        f"Options: {opts}")

        def _set_text(widget, key):
            if key in settings:
                widget.setText(str(settings[key]))

        _set_combo(self.spatial_fast_combo, 'spatial_fast_axis')
        _set_combo(self.inner_fast_combo,   'inner_fast_axis')
        _set_combo(self.x_setter_combo,     'x_setter')
        _set_combo(self.y_setter_combo,     'y_setter')
        _set_combo(self.a_setter_combo,     'a_setter')
        _set_combo(self.b_setter_combo,     'b_setter')
        _set_combo(self.meas_combo,         'measurement')
        _set_combo(self.display_combo,      'display_channel')
        _set_combo(self.reduce_combo,       'map_reduction')
        _set_combo(self.align_basis_combo,  'align_basis')

        for widget, key in (
            (self.x_start, 'x_start'), (self.x_end, 'x_end'), (self.x_step, 'x_step'),
            (self.y_start, 'y_start'), (self.y_end, 'y_end'), (self.y_step, 'y_step'),
            (self.a_start, 'a_start'), (self.a_end, 'a_end'), (self.a_step, 'a_step'),
            (self.b_start, 'b_start'), (self.b_end, 'b_end'), (self.b_step, 'b_step'),
            (self.wait_ms, 'wait_ms'), (self.inner_reset_ms, 'inner_reset_ms'),
            (self.n_scans, 'averages'),
            (self.pick_ia, 'pick_a_index'), (self.pick_ib, 'pick_b_index'),
            (self.align_every_n, 'align_every_n'),
            (self.folder, 'folder'), (self.filename, 'filename'),
        ):
            _set_text(widget, key)

        if 'interleave_a' in settings:
            self.interleave_a_chk.setChecked(bool(settings['interleave_a']))
        if 'interleave_b' in settings:
            self.interleave_b_chk.setChecked(bool(settings['interleave_b']))
        if 'show_average' in settings:
            self.show_avg_chk.setChecked(bool(settings['show_average']))
        if 'align_enabled' in settings:
            self.align_chk.setChecked(bool(settings['align_enabled']))

        self._update_size_label()
        return warnings_out

    def set_display_channel(self, channel: str) -> dict:
        idx = self.display_combo.findText(str(channel))
        if idx < 0:
            opts = [self.display_combo.itemText(i)
                    for i in range(self.display_combo.count())]
            return {'error': f"Unknown channel '{channel}'. Options: {opts}"}
        self.display_combo.setCurrentIndex(idx)
        return {'status': 'ok', 'display_channel': self.display_combo.currentText()}

    def set_filename(self, name: str):
        self.filename.setText(name.strip())

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
