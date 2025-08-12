import pyvisa
import time

class ESP300Controller:
    def __init__(self, gpib_address="GPIB0::1::INSTR", timeout=2000):
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(gpib_address)
        self.inst.timeout = timeout
        self.initialize()

    def initialize(self):
        self.inst.write("*CLS")
        self.inst.write("@E0")
        self.inst.write("@1H")
        print("ESP300 Controller online")

    def set_speed(self, axis, speed):
        """Set the absolute velocity for the axis (mm/sec or deg/sec depending on stage)"""
        self.inst.write(f"{axis}VA{speed}")

    def move_absolute(self, axis, position):
        """Start motion to absolute position (non-blocking)"""
        self.inst.write(f"{axis}PA{position}")

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
