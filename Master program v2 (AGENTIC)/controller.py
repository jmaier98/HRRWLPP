"""
MicroscopeController — single API surface used by both the GUI tabs and the agent.

Tabs get instruments and shared AXIS_SETTERS / MEASUREMENT_FUNCS from here.
The agent calls named methods (run_1d_scan, get_state, etc.) via agent_tools.
All actions are logged to self._log.
"""

from __future__ import annotations
import io, json, math, time, base64
from datetime import datetime
from typing import Callable, Dict, Tuple

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal


class ScanSignals(QObject):
    """Qt signals emitted by the controller so tabs stay in sync with agent actions."""
    # Live scan data
    scan_started   = pyqtSignal(object)             # xs array
    point_ready    = pyqtSignal(int, float, tuple)  # (ix, x_val, d6)
    scan_complete  = pyqtSignal(int)                # scan index (1-based)

    # Agent configured a 1D scan — tabs should update their GUI fields
    # payload dict keys: axis_key, meas_key, x_start, x_end, n_points, n_scans, wait_ms
    scan_configured = pyqtSignal(object)

    # 2D scan agent signals
    # scan_2d_configured: x_key, y_key, meas_key, x_start, x_end, x_points,
    #                     y_start, y_end, y_points, n_scans, wait_ms
    scan_2d_configured = pyqtSignal(object)
    # scan_2d_started: {'xs': array, 'ys': array, 'n_scans': int}
    scan_2d_started    = pyqtSignal(object)
    # scan_2d_point: ix, iy, x, y, d6
    scan_2d_point      = pyqtSignal(int, int, float, float, tuple)
    # scan_2d_done: scan_index (1-based)
    scan_2d_done       = pyqtSignal(int)

    # 3D scan agent signals
    # scan_3d_configured: full settings dict (same keys as ThreeDScanTab.apply_settings)
    scan_3d_configured = pyqtSignal(object)
    # scan_3d_point: iz, ix, iy, z, x, y, d6
    scan_3d_point      = pyqtSignal(int, int, int, float, float, float, tuple)
    # scan_3d_layer_done: layer count (1-based)
    scan_3d_layer_done = pyqtSignal(int)

    # Agent set an instrument control — dynamic controls tab should update its field
    # payload: (label: str, value: float)
    control_set = pyqtSignal(str, float)

    # Agent or agent-stop requested — all running scans/tuners should abort
    stop_all = pyqtSignal()

    # Ask the main window to switch to a named tab
    activate_tab = pyqtSignal(str)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class MicroscopeController:
    def __init__(self, instrument_manager, state):
        self.im    = instrument_manager
        self.state = state
        self._log: list[str] = []

        # Populated lazily when first accessed (instruments may not be open yet)
        self._axis_setters: Dict[str, Callable[[float], None]] | None = None
        self._measurement_funcs: Dict[str, Callable[[], Tuple]] | None = None

        # Signals emitted during agent-driven scans so GUI tabs can plot live
        self.scan_signals = ScanSignals()

        # Set to True by request_stop_all(); checked every point in blocking scans
        self._agent_stop_requested = False

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, msg: str):
        self._log.append(f"[{_ts()}] {msg}")

    def get_log(self, last_n: int = 50) -> list[str]:
        return self._log[-last_n:]

    # ── Instrument access ─────────────────────────────────────────────────────

    def get_instrument(self, name: str):
        try:
            return self.im.get(name)
        except KeyError:
            return None

    # ── Axis setters & measurement funcs (shared with tabs) ───────────────────

    @property
    def AXIS_SETTERS(self) -> Dict[str, Callable[[float], None]]:
        if self._axis_setters is None:
            self._axis_setters = self._build_axis_setters()
        return self._axis_setters

    @property
    def MEASUREMENT_FUNCS(self) -> Dict[str, Callable[[], Tuple]]:
        if self._measurement_funcs is None:
            self._measurement_funcs = self._build_measurement_funcs()
        return self._measurement_funcs

    def _build_axis_setters(self) -> Dict[str, Callable[[float], None]]:
        from helpers.power_tuners import (
            tune_pump_power_blocking,
            tune_probe_power_blocking,
            set_fast_probe_power_blocking,
        )

        stage   = self.get_instrument("ESP")
        galvo   = self.get_instrument("Galvo")
        btt     = self.get_instrument("BTT")
        pm      = self.get_instrument("PM")
        shutter = self.get_instrument("Shutter")
        k2400   = self.get_instrument("Keithley2400")

        setters: Dict[str, Callable[[float], None]] = {}

        if stage is not None:
            setters["Newport Stage mm"] = lambda x: stage.move(float(x))
            setters["Newport Stage ps"] = lambda x: stage.move_ps(float(x))

        if galvo is not None:
            setters["Galvo X (mu)"] = lambda x: galvo.move_x(float(x))
            setters["Galvo Y (mu)"] = lambda y: galvo.move_y(float(y))

        if k2400 is not None:
            setters["Keithley V"] = lambda x: k2400.ramp_voltage(float(x))

        if btt is not None:
            setters["Rot 1 (°)"]      = lambda x: btt.rot_1(float(x), 5000)
            setters["Pump Waveplate"] = lambda x: btt.rot_3(float(x), 5000)

        if btt is not None and pm is not None and shutter is not None:
            setters["Pump Power (µW)"] = lambda x: tune_pump_power_blocking(
                float(x), pm, btt, shutter, self.state
            )
            setters["Probe Power (µW)"] = lambda x: tune_probe_power_blocking(
                float(x), pm, btt, shutter, self.state
            )

        if btt is not None:
            setters["Fast Probe Power (µW)"] = lambda x: set_fast_probe_power_blocking(
                float(x), btt, self.state
            )

        setters["(dummy) no-op axis"] = lambda x: None
        return setters

    def _build_measurement_funcs(self) -> Dict[str, Callable[[], Tuple]]:
        lockin    = self.get_instrument("SR830")
        lakeshore = self.get_instrument("Lakeshore331")
        k2400     = self.get_instrument("Keithley2400")
        btt       = self.get_instrument("BTT")

        funcs: Dict[str, Callable[[], Tuple]] = {}

        def _safe_div(a, b, eps=1e-12):
            if not (math.isfinite(a) and math.isfinite(b)):
                return math.nan
            return a / (b if abs(b) > eps else (eps if b >= 0 else -eps))

        if lockin is not None:
            funcs["Lock-in X only"] = lambda: (lockin.read_x(),)
            funcs["Lock-in (X, Y, R, θ)"] = lambda: (
                lockin.read_x(), lockin.read_y(), lockin.read_r(), lockin.read_theta()
            )
            funcs["R (calculated), I, V"] = lambda: (
                _safe_div(lockin.read_x2(), lockin.read_x()),
                lockin.read_x(),
                lockin.read_x2(),
                math.nan, math.nan, math.nan,
            )
            funcs["Double PC (X,Y of both)"] = lambda: (
                *lockin.read_xy(),
                *lockin.read_xy2(),
            )

        if lockin is not None and lakeshore is not None:
            funcs["4 point probe vs temp"] = lambda: (
                lockin.read_x(), lockin.read_y(), lakeshore.read_temp(),
                math.nan, math.nan, math.nan,
            )
            funcs["lockin 2 vs temp"] = lambda: (
                lockin.read_x2(), lockin.read_y2(), lakeshore.read_temp(),
                math.nan, math.nan, math.nan,
            )

        if k2400 is not None:
            funcs["Keithley I (2-wire)"] = lambda: (
                (k2400.read_current() or math.nan,)
            )
            funcs["Keithley I+V (2-wire)"] = lambda: (
                k2400.read_iv() or (math.nan, math.nan)
            )

        if btt is not None:
            funcs["Rot 1 angle"] = lambda: (self.state.settings.get("rot_1_angle", math.nan),)

        funcs["(dummy) all NaN"] = lambda: (math.nan,) * 6
        return funcs

    # ── State snapshot ────────────────────────────────────────────────────────

    def notify_control_set(self, label: str, value: float):
        """Emit control_set so the Dynamic Controls tab can update its field."""
        self.scan_signals.activate_tab.emit("Dynamic Controls")
        self.scan_signals.control_set.emit(label, value)

    def request_stop_all(self):
        """Immediately flag all running scans to abort, then notify tabs."""
        self._agent_stop_requested = True
        self.scan_signals.stop_all.emit()

    def get_state(self) -> dict:
        return {
            "instruments_open": list(self.im._instruments.keys()),
            "settings": dict(self.state.settings),
        }

    # ── Agent-callable 1D scan (blocking, returns PNG + stats) ───────────────

    def run_1d_scan_blocking(
        self,
        axis_key: str,
        meas_key: str,
        x_start: float,
        x_end: float,
        n_points: int,
        n_scans: int = 1,
        wait_ms: int = 10,
    ) -> dict:
        """
        Run a 1D scan entirely in the calling thread (for agent use).
        Returns a dict with 'data' (list of rows), 'png_b64', and 'stats'.
        """
        set_x = self.AXIS_SETTERS.get(axis_key)
        measure = self.MEASUREMENT_FUNCS.get(meas_key)
        if set_x is None:
            return {"error": f"Unknown axis: {axis_key}"}
        if measure is None:
            return {"error": f"Unknown measurement: {meas_key}"}

        xs = np.linspace(x_start, x_end, max(2, int(n_points)))
        wait_s = max(0.0, wait_ms / 1000.0)
        all_scans: list[np.ndarray] = []

        self.scan_signals.activate_tab.emit("1D Scan")
        self.log(f"Agent 1D scan: axis={axis_key} meas={meas_key} "
                 f"x=[{x_start},{x_end}] n={n_points} avg={n_scans}")

        self.scan_signals.scan_configured.emit({
            "axis_key": axis_key,
            "meas_key": meas_key,
            "x_start":  x_start,
            "x_end":    x_end,
            "n_points": n_points,
            "n_scans":  n_scans,
            "wait_ms":  wait_ms,
        })
        self._agent_stop_requested = False
        self.scan_signals.scan_started.emit(xs)

        for scan_idx in range(1, max(1, int(n_scans)) + 1):
            scan = np.full((len(xs), 6), np.nan)
            for ix, x in enumerate(xs):
                if self._agent_stop_requested:
                    self.log("Agent scan stopped by user.")
                    return {"error": "Scan stopped by user."}
                try:
                    set_x(float(x))
                except Exception as e:
                    return {"error": f"Axis move failed at x={x}: {e}"}
                if wait_s > 0:
                    time.sleep(wait_s)
                if self._agent_stop_requested:
                    self.log("Agent scan stopped by user.")
                    return {"error": "Scan stopped by user."}
                try:
                    d = measure()
                    if not isinstance(d, (list, tuple)):
                        d = (float(d),)
                    d6 = list(d) + [math.nan] * 6
                    scan[ix, :] = [float(v) for v in d6[:6]]
                except Exception as e:
                    return {"error": f"Measurement failed at x={x}: {e}"}
                d6_tuple = tuple(scan[ix, :].tolist())
                self.scan_signals.point_ready.emit(ix, float(x), d6_tuple)
            all_scans.append(scan)
            self.scan_signals.scan_complete.emit(scan_idx)

        avg = np.nanmean(np.stack(all_scans, axis=0), axis=0)  # (N, 6)

        data_rows = [
            {"x": float(xs[i]), **{f"d{j+1}": float(avg[i, j]) for j in range(6)}}
            for i in range(len(xs))
        ]

        d1_vals = [r["d1"] for r in data_rows if math.isfinite(r["d1"])]
        stats = {
            "n_points": len(xs),
            "n_scans": n_scans,
            "d1_min":  round(min(d1_vals), 6) if d1_vals else None,
            "d1_max":  round(max(d1_vals), 6) if d1_vals else None,
            "d1_mean": round(float(np.nanmean(d1_vals)), 6) if d1_vals else None,
        }

        png_b64 = _render_1d_png(xs, avg[:, 0], axis_key, meas_key)
        self._agent_stop_requested = False
        self.log(f"Agent 1D scan done. d1 range: {stats['d1_min']} – {stats['d1_max']}")
        return {"data": data_rows, "stats": stats, "png_b64": png_b64}

    def run_2d_scan_blocking(
        self,
        x_key: str,
        y_key: str,
        meas_key: str,
        x_start: float,
        x_end: float,
        x_points: int,
        y_start: float,
        y_end: float,
        y_points: int,
        n_scans: int = 1,
        wait_ms: int = 5,
        x_step: float | None = None,
        y_step: float | None = None,
    ) -> dict:
        """Run a 2D raster scan in the calling thread (for agent use)."""
        set_x   = self.AXIS_SETTERS.get(x_key)
        set_y   = self.AXIS_SETTERS.get(y_key)
        measure = self.MEASUREMENT_FUNCS.get(meas_key)
        if set_x is None:
            return {"error": f"Unknown X axis: {x_key}"}
        if set_y is None:
            return {"error": f"Unknown Y axis: {y_key}"}
        if measure is None:
            return {"error": f"Unknown measurement: {meas_key}"}

        xs = np.linspace(x_start, x_end, max(2, int(x_points)))
        ys = np.linspace(y_start, y_end, max(2, int(y_points)))
        wait_s = max(0.0, wait_ms / 1000.0)

        self.scan_signals.activate_tab.emit("2D Scan")
        self.log(f"Agent 2D scan: X={x_key} Y={y_key} meas={meas_key} "
                 f"[{x_start},{x_end}]x[{y_start},{y_end}] "
                 f"{x_points}x{y_points} avg={n_scans}")

        # Compute exact step sizes to display in the GUI fields
        _x_step = x_step if x_step is not None else (
            abs(x_end - x_start) / (x_points - 1) if x_points > 1 else 0
        )
        _y_step = y_step if y_step is not None else (
            abs(y_end - y_start) / (y_points - 1) if y_points > 1 else 0
        )
        self.scan_signals.scan_2d_configured.emit({
            "x_key": x_key, "y_key": y_key, "meas_key": meas_key,
            "x_start": x_start, "x_end": x_end, "x_points": x_points, "x_step": _x_step,
            "y_start": y_start, "y_end": y_end, "y_points": y_points, "y_step": _y_step,
            "n_scans": n_scans, "wait_ms": wait_ms,
        })
        self._agent_stop_requested = False
        self.scan_signals.scan_2d_started.emit({
            "xs": xs.tolist(), "ys": ys.tolist(), "n_scans": n_scans
        })

        all_scans: list[np.ndarray] = []
        _last = [float('nan'), float('nan')]

        def _seq_xy(x, y):
            xc, yc = (x != _last[0]), (y != _last[1])
            _last[0], _last[1] = x, y
            if yc: set_y(float(y))
            if xc: set_x(float(x))

        for scan_idx in range(1, max(1, int(n_scans)) + 1):
            scan = np.full((len(ys), len(xs), 6), np.nan, float)
            for iy, y in enumerate(ys):
                for ix, x in enumerate(xs):
                    if self._agent_stop_requested:
                        self.log("Agent 2D scan stopped by user.")
                        return {"error": "Scan stopped by user."}
                    try:
                        _seq_xy(x, y)
                    except Exception as e:
                        return {"error": f"Axis move failed at ({x},{y}): {e}"}
                    if wait_s > 0:
                        time.sleep(wait_s)
                    if self._agent_stop_requested:
                        self.log("Agent 2D scan stopped by user.")
                        return {"error": "Scan stopped by user."}
                    try:
                        d = measure()
                        if not isinstance(d, (list, tuple)):
                            d = (float(d),)
                        d6 = list(d) + [math.nan] * 6
                        scan[iy, ix, :] = [float(v) for v in d6[:6]]
                    except Exception as e:
                        return {"error": f"Measurement failed at ({x},{y}): {e}"}
                    d6_tuple = tuple(scan[iy, ix, :].tolist())
                    self.scan_signals.scan_2d_point.emit(ix, iy, float(x), float(y), d6_tuple)
            all_scans.append(scan)
            self.scan_signals.scan_2d_done.emit(scan_idx)

        avg = np.nanmean(np.stack(all_scans, axis=0), axis=0)  # (Ny, Nx, 6)

        d1_vals = avg[:, :, 0].ravel()
        d1_vals = d1_vals[np.isfinite(d1_vals)]
        stats = {
            "x_points": len(xs), "y_points": len(ys), "n_scans": n_scans,
            "d1_min":  round(float(d1_vals.min()), 6) if len(d1_vals) else None,
            "d1_max":  round(float(d1_vals.max()), 6) if len(d1_vals) else None,
            "d1_mean": round(float(d1_vals.mean()), 6) if len(d1_vals) else None,
        }

        png_b64 = _render_2d_png(xs, ys, avg[:, :, 0], x_key, y_key)
        self._agent_stop_requested = False
        self.log(f"Agent 2D scan done. d1 range: {stats['d1_min']} – {stats['d1_max']}")
        return {"stats": stats, "png_b64": png_b64}

    def run_3d_scan_blocking(
        self,
        z_key: str,
        x_key: str,
        y_key: str,
        meas_key: str,
        z_start: float,
        z_end: float,
        z_step: float,
        x_start: float,
        x_end: float,
        x_step: float,
        y_start: float,
        y_end: float,
        y_step: float,
        fast_axis: str = "X",
        wait_ms: int = 5,
        z_reset_ms: int = 0,
    ) -> dict:
        """Run a 3D scan in the calling thread (for agent use). Returns stats + PNG."""
        set_z   = self.AXIS_SETTERS.get(z_key)
        set_x   = self.AXIS_SETTERS.get(x_key)
        set_y   = self.AXIS_SETTERS.get(y_key)
        measure = self.MEASUREMENT_FUNCS.get(meas_key)
        if set_z is None:
            return {"error": f"Unknown Z axis: {z_key}"}
        if set_x is None:
            return {"error": f"Unknown X axis: {x_key}"}
        if set_y is None:
            return {"error": f"Unknown Y axis: {y_key}"}
        if measure is None:
            return {"error": f"Unknown measurement: {meas_key}"}

        nz = int(np.floor(abs((z_end - z_start) / z_step)) + 1)
        nx = int(np.floor(abs((x_end - x_start) / x_step)) + 1)
        ny = int(np.floor(abs((y_end - y_start) / y_step)) + 1)
        zs = np.linspace(z_start, z_start + (nz - 1) * z_step * np.sign(z_end - z_start), nz)
        xs = np.linspace(x_start, x_start + (nx - 1) * x_step * np.sign(x_end - x_start), nx)
        ys = np.linspace(y_start, y_start + (ny - 1) * y_step * np.sign(y_end - y_start), ny)

        wait_s    = max(0.0, wait_ms / 1000.0)
        z_reset_s = max(0.0, z_reset_ms / 1000.0)
        fa = str(fast_axis).upper()
        z_fast = fa.startswith('Z')

        self.scan_signals.activate_tab.emit("3D Scan")
        self.log(
            f"Agent 3D scan: Z={z_key} X={x_key} Y={y_key} meas={meas_key} "
            f"fast={'Z' if z_fast else fa} "
            f"Nz={nz} Nx={nx} Ny={ny}"
        )
        self.scan_signals.scan_3d_configured.emit({
            "z_setter": z_key, "x_setter": x_key, "y_setter": y_key,
            "measurement": meas_key,
            "fast_axis":  "Z fast" if z_fast else (f"{fa} fast"),
            "z_start": z_start, "z_end": z_end, "z_step": z_step,
            "x_start": x_start, "x_end": x_end, "x_step": x_step,
            "y_start": y_start, "y_end": y_end, "y_step": y_step,
            "wait_ms": wait_ms, "z_reset_ms": z_reset_ms,
        })
        self._agent_stop_requested = False

        data = np.full((nz, ny, nx, 6), np.nan, float)
        _last = [float('nan'), float('nan')]

        def _seq_xy(x, y):
            xc, yc = (x != _last[0]), (y != _last[1])
            _last[0], _last[1] = x, y
            if yc: set_y(float(y))
            if xc: set_x(float(x))

        def _measure_point():
            try:
                d = measure()
                if not isinstance(d, (list, tuple)):
                    d = (float(d),)
                d6 = list(d) + [math.nan] * 6
                return tuple(float(v) for v in d6[:6])
            except Exception as e:
                return None, str(e)

        def _xy_iter():
            if z_fast or fa == 'X':
                for iy, y in enumerate(ys):
                    for ix, x in enumerate(xs):
                        yield ix, iy, x, y
            else:
                for ix, x in enumerate(xs):
                    for iy, y in enumerate(ys):
                        yield ix, iy, x, y

        if z_fast:
            xy_count = 0
            for ix, iy, x, y in _xy_iter():
                if self._agent_stop_requested:
                    return {"error": "Scan stopped by user."}
                try:
                    _seq_xy(x, y)
                except Exception as e:
                    return {"error": f"XY move failed at ({x},{y}): {e}"}
                for iz, z in enumerate(zs):
                    if self._agent_stop_requested:
                        return {"error": "Scan stopped by user."}
                    try:
                        set_z(float(z))
                    except Exception as e:
                        return {"error": f"Z move failed at z={z}: {e}"}
                    if iz == 0 and z_reset_s > 0:
                        time.sleep(z_reset_s)
                    if wait_s > 0:
                        time.sleep(wait_s)
                    d6 = _measure_point()
                    if isinstance(d6, tuple) and len(d6) == 2 and isinstance(d6[1], str):
                        return {"error": d6[1]}
                    data[iz, iy, ix, :] = np.asarray(d6, float)
                    self.scan_signals.scan_3d_point.emit(iz, ix, iy, float(z), float(x), float(y), d6)
                xy_count += 1
                self.scan_signals.scan_3d_layer_done.emit(xy_count)
        else:
            for s, z in enumerate(zs):
                if self._agent_stop_requested:
                    return {"error": "Scan stopped by user."}
                try:
                    set_z(float(z))
                except Exception as e:
                    return {"error": f"Z move failed at z={z}: {e}"}
                for ix, iy, x, y in _xy_iter():
                    if self._agent_stop_requested:
                        return {"error": "Scan stopped by user."}
                    try:
                        _seq_xy(x, y)
                    except Exception as e:
                        return {"error": f"XY move failed at ({x},{y}): {e}"}
                    if wait_s > 0:
                        time.sleep(wait_s)
                    d6 = _measure_point()
                    if isinstance(d6, tuple) and len(d6) == 2 and isinstance(d6[1], str):
                        return {"error": d6[1]}
                    data[s, iy, ix, :] = np.asarray(d6, float)
                    self.scan_signals.scan_3d_point.emit(s, ix, iy, float(z), float(x), float(y), d6)
                self.scan_signals.scan_3d_layer_done.emit(s + 1)

        d1_vals = data[:, :, :, 0].ravel()
        d1_vals = d1_vals[np.isfinite(d1_vals)]
        stats = {
            "nz": nz, "nx": nx, "ny": ny,
            "d1_min":  round(float(d1_vals.min()), 6) if len(d1_vals) else None,
            "d1_max":  round(float(d1_vals.max()), 6) if len(d1_vals) else None,
            "d1_mean": round(float(d1_vals.mean()), 6) if len(d1_vals) else None,
        }
        img = np.nanmean(data[:, :, :, 0], axis=0)
        png_b64 = _render_2d_png(xs, ys, img, x_key, y_key)
        self._agent_stop_requested = False
        self.log(f"Agent 3D scan done. d1 range: {stats['d1_min']} – {stats['d1_max']}")
        return {"stats": stats, "png_b64": png_b64}


