# Keithley2400.py
import pyvisa
import threading
import time
from typing import Optional, Tuple


class _LockedResource:
    """Serialises access to a VISA resource across threads.

    The GUI gate-ramp thread, scan workers and the agent can all reach the
    same meter; without this a write from one can land between another's
    query and its read, and the replies come back mismatched.
    """
    def __init__(self, inst):
        self._inst = inst
        self._lock = threading.RLock()

    def write(self, cmd):
        with self._lock:
            return self._inst.write(cmd)

    def query(self, cmd):
        with self._lock:
            return self._inst.query(cmd)

    def clear(self):
        with self._lock:
            return self._inst.clear()

    def __getattr__(self, name):
        return getattr(self._inst, name)

    def __setattr__(self, name, value):
        if name in ("_inst", "_lock"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inst, name, value)

# ---------- Editable defaults (your quick knobs) ----------
GPIB_ADDR_DEFAULT               = 22
TIMEOUT_MS_DEFAULT              = 5000

# Safety / behavior knobs
VOLTAGE_HARD_LIMIT_V            = 100.0     # ±V absolute hard limit (never exceeded)
VOLTAGE_SOFT_LIMIT_V            = 100.0     # ±V default soft limit for set/ramp
CURRENT_COMPLIANCE_DEFAULT_A    = 1e-3      # 1 mA default compliance
NPLC_DEFAULT                    = 1.0       # integration time (speed vs noise)
SOURCE_V_RANGE_DEFAULT          = 200.0     # V, fixed source range (no mid-ramp range switch)

# Ramp behavior - used when the shared microscope state has no override.
# The live values live in state.settings as "keithley_ramp_step_v" and
# "keithley_ramp_dwell_s", so they can be edited from the GUI/agent.
RAMP_STEP_V_DEFAULT             = 0.05      # V per step
RAMP_DWELL_S_DEFAULT            = 0.02      # s between steps

STATE_KEY_STEP_V                = "keithley_ramp_step_v"
STATE_KEY_DWELL_S               = "keithley_ramp_dwell_s"

# Fast (measurement-free) ramp — used to walk a gate back down in a hurry
RAMP_DOWN_RATE_V_PER_S_DEFAULT  = 7.0       # V/s  (≈1.4 s from 10 V)
RAMP_UPDATE_INTERVAL_S          = 0.005     # s between setpoint updates

