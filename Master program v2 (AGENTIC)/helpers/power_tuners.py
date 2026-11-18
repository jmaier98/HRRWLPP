import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal
from time import sleep

DEFAULT_FEEDRATE = 1000

# ── Sweep / averaging parameters ──────────────────────────────────────────────
FAST_SCAN_STEP_DEG = 2    # step size for the probe calibration sweep (degrees)
SWEEP_STEP_DEG    = 5     # rough scan step size (degrees)
SWEEP_RANGE_DEG   = 90    # rough scan range — one full cos²(2θ) period
N_AVG_COARSE      = 3     # readings averaged at each rough-scan step
MEDIUM_RANGE_DEG  = 20    # medium scan range, centred on rough-scan best (degrees)
MEDIUM_STEP_DEG   = 1.0   # medium scan step size (degrees)
N_AVG_MEDIUM      = 5     # readings averaged at each medium-scan step
FINE_RANGE_DEG       = 2     # fine scan range, centred on medium-scan best (degrees)
FINE_STEP_DEG        = 0.1   # fine scan step size (degrees)
N_AVG_FINE           = 5     # readings averaged at each fine-scan step
N_AVG_FINAL          = 10    # readings averaged at the final (target) angle when
                             # the fine scan is skipped (probe tuning)
SETTLE_DELAY         = 0.15  # seconds to wait after each rough/medium motor move
FINE_SETTLE_DELAY    = 0.50  # seconds to wait between fine-scan steps
BACKLASH_BACKOFF_DEG = 1.0   # degrees to back off before final approach (cancels motor slop)
RAIL_SETTLE_DELAY    = 2.0   # seconds to wait after btt.clear() for rails to physically retract
PM_SETTLE_DELAY      = 1.0   # seconds to wait after the power meter moves into place,
                             # before a beam is unblocked, and again before measuring


# ── Shutter discipline ────────────────────────────────────────────────────
#
# The sample is exposed whenever a beam is unblocked and the power meter is not
# in the way. Every tuning routine therefore follows the same order:
#
#   note shutter state → close both → power meter in → wait → open the one
#   beam under test ... tune ... close both → power meter out → wait →
#   reopen whichever shutters were open before the routine started
#
# so the sample is never exposed while the power meter is moving, and comes
# back exactly as the operator (or scan) left it, however the routine ended.

def _mark_shutters(state, pump=None, probe=None):
    """Record shutter positions in the shared state, if one was supplied."""
    if state is None:
        return
    if pump is not None:
        state.settings["pump_shutter_open"] = bool(pump)
    if probe is not None:
        state.settings["probe_shutter_open"] = bool(probe)


def _close_both_shutters(shutter, state=None):
    shutter.closePump()
    shutter.closeProbe()
    _mark_shutters(state, pump=False, probe=False)


def _expose_beam_for_pm(shutter, btt, tune_pump: bool, state=None):
    """Both shutters closed → power meter in → settle → unblock one beam."""
    _close_both_shutters(shutter, state)
    btt.powermeter()
    sleep(PM_SETTLE_DELAY)          # let the rails finish moving before exposing
    if tune_pump:
        shutter.openPump()
        _mark_shutters(state, pump=True)
    else:
        shutter.openProbe()
        _mark_shutters(state, probe=True)
    sleep(PM_SETTLE_DELAY)          # let the reading stabilise


def _block_beams_and_retract(shutter, btt, state=None):
    """Close both shutters BEFORE the power meter comes out of the beam path."""
    try:
        _close_both_shutters(shutter, state)
    finally:
        btt.clear()


def _shutter_snapshot(state):
    """(pump_open, probe_open) as recorded in the shared state."""
    if state is None:
        return False, False
    return (bool(state.settings.get("pump_shutter_open", False)),
            bool(state.settings.get("probe_shutter_open", False)))


