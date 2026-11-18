# tabs/alignment_tab.py
from __future__ import annotations
import time
from functools import partial
from typing import Callable, List, Optional, Tuple

import numpy as np
from scipy.optimize import curve_fit

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot, Qt, QMetaObject, Q_ARG
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


def _double_sigmoid(x, x1, x2, w1, w2, B1, B2, A):
    """Rising edge at x1 minus a falling edge at x2, each with its own width
    and height.

    The two edges are independent, so the level far to the left (A) and the
    level far to the right (A + B1 - B2) need not match. Real features sit on
    a step in the background — one side of the wire is brighter than the other
    — and the earlier shared-amplitude form had to split the difference,
    pulling the fitted edges outwards and biasing the centre.
    """
    x = np.asarray(x, float)
    return (A
            + B1 / (1 + np.exp(-(x - x1) / w1))
            - B2 / (1 + np.exp(-(x - x2) / w2)))


def _template_model(x, shift, scale, offset, tx, ty):
    """The recorded shape, slid along x and stretched vertically."""
    return offset + scale * np.interp(np.asarray(x, float) - shift, tx, ty)


def _extremum_center(xc, yc) -> float:
    """Position of the most extreme point relative to the median level.

    Used to seed the template fit, and as the reference centre when the
    original shape is recorded with no template to fit against yet.
    """
    base = float(np.median(yc))
    return float(xc[int(np.argmax(np.abs(np.asarray(yc, float) - base)))])


def _template_arrays(template: dict):
    """(x, y, centre) of a stored template, sorted by x. None if unusable."""
    if not template:
        return None
    try:
        tx = np.asarray(template["x"], float)
        ty = np.asarray(template["y"], float)
        tc = float(template["center"])
    except Exception:
        return None
    good = np.isfinite(tx) & np.isfinite(ty)
    tx, ty = tx[good], ty[good]
    if tx.size < 4 or not np.isfinite(tc):
        return None
    order = np.argsort(tx)
    return tx[order], ty[order], tc


# Fraction of the scanned range the shifted template must still cover. Beyond
# this the template hangs off the end of the data, np.interp flattens it to its
# end value, and a slid-away template can score as well as a real match.
TEMPLATE_MIN_OVERLAP = 0.6
TEMPLATE_GRID_STEPS  = 401
# Least of the trace's variation the matched shape must account for. A
# template slid onto a featureless stretch can be squashed almost flat and
# still leave a small residual; that is not a match, so it is rejected.
TEMPLATE_MIN_R2      = 0.5


def _template_shift_bounds(xc, tx, min_overlap: float = TEMPLATE_MIN_OVERLAP):
    """Shifts that keep the template covering `min_overlap` of the scan."""
    x0, x1 = float(xc.min()), float(xc.max())
    t0, t1 = float(tx.min()), float(tx.max())
    need = min_overlap * (x1 - x0)
    if (t1 - t0) < need:                       # template shorter than required
        need = min_overlap * (t1 - t0)
    lo = (x0 + need) - t1                      # template's right end reaches x0+need
    hi = (x1 - need) - t0                      # template's left end reaches x1-need
    if lo > hi:
        lo = hi = 0.0
    return lo, hi


def _template_overlap_mask(xc, tx, shift):
    """Points whose shifted position lands inside the recorded shape.

    Outside that range np.interp returns the template's end value, i.e. an
    invented flat line. Scoring those points would let a badly shifted
    template look good against a flat stretch of data, and would let a steep
    edge just off the end dominate the residual, so they are left out.
    """
    u = np.asarray(xc, float) - shift
    return (u >= tx[0]) & (u <= tx[-1])


def _template_linear_solve(yc, t):
    """Best scale/offset for a fixed shift, and the mean squared residual.

    With the shift held fixed the model is linear in (scale, offset), so this
    part needs no iteration — which is what makes the shift grid below cheap.
    The score is per-point, so shifts compared over different numbers of
    overlapping points stay comparable.
    """
    if t.size < 3:
        return None
    A = np.column_stack([t, np.ones_like(t)])
    try:
        coef, *_ = np.linalg.lstsq(A, yc, rcond=None)
    except np.linalg.LinAlgError:
        return None
    resid = yc - A @ coef
    return float(coef[0]), float(coef[1]), float(resid @ resid) / t.size


