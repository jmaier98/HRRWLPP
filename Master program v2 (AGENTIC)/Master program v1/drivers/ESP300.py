import pyvisa
import time

_TMO_ERROR_CODE = -1073807339  # VI_ERROR_TMO

class ESP300Controller:
    def __init__(self, state, gpib_address="GPIB0::1::INSTR", timeout=2000):
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(gpib_address)
        self.inst.timeout = timeout
        self.initialize()
        self.state = state

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
        self._write("*CLS")
        self._write("@E0")
        self._write("@1H")
        print("ESP300 Controller online")

    def set_speed(self, axis, speed):
        """Set the absolute velocity for the axis (mm/sec or deg/sec depending on stage)"""
        self._write(f"{axis}VA{speed}")
        self.state.settings["delay_stage_speed"] = speed

    def move_absolute(self, axis, position):
        """Start motion to absolute position (non-blocking)"""
        if abs(position) < 75:
            self._write(f"{axis}PA{position}")
            self.state.settings["delay_stage_pos"] = position
            self.state.settings["delay_ps"] = self.mm_to_ps(position)
        else:
            print("out of range for stage, refusing to move")

    def is_moving(self, axis):
        """Check if the axis is still moving (MD = motion done)"""
        self._write(f"{axis}MD?")
        resp = self._read()
        return resp.strip() == '0'  # 0 = still moving, 1 = done

    def get_position(self, axis):
        self._write(f"{axis}TP")
        return float(self._read().strip())

    def move_with_monitoring(self, axis, position, speed, poll_interval=0.01):
        """Move with specified speed and print live position"""
        self.set_speed(axis, speed)
        self.move_absolute(axis, position)

        print(f"Moving axis {axis} to {position} at speed {speed}...")
        while self.is_moving(axis):
            pos = self.get_position(axis)
            print(f"  Position: {pos:.3f}", end='\r')
            time.sleep(poll_interval)

        final_pos = self.get_position(axis)
        print(f"\nArrived at {final_pos:.3f}")

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
        return (t0-mm)*6.667
    def ps_to_mm(self, ps):
        t0 = self.state.settings["time zero pos"]
        print(t0, ps)
        return t0 - ps/6.667
    def close(self):
        self.inst.close()
        self.rm.close()

# --- Example usage ---
if __name__ == "__main__":
    esp = ESP300Controller("GPIB0::1::INSTR")
    try:
        esp.move_with_monitoring(axis=1, position=-30.0, speed=5.0)
    finally:
        esp.close()