def _retract_and_restore(shutter, btt, state, snapshot):
    """Block both beams, retract the power meter, then, once it is clear of
    the beam path, reopen whichever shutters were open in `snapshot`."""
    _block_beams_and_retract(shutter, btt, state)
    was_pump_open, was_probe_open = snapshot
    if not (was_pump_open or was_probe_open):
        return
    sleep(RAIL_SETTLE_DELAY)   # power meter clear of the beam path
    if was_pump_open:
        shutter.openPump()
        _mark_shutters(state, pump=True)
    if was_probe_open:
        shutter.openProbe()
        _mark_shutters(state, probe=True)


# ── Noise-robust averaging ─────────────────────────────────────────────────────

def _avg_reading(pm, n=5):
    """
    Take n power readings and return the trimmed mean.
    For n >= 5 the single lowest and highest values are discarded first.
    """
    readings = [pm.read_power() for _ in range(n)]
    if n >= 5:
        readings = sorted(readings)[1:-1]
    return float(np.mean(readings))


# ── Malus's-law fit helpers ────────────────────────────────────────────────────
#
# Physical model (HWP in front of linear polariser):
#   P(θ) = C + D·cos(4θ) + E·sin(4θ)     (θ in degrees)
#
# Equivalent to  P(θ) = C + R·cos(4θ − φ)
# where  R = √(D²+E²),   φ = atan2(E, D)
#   P_max = C + R,   P_min = C − R

def _fit_malus(angles_deg, powers):
    """
    Linear least-squares fit to P(θ) = C + D·cos(4θ) + E·sin(4θ).
    Returns (C, D, E).  Never fails — lstsq always produces a solution.
    """
    theta = np.deg2rad(np.asarray(angles_deg, dtype=float))
    P = np.asarray(powers, dtype=float)
    A = np.column_stack([np.ones_like(theta), np.cos(4 * theta), np.sin(4 * theta)])
    coeffs, _, _, _ = np.linalg.lstsq(A, P, rcond=None)
    return float(coeffs[0]), float(coeffs[1]), float(coeffs[2])


def _fit_range(C, D, E):
    """Return (P_min, P_max) from fit coefficients."""
    R = np.sqrt(D ** 2 + E ** 2)
    return C - R, C + R


def _fit_power(theta_deg, C, D, E):
    """Evaluate the fitted model at theta_deg."""
    theta = np.deg2rad(theta_deg)
    return C + D * np.cos(4 * theta) + E * np.sin(4 * theta)


def _fit_deriv(theta_deg, C, D, E):
    """
    dP/dθ at theta_deg, in units of (power / degree).
    dP/dθ_rad = 4·(−D·sin(4θ) + E·cos(4θ))
    """
    theta = np.deg2rad(theta_deg)
    dP_drad = 4.0 * (-D * np.sin(4 * theta) + E * np.cos(4 * theta))
    return dP_drad / (np.pi / 180.0)   # convert to per-degree


def _best_angle(target, C, D, E, ref_angle):
    """
    Find the angle (degrees) closest to ref_angle where the fitted model
    equals target.  Returns None if target is outside [P_min, P_max].
    """
    R = np.sqrt(D ** 2 + E ** 2)
    if R < 1e-12:
        return None                     # flat curve — can't tune

    ratio = (target - C) / R
    if abs(ratio) > 1.0:
        return None                     # out of range

    phi = np.arctan2(E, D)             # radians
    arc = np.arccos(np.clip(ratio, -1.0, 1.0))

    # Two base solutions from the arccos inversion
    base_solutions = [
        np.rad2deg((phi + arc) / 4),
        np.rad2deg((phi - arc) / 4),
    ]

    # Expand over several 90° periods so we cover angles near ref_angle
    candidates = [b + k * 90.0 for b in base_solutions for k in range(-6, 7)]

    # Return the candidate closest to ref_angle
    return min(candidates, key=lambda c: abs(c - ref_angle))


# ── Base tuner class ───────────────────────────────────────────────────────────