def _template_grid_search(xc, yc, tx, ty, lo, hi, n=TEMPLATE_GRID_STEPS):
    """Scan every plausible shift and keep the best. Returns (shift, scale, offset).

    The residual as a function of shift has one deep minimum where the feature
    lines up and a broad flat plain everywhere else, so a local optimiser
    started at a bad guess just sits on the plain. Sweeping the whole allowed
    range first finds the real minimum regardless of the starting guess.
    """
    need = max(3, int(TEMPLATE_MIN_OVERLAP * len(xc)))
    best = None
    for shift in np.linspace(lo, hi, max(3, int(n))):
        m = _template_overlap_mask(xc, tx, shift)
        if m.sum() < need:
            continue
        t = np.interp(xc[m] - shift, tx, ty)
        sol = _template_linear_solve(yc[m], t)
        if sol is None:
            continue
        scale, offset, mse = sol
        if scale <= 0:            # an inverted feature is not the same feature
            continue
        if best is None or mse < best[3]:
            best = (float(shift), scale, offset, mse)
    return best


def _fit_template_center(x: np.ndarray, y: np.ndarray, template: dict
                         ) -> Tuple[Optional[float], Optional[dict]]:
    """Match the trace against the shape recorded when the origin was set.

    Only a translation along the scan axis and a vertical scale + offset are
    free, so the feature keeps whatever odd profile it actually has instead of
    being forced onto a sigmoid. The centre is the recorded centre carried
    along by the fitted shift.

    What is minimised is the sum of squared residuals between the trace and
    the shifted/scaled template, over every measured point.
    """
    mask = np.isfinite(y)
    if mask.sum() < 4:
        return None, None
    xc, yc = np.asarray(x, float)[mask], np.asarray(y, float)[mask]
    order = np.argsort(xc)
    xc, yc = xc[order], yc[order]

    tpl = _template_arrays(template)
    if tpl is None:
        # No shape on file yet: this run is the one that records it, so the
        # centre is defined from the trace itself.
        return _extremum_center(xc, yc), {
            "model": "template_seed",
            "center_err": float("nan"),
        }
    tx, ty, tc = tpl

    lo, hi = _template_shift_bounds(xc, tx)
    best = _template_grid_search(xc, yc, tx, ty, lo, hi)
    if best is None:
        print("[Alignment] Template fit failed: no usable shift found.")
        return None, None
    shift0, scale0, offset0, _ = best

    def model(xv, shift, scale, offset):
        return _template_model(xv, shift, scale, offset, tx, ty)

    # Polish the grid winner. The grid is already within one step of the
    # minimum, so this only refines it — it cannot wander to the far side.
    step = (hi - lo) / max(1, TEMPLATE_GRID_STEPS - 1)
    b_lo = [max(lo, shift0 - 2 * step), 1e-6, -np.inf]
    b_hi = [min(hi, shift0 + 2 * step), np.inf, np.inf]
    shift, scale, offset = shift0, scale0, offset0
    center_err = float("nan")
    # Refine on the same overlapping points the grid scored, so the polish
    # step optimises the same quantity the search did.
    m = _template_overlap_mask(xc, tx, shift0)
    if b_lo[0] < b_hi[0] and m.sum() >= 4:
        try:
            popt, pcov = curve_fit(model, xc[m], yc[m],
                                   p0=[shift0, scale0, offset0],
                                   bounds=(b_lo, b_hi), maxfev=20000)
            shift, scale, offset = popt
            center_err = _center_err(pcov, [0])
        except Exception as e:
            print(f"[Alignment] Template refine failed ({e}); using grid best.")

    center = tc + shift
    if not (xc.min() <= center <= xc.max()):
        # The matched feature centre is outside the window that was scanned,
        # so nothing was really found — better to report a failure than to
        # hand back a centre the scan never visited.
        print(f"[Alignment] Template fit rejected: centre {center:.4f} lies "
              f"outside the scanned range [{xc.min():.4f}, {xc.max():.4f}].")
        return None, None

    m = _template_overlap_mask(xc, tx, shift)
    if m.sum() < 3:
        m = np.ones_like(xc, dtype=bool)
    resid = yc[m] - model(xc[m], shift, scale, offset)
    rms   = float(np.sqrt(np.mean(resid ** 2)))

    # How much of the trace's own variation the matched shape accounts for.
    ss_tot = float(np.sum((yc[m] - yc[m].mean()) ** 2))
    r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else 0.0
    if r2 < TEMPLATE_MIN_R2:
        print(f"[Alignment] Template fit rejected: shape explains only "
              f"{100 * max(r2, 0.0):.0f}% of the trace (needs "
              f"{100 * TEMPLATE_MIN_R2:.0f}%). The feature is probably not in "
              f"the scanned window.")
        return None, None
    return center, {"model": "template", "shift": shift, "scale": scale,
                    "offset": offset, "tx": tx, "ty": ty,
                    "template_center": tc, "rms": rms, "r2": r2,
                    "center_err": center_err}


