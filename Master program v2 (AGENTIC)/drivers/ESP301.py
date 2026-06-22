import pyvisa
import time

_TMO_ERROR_CODE = -1073807339  # VI_ERROR_TMO
STAGE_LIMIT_MM = 75.0          # hard travel limit ±mm


class ESP301Controller:
    """
    Drop-in replacement for ESP300Controller.
    The ESP301 uses the same Newport motion command set (PA, VA, MD?, TP, etc.)
    but requires axis-level motor-on (MO) instead of the @E0/@1H init sequence.
    """

    def __init__(self, state, gpib_address="GPIB0::1::INSTR", timeout=5000):
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(gpib_address)
        self.inst.timeout = timeout
        self.state = state
        self.initialize()

    def _write(self, cmd, retries=3):
        for attempt in range(retries):
            try:
                self.inst.write(cmd)
                return
            except pyvisa.errors.VisaIOError as e:
                if e.error_code == _TMO_ERROR_CODE and attempt < retries - 1:
                    print(f"VI_ERROR_TMO on write '{cmd}', retrying ({attempt+1}/{retries-1})...")
                    time.sleep(0.5)
                else:
                    raise

    def _read(self, retries=3):
        for attempt in range(retries):
            try:
                return self.inst.read()
            except pyvisa.errors.VisaIOError as e:
                if e.error_code == _TMO_ERROR_CODE and attempt < retries - 1:
                    print(f"VI_ERROR_TMO on read, retrying ({attempt+1}/{retries-1})...")
                    time.sleep(0.5)
                else:
                    raise

    def initialize(self):
        self._write("*CLS")          # clear error/status registers
        self._write("1MO")           # enable motor on axis 1 (ESP301 replaces @E0/@1H)
        time.sleep(0.1)
        print("ESP301 Controller online")

    def set_speed(self, axis, speed):
        """Set the absolute velocity for the axis (mm/sec or deg/sec)."""
        self._write(f"{axis}VA{speed}")
        self.state.settings["delay_stage_speed"] = speed

    def move_absolute(self, axis, position):
        """Start motion to absolute position (non-blocking)."""
        pos = float(position)
        if abs(pos) > STAGE_LIMIT_MM:
            raise ValueError(
                f"Stage position {pos:.3f} mm exceeds ±{STAGE_LIMIT_MM} mm limit."
            )
        self._write(f"{axis}PA{pos}")
        self.state.settings["delay_stage_pos"] = pos
        self.state.settings["delay_ps"] = self.mm_to_ps(pos)

    def is_moving(self, axis):
        """Return True if axis is still moving (MD? returns 0 = in motion, 1 = done).
        A TMO during polling means the controller is busy — treat as still moving."""
        try:
            self._write(f"{axis}MD?")
            resp = self._read()
            return resp.strip() == '0'
        except pyvisa.errors.VisaIOError as e:
            if e.error_code == _TMO_ERROR_CODE:
                return True  # controller busy mid-move; keep polling
            raise

    def get_position(self, axis):
        self._write(f"{axis}TP")
        return float(self._read().strip())

    def move_with_monitoring(self, axis, position, speed, poll_interval=0.01):
        """Move with specified speed and print live position."""
        self.set_speed(axis, speed)
        self.move_absolute(axis, position)

        print(f"Moving axis {axis} to {position} at speed {speed}...")
        while self.is_moving(axis):
            pos = self.get_position(axis)
            print(f"  Position: {pos:.3f}", end='\r')
            time.sleep(poll_interval)

        final_pos = self.get_position(axis)
        print(f"\nArrived at {final_pos:.3f}")

    def move(self, position):
        """Send move command without waiting for completion."""
        self.move_absolute(1, position)

    def move_ps(self, position):
        """Send move command in ps units without waiting for completion."""
        self.move_absolute(1, self.ps_to_mm(position))

    def move_and_wait(self, position):
        self.move_absolute(1, position)
        while self.is_moving(1):
            time.sleep(0.02)

    def move_and_wait_ps(self, position):
        self.move_absolute(1, self.ps_to_mm(position))
        while self.is_moving(1):
            time.sleep(0.02)

    def mm_to_ps(self, mm):
        t0 = self.state.settings["time zero pos"]
        return (t0 - mm) * 6.667

    def ps_to_mm(self, ps):
        t0 = self.state.settings["time zero pos"]
        print(t0, ps)
        return t0 - ps / 6.667

    def close(self):
        self.inst.close()
        self.rm.close()


# --- Example usage ---
if __name__ == "__main__":
    class _FakeState:
        settings = {"time zero pos": 0.0, "delay_stage_speed": 5.0,
                    "delay_stage_pos": 0.0, "delay_ps": 0.0}

    esp = ESP301Controller(_FakeState(), "GPIB0::1::INSTR")
    try:
        esp.move_with_monitoring(axis=1, position=-30.0, speed=5.0)
    finally:
        esp.close()