class _BaseTuner(QThread):
    """
    Shared algorithm for pump / probe power tuning.

    Uses nested scans — rough, medium, and (unless SKIP_FINE) fine — to
    converge on the target power.  With SKIP_FINE the tuner parks at the
    medium-scan best angle and skips the fine scan and the final power
    verification.

    Signals
    -------
    reading_ready(angle_deg, power_uW)
        Emitted after every power measurement so the UI can update live.
    phase_changed(str)
        Emitted when a new scan phase begins ("Rough sweep", "Medium scan",
        "Fine scan", "Final") so the UI can start a new plot trace.
    status_message(str)
        Human-readable progress / error messages.
    finished()
        Always emitted when the thread exits (success or failure).
    """
    reading_ready  = pyqtSignal(float, float)
    phase_changed  = pyqtSignal(str)
    status_message = pyqtSignal(str)
    finished       = pyqtSignal()

    SKIP_FINE = False   # subclasses set True to stop after the medium scan
    TUNES_PUMP = False  # True in the pump tuner: the pump is the beam measured

    def __init__(self, target, pm, btt, shutter, initial_wp, state=None):
        super().__init__()
        self.target     = target
        self.pm         = pm
        self.btt        = btt
        self.shutter    = shutter
        self.initial_wp = initial_wp
        self.state      = state
        # None until the run ends. Set to a message on any path that gives up
        # early, so a caller blocking on finished() can tell success from
        # failure — a status message alone is invisible to the agent.
        self.error: str | None = None
        self.final_power: float | None = None
        self.final_angle: float | None = None

    # ── Subclass interface ─────────────────────────────────────────────────────

    def _move(self, angle_deg):
        """Move the waveplate to angle_deg (absolute)."""
        raise NotImplementedError

    # ── Main algorithm ─────────────────────────────────────────────────────────

    def run(self):
        snapshot = _shutter_snapshot(self.state)
        try:
            # Both shutters shut until the power meter is physically in the
            # beam path, then only the beam being tuned is opened.
            _expose_beam_for_pm(self.shutter, self.btt, self.TUNES_PUMP,
                                self.state)

            # ── Phase 1: rough sweep to map the power curve ────────────────────
            self.status_message.emit(
                f"Rough sweep {SWEEP_RANGE_DEG}° to map power curve..."
            )
            self.phase_changed.emit("Rough sweep")
            angles, powers = [], []
            n_steps = int(SWEEP_RANGE_DEG / SWEEP_STEP_DEG) + 1
            for i in range(n_steps):
                angle = self.initial_wp + i * SWEEP_STEP_DEG
                self._move(angle)
                sleep(SETTLE_DELAY)
                p = _avg_reading(self.pm, N_AVG_COARSE)
                angles.append(angle)
                powers.append(p)
                self.reading_ready.emit(angle, p)

            # ── Phase 2: fit Malus's law ───────────────────────────────────────
            C, D, E = _fit_malus(angles, powers)
            P_min, P_max = _fit_range(C, D, E)
            self.status_message.emit(
                f"Fit complete -- P_min={P_min:.2f} uW, P_max={P_max:.2f} uW"
            )

            # ── Phase 3: out-of-range check ────────────────────────────────────
            if self.target < P_min:
                self.error = (
                    f"OUT OF RANGE: target {self.target:.2f} uW is below the "
                    f"minimum achievable power ({P_min:.2f} uW). "
                    f"Check beam alignment or increase source power."
                )
                self.status_message.emit(self.error)
                return
            if self.target > P_max:
                self.error = (
                    f"OUT OF RANGE: target {self.target:.2f} uW exceeds the "
                    f"maximum achievable power ({P_max:.2f} uW). "
                    f"Reduce the target or check the optical path."
                )
                self.status_message.emit(self.error)
                return

            # Rough best angle from fit
            rough_best = _best_angle(self.target, C, D, E, self.initial_wp)
            if rough_best is None:
                self.error = "Fit error: could not invert model to find target angle."
                self.status_message.emit(self.error)
                return
            self.status_message.emit(f"Rough best angle: {rough_best:.2f} deg")

            # ── Phase 4: medium scan centred on rough best ─────────────────────
            self.status_message.emit(
                f"Medium scan +/-{MEDIUM_RANGE_DEG / 2:.0f} deg around {rough_best:.2f} deg..."
            )
            self.phase_changed.emit("Medium scan")
            med_start = rough_best - MEDIUM_RANGE_DEG / 2
            med_n = int(MEDIUM_RANGE_DEG / MEDIUM_STEP_DEG) + 1
            med_angles, med_powers = [], []
            for i in range(med_n):
                angle = med_start + i * MEDIUM_STEP_DEG
                self._move(angle)
                sleep(SETTLE_DELAY)
                p = _avg_reading(self.pm, N_AVG_MEDIUM)
                med_angles.append(angle)
                med_powers.append(p)
                self.reading_ready.emit(angle, p)

            # Fit medium data and find best angle analytically
            try:
                C_m, D_m, E_m = _fit_malus(med_angles, med_powers)
                med_best_angle = _best_angle(self.target, C_m, D_m, E_m, rough_best)
                if med_best_angle is None:
                    raise ValueError("_best_angle returned None")
            except Exception:
                # Fall back to closest measured point
                med_best_idx   = min(range(len(med_powers)),
                                     key=lambda i: abs(med_powers[i] - self.target))
                med_best_angle = med_angles[med_best_idx]
            self.status_message.emit(f"Medium best angle: {med_best_angle:.2f} deg")

            if self.SKIP_FINE:
                # Skip the fine scan: park at the medium best angle (backlash
                # cancellation) and take a quick averaged reading there.
                self._move(med_best_angle - BACKLASH_BACKOFF_DEG)
                sleep(SETTLE_DELAY)
                self._move(med_best_angle)
                sleep(SETTLE_DELAY)
                self.phase_changed.emit("Final")
                final_power = _avg_reading(self.pm, N_AVG_FINAL)
                err      = self.target - final_power
                frac_err = 100.0 * err / self.target if self.target != 0 else float('nan')
                self.status_message.emit(
                    f"Done. Power = {final_power:.2f} uW  "
                    f"(target {self.target:.2f} uW, error {frac_err:+.1f}%)"
                )
                self.reading_ready.emit(med_best_angle, final_power)
                self.final_angle, self.final_power = med_best_angle, final_power
                return

            # ── Phase 5: fine scan centred on medium best ──────────────────────
            self.status_message.emit(
                f"Fine scan +/-{FINE_RANGE_DEG / 2:.2f} deg around {med_best_angle:.2f} deg..."
            )
            self.phase_changed.emit("Fine scan")
            fine_start = med_best_angle - FINE_RANGE_DEG / 2
            fine_n = int(FINE_RANGE_DEG / FINE_STEP_DEG) + 1
            fine_angles, fine_powers = [], []
            for i in range(fine_n):
                angle = fine_start + i * FINE_STEP_DEG
                self._move(angle)
                sleep(FINE_SETTLE_DELAY)
                p = _avg_reading(self.pm, N_AVG_FINE)
                fine_angles.append(angle)
                fine_powers.append(p)
                self.reading_ready.emit(angle, p)

            # Fit fine data and find best angle analytically
            try:
                C_f, D_f, E_f = _fit_malus(fine_angles, fine_powers)
                fine_best_angle = _best_angle(self.target, C_f, D_f, E_f, med_best_angle)
                if fine_best_angle is None:
                    raise ValueError("_best_angle returned None")
            except Exception:
                # Fall back to closest measured point
                fine_best_idx   = min(range(len(fine_powers)),
                                      key=lambda i: abs(fine_powers[i] - self.target))
                fine_best_angle = fine_angles[fine_best_idx]

            # Approach the winning angle (backlash cancellation)
            self._move(fine_best_angle - BACKLASH_BACKOFF_DEG)
            sleep(FINE_SETTLE_DELAY)
            self._move(fine_best_angle)
            sleep(FINE_SETTLE_DELAY)

            # 30-reading average for accurate final power
            self.phase_changed.emit("Final")
            final_power = _avg_reading(self.pm, 30)
            err      = self.target - final_power
            frac_err = 100.0 * err / self.target if self.target != 0 else float('nan')
            self.status_message.emit(
                f"Done. Power = {final_power:.2f} uW  "
                f"(target {self.target:.2f} uW, error {frac_err:+.1f}%)"
            )
            self.reading_ready.emit(fine_best_angle, final_power)
            self.final_angle, self.final_power = fine_best_angle, final_power

        except Exception as exc:
            self.error = f"Tuner error: {exc}"
            self.status_message.emit(self.error)

        finally:
            # Shutters first, power meter second: never uncover the sample
            # while a beam is still live, however the run ended. Then put the
            # shutters back the way they were before the tune.
            _retract_and_restore(self.shutter, self.btt, self.state, snapshot)
            self.finished.emit()