def _eval_fit(info: dict, x) -> np.ndarray:
    """Evaluate whichever model `info` describes."""
    if info["model"] == "template":
        return _template_model(x, info["shift"], info["scale"], info["offset"],
                               info["tx"], info["ty"])
    if info["model"] == "template_seed":
        return np.full(np.asarray(x, float).shape, np.nan)
    if info["model"] == "double_sigmoid":
        return _double_sigmoid(x, info["x1"], info["x2"], info["w1"],
                               info["w2"], info["B1"], info["B2"], info["A"])
    return _sigmoid(x, info["x0"], info["w"], info["A"], info["B"])


def _center_err(pcov, idx, scale: float = 1.0) -> float:
    """1-sigma uncertainty of the feature centre from the fit covariance.

    ``idx`` lists the parameters the centre is built from; for the double
    sigmoid the centre is (x1+x2)/2, so both variances and their covariance
    contribute and ``scale`` is 0.5.  Returns NaN when curve_fit could not
    estimate the covariance (singular Jacobian, parameter on a bound).
    """
    try:
        cov = np.asarray(pcov, float)
        var = sum(cov[i, j] for i in idx for j in idx)
        if not np.isfinite(var) or var < 0:
            return float("nan")
        return float(scale * np.sqrt(var))
    except Exception:
        return float("nan")


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
        popt, pcov = curve_fit(_sigmoid, xc, yc, p0=p0, bounds=bounds, maxfev=5000)
        x0, w, A, B = popt
        return x0, {"model": "sigmoid", "x0": x0, "w": w, "A": A, "B": B,
                    "center_err": _center_err(pcov, [0])}
    except Exception as e:
        print(f"[Alignment] Sigmoid fit failed: {e}")
        return None, None


