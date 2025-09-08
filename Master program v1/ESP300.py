import pyvisa
import time

class ESP300Controller:
    def __init__(self, state, gpib_address="GPIB0::1::INSTR", timeout=2000):
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(gpib_address)
        self.inst.timeout = timeout
        self.initialize()
        self.state = state

    def initialize(self):
        self.inst.write("*CLS")
        self.inst.write("@E0")
        self.inst.write("@1H")
        print("ESP300 Controller online")

    def set_speed(self, axis, speed):
        """Set the absolute velocity for the axis (mm/sec or deg/sec depending on stage)"""
        self.inst.write(f"{axis}VA{speed}")
        self.state.settings["delay_stage_speed"] = speed

    def move_absolute(self, axis, position):
        """Start motion to absolute position (non-blocking)"""
        if abs(position) < 75:
            self.inst.write(f"{axis}PA{position}")
            self.state.settings["delay_stage_pos"] = position
            self.state.settings["delay_ps"] = self.mm_to_ps(position)
        else:
            print("out of range for stage, refusing to move")

    def is_moving(self, axis):
        """Check if the axis is still moving (MD = motion done)"""
        self.inst.write(f"{axis}MD?")
        resp = self.inst.read()
        return resp.strip() == '0'  # 0 = still moving, 1 = done

    def get_position(self, axis):
        self.inst.write(f"{axis}TP")
        return float(self.inst.read().strip())

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