# ── Concrete tuner classes ─────────────────────────────────────────────────────

class ProbeTuner(_BaseTuner):
    """Tunes the probe power by rotating the probe waveplate (rot_3)."""

    TUNES_PUMP = False

    def _move(self, angle_deg):
        self.btt.rot_3(angle_deg, DEFAULT_FEEDRATE)


class PumpTuner(_BaseTuner):
    """Tunes the pump power by rotating the pump waveplate (rot_1)."""

    TUNES_PUMP = True

    def _move(self, angle_deg):
        self.btt.rot_1(angle_deg, DEFAULT_FEEDRATE)


# ── Blocking (synchronous) tuner functions for use in scan worker threads ──────
#
# These run the same scan algorithm as _BaseTuner but without Qt
# signals, so they can be called directly from any background QThread
# (e.g. scan workers).
# status_cb: optional callable(str) for progress messages (e.g. print).
# Raises RuntimeError if the target is out of the achievable power range.
# They run with both shutters closed except while the power meter is in the
# beam path, and put the caller's shutter state back once it is clear.

def _blocking_tune(target, pm, btt, shutter, move_fn, state_wp_key, status_cb,
                   tune_pump: bool = False, skip_fine: bool = False,
                   state=None):
    """Shared scan implementation for both pump and probe blocking tuners.

    tune_pump=True  → open pump shutter, close probe (measuring pump beam)
    tune_pump=False → close pump shutter, open probe (measuring probe beam)
    skip_fine=True  → skip the fine scan: park at the medium-scan best angle
                      and take an N_AVG_FINAL-reading averaged measurement
                      there; returns (angle, measured_power).
    The shutters are put back the way the caller had them once the power
    meter is out of the beam path.
    """
    def emit(msg):
        if status_cb:
            status_cb(msg)
        else:
            print(f"[PowerTuner] {msg}")

    snapshot = _shutter_snapshot(state)
    try:
        _expose_beam_for_pm(shutter, btt, tune_pump, state)
        return _blocking_tune_core(target, pm, btt, move_fn, state_wp_key,
                                   emit, skip_fine)
    finally:
        _retract_and_restore(shutter, btt, state, snapshot)