def _fit_double_sigmoid_center(x: np.ndarray, y: np.ndarray
                                ) -> Tuple[Optional[float], Optional[dict]]:
    mask = np.isfinite(y)
    if mask.sum() < 7:                      # 7 free parameters
        return None, None
    xc, yc = x[mask], y[mask]
    order = np.argsort(xc)                  # edge estimates below assume sorted x
    xc, yc = xc[order], yc[order]
    span = xc.max() - xc.min()
    if span <= 0:
        return None, None

    # Seed from the data: the two baselines are read off the ends, the peak
    # from the most extreme point, and the edges from where the signal first
    # crosses halfway on each side of that peak.
    n_edge   = max(1, len(yc) // 10)
    base_l   = float(np.mean(yc[:n_edge]))
    base_r   = float(np.mean(yc[-n_edge:]))
    peak_idx = int(np.argmax(np.abs(yc - 0.5 * (base_l + base_r))))
    peak     = float(yc[peak_idx])

    half = 0.5 * (peak + max(base_l, base_r)) if peak > max(base_l, base_r) \
        else 0.5 * (peak + min(base_l, base_r))
    # Left edge: the half-crossing closest to the peak from below; right edge:
    # the first one after it.
    left_x  = _half_crossings(xc[:peak_idx + 1], yc[:peak_idx + 1], half)
    right_x = _half_crossings(xc[peak_idx:],     yc[peak_idx:],     half)
    x1_0 = max(left_x)  if left_x  else xc[peak_idx] - span / 8
    x2_0 = min(right_x) if right_x else xc[peak_idx] + span / 8

    w0 = max(span / 20, 1e-6)
    p0 = [x1_0, x2_0, w0, w0, peak - base_l, peak - base_r, base_l]
    lo = [xc.min() - span * .5, xc.min() - span * .5, 1e-9, 1e-9,
          -np.inf, -np.inf, -np.inf]
    hi = [xc.max() + span * .5, xc.max() + span * .5, span, span,
          np.inf, np.inf, np.inf]
    p0 = [min(max(v, l), h) for v, l, h in zip(p0, lo, hi)]

    try:
        popt, pcov = curve_fit(_double_sigmoid, xc, yc, p0=p0,
                               bounds=(lo, hi), maxfev=20000)
    except Exception as e:
        print(f"[Alignment] Double-sigmoid fit failed: {e}")
        return None, None

    x1, x2, w1, w2, B1, B2, A = popt
    # The centre is the midpoint of the two edges, which is unchanged if the
    # fit labels them the other way round, so no re-ordering is needed.
    center = (x1 + x2) / 2
    return center, {"model": "double_sigmoid", "x1": x1, "x2": x2,
                    "w1": w1, "w2": w2, "B1": B1, "B2": B2, "A": A,
                    "center_err": _center_err(pcov, [0, 1], scale=0.5)}


def _half_crossings(xs, ys, half) -> list:
    """Every x where ys crosses the `half` level, by linear interpolation."""
    out = []
    for i in range(len(ys) - 1):
        a, b = float(ys[i]), float(ys[i + 1])
        if a == b:
            continue
        if (a - half) * (b - half) <= 0:
            frac = (half - a) / (b - a)
            out.append(float(xs[i]) + frac * (float(xs[i + 1]) - float(xs[i])))
    return out


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
        h_template: dict | None = None,
        v_template: dict | None = None,
        pre_wait_s: float = 0.0,
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
        self._pre_wait_s      = max(0.0, pre_wait_s)
        self._h_fit           = h_fit_mode
        self._v_fit           = v_fit_mode
        self._h_template      = h_template
        self._v_template      = v_template
        # Raw traces, kept so the tab can store them as the reference shape
        # when this run sets the original position.
        self.h_trace: tuple | None = None
        self.v_trace: tuple | None = None
        self.x_result: Optional[float] = None
        self.y_result: Optional[float] = None
        self.x_err: float = float("nan")
        self.y_err: float = float("nan")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _scan(self, positions: np.ndarray, fixed: float,
              direction: str, fit_mode: str, template: dict | None = None):
        """
        1D scan along `positions`.  direction='horizontal' keeps Y=fixed;
        direction='vertical' keeps X=fixed.
        Returns (center, pos, vals, fitted, center_err).
        """
        vals = []
        for p in positions:
            if self._stop[0]:
                return (None, positions, np.full(len(positions), np.nan),
                        np.array([]), float("nan"))
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

        if fit_mode == "template":
            center, info = _fit_template_center(positions, vals, template)
        elif fit_mode == "double_sigmoid":
            center, info = _fit_double_sigmoid_center(positions, vals)
        else:
            center, info = _fit_sigmoid_center(positions, vals)
        fitted = _eval_fit(info, pf) if info is not None else np.full_like(pf, np.nan)

        err = float("nan") if info is None else info.get("center_err", float("nan"))
        return center, positions, vals, fitted, err

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

            # ── Settle at the first point before the horizontal cut ───────────
            # Lets the lock-in settle after e.g. a shutter opening or a power
            # change, with the beam already near the feature.
            if self._pre_wait_s > 0:
                self._set_galvo(h_pos[0], self._hy0)
                t_end = time.perf_counter() + self._pre_wait_s
                while time.perf_counter() < t_end:
                    if self._stop[0]:
                        self.finished.emit(True, None, None, "Stopped by user.")
                        return
                    time.sleep(min(0.05, max(0.0, t_end - time.perf_counter())))

            x_center, tp, tv, tf, x_err = self._scan(h_pos, self._hy0,
                                                     "horizontal", self._h_fit,
                                                     self._h_template)
            self.h_trace = (np.asarray(tp, float).tolist(),
                            np.asarray(tv, float).tolist())
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
            y_center, tp, tv, tf, y_err = self._scan(v_pos, self._vx0,
                                                     "vertical", self._v_fit,
                                                     self._v_template)
            self.v_trace = (np.asarray(tp, float).tolist(),
                            np.asarray(tv, float).tolist())
            self.progress.emit("vertical", y_center, tp, tv, tf)
            self._set_galvo(self._vx0, self._vy0)
            time.sleep(self._wait_s * 2)

            self.x_result = x_center
            self.y_result = y_center
            self.x_err    = x_err
            self.y_err    = y_err
            xc_str = f"{x_center:.4f}" if x_center is not None else "N/A"
            yc_str = f"{y_center:.4f}" if y_center is not None else "N/A"
            self.finished.emit(False, x_center, y_center,
                               f"Done. X centre = {xc_str},  Y centre = {yc_str}")

        except Exception as e:
            self.finished.emit(True, None, None, f"Unexpected error: {e}")


# ── Tab ───────────────────────────────────────────────────────────────────────

class AlignmentTab(QWidget):
    # Emitted after every completed find-feature alignment, for the Tracking tab.
    alignment_recorded = pyqtSignal(dict)
    # Emitted when the origin is (re)set — tracking history starts over from here.
    origin_reset       = pyqtSignal(dict)

    def __init__(self, controller, webcam_tab=None):
        super().__init__()
        self.controller = controller
        self.webcam_tab = webcam_tab
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
        self.h_fit.addItem("Recorded feature shape",           "template")

        # Vertical scan
        self.vx0   = QLineEdit("0");    self.vx0.setValidator(dv)
        self.vy0   = QLineEdit("0");    self.vy0.setValidator(dv)
        self.v_dist= QLineEdit("100");  self.v_dist.setValidator(dv_pos)
        self.n_v   = QLineEdit("50");   self.n_v.setValidator(iv_pos)
        self.v_dir = QCheckBox("Bottom → Top");  self.v_dir.setChecked(True)
        self.v_fit = QComboBox()
        self.v_fit.addItem("Single sigmoid (edge)",          "sigmoid")
        self.v_fit.addItem("Double sigmoid / hat (wire)",    "double_sigmoid")
        self.v_fit.addItem("Recorded feature shape",           "template")

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
        self.pre_wait_ms = QLineEdit("0")
        self.pre_wait_ms.setValidator(QIntValidator(0, 10_000_000, self))
        self.pre_wait_ms.setToolTip(
            "Move to the first point of the horizontal cut, then wait this long "
            "before scanning (lets the lock-in settle after a shutter opens or "
            "the power changes).")

        form.addRow(QLabel("── Measurement ──────────────────"))
        form.addRow("Signal:",            self.meas_combo)
        form.addRow("Wait (ms):",         self.wait_ms)
        form.addRow("Wait before alignment (ms):", self.pre_wait_ms)
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

        self.track_chk = QCheckBox("Track feature")
        self.track_chk.setToolTip(
            "Start each cut at the entered start point shifted by the offset "
            "from the last alignment, so a feature that has drifted far from "
            "the original position stays inside the scanned window. Ignored "
            "when setting the original position, which defines a fresh "
            "reference with zero offset.")

        self.move_to_target_chk = QCheckBox("Return to target after alignment")
        self.move_to_target_chk.setToolTip(
            "The last step of every alignment (manual, set-original, or during "
            "a scan) parks the galvo at Target pos + the measured offset, so a "
            "scan that re-aligns comes back to the same spot on the sample. "
            "Scans whose X/Y axes are the galvo override this by moving to "
            "their own scan point + offset.")

        # Target position: entered once, used by every alignment.
        self.target_x_edit = QLineEdit(str(self.state.settings.get("align_target_x", 0.0)))
        self.target_y_edit = QLineEdit(str(self.state.settings.get("align_target_y", 0.0)))
        for w, key in ((self.target_x_edit, "align_target_x"),
                       (self.target_y_edit, "align_target_y")):
            w.setValidator(dv)
            w.setToolTip("Galvo position to return to after alignment "
                         "(the measured offset is added to it).")
            w.editingFinished.connect(partial(self._save_target, w, key))
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("X:")); target_row.addWidget(self.target_x_edit)
        target_row.addWidget(QLabel("Y:")); target_row.addWidget(self.target_y_edit)
        form.addRow(QLabel("── Tracking ──────────────────"))
        form.addRow(self.track_chk)
        form.addRow(QLabel("\u2500\u2500 After alignment \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"))
        form.addRow(self.move_to_target_chk)
        form.addRow("Target pos:", target_row)

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
            pre_wait_s = int(self.pre_wait_ms.text() or 0) / 1000.0
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Invalid parameter values.")
            return

        # Follow the drift: start the cuts where the feature was last seen.
        tdx, tdy = self._track_offsets(update_original)
        hx0 += tdx; hy0 += tdy
        vx0 += tdx; vy0 += tdy
        if tdx or tdy:
            self.controller.log(
                f"Tracking feature: cut start points shifted by "
                f"({tdx:.4f},{tdy:.4f})")

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
            pre_wait_s=pre_wait_s,
            h_fit_mode=self.h_fit.currentData(),
            v_fit_mode=self.v_fit.currentData(),
            # A Set-original run in template mode re-seeds the shape: it must
            # fit the trace on its own terms, not against the old recording,
            # which is about to be replaced by this scan anyway.
            h_template=None if update_original else self.get_template("h"),
            v_template=None if update_original else self.get_template("v"),
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
            lbl = {"double_sigmoid": "Hat fit",
                   "template": "Template fit"}.get(fit_mode, "Sigmoid fit")
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
        worker = self._worker
        x_err = getattr(worker, "x_err", float("nan")) if worker else float("nan")
        y_err = getattr(worker, "y_err", float("nan")) if worker else float("nan")
        # Wait for the thread to exit before dropping the last reference
        # (finished is emitted from inside run()), else Qt aborts the process.
        self._worker = None
        if worker is not None:
            worker.wait()

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
            self._store_templates(worker, x_center, y_center)
            self._record_origin(x_center, y_center, "manual")
            self._move_to_target(0.0, 0.0)
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
            self._record_alignment(x_center, y_center, dx, dy, x_err, y_err, "manual")
            self._move_to_target(dx, dy)

    # ── Feature tracking ────────────────────────────────────

    def _track_offsets(self, update_original: bool = False,
                       params: dict | None = None) -> tuple:
        """How far to slide the cut start points to follow the feature.

        (0, 0) unless "Track feature" is ticked. A set-original run always gets
        (0, 0): it defines a new reference, and the stored offset belongs to the
        old one.
        """
        if update_original:
            return 0.0, 0.0
        tracking = (params.get("track_feature") if params is not None
                    else self.track_chk.isChecked())
        if not tracking:
            return 0.0, 0.0
        return (float(self.state.settings.get("x_offset", 0.0) or 0.0),
                float(self.state.settings.get("y_offset", 0.0) or 0.0))

    # ── Reference-shape (template) storage ────────────────────────────────────

    def get_template(self, axis: str) -> dict | None:
        """The trace recorded for this axis when the original was last set."""
        tpl = self.state.settings.get(f"align_template_{axis}")
        return tpl if isinstance(tpl, dict) else None

    def _store_templates(self, worker, x_center, y_center):
        """Remember the traces from a Set-original run as the reference shape.

        Stored for both axes on every set-original run, whatever fit model was
        used, so switching to the template model later needs no extra step.
        """
        for axis, trace, center in (("h", getattr(worker, "h_trace", None), x_center),
                                    ("v", getattr(worker, "v_trace", None), y_center)):
            if not trace or center is None:
                continue
            xs, ys = trace
            self.state.settings[f"align_template_{axis}"] = {
                "x": list(xs), "y": list(ys), "center": float(center),
            }
        self.state.save()

    # ── Move to target ────────────────────────────────────────────────────────

    def _target_position(self) -> tuple:
        """The Target pos X/Y entries, or (None, None) if either is invalid."""
        try:
            return (float(self.target_x_edit.text()),
                    float(self.target_y_edit.text()))
        except ValueError:
            return None, None

    def _save_target(self, widget: QLineEdit, key: str):
        """Remember the target across restarts."""
        try:
            self.state.settings[key] = float(widget.text())
        except ValueError:
            return
        self.state.save()

    def _move_to_target(self, dx: float, dy: float, params: dict | None = None):
        """Park the galvo at Target pos shifted by the measured offset.

        `params` carries the target captured on the GUI thread (scan/agent
        paths); without it the fields are read directly.
        """
        if params is not None:
            if not params.get("move_to_target"):
                return
            tx, ty = params.get("target_x"), params.get("target_y")
        else:
            if not self.move_to_target_chk.isChecked():
                return
            tx, ty = self._target_position()

        if tx is None or ty is None:
            self.controller.log(
                "Return to target skipped: Target pos X/Y is not a valid number.")
            return
        if not self.galvo:
            return

        x, y = tx + dx, ty + dy
        self.galvo.move_xy(x, y)
        self.state.settings["galvo_x_position"] = x
        self.state.settings["galvo_y_position"] = y
        self.controller.log(
            f"Moved to target ({tx:g},{ty:g}) + offset ({dx:.4f},{dy:.4f}) "
            f"-> ({x:.4f},{y:.4f})")

    # ── Drift-tracking hooks ──────────────────────────────────────────────────

    def _record_alignment(self, x_center, y_center, dx, dy, x_err, y_err,
                          source: str):
        """Publish one alignment result to the Tracking tab."""
        self.alignment_recorded.emit({
            "t":        time.time(),
            "x_center": float(x_center),
            "y_center": float(y_center),
            "x_offset": float(dx),
            "y_offset": float(dy),
            "x_err":    float(x_err),
            "y_err":    float(y_err),
            "source":   source,
        })

    def _record_origin(self, x_center, y_center, source: str):
        """Tell the Tracking tab to clear its history: a new origin was set."""
        self.origin_reset.emit({
            "t":        time.time(),
            "x_center": float(x_center),
            "y_center": float(y_center),
            "source":   source,
        })

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
            "pre_wait_s": int(self.pre_wait_ms.text() or 0) / 1000.0,
            "h_fit_mode": self.h_fit.currentData(),
            "v_fit_mode": self.v_fit.currentData(),
            "h_template": self.get_template("h"),
            "v_template": self.get_template("v"),
            "move_to_target": self.move_to_target_chk.isChecked(),
            "track_feature":  self.track_chk.isChecked(),
            "target_x":       self._target_position()[0],
            "target_y":       self._target_position()[1],
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

        tdx, tdy = self._track_offsets(False, p)
        if tdx or tdy:
            print(f"[Alignment] Tracking feature: start points shifted by "
                  f"({tdx:.4f},{tdy:.4f})")

        worker = _AlignmentWorker(
            h_start_x=p["hx0"] + tdx, h_start_y=p["hy0"] + tdy,
            h_distance=p["h_dist"], h_left_right=p["h_left_right"],
            v_start_x=p["vx0"] + tdx, v_start_y=p["vy0"] + tdy,
            v_distance=p["v_dist"], v_bottom_top=p["v_bottom_top"],
            n_h=p["n_h"], n_v=p["n_v"],
            set_galvo_fn=lambda x, y: self.galvo.move_xy(x, y),
            measure_fn=self._measure,
            stop_flag_ref=stop_flag_ref,
            wait_s=p["wait_s"],
            pre_wait_s=p.get("pre_wait_s", 0.0),
            h_fit_mode=p["h_fit_mode"],
            v_fit_mode=p["v_fit_mode"],
            h_template=p.get("h_template"),
            v_template=p.get("v_template"),
        )
        # Plot the cuts and fits live, exactly as a manual run does, so the
        # alignments a long scan performs can be eyeballed afterwards.
        worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
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
        self._record_alignment(x_center, y_center, dx, dy,
                               worker.x_err, worker.y_err, "scan")
        self._move_to_target(dx, dy, p)
        return dx, dy

    # ── Agent API ─────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        return {
            "measurement":     self.meas_combo.currentText(),
            "wait_ms":         self.wait_ms.text(),
            "pre_wait_ms":     self.pre_wait_ms.text(),
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
            "move_to_target":  self.move_to_target_chk.isChecked(),
            "track_feature":   self.track_chk.isChecked(),
            "target_x":        self._target_position()[0],
            "target_y":        self._target_position()[1],
            "has_h_template":  self.get_template("h") is not None,
            "has_v_template":  self.get_template("v") is not None,
            "measurements_available": list(self.meas_combo.itemText(i)
                                           for i in range(self.meas_combo.count())),
            "fit_models_available": ["sigmoid", "double_sigmoid", "template"],
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
        _set_text(self.pre_wait_ms, "pre_wait_ms")
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
        if "move_to_target" in settings:
            self.move_to_target_chk.setChecked(bool(settings["move_to_target"]))
        if "track_feature" in settings:
            self.track_chk.setChecked(bool(settings["track_feature"]))
        for w, key, skey in ((self.target_x_edit, "target_x", "align_target_x"),
                             (self.target_y_edit, "target_y", "align_target_y")):
            if key in settings:
                w.setText(str(settings[key]))
                self._save_target(w, skey)

        for combo, key in ((self.h_fit, "h_fit_model"), (self.v_fit, "v_fit_model")):
            if key in settings:
                for i in range(combo.count()):
                    if combo.itemData(i) == settings[key]:
                        combo.setCurrentIndex(i)
                        break
                else:
                    warnings.append(f"Unknown fit model for {key}: {settings[key]}")
                if settings[key] == "template" and self.get_template(key[0]) is None:
                    warnings.append(
                        f"No recorded feature shape for the {key[0]} axis yet — "
                        "run 'Set original position' first.")

        # Bring the GUI to this tab so the operator sees the agent's settings,
        # mirroring how the 2D scan activates its tab.
        self.controller.scan_signals.activate_tab.emit("Alignment")
        return warnings

    @pyqtSlot(str)
    def _agent_alignment_finished(self, message: str):
        """Final GUI update after an agent alignment run (runs on the GUI thread)."""
        self.status_lbl.setText("Done.")
        self.info_lbl.setText(message)
        self._worker = None

    def run_find_feature_blocking(self, set_origin: bool = False) -> dict:
        """
        Run the alignment scan (for agent use) with the same live GUI plotting as a
        manual run, then block until it finishes.
        If set_origin=True, saves the found position as the new origin.
        Returns a dict with x_center, y_center, x_offset, y_offset.
        """
        if not self.galvo:
            return {"error": "Galvo not available"}

        # Activate the tab so the operator watches the cuts live, like a manual run.
        self.controller.scan_signals.activate_tab.emit("Alignment")
        p = self.get_alignment_params()
        self._stop_flag[0] = False
        tdx, tdy = self._track_offsets(set_origin, p)
        if tdx or tdy:
            self.controller.log(f"Tracking feature: cut start points shifted "
                                f"by ({tdx:.4f},{tdy:.4f})")

        worker = _AlignmentWorker(
            h_start_x=p["hx0"] + tdx, h_start_y=p["hy0"] + tdy,
            h_distance=p["h_dist"], h_left_right=p["h_left_right"],
            v_start_x=p["vx0"] + tdx, v_start_y=p["vy0"] + tdy,
            v_distance=p["v_dist"], v_bottom_top=p["v_bottom_top"],
            n_h=p["n_h"],           n_v=p["n_v"],
            set_galvo_fn=lambda x, y: self.galvo.move_xy(x, y),
            measure_fn=self._measure,
            stop_flag_ref=self._stop_flag,
            wait_s=p["wait_s"],
            pre_wait_s=p.get("pre_wait_s", 0.0),
            h_fit_mode=p["h_fit_mode"],
            v_fit_mode=p["v_fit_mode"],
            # As in the manual path: setting the origin re-seeds the shape.
            h_template=None if set_origin else p.get("h_template"),
            v_template=None if set_origin else p.get("v_template"),
        )
        # Live plotting: same GUI handler the manual run uses (horizontal then vertical).
        worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker = worker
        worker.start()
        worker.wait()

        x_center = worker.x_result
        y_center = worker.y_result
        if x_center is None or y_center is None:
            QMetaObject.invokeMethod(self, "_agent_alignment_finished",
                                     Qt.ConnectionType.QueuedConnection,
                                     Q_ARG(str, "Alignment fit failed."))
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
            self._store_templates(worker, x_center, y_center)
            self._record_origin(x_center, y_center, "agent")
            self._move_to_target(0.0, 0.0, p)
            QMetaObject.invokeMethod(
                self, "_agent_alignment_finished", Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, f"Origin set — X = {x_center:.4f}, Y = {y_center:.4f}"))
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
            self._record_alignment(x_center, y_center, dx, dy,
                                   worker.x_err, worker.y_err, "agent")
            self._move_to_target(dx, dy, p)
            QMetaObject.invokeMethod(
                self, "_agent_alignment_finished", Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, f"Found — X = {x_center:.4f}, Y = {y_center:.4f}  |  "
                           f"Offset Δx = {dx:.4f}, Δy = {dy:.4f}"))
            return {"x_center": x_center, "y_center": y_center,
                    "x_offset": dx, "y_offset": dy, "origin_updated": False}

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._stop_flag[0] = True
            self._worker.wait(2000)
        event.accept()
