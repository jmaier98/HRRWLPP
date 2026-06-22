"""
Scanner Model — pure Python, no GUI dependencies.

Simulates:
  • 2D scanning stage with a CW detector (sum-of-Gaussians sample)
  • Pump-probe transient spectroscopy with physically realistic
    bi-exponential decays whose time constants vary across the sample
"""

import numpy as np
from dataclasses import dataclass, field


# ── Data containers ───────────────────────────────────────────

@dataclass
class ScanResult:
    """A single CW measurement at a stage position."""
    x: float
    y: float
    value: float


@dataclass
class PPTrace:
    """One pump-probe time trace captured at a fixed stage position."""
    x: float
    y: float
    pump_power: float        # mW
    probe_power: float       # mW
    delays: list[float]      # ps
    signals: list[float]     # mOD (milli-optical-density change)


# ── Model ─────────────────────────────────────────────────────

class ScannerModel:
    """Simulated 2D scanning stage + CW detector + pump-probe spectrometer."""

    X_MIN, X_MAX = 0.0, 100.0
    Y_MIN, Y_MAX = 0.0, 100.0

    def __init__(self):
        # ── Stage state ───────────────────────────────────────
        self._x = 50.0
        self._y = 50.0
        self._scanning = False
        self._dwell_time = 0.1   # s

        # ── CW sample: Gaussian reflectance peaks ─────────────
        self._peaks = [
            {"x0": 30, "y0": 40, "amp": 8.0,  "sigma": 8},
            {"x0": 70, "y0": 60, "amp": 5.0,  "sigma": 12},
            {"x0": 50, "y0": 80, "amp": 10.0, "sigma": 6},
            {"x0": 20, "y0": 20, "amp": 3.0,  "sigma": 15},
        ]
        self._scan_data: list[ScanResult] = []

        # ── Pump-probe state ──────────────────────────────────
        self._pump_power  = 10.0   # mW
        self._probe_power =  1.0   # mW
        self._current_delay = 0.0  # ps

        # Fast component: hot-carrier cooling (sub-ps to few ps)
        self._pp_fast = [
            {"x0": 30, "y0": 40, "amp": 10.0, "tau": 0.8,  "sigma": 12},
            {"x0": 70, "y0": 60, "amp":  6.0, "tau": 1.5,  "sigma": 15},
        ]
        # Slow component: band-edge carrier recombination (10s–100s ps)
        self._pp_slow = [
            {"x0": 50, "y0": 80, "amp":  8.0, "tau": 120.0, "sigma": 10},
            {"x0": 20, "y0": 20, "amp":  4.0, "tau": 300.0, "sigma": 18},
        ]
        # IRF half-width (ps) — Gaussian instrument response
        self._irf_sigma = 0.15

        self._pp_traces: list[PPTrace] = []

    # ── Properties ────────────────────────────────────────────

    @property
    def x(self) -> float: return self._x

    @property
    def y(self) -> float: return self._y

    @property
    def dwell_time(self) -> float: return self._dwell_time

    @property
    def is_scanning(self) -> bool: return self._scanning

    @property
    def scan_data(self) -> list[ScanResult]: return list(self._scan_data)

    @property
    def pump_power(self) -> float: return self._pump_power

    @property
    def probe_power(self) -> float: return self._probe_power

    @property
    def current_delay(self) -> float: return self._current_delay

    @property
    def pp_traces(self) -> list[PPTrace]: return list(self._pp_traces)

    # ── Stage motion ──────────────────────────────────────────

    def move_to(self, x: float, y: float) -> tuple[float, float]:
        if self._scanning:
            raise RuntimeError("Cannot move while scanning")
        self._x = float(np.clip(x, self.X_MIN, self.X_MAX))
        self._y = float(np.clip(y, self.Y_MIN, self.Y_MAX))
        return self._x, self._y

    def move_relative(self, dx: float, dy: float) -> tuple[float, float]:
        return self.move_to(self._x + dx, self._y + dy)

    # ── CW detector ───────────────────────────────────────────

    def _sample_value(self, x: float, y: float) -> float:
        signal = sum(
            p["amp"] * np.exp(-((x - p["x0"])**2 + (y - p["y0"])**2) / (2 * p["sigma"]**2))
            for p in self._peaks
        )
        return max(0.0, signal + np.random.normal(0, 0.1))

    def measure(self) -> ScanResult:
        r = ScanResult(x=self._x, y=self._y, value=self._sample_value(self._x, self._y))
        self._scan_data.append(r)
        return r

    def set_dwell_time(self, seconds: float) -> float:
        self._dwell_time = float(np.clip(seconds, 0.01, 10.0))
        return self._dwell_time

    def run_line_scan(self, x_start, y_start, x_end, y_end, num_points) -> list[ScanResult]:
        if self._scanning:
            raise RuntimeError("Scan already in progress")
        self._scanning = True
        results = []
        try:
            for x, y in zip(np.linspace(x_start, x_end, num_points),
                            np.linspace(y_start, y_end, num_points)):
                self.move_to(float(x), float(y))
                self._scanning = True
                results.append(self.measure())
        finally:
            self._scanning = False
        return results

    def run_2d_scan(self, x_start, y_start, x_end, y_end, x_points, y_points) -> list[ScanResult]:
        if self._scanning:
            raise RuntimeError("Scan already in progress")
        self._scanning = True
        results = []
        try:
            for y in np.linspace(y_start, y_end, y_points):
                for x in np.linspace(x_start, x_end, x_points):
                    self._x = float(np.clip(x, self.X_MIN, self.X_MAX))
                    self._y = float(np.clip(y, self.Y_MIN, self.Y_MAX))
                    results.append(self.measure())
        finally:
            self._scanning = False
        return results

    def clear_data(self):
        self._scan_data.clear()

    # ── Pump-probe spectrometer ───────────────────────────────

    def set_pump_power(self, power_mw: float) -> float:
        """Set pump power. Clamped to [0.01, 200] mW."""
        self._pump_power = float(np.clip(power_mw, 0.01, 200.0))
        return self._pump_power

    def set_probe_power(self, power_mw: float) -> float:
        """Set probe power. Clamped to [0.001, 10] mW."""
        self._probe_power = float(np.clip(power_mw, 0.001, 10.0))
        return self._probe_power

    def set_delay(self, delay_ps: float) -> float:
        """Set current time delay (ps). Clamped to [-10, 2000]."""
        self._current_delay = float(np.clip(delay_ps, -10.0, 2000.0))
        return self._current_delay

    def _pp_signal_at(self, delay: float, x: float, y: float) -> float:
        """
        Simulated pump-probe signal (mOD) at position (x,y) and time delay.

        Physics:
          • Bi-exponential decay: fast (hot carriers) + slow (recombination)
          • Signal scales linearly with pump power
          • Noise scales inversely with sqrt(probe power)
          • Gaussian IRF with sigma = self._irf_sigma ps
          • Causality: signal is zero for delay << 0
        """
        pump_factor  = self._pump_power / 10.0          # normalised to 10 mW
        noise_sigma  = 0.05 / np.sqrt(self._probe_power) # better SNR with more probe

        # Causal step convolved with Gaussian IRF
        irf = 0.5 * (1.0 + np.tanh(delay / (self._irf_sigma * 2.0)))

        signal = 0.0
        for p in self._pp_fast:
            spatial = p["amp"] * np.exp(
                -((x - p["x0"])**2 + (y - p["y0"])**2) / (2 * p["sigma"]**2)
            )
            signal += spatial * np.exp(-max(0.0, delay) / p["tau"])

        for p in self._pp_slow:
            spatial = p["amp"] * np.exp(
                -((x - p["x0"])**2 + (y - p["y0"])**2) / (2 * p["sigma"]**2)
            )
            signal += spatial * np.exp(-max(0.0, delay) / p["tau"])

        return signal * irf * pump_factor + np.random.normal(0, noise_sigma)

    def measure_at_delay(self, delay_ps: float) -> float:
        """Measure pump-probe signal at a specific delay at the current position."""
        self._current_delay = delay_ps
        return self._pp_signal_at(delay_ps, self._x, self._y)

    def capture_time_trace(
        self, t_start: float, t_end: float, num_points: int
    ) -> PPTrace:
        """Sweep time delay and record a full transient trace at the current position."""
        if self._scanning:
            raise RuntimeError("Cannot capture trace while scanning")
        delays  = list(np.linspace(t_start, t_end, num_points))
        signals = [self._pp_signal_at(t, self._x, self._y) for t in delays]
        trace = PPTrace(
            x=self._x, y=self._y,
            pump_power=self._pump_power,
            probe_power=self._probe_power,
            delays=delays, signals=signals,
        )
        self._pp_traces.append(trace)
        return trace

    def clear_pp_data(self):
        """Discard all pump-probe traces."""
        self._pp_traces.clear()

    # ── State serialization ───────────────────────────────────

    def get_state(self) -> dict:
        return {
            "position":             {"x": round(self._x, 3), "y": round(self._y, 3)},
            "dwell_time":           self._dwell_time,
            "is_scanning":          self._scanning,
            "num_points_collected": len(self._scan_data),
            "stage_limits":         {"x": [self.X_MIN, self.X_MAX],
                                     "y": [self.Y_MIN, self.Y_MAX]},
            "pump_probe": {
                "pump_power_mw":    self._pump_power,
                "probe_power_mw":   self._probe_power,
                "current_delay_ps": self._current_delay,
                "num_traces":       len(self._pp_traces),
            },
        }