def _blocking_tune_core(target, pm, btt, move_fn, state_wp_key, emit, skip_fine):
    """The scan itself. Assumes the power meter is in place and exactly the
    beam under test is unblocked; the caller owns shutter and rail cleanup."""
    initial_wp = float(state_wp_key())

    # ── Phase 1: rough sweep ────────────────────────────────────────────────
    emit(f"Rough sweep {SWEEP_RANGE_DEG} deg to map power curve...")
    angles, powers = [], []
    n_steps = int(SWEEP_RANGE_DEG / SWEEP_STEP_DEG) + 1
    for i in range(n_steps):
        angle = initial_wp + i * SWEEP_STEP_DEG
        move_fn(angle)
        sleep(SETTLE_DELAY)
        p = _avg_reading(pm, N_AVG_COARSE)
        angles.append(angle)
        powers.append(p)

    # ── Phase 2: fit ────────────────────────────────────────────────────────
    C, D, E = _fit_malus(angles, powers)
    P_min, P_max = _fit_range(C, D, E)
    emit(f"Fit: P_min={P_min:.2f} uW, P_max={P_max:.2f} uW, target={target:.2f} uW")

    if target < P_min or target > P_max:
        raise RuntimeError(
            f"Power target {target:.2f} uW is out of achievable range "
            f"[{P_min:.2f}, {P_max:.2f}] uW"
        )

    rough_best = _best_angle(target, C, D, E, initial_wp)
    if rough_best is None:
        raise RuntimeError("Could not invert Malus fit to find target angle")
    emit(f"Rough best angle: {rough_best:.2f} deg")

    # ── Phase 3: medium scan ────────────────────────────────────────────────
    emit(f"Medium scan +/-{MEDIUM_RANGE_DEG / 2:.0f} deg around {rough_best:.2f} deg...")
    med_start = rough_best - MEDIUM_RANGE_DEG / 2
    med_n = int(MEDIUM_RANGE_DEG / MEDIUM_STEP_DEG) + 1
    med_angles, med_powers = [], []
    for i in range(med_n):
        angle = med_start + i * MEDIUM_STEP_DEG
        move_fn(angle)
        sleep(SETTLE_DELAY)
        p = _avg_reading(pm, N_AVG_MEDIUM)
        med_angles.append(angle)
        med_powers.append(p)

    # Fit medium data and find best angle analytically
    try:
        C_m, D_m, E_m = _fit_malus(med_angles, med_powers)
        med_best_angle = _best_angle(target, C_m, D_m, E_m, rough_best)
        if med_best_angle is None:
            raise ValueError("_best_angle returned None")
    except Exception:
        med_best_idx   = min(range(len(med_powers)),
                             key=lambda i: abs(med_powers[i] - target))
        med_best_angle = med_angles[med_best_idx]
    emit(f"Medium best angle: {med_best_angle:.2f} deg")

    if skip_fine:
        # Skip the fine scan: park at the medium best angle (backlash
        # cancellation) and take a quick averaged reading there.
        move_fn(med_best_angle - BACKLASH_BACKOFF_DEG)
        sleep(SETTLE_DELAY)
        move_fn(med_best_angle)
        sleep(SETTLE_DELAY)
        final_power = _avg_reading(pm, N_AVG_FINAL)
        err      = target - final_power
        frac_err = 100.0 * err / target if target != 0 else float('nan')
        emit(f"Done. Power = {final_power:.2f} uW "
             f"(target {target:.2f} uW, error {frac_err:+.1f}%)")
        return med_best_angle, final_power

    # ── Phase 4: fine scan ──────────────────────────────────────────────────
    emit(f"Fine scan +/-{FINE_RANGE_DEG / 2:.2f} deg around {med_best_angle:.2f} deg...")
    fine_start = med_best_angle - FINE_RANGE_DEG / 2
    fine_n = int(FINE_RANGE_DEG / FINE_STEP_DEG) + 1
    fine_angles, fine_powers = [], []
    for i in range(fine_n):
        angle = fine_start + i * FINE_STEP_DEG
        move_fn(angle)
        sleep(FINE_SETTLE_DELAY)
        p = _avg_reading(pm, N_AVG_FINE)
        fine_angles.append(angle)
        fine_powers.append(p)

    # Fit fine data and find best angle analytically
    try:
        C_f, D_f, E_f = _fit_malus(fine_angles, fine_powers)
        fine_best_angle = _best_angle(target, C_f, D_f, E_f, med_best_angle)
        if fine_best_angle is None:
            raise ValueError("_best_angle returned None")
    except Exception:
        fine_best_idx   = min(range(len(fine_powers)),
                              key=lambda i: abs(fine_powers[i] - target))
        fine_best_angle = fine_angles[fine_best_idx]

    # Approach the winning angle (backlash cancellation)
    move_fn(fine_best_angle - BACKLASH_BACKOFF_DEG)
    sleep(FINE_SETTLE_DELAY)
    move_fn(fine_best_angle)
    sleep(FINE_SETTLE_DELAY)

    # 30-reading average for accurate final power
    final_power = _avg_reading(pm, 30)
    err      = target - final_power
    frac_err = 100.0 * err / target if target != 0 else float('nan')
    emit(f"Done. Power = {final_power:.2f} uW (error {frac_err:+.1f}%)")
    return fine_best_angle, final_power


