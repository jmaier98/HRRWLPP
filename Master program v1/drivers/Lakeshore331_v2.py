# --- add/modify in your LakeShore331 driver ---

import pyvisa

class LakeShore331:
    def __init__(self, gpib_addr: int = 13, timeout: int = 2000):
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(f"GPIB::{gpib_addr}::INSTR")
        self._inst.timeout = timeout
        self._last_range = 0  # remember last non-zero range for re-enable

    # --- low level helpers ---
    def write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    # --- mode & status ---
    def set_remote(self, lockout: bool = False):
        """Place instrument in remote (optional local lockout)."""
        self.write(f"MODE {2 if lockout else 1}")  # 0=local, 1=remote, 2=remote+lockout
        # MODE is optional with GPIB, but harmless and explicit.  :contentReference[oaicite:3]{index=3}

    def get_heater_output_percent(self) -> float:
        """Return Loop 1 heater output in %."""
        return float(self.query("HTR?"))  # :contentReference[oaicite:4]{index=4}

    def get_heater_error(self) -> int:
        """0=no error, 1=open load, 2=short."""
        return int(self.query("HTRST?"))  # :contentReference[oaicite:5]{index=5}

    # --- control configuration ---
    def set_control_mode(self, mode: int = 1, loop: int = 1):
        """
        1=Manual PID (closed loop), 2=Zone, 3=Open Loop, 4/5/6=AutoTune variants.
        """
        self.write(f"CMODE {loop},{mode}")  # :contentReference[oaicite:6]{index=6}

    def set_setpoint(self, value: float, loop: int = 1):
        self.write(f"SETP {loop},{value}")

    def set_pid(self, p: float, i: float, d: float, loop: int = 1):
        self.write(f"PID {loop},{p},{i},{d}")

    def set_heater_range(self, rng: int):
        """
        rng: 0=Off, 1=Low (0.5 W), 2=Med (5 W), 3=High (50 W)
        """
        self.write(f"RANGE {rng}")         # heater ON/OFF is controlled by RANGE. :contentReference[oaicite:7]{index=7}
        if rng > 0:
            self._last_range = rng

    # --- one-button enable/disable that "does the right thing" ---
    def enable_heater(self, enable: bool, *, closed_loop: bool = True, loop: int = 1):
        """
        If enabling: put the loop in a driving mode and apply last known non-zero range (or Low).
        If disabling: set RANGE 0.
        """
        if not enable:
            self.set_heater_range(0)  # OFF
            return

        # Make sure we're in a mode that can drive the output
        if closed_loop:
            self.set_control_mode(1, loop=loop)  # Manual PID (closed-loop) :contentReference[oaicite:8]{index=8}
        else:
            self.set_control_mode(3, loop=loop)  # Open loop; user must set MOUT separately

        # IMPORTANT: Apply RANGE last (re-enables heater if something auto-disabled it)
        self.set_heater_range(self._last_range or 1)

    # --- open-loop helper (optional) ---
    def set_manual_output_percent(self, percent: float, loop: int = 1):
        """
        Use only in open-loop mode (CMODE=3). percent in 0..100 (or negative if bipolar on Loop 2).
        """
        self.write(f"MOUT {loop},{percent}")

    # --- calls that can auto-disable the heater should re-apply RANGE afterwards ---
    def set_control_input_units(self, loop: int = 1, input_="A", units: int = 1, powerup_enable: int = 1, out_disp: int = 1):
        """
        CSET <loop>, <input A/B>, <units 1=K 2=C 3=sensor>, <powerup 0/1>, <current/power 1/2>
        Changing control input/curve can auto-shutoff the heater on the 331; re-apply RANGE. :contentReference[oaicite:9]{index=9}
        """
        self.write(f"CSET {loop},{input_},{units},{powerup_enable},{out_disp}")
        if self._last_range:
            self.write(f"RANGE {self._last_range}")  # re-enable if it got turned off

    def close(self):
        self._inst.close()
        self._rm.close()