# ── PNG renderers (headless) ─────────────────────────────────────────────────

def _render_1d_png(xs, ys, xlabel: str, ylabel: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 3), dpi=90)
    fig.patch.set_facecolor("#f5f5f5")
    ax.set_facecolor("#ffffff")

    mask = np.isfinite(ys)
    ax.plot(xs[mask], ys[mask], color="#2980b9", linewidth=1.5)
    ax.set_xlabel(xlabel, fontsize=8)
    ax.set_ylabel(ylabel, fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(True, alpha=0.3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=90,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode()


def _render_2d_png(xs, ys, img: np.ndarray, xlabel: str, ylabel: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(4, 4), dpi=90)
    fig.patch.set_facecolor("#f5f5f5")
    ax.set_facecolor("#ffffff")

    extent = [float(xs.min()), float(xs.max()), float(ys.min()), float(ys.max())]
    vals = img[np.isfinite(img)]
    vmin = float(vals.min()) if len(vals) > 0 else None
    vmax = float(vals.max()) if len(vals) > 0 else None
    im = ax.imshow(img, origin='lower', extent=extent, aspect='auto',
                   cmap='viridis', vmin=vmin, vmax=vmax)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_xlabel(xlabel, fontsize=8)
    ax.set_ylabel(ylabel, fontsize=8)
    ax.tick_params(labelsize=7)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=90,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode()