def tune_pump_power_blocking(target, pm, btt, shutter, state, status_cb=None):
    """
    Blocking pump power tuner for use inside scan worker threads.

    Performs rough / medium / fine scans of the pump waveplate (rot_1) and
    parks at the angle whose measured power is closest to target.

    Parameters
    ----------
    target   : float  -- desired power in uW
    pm       : PowerMeter instance
    btt      : BTT instance
    shutter  : Shutter instance
    state    : MicroscopeState  (reads/writes rot_1_angle, pump_power)
    status_cb: optional callable(str) for progress messages

    Returns
    -------
    (final_angle_deg, final_power_uW)

    Raises
    ------
    RuntimeError if target power is out of the achievable range.
    """
    def move(angle):
        btt.rot_1(angle, DEFAULT_FEEDRATE)   # rot_1 = pump waveplate
        state.settings["rot_1_angle"] = angle

    angle, power = _blocking_tune(
        target, pm, btt, shutter,
        move_fn=move,
        state_wp_key=lambda: state.settings.get("rot_1_angle", 0.0),
        status_cb=status_cb,
        tune_pump=True,
        state=state,
    )
    state.settings["pump_power"] = power
    return angle, power


def tune_probe_power_blocking(target, pm, btt, shutter, state, status_cb=None):
    """
    Blocking probe power tuner for use inside scan worker threads.

    Performs rough / medium / fine scans of the probe waveplate (rot_3) and
    parks at the angle whose measured power is closest to target.

    Parameters
    ----------
    target   : float  -- desired power in uW
    pm       : PowerMeter instance
    btt      : BTT instance
    shutter  : Shutter instance
    state    : MicroscopeState  (reads/writes rot_3_angle, probe_power)
    status_cb: optional callable(str) for progress messages

    Returns
    -------
    (final_angle_deg, final_power_uW)

    Raises
    ------
    RuntimeError if target power is out of the achievable range.
    """
    def move(angle):
        btt.rot_3(angle, DEFAULT_FEEDRATE)   # rot_3 = probe waveplate
        state.settings["rot_3_angle"] = angle

    angle, power = _blocking_tune(
        target, pm, btt, shutter,
        move_fn=move,
        state_wp_key=lambda: state.settings.get("rot_3_angle", 0.0),
        status_cb=status_cb,
        state=state,
    )
    state.settings["probe_power"] = power
    return angle, power


