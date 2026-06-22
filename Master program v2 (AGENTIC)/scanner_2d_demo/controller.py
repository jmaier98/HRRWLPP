"""
Scanner Controller — the single API surface for both GUI and agent.

Every action goes through here. The GUI calls these methods;
the agent calls these methods via tool definitions.
"""

from model import ScannerModel, ScanResult, PPTrace


class ScannerController:
    """Thin controller that wraps the model and adds logging/validation."""

    def __init__(self, model: ScannerModel | None = None):
        self.model = model or ScannerModel()
        self._log: list[str] = []

    def _record(self, msg: str):
        self._log.append(msg)

    # ── Stage control ─────────────────────────────────────────

    def move_to(self, x: float, y: float) -> dict:
        actual = self.model.move_to(x, y)
        self._record(f"Moved to ({actual[0]:.3f}, {actual[1]:.3f})")
        return {"x": actual[0], "y": actual[1]}

    def move_relative(self, dx: float, dy: float) -> dict:
        actual = self.model.move_relative(dx, dy)
        self._record(f"Moved relative ({dx}, {dy}) → ({actual[0]:.3f}, {actual[1]:.3f})")
        return {"x": actual[0], "y": actual[1]}

    # ── CW measurements ───────────────────────────────────────

    def measure(self) -> dict:
        r = self.model.measure()
        self._record(f"Measured at ({r.x:.3f}, {r.y:.3f}): {r.value:.4f}")
        return {"x": r.x, "y": r.y, "value": round(r.value, 4)}

    def set_dwell_time(self, seconds: float) -> dict:
        actual = self.model.set_dwell_time(seconds)
        self._record(f"Dwell time set to {actual:.3f}s")
        return {"dwell_time": actual}

    def run_line_scan(self, x_start, y_start, x_end, y_end, num_points) -> dict:
        results = self.model.run_line_scan(x_start, y_start, x_end, y_end, num_points)
        self._record(f"Line scan: ({x_start},{y_start})→({x_end},{y_end}), {num_points} pts")
        return {
            "num_points": len(results),
            "data": [{"x": r.x, "y": r.y, "value": round(r.value, 4)} for r in results],
        }

    def run_2d_scan(self, x_start, y_start, x_end, y_end, x_points, y_points) -> dict:
        results = self.model.run_2d_scan(x_start, y_start, x_end, y_end, x_points, y_points)
        self._record(
            f"2D scan: ({x_start},{y_start})→({x_end},{y_end}), "
            f"{x_points}×{y_points} = {len(results)} pts"
        )
        return {
            "num_points": len(results),
            "x_points": x_points,
            "y_points": y_points,
            "data": [{"x": r.x, "y": r.y, "value": round(r.value, 4)} for r in results],
        }

    def clear_data(self) -> dict:
        self.model.clear_data()
        self._record("Scan data cleared")
        return {"status": "cleared"}

    def get_data(self) -> dict:
        data = self.model.scan_data
        return {
            "num_points": len(data),
            "data": [{"x": r.x, "y": r.y, "value": round(r.value, 4)} for r in data],
        }

    # ── Pump-probe spectrometer ───────────────────────────────

    def set_pump_power(self, power_mw: float) -> dict:
        """Set pump beam power (mW)."""
        actual = self.model.set_pump_power(power_mw)
        self._record(f"Pump power set to {actual:.2f} mW")
        return {"pump_power_mw": actual}

    def set_probe_power(self, power_mw: float) -> dict:
        """Set probe beam power (mW)."""
        actual = self.model.set_probe_power(power_mw)
        self._record(f"Probe power set to {actual:.3f} mW")
        return {"probe_power_mw": actual}

    def measure_at_delay(self, delay_ps: float) -> dict:
        """Single pump-probe measurement at a specific time delay."""
        sig = self.model.measure_at_delay(delay_ps)
        self._record(f"PP single: delay={delay_ps:.2f} ps, signal={sig:.4f} mOD")
        return {
            "x": round(self.model.x, 3),
            "y": round(self.model.y, 3),
            "delay_ps": delay_ps,
            "signal_mOD": round(sig, 4),
        }

    def capture_time_trace(self, t_start: float, t_end: float, num_points: int) -> dict:
        """Capture a full time trace at the current stage position."""
        trace = self.model.capture_time_trace(t_start, t_end, num_points)
        self._record(
            f"PP trace at ({trace.x:.1f},{trace.y:.1f}) mm: "
            f"{t_start}→{t_end} ps, {num_points} pts"
        )
        return {
            "x": trace.x, "y": trace.y,
            "pump_power_mw": trace.pump_power,
            "probe_power_mw": trace.probe_power,
            "num_points": num_points,
            "delays":  [round(d, 4) for d in trace.delays],
            "signals": [round(s, 4) for s in trace.signals],
        }

    def get_pp_traces(self) -> dict:
        """Return all captured pump-probe traces."""
        traces = self.model.pp_traces
        return {
            "num_traces": len(traces),
            "traces": [
                {
                    "x": t.x, "y": t.y,
                    "pump_power_mw": t.pump_power,
                    "probe_power_mw": t.probe_power,
                    "num_points": len(t.delays),
                    "delays":  [round(d, 4) for d in t.delays],
                    "signals": [round(s, 4) for s in t.signals],
                }
                for t in traces
            ],
        }

    def clear_pp_data(self) -> dict:
        """Clear all pump-probe traces."""
        self.model.clear_pp_data()
        self._record("PP data cleared")
        return {"status": "cleared"}

    # ── Observation ───────────────────────────────────────────

    def get_state(self) -> dict:
        return self.model.get_state()

    def get_log(self, last_n: int = 20) -> list[str]:
        return self._log[-last_n:]
