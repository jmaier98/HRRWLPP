# Keithley2400.py
import pyvisa
import time
from typing import Optional, Tuple

# ---------- Editable defaults (your quick knobs) ----------
GPIB_ADDR_DEFAULT               = 22
TIMEOUT_MS_DEFAULT              = 5000

# Safety / behavior knobs
VOLTAGE_SOFT_LIMIT_V            = 10.0      # ±V soft limit for set/ramp
CURRENT_COMPLIANCE_DEFAULT_A    = 1e-3      # 1 mA default compliance
NPLC_DEFAULT                    = 1.0       # integration time (speed vs noise)

# Ramp behavior
RAMP_STEP_V_DEFAULT             = 0.05      # V per step
RAMP_DWELL_S_DEFAULT            = 0.02      # s per step

class Keithley2400:
    """
    Minimal, low-level driver for the Keithley 2400 SourceMeter via GPIB.
    - Defaults to Source=Voltage, Measure=Current (gating ready).
    - 2-wire / 4-wire switchable via :SYST:RSEN.
    - Front terminals by default via :ROUT:TERM FRON.
    - No timestamps in :READ? data (format is CURR,VOLT).
    """

    def __init__(
        self,
        gpib_addr: int = GPIB_ADDR_DEFAULT,
        timeout_ms: int = TIMEOUT_MS_DEFAULT,
        init_clear: bool = True,
        four_wire: bool = False
    ):
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(f"GPIB::{gpib_addr}::INSTR")
        self._inst.timeout = timeout_ms

        # Store soft limits (you can adjust later with set_soft_limits)
        self._v_soft_limit  = float(VOLTAGE_SOFT_LIMIT_V)
        self._i_compliance  = float(CURRENT_COMPLIANCE_DEFAULT_A)
        self._nplc          = float(NPLC_DEFAULT)

        try:
            # Snapshot current output state so we can restore it
            try:
                pre_out = self._inst.query(":OUTP:STAT?").strip()
                pre_out_on = (pre_out == "1")
            except Exception:
                pre_out_on = False  # fall back

            # Minimal, non-destructive config (no *RST)
            self._inst.write(":ROUT:TERM FRON")
            self._inst.write(":SOUR:FUNC VOLT")
            self._inst.write(":SENS:FUNC 'CURR'")
            self._inst.write(f":SENS:CURR:PROT {self._i_compliance:.6e}")
            self._inst.write(":SENS:CURR:RANG:AUTO ON")
            self._inst.write(f":SENS:CURR:NPLC {self._nplc}")
            self.set_four_wire(four_wire)
            self._inst.write(":FORM:ELEM CURR,VOLT")

            # Restore prior output state
            self._inst.write(f":OUTP {'ON' if pre_out_on else 'OFF'}")

            print(f"Keithley2400 online (Src=V, Meas=I, FRONT, 4W={four_wire}).")
        except Exception as e:
            print(f"Keithley2400 init error: {e}")
            raise

    # ---------------- Basic identity / raw I/O ----------------
    def identify(self) -> str:
        try:
            return self._inst.query("*IDN?").strip()
        except Exception as e:
            print(f"*IDN? error: {e}")
            return ""

    def write(self, scpi: str):
        """Raw SCPI write (for debugging/advanced use)."""
        self._inst.write(scpi)

    def query(self, scpi: str) -> str:
        """Raw SCPI query (for debugging/advanced use)."""
        return self._inst.query(scpi)

    # ---------------- Output and mode control ----------------
    def output_on(self):
        try:
            self._inst.write(":OUTP ON")
        except Exception as e:
            print(f"output_on error: {e}")

    def output_off(self):
        try:
            self._inst.write(":OUTP OFF")
        except Exception as e:
            print(f"output_off error: {e}")

    def set_source_voltage_mode(self):
        """Ensure Source=V, Measure=I (gating / IV-ready)."""
        try:
            self._inst.write(":SOUR:FUNC VOLT")
            self._inst.write(":SENS:FUNC 'CURR'")
            self._inst.write(":FORM:ELEM CURR,VOLT")  # keep CURR,VOLT
        except Exception as e:
            print(f"set_source_voltage_mode error: {e}")

    def set_source_current_mode(self):
        """Flip to Source=I, Measure=V (available for future use)."""
        try:
            self._inst.write(":SOUR:FUNC CURR")
            self._inst.write(":SENS:FUNC 'VOLT'")
            self._inst.write(":FORM:ELEM VOLT,CURR")  # keep VOLT,CURR in this mode
        except Exception as e:
            print(f"set_source_current_mode error: {e}")

    def set_four_wire(self, enable: bool = True):
        """
        4-wire (remote sense) ON/OFF.
        ON  => :SYST:RSEN ON
        OFF => :SYST:RSEN OFF  (2-wire)
        """
        try:
            self._inst.write(f":SYST:RSEN {'ON' if enable else 'OFF'}")
        except Exception as e:
            print(f"set_four_wire error: {e}")

    # ---------------- Soft limits & measurement params ----------------
    def set_soft_limits(
        self,
        voltage_limit_V: Optional[float] = None,
        current_compliance_A: Optional[float] = None
    ):
        """Update driver-level soft limits and push compliance to instrument."""
        if voltage_limit_V is not None:
            self._v_soft_limit = abs(float(voltage_limit_V))
        if current_compliance_A is not None:
            self._i_compliance = float(current_compliance_A)
            try:
                self._inst.write(f":SENS:CURR:PROT {self._i_compliance:.6e}")
            except Exception as e:
                print(f"Set compliance error: {e}")

    def set_current_compliance(self, amps: float):
        """Set current compliance for Source=V / Measure=I mode."""
        self.set_soft_limits(current_compliance_A=amps)

    def set_voltage_compliance_for_source_I(self, volts: float):
        """Compliance limit when you switch to Source=I mode (future IV use)."""
        try:
            self._inst.write(f":SENS:VOLT:PROT {float(volts):.6f}")
        except Exception as e:
            print(f"set_voltage_compliance_for_source_I error: {e}")

    def set_nplc(self, nplc: float):
        """Set integration time (Number of Power Line Cycles)."""
        self._nplc = float(nplc)
        try:
            # Apply to whichever function is active
            # (If measuring current)
            self._inst.write(f":SENS:CURR:NPLC {self._nplc}")
            # (If measuring voltage later)
            self._inst.write(f":SENS:VOLT:NPLC {self._nplc}")
        except Exception as e:
            print(f"set_nplc error: {e}")

    # ---------------- Set/Read primitives (low-level) ----------------
    def set_voltage(self, volts: float):
        """Set the source voltage setpoint (does not toggle output state)."""
        v = float(volts)
        if abs(v) > self._v_soft_limit:
            raise ValueError(f"Requested {v:.6f} V exceeds ±{self._v_soft_limit:.6f} V soft limit.")
        try:
            self._inst.write(f":SOUR:VOLT {v:.6f}")
        except Exception as e:
            print(f"set_voltage error: {e}")

    def read_current(self) -> Optional[float]:
        """
        Trigger a single measurement and return CURRENT in Amps.
        Assumes mode is Source=V / Measure=I; data format CURR,VOLT.
        """
        try:
            resp = self._inst.query(":READ?")
            # Expect "curr,volt"
            parts = resp.strip().split(",")
            return float(parts[0])
        except Exception as e:
            print(f"read_current error: {e}")
            return None

    def read_iv(self) -> Optional[Tuple[float, float]]:
        """
        Return (current [A], voltage [V]) for a single :READ? call.
        Useful during ramps/gating or for building IV sweeps at a higher layer.
        """
        try:
            resp = self._inst.query(":READ?")
            c, v = [float(x) for x in resp.strip().split(",")[:2]]
            return c, v
        except Exception as e:
            print(f"read_iv error: {e}")
            return None

    def read_voltage_measured(self) -> Optional[float]:
        """
        If you want the meter's measured voltage (when Source=V, this should be near the setpoint).
        """
        try:
            # Switch format temporarily to VOLT only, then restore
            self._inst.write(":FORM:ELEM VOLT")
            resp = self._inst.query(":READ?")
            v = float(resp.strip())
            # Restore normal format
            self._inst.write(":FORM:ELEM CURR,VOLT")
            return v
        except Exception as e:
            print(f"read_voltage_measured error: {e}")
            # Try to restore format even on error
            try: self._inst.write(":FORM:ELEM CURR,VOLT")
            except: pass
            return None

    # ---------------- Safe ramp (blocking; call from your worker) ----------------
    def ramp_voltage(
        self,
        target_V: float,
        step_V: Optional[float] = None,
        dwell_s: Optional[float] = None,
        abort_flag: Optional[callable] = None,
        verbose: bool = False
    ):
        """
        Ramp the source voltage to target_V in fixed steps.
        - step_V/dwell_s default to the top-of-file constants.
        - abort_flag(): return True to abort cleanly mid-ramp.
        """
        if step_V is None:
            step_V = RAMP_STEP_V_DEFAULT
        if dwell_s is None:
            dwell_s = RAMP_DWELL_S_DEFAULT

        tV = float(target_V)
        if abs(tV) > self._v_soft_limit:
            raise ValueError(f"Target {tV:.6f} V exceeds ±{self._v_soft_limit:.6f} V soft limit.")

        # Ensure correct mode and output
        self.set_source_voltage_mode()
        self.output_on()

        try:
            v_now = float(self._inst.query(":SOUR:VOLT?").strip())
        except Exception:
            # If query fails, assume we start from 0 V
            v_now = 0.0

        step = abs(step_V) if tV >= v_now else -abs(step_V)

        v = v_now
        while (step > 0 and v < tV) or (step < 0 and v > tV):
            if abort_flag and abort_flag():
                if verbose: print("Ramp aborted by flag.")
                return
            v_next = v + step
            if (step > 0 and v_next > tV) or (step < 0 and v_next < tV):
                v_next = tV

            self.set_voltage(v_next)

            # Optional: take a quick reading to keep an eye on compliance
            try:
                _ = self._inst.query(":READ?")  # CURR,VOLT (ignored here)
            except Exception as e:
                if verbose: print(f"READ during ramp error: {e}")

            time.sleep(dwell_s)
            v = v_next

        if verbose:
            print(f"Reached target {tV:.6f} V.")

    # ---------------- Cleanup ----------------
    def close(self):
        try:
            self._inst.close()
        finally:
            self._rm.close()


if __name__ == "__main__":
    k = Keithley2400(gpib_addr=GPIB_ADDR_DEFAULT, four_wire=False)
    print(k.identify())
    k.set_current_compliance(CURRENT_COMPLIANCE_DEFAULT_A)
    k.set_voltage(0.0)
    k.output_on()
    k.ramp_voltage(1.0, verbose=True)
    iv = k.read_iv()
    print("I,V @ 1V =", iv)
    k.output_off()
    k.close()