# ── Fast probe power calibration ───────────────────────────────────────────────

class ProbeCalibrationScanner(QThread):
    """
    Sweeps the probe waveplate (rot_3) over 90° in FAST_SCAN_STEP_DEG steps,
    fits Malus's law,
    and stores the coefficients in state.settings so that set_fast_probe_power_blocking
    can set probe power without using the power meter.

    Signals
    -------
    reading_ready(angle_deg, power_uW)
        Emitted after every power measurement so the UI can plot live.
    phase_changed(str)
        Emitted once when the sweep begins (matches _BaseTuner's interface).
    status_message(str)
    calibration_done(C, D, E, start_angle)
    finished()
    """
    reading_ready    = pyqtSignal(float, float)
    phase_changed    = pyqtSignal(str)
    status_message   = pyqtSignal(str)
    calibration_done = pyqtSignal(float, float, float, float)
    finished         = pyqtSignal()

    def __init__(self, pm, btt, shutter, state):
        super().__init__()
        self.pm      = pm
        self.btt     = btt
        self.shutter = shutter
        self.state   = state

    def run(self):
        snapshot = _shutter_snapshot(self.state)
        try:
            _expose_beam_for_pm(self.shutter, self.btt, tune_pump=False,
                                state=self.state)

            start_angle = float(self.state.settings.get("rot_3_angle", 0.0))
            self.status_message.emit(
                f"Calibration sweep: 90° from {start_angle:.1f}° in "
                f"{FAST_SCAN_STEP_DEG}° steps…"
            )
            self.phase_changed.emit("Calibration sweep")

            n_steps = int(SWEEP_RANGE_DEG / FAST_SCAN_STEP_DEG) + 1
            angles, powers = [], []
            for i in range(n_steps):
                angle = start_angle + i * FAST_SCAN_STEP_DEG
                self.btt.rot_3(angle, DEFAULT_FEEDRATE)   # rot_3 = probe waveplate
                sleep(SETTLE_DELAY)
                p = _avg_reading(self.pm, N_AVG_COARSE)
                angles.append(angle)
                powers.append(p)
                self.reading_ready.emit(angle, p)

            C, D, E = _fit_malus(angles, powers)
            P_min, P_max = _fit_range(C, D, E)
            self.status_message.emit(
                f"Calibration done — P_min={P_min:.2f} µW, P_max={P_max:.2f} µW"
            )

            self.state.settings["probe_fast_cal_C"]     = C
            self.state.settings["probe_fast_cal_D"]     = D
            self.state.settings["probe_fast_cal_E"]     = E
            self.state.settings["probe_fast_cal_start"] = start_angle

            self.calibration_done.emit(C, D, E, start_angle)

        except Exception as exc:
            self.status_message.emit(f"Calibration error: {exc}")
        finally:
            _retract_and_restore(self.shutter, self.btt, self.state, snapshot)
            self.finished.emit()