class Keithley2400:
    """
    Minimal, low-level driver for the Keithley 2400 SourceMeter via GPIB.
    - Defaults to Source=Voltage, Measure=Current (gating ready).
    - 2-wire / 4-wire switchable via :SYST:RSEN.
    - Front terminals by default via :ROUT:TERM FRON.
    - No timestamps in :READ? data (elements are VOLT,CURR).

    Note on :READ? field order — the 2400 returns the selected elements in a
    FIXED order (VOLT, CURR, RES, TIME, STAT) no matter what order they are
    listed in :FORM:ELEM. Writing ":FORM:ELEM CURR,VOLT" therefore still yields
    "volt,curr", so field 0 is the voltage. Rather than trust that, the driver
    asks the meter which elements it will return (_refresh_element_map) and
    indexes the reply by name.
    """

    # The instrument's fixed output ordering for :READ? fields.
    _ELEMENT_ORDER = ("VOLT", "CURR", "RES", "TIME", "STAT")

    def __init__(
        self,
        state=None,
        gpib_addr: int = GPIB_ADDR_DEFAULT,
        timeout_ms: int = TIMEOUT_MS_DEFAULT,
        init_clear: bool = True,
        four_wire: bool = False
    ):
        # Shared MicroscopeState - supplies the live ramp step/dwell so that
        # every voltage change in the program is rate-limited the same way.
        self.state = state
        self._rm = pyvisa.ResourceManager()
        self._inst = _LockedResource(self._rm.open_resource(f"GPIB::{gpib_addr}::INSTR"))
        self._inst.timeout = timeout_ms

        # Store limits — soft limit is user-adjustable; hard limit is absolute
        self._v_hard_limit  = float(VOLTAGE_HARD_LIMIT_V)
        self._v_soft_limit  = float(VOLTAGE_SOFT_LIMIT_V)
        self._i_compliance  = float(CURRENT_COMPLIANCE_DEFAULT_A)
        self._nplc          = float(NPLC_DEFAULT)

        # Which :READ? field holds what. Replaced with the meter's own answer
        # in _refresh_element_map(); these are the defaults if that query fails.
        self._elem_order = ["VOLT", "CURR"]
        self._v_idx      = 0
        self._i_idx      = 1

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
            self._set_source_v_range()
            self._inst.write(":SENS:FUNC 'CURR'")
            self._inst.write(f":SENS:CURR:PROT {self._i_compliance:.6e}")
            self._inst.write(":SENS:CURR:RANG:AUTO ON")
            self._inst.write(f":SENS:CURR:NPLC {self._nplc}")
            self.set_four_wire(four_wire)
            self._inst.write(":FORM:ELEM VOLT,CURR")
            self._refresh_element_map()

            # Restore prior output state
            self._inst.write(f":OUTP {'ON' if pre_out_on else 'OFF'}")

            print(f"Keithley2400 online (Src=V {SOURCE_V_RANGE_DEFAULT:g} V range, "
                  f"Meas=I, FRONT, 4W={four_wire}, "
                  f":READ? = {','.join(self._elem_order)}).")
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

    def _set_source_v_range(self, volts: float = SOURCE_V_RANGE_DEFAULT):
        """Fix the voltage-source range (autorange off).

        Locked to 200 V by default so a ramp past ±21 V never makes the
        meter switch ranges partway through. On the 200 V range the 2400
        caps current compliance at 105 mA.
        """
        self._inst.write(":SOUR:VOLT:RANG:AUTO OFF")
        self._inst.write(f":SOUR:VOLT:RANG {float(volts):g}")

    def set_source_voltage_mode(self):
        """Ensure Source=V, Measure=I (gating / IV-ready)."""
        try:
            self._inst.write(":SOUR:FUNC VOLT")
            self._set_source_v_range()
            self._inst.write(":SENS:FUNC 'CURR'")
            self._inst.write(":FORM:ELEM VOLT,CURR")
            self._refresh_element_map()
        except Exception as e:
            print(f"set_source_voltage_mode error: {e}")

    def set_source_current_mode(self):
        """Flip to Source=I, Measure=V (available for future use)."""
        try:
            self._inst.write(":SOUR:FUNC CURR")
            self._inst.write(":SENS:FUNC 'VOLT'")
            self._inst.write(":FORM:ELEM VOLT,CURR")
            self._refresh_element_map()
        except Exception as e:
            print(f"set_source_current_mode error: {e}")

    def is_output_on(self) -> Optional[bool]:
        """True/False if the output state could be read, None on error."""
        try:
            return self._inst.query(":OUTP:STAT?").strip() in ("1", "ON")
        except Exception as e:
            print(f"is_output_on error: {e}")
            return None

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
            requested = abs(float(voltage_limit_V))
            if requested > self._v_hard_limit:
                raise ValueError(
                    f"Requested soft limit ±{requested} V exceeds hard limit ±{self._v_hard_limit} V."
                )
            self._v_soft_limit = requested
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

    def get_current_compliance(self) -> float:
        """Compliance last pushed to the instrument, in Amps (no query)."""
        return self._i_compliance

    def get_voltage_limits(self) -> Tuple[float, float]:
        """(soft, hard) ±voltage limits in V."""
        return self._v_soft_limit, self._v_hard_limit

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

    # ---------------- Ramp parameters (from shared state) ----------------
    def get_ramp_step_v(self) -> float:
        """Voltage increment per ramp step, in Volts."""
        try:
            step = abs(float(self.state.settings[STATE_KEY_STEP_V]))
        except Exception:
            return RAMP_STEP_V_DEFAULT
        # A zero/absent step would make the ramp loop never advance.
        return step if step > 0 else RAMP_STEP_V_DEFAULT

    def get_ramp_dwell_s(self) -> float:
        """Wait between ramp steps, in seconds."""
        try:
            return max(0.0, float(self.state.settings[STATE_KEY_DWELL_S]))
        except Exception:
            return RAMP_DWELL_S_DEFAULT

    # ---------------- Set/Read primitives (low-level) ----------------
    def set_voltage_immediate(self, volts: float):
        """Write the source-voltage setpoint in one jump - no ramp.

        Only for callers that are already stepping the voltage themselves:
        the ramp loops below, and the gate-breakdown sweep, which walks the
        gate up in its own controlled steps while measuring at each one.
        Everything else should use set_voltage(), which ramps.
        """
        v = float(volts)
        if abs(v) > self._v_hard_limit:
            raise ValueError(f"Requested {v:.6f} V exceeds ±{self._v_hard_limit:.6f} V hard limit.")
        if abs(v) > self._v_soft_limit:
            raise ValueError(f"Requested {v:.6f} V exceeds ±{self._v_soft_limit:.6f} V soft limit.")
        try:
            self._inst.write(f":SOUR:VOLT {v:.6f}")
        except Exception as e:
            # Re-raise so a ramp stops at the first failed write instead of
            # pushing every remaining step into a meter that isn't listening.
            print(f"set_voltage error: {e}")
            raise

    def set_voltage(self, volts: float, abort_flag: Optional[callable] = None):
        """Ramp the source voltage from where it sits now to `volts`.

        Steps by the state's keithley_ramp_step_v, waiting
        keithley_ramp_dwell_s between steps, so no caller can slam a gate
        with a step change. Deliberately does not touch the output state or
        the source function - it stays a drop-in for the old direct write.
        """
        target = float(volts)
        if abs(target) > self._v_hard_limit:
            raise ValueError(f"Requested {target:.6f} V exceeds ±{self._v_hard_limit:.6f} V hard limit.")
        if abs(target) > self._v_soft_limit:
            raise ValueError(f"Requested {target:.6f} V exceeds ±{self._v_soft_limit:.6f} V soft limit.")
        self._ramp_setpoints(target, self.get_ramp_step_v(),
                             self.get_ramp_dwell_s(), abort_flag)

    def _ramp_setpoints(self, target_V: float, step_V: float, dwell_s: float,
                        abort_flag: Optional[callable] = None,
                        read_each_step: bool = False,
                        verbose: bool = False):
        """Walk the setpoint from the present voltage to target_V.

        Shared by set_voltage() and ramp_voltage(); assumes the caller has
        already checked the limits.
        """
        tV   = float(target_V)
        step = abs(float(step_V)) or RAMP_STEP_V_DEFAULT

        v = self.get_voltage_setpoint()
        if v is None:
            # Unknown starting point: refuse rather than jump straight to the
            # target - a one-write step to tens of volts can kill a gate.
            raise RuntimeError("Could not read present voltage; refusing to "
                               f"jump to {tV:g} V without a ramp.")

        if tV < v:
            step = -step

        while (step > 0 and v < tV) or (step < 0 and v > tV):
            if abort_flag and abort_flag():
                if verbose:
                    print("Ramp aborted by flag.")
                return
            v_next = v + step
            if (step > 0 and v_next > tV) or (step < 0 and v_next < tV):
                v_next = tV

            try:
                self.set_voltage_immediate(v_next)
            except Exception as e:
                raise RuntimeError(f"Ramp to {tV:g} V failed stepping "
                                   f"{v:g} -> {v_next:g} V: {e}") from e

            if read_each_step:
                # Keeps the meter live and lets compliance be watched.
                try:
                    self._inst.query(":READ?")
                except Exception as e:
                    if verbose:
                        print(f"READ during ramp error: {e}")

            if dwell_s > 0:
                time.sleep(dwell_s)
            v = v_next

        # Land exactly on the target even when the step divides unevenly.
        self.set_voltage_immediate(tV)
        if verbose:
            print(f"Reached target {tV:.6f} V.")

    def _refresh_element_map(self, elements: str = "VOLT,CURR"):
        """Rebuild the :READ? field map for the elements just written to
        :FORM:ELEM.

        Must be called after anything that changes :FORM:ELEM, otherwise
        read_current()/read_iv() would pick the wrong field. The map is
        derived from the instrument's fixed output order rather than by
        querying :FORM:ELEM? - that query's reply could be left in the output
        buffer and then read back by the next :SOUR:VOLT? as "VOLT,CURR".
        """
        names = {tok.strip().upper()[:4] for tok in elements.split(",") if tok.strip()}
        order = [e for e in self._ELEMENT_ORDER if e in names]

        if not order:
            order = ["VOLT", "CURR"]
        self._elem_order = order
        self._v_idx = order.index("VOLT") if "VOLT" in order else None
        self._i_idx = order.index("CURR") if "CURR" in order else None

    def _read_fields(self) -> Optional[list]:
        """Trigger one measurement and return its :READ? fields as floats."""
        try:
            resp = self._inst.query(":READ?")
            return [float(x) for x in resp.strip().split(",")]
        except Exception as e:
            print(f"READ? error: {e}")
            return None

    def get_voltage_setpoint(self) -> Optional[float]:
        """Read back the source-voltage setpoint in Volts (None if unreadable).

        Needed to ramp *from* wherever the gate currently sits rather than
        assuming 0 V — the output can already be biased when a sweep starts.
        """
        try:
            return float(self._inst.query(":SOUR:VOLT?").strip())
        except ValueError as e:
            # A non-numeric reply means a stale response was sitting in the
            # output buffer. Clear it and ask once more.
            print(f"get_voltage_setpoint: stale reply ({e}); resyncing.")
            try:
                self._inst.clear()
                return float(self._inst.query(":SOUR:VOLT?").strip())
            except Exception as e2:
                print(f"get_voltage_setpoint error: {e2}")
                return None
        except Exception as e:
            print(f"get_voltage_setpoint error: {e}")
            return None

    def in_compliance(self) -> Optional[bool]:
        """True when the source has hit the current-compliance limit.

        For gate testing this is the hardware's own breakdown flag: once the
        gate leaks more than the compliance setting, the meter clamps the
        current and trips this bit.
        """
        try:
            return self._inst.query(":SENS:CURR:PROT:TRIP?").strip() == "1"
        except Exception as e:
            print(f"in_compliance error: {e}")
            return None

    def read_current(self) -> Optional[float]:
        """
        Trigger a single measurement and return the measured CURRENT in Amps.
        Assumes mode is Source=V / Measure=I.
        """
        fields = self._read_fields()
        if fields is None:
            return None
        if self._i_idx is None or self._i_idx >= len(fields):
            print(f"read_current error: no CURR field in :READ? "
                  f"({','.join(self._elem_order)})")
            return None
        return fields[self._i_idx]

    def read_iv(self) -> Optional[Tuple[float, float]]:
        """
        Return (current [A], voltage [V]) for a single :READ? call.
        Useful during ramps/gating or for building IV sweeps at a higher layer.
        """
        fields = self._read_fields()
        if fields is None:
            return None
        try:
            return fields[self._i_idx], fields[self._v_idx]
        except (TypeError, IndexError):
            print(f"read_iv error: :READ? returned {len(fields)} field(s) as "
                  f"{','.join(self._elem_order)}; need both VOLT and CURR.")
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
            return v
        except Exception as e:
            print(f"read_voltage_measured error: {e}")
            return None
        finally:
            # Restore the normal format (and the field map) either way.
            try:
                self._inst.write(":FORM:ELEM VOLT,CURR")
                self._refresh_element_map()
            except Exception:
                pass

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
            step_V = self.get_ramp_step_v()
        if dwell_s is None:
            dwell_s = self.get_ramp_dwell_s()

        tV = float(target_V)
        if abs(tV) > self._v_soft_limit:
            raise ValueError(f"Target {tV:.6f} V exceeds ±{self._v_soft_limit:.6f} V soft limit.")

        # Ensure correct mode and output
        self.set_source_voltage_mode()
        self.output_on()

        self._ramp_setpoints(tV, step_V, dwell_s, abort_flag,
                             read_each_step=True, verbose=verbose)

    def ramp_voltage_fast(
        self,
        target_V: float,
        rate_V_per_s: Optional[float] = None,
        update_interval_s: float = RAMP_UPDATE_INTERVAL_S,
    ) -> float:
        """Ramp to target_V at a fixed rate (V/s) without measuring. Returns
        the elapsed time in seconds.

        ramp_voltage() takes a reading at every step (~1 NPLC each, plus the
        dwell), which makes it far too slow to walk a gate down after a
        breakdown — 10 V at 0.01 V steps is ~20 s. This one only writes
        setpoints, and paces them against the wall clock so the ramp takes
        |ΔV| / rate seconds regardless of how slow the GPIB writes are.

        Deliberately has no abort flag: it is the path that makes the device
        safe, so nothing should be able to interrupt it partway.
        """
        tV = float(target_V)
        if abs(tV) > self._v_soft_limit:
            raise ValueError(f"Target {tV:.6f} V exceeds ±{self._v_soft_limit:.6f} V soft limit.")

        rate = float(rate_V_per_s if rate_V_per_s is not None
                     else RAMP_DOWN_RATE_V_PER_S_DEFAULT)
        if rate <= 0:
            # The direction comes from target vs. present voltage, so a
            # non-positive rate is a caller mistake, not a downward ramp.
            raise ValueError(f"Ramp rate must be > 0 V/s (got {rate}).")

        v0 = self.get_voltage_setpoint()
        if v0 is None:
            v0 = 0.0
        span = tV - v0
        if abs(span) < 1e-9:
            self.set_voltage_immediate(tV)
            return 0.0

        duration = abs(span) / rate
        t0 = time.perf_counter()
        while True:
            frac = (time.perf_counter() - t0) / duration
            if frac >= 1.0:
                break
            self.set_voltage_immediate(v0 + span * frac)
            time.sleep(update_interval_s)

        self.set_voltage_immediate(tV)   # land exactly on the target
        return time.perf_counter() - t0

    # ---------------- Cleanup ----------------
    def close(self):
        try:
            self._inst.close()
        finally:
            self._rm.close()


if __name__ == "__main__":
    k = Keithley2400(state=None, gpib_addr=GPIB_ADDR_DEFAULT, four_wire=False)
    print(k.identify())
    k.set_current_compliance(CURRENT_COMPLIANCE_DEFAULT_A)
    k.set_voltage_immediate(0.0)
    k.output_on()
    k.ramp_voltage(1.0, verbose=True)
    iv = k.read_iv()
    print("I,V @ 1V =", iv)
    k.output_off()
    k.close()
