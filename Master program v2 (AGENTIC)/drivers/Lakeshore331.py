import pyvisa
from typing import Literal, Tuple, Optional

HeaterRangeName = Literal["off", "low", "medium", "high"]
TempUnitsName = Literal["K", "C", "S", "L"]  # Kelvin, Celsius, Sensor units, Linear


class LakeShore331:
    """
    Minimal driver for the Lake Shore 331 Temperature Controller over GPIB.
    Implements: read temperature, set setpoint (temperature), set heater range, set PID.

    Defaults:
      - GPIB primary address: 14 (as specified)
      - Control loop: 1
      - Input: 'A'
      - Temperature units for reads via KRDG?/CRDG?/SRDG?/LDAT?: 'K'
    """

    _RANGE_MAP_STR2INT = {"off": 0, "low": 1, "medium": 2, "high": 3}
    _RANGE_MAP_INT2STR = {v: k for k, v in _RANGE_MAP_STR2INT.items()}

    def __init__(
        self,
        gpib_addr: int = 13,
        loop: int = 1,
        input_chan: Literal["A", "B"] = "A",
        default_units: TempUnitsName = "K",
        timeout_ms: int = 2000,
    ):
        self.loop = int(loop)
        if self.loop not in (1, 2):
            raise ValueError("loop must be 1 or 2")
        self.input = input_chan.upper()
        if self.input not in ("A", "B"):
            raise ValueError("input_chan must be 'A' or 'B'")
        if default_units not in ("K", "C", "S", "L"):
            raise ValueError("default_units must be one of 'K','C','S','L'")

        self.default_units: TempUnitsName = default_units

        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(f"GPIB::{gpib_addr}::INSTR")

        # The 331 typically uses CRLF by default for IEEE-488; VISA will handle this
        # fine in most cases, but we set explicit terminations for robustness.
        # If your interface expects just '\n', change both to '\n'.
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        self._inst.timeout = timeout_ms

        # Quick sanity ping (won't raise if unit is quiet, so we guard)
        try:
            _idn = self.identify()
            print(f"LakeShore331 online: {_idn}")
        except Exception as e:
            print(f"LakeShore331 connected (IDN query failed: {e})")

        # Optional: ensure we’re using the intended input/units for the control loop.
        # (CSET <loop>, <input>, <units>, <powerup enable>, <current/power>)
        # We leave powerup enable and current/power as-is by reading then writing only if needed.
        try:
            cin, units, pwrup, display = self.get_control_loop_params()
            desired_units = {"K": 1, "C": 2, "S": 3}.get(self.default_units, units)
            if (cin != self.input) or (units != desired_units):
                self._inst.write(f"CSET {self.loop},{self.input},{desired_units}")
        except Exception:
            # Non-fatal if this fails—core functionality still works.
            pass

    # -----------------------
    # Basic utilities
    # -----------------------
    def identify(self) -> str:
        """Return identification string: 'LSCI,MODEL331...,serial,...'"""
        return self._inst.query("QIDN?").strip()

    def close(self):
        try:
            self._inst.close()
        finally:
            self._rm.close()

    # -----------------------
    # Temperature reading
    # -----------------------
    def _read_input_status_ok(self, input_chan: Optional[str] = None) -> bool:
        """
        RDGST? <A|B> returns status bits; 000 means valid reading.
        """
        ch = (input_chan or self.input).upper()
        status = self._inst.query(f"RDGST? {ch}").strip()
        try:
            return int(status) == 0
        except Exception:
            return False
    def get_control_mode(self) -> int:
        """CMODE? <loop> → mode (1=Manual PID, 2=Zone, 3=Open, 4/5/6=AutoTune)"""
        resp = self._inst.query(f"CMODE? {self.loop}").strip()
        return int(resp)

    def engage_closed_loop(self, desired_mode: int = 1):
        """
        Ensure a closed-loop control mode and nonzero heater range.
        desired_mode: 1=Manual PID (safe default), 2=Zone also OK if you use zones.
        """
        mode = self.get_control_mode()
        if mode == 3:
            # Was Open Loop → switch to Manual PID by default
            self.set_control_mode(desired_mode)

        # If range is Off, bump to Low so the loop can actually drive power
        code, _name = self.get_heater_range()
        if code == 0:
            self.set_heater_range(1)  # Low (0.5 W)

    def set_manual_output(self, percent: float):
        """
        Open-loop helper: set manual heater power (0–100 %) after CMODE 3.
        """
        if not (0.0 <= float(percent) <= 100.0):
            raise ValueError("manual output must be 0–100%")
        # Ensure Open Loop mode for MOUT to make sense
        if self.get_control_mode() != 3:
            self.set_control_mode(3)
        self._inst.write(f"MOUT {self.loop},{percent}")

    def read_temperature(self, units: Optional[TempUnitsName] = None, input_chan: Optional[str] = None) -> float:
        """
        Read temperature-like value from the selected input in desired units.
          units:
            'K' -> KRDG? (Kelvin)
            'C' -> CRDG? (Celsius)
            'S' -> SRDG? (Sensor units)
            'L' -> LDAT? (Linear data)
        Returns float; raises on invalid reading.
        """
        ch = (input_chan or self.input).upper()
        u = (units or self.default_units).upper()
        if u not in ("K", "C", "S", "L"):
            raise ValueError("units must be one of 'K','C','S','L'")

        if not self._read_input_status_ok(ch):
            raise RuntimeError(f"Input {ch} reading invalid (RDGST? != 000)")

        cmd = {"K": "KRDG?", "C": "CRDG?", "S": "SRDG?", "L": "LDAT?"}[u]
        resp = self._inst.query(f"{cmd} {ch}").strip()
        return float(resp)

    # -----------------------
    # Setpoint (temperature target)
    # -----------------------
    SETPOINT_MIN_K = 0.0    # hard lower limit (K)
    SETPOINT_MAX_K = 400.0  # hard upper limit (K)

    def set_setpoint(self, value: float):
        """
        SETP <loop>,<value>
        The units used here follow the loop’s configured setpoint units from CSET (defaulted above).
        Raises ValueError if the setpoint is outside [0, 400] K.
        """
        v = float(value)
        if not (self.SETPOINT_MIN_K <= v <= self.SETPOINT_MAX_K):
            raise ValueError(
                f"Setpoint {v} K is outside the allowed range "
                f"[{self.SETPOINT_MIN_K}, {self.SETPOINT_MAX_K}] K."
            )
        self._inst.write(f"SETP {self.loop},{v}")

    def get_setpoint(self) -> float:
        """SETP? <loop>"""
        resp = self._inst.query(f"SETP? {self.loop}").strip()
        return float(resp)

    # -----------------------
    # Heater range
    # -----------------------
    def set_heater_range(self, range_value: HeaterRangeName | int):
        """
        RANGE <n> where n ∈ {0,1,2,3} = off, low (0.5W), medium (5W), high (50W).
        Accepts either string ('off'/'low'/'medium'/'high') or the integer code.
        """
        if isinstance(range_value, str):
            key = range_value.lower()
            if key not in self._RANGE_MAP_STR2INT:
                raise ValueError("heater range must be one of: 'off','low','medium','high'")
            code = self._RANGE_MAP_STR2INT[key]
        else:
            code = int(range_value)
            if code not in (0, 1, 2, 3):
                raise ValueError("heater range integer must be 0..3")

        self._inst.write(f"CSET 1, A, 0, 0, 10, 30, 0, {code}, 0")
        print(f"Set heater range to {self._RANGE_MAP_INT2STR[code]} ({code})")
        self.get_heater_range()  # confirm


    def get_heater_range(self) -> Tuple[int, HeaterRangeName]:
        """RANGE? → (code, name)"""
        resp = self._inst.query("RANGE?").strip()
        code = int(resp)
        print(f"Heater range is {self._RANGE_MAP_INT2STR.get(code, 'unknown')} ({code})")
        return code, self._RANGE_MAP_INT2STR.get(code, "unknown")

    def read_heater_output_percent(self) -> float:
        """HTR? → returns Loop 1 heater output (%) (for Loop 2, see AOUT?)"""
        resp = self._inst.query("HTR?").strip()
        return float(resp)

    # -----------------------
    # PID controls
    # -----------------------
    def set_pid(self, p: float, i: float, d: float = 0.0):
        """
        PID <loop>,<P>,<I>,<D>
        P: 0.1–1000, I: 0.1–1000, D: 0–200
        """
        if not (0.1 <= float(p) <= 1000):
            raise ValueError("P out of range (0.1–1000)")
        if not (0.1 <= float(i) <= 1000):
            raise ValueError("I out of range (0.1–1000)")
        if not (0.0 <= float(d) <= 200):
            raise ValueError("D out of range (0–200)")
        self._inst.write(f"PID {self.loop},{p},{i},{d}")

    def get_pid(self) -> Tuple[float, float, float]:
        """PID? <loop> → P,I,D"""
        resp = self._inst.query(f"PID? {self.loop}").strip()
        p_str, i_str, d_str = resp.split(",")
        return float(p_str), float(i_str), float(d_str)

    # -----------------------
    # Optional helpers (nice to have)
    # -----------------------
    def get_control_loop_params(self) -> Tuple[str, int, int, int]:
        """
        CSET? <loop> → <input>, <units>, <powerup enable>, <current/power>
        units: 1=K, 2=C, 3=sensor units
        """
        resp = self._inst.query(f"CSET? {self.loop}").strip()
        cin, units, pwrup, display = resp.split(",")
        return cin, int(units), int(pwrup), int(display)

    def set_control_mode(self, mode: int):
        """
        CMODE <loop>,<mode>
        1=Manual PID, 2=Zone, 3=Open, 4=AutoTune PID, 5=AutoTune PI, 6=AutoTune P
        """
        if mode not in (1, 2, 3, 4, 5, 6):
            raise ValueError("mode must be 1..6")
        self._inst.write(f"CMODE {self.loop},{mode}")

    def set_ramp(self, on: bool, rate_k_per_min: float = 1.0):
        """
        RAMP <loop>, <off/on>, <rate>  ; rate 0.1–100 K/min
        """
        if on and not (0.1 <= rate_k_per_min <= 100.0):
            raise ValueError("ramp rate must be 0.1–100 K/min")
        self._inst.write(f"RAMP {self.loop},{1 if on else 0},{rate_k_per_min}")

    def get_ramp(self) -> Tuple[bool, float]:
        """RAMP? <loop> → <off/on>,<rate>"""
        resp = self._inst.query(f"RAMP? {self.loop}").strip()
        on_str, rate_str = resp.split(",")
        return (on_str.strip() == "1"), float(rate_str)

    # Quick one-call convenience methods that mirror your ask:
    def read_temp(self) -> float:
        """Convenience: read temperature in default units (default 'K')."""
        return self.read_temperature()

    def set_temp(self, value: float):
        """Convenience: set the control setpoint (in the loop’s configured units)."""
        self.set_setpoint(value)

    def set_heater(self, range_value: HeaterRangeName | int):
        """Convenience: alias for set_heater_range."""
        self.set_heater_range(range_value)

    def set_pid_vals(self, p: float, i: float, d: float = 0.0):
        """Convenience: alias for set_pid."""
        self.set_pid(p, i, d)


if __name__ == "__main__":
    # Simple smoke test (will run queries/writes if a 331 is present on GPIB::14)
    ls = LakeShore331(gpib_addr=13, loop=1, input_chan="A", default_units="K", timeout_ms=2000)

    try:
        print("IDN:", ls.identify())
        print("Current temp (K):", ls.read_temp())
        print("Current setpoint:", ls.get_setpoint())
        print("Current PID:", ls.get_pid())
        print("Heater range:", ls.get_heater_range())

        # Example: set a new setpoint and PID (comment out if you don't want to change anything)
        # ls.set_temp(300.0)
        # ls.set_pid_vals(10.0, 50.0, 0.0)
        # ls.set_heater("medium")

    finally:
        ls.close()