def set_fast_probe_power_blocking(target, btt, state, status_cb=None):
    """
    Set probe power using the stored Malus's-law fit — no power meter needed.

    Reads fit coefficients written by ProbeCalibrationScanner, computes the
    waveplate angle that gives the target power, and rotates to it.

    Parameters
    ----------
    target    : float  -- desired power in µW
    btt       : BTT instance
    state     : MicroscopeState  (must contain probe_fast_cal_C/D/E/start)
    status_cb : optional callable(str) for progress messages

    Raises
    ------
    RuntimeError if calibration data is missing or target is out of range.
    """
    def emit(msg):
        if status_cb:
            status_cb(msg)
        else:
            print(f"[FastProbe] {msg}")

    C = state.settings.get("probe_fast_cal_C")
    D = state.settings.get("probe_fast_cal_D")
    E = state.settings.get("probe_fast_cal_E")
    if C is None or D is None or E is None:
        raise RuntimeError(
            "No probe calibration data. Run 'Build Probe Power Table' first."
        )
    C, D, E = float(C), float(D), float(E)

    P_min, P_max = _fit_range(C, D, E)
    if target < P_min or target > P_max:
        raise RuntimeError(
            f"Fast probe power {target:.2f} µW is out of calibrated range "
            f"[{P_min:.2f}, {P_max:.2f}] µW"
        )

    ref = float(state.settings.get(
        "rot_3_angle",
        state.settings.get("probe_fast_cal_start", 0.0)
    ))
    angle = _best_angle(target, C, D, E, ref)
    if angle is None:
        raise RuntimeError("Could not invert Malus fit to find target angle.")

    emit(f"Fast probe: {angle:.2f} deg -> {target:.2f} uW")
    btt.rot_3(angle, DEFAULT_FEEDRATE)   # rot_3 = probe waveplate
    state.settings["rot_3_angle"] = angle
    state.settings["probe_power"] = target
