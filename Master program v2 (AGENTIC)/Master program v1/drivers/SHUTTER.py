import serial
import time




class SHUTTER:
    def __init__(self, baud=9600, timeout=1):
        self.ser = serial.Serial('COM4', baud, timeout=timeout)
        time.sleep(2)
        print("Shutter online")

    def set_servo(self, servo, angle):
        """
        servo: 1 or 2
        angle: 0 or 90
        Returns 'OK' or 'NO_MOVE'.
        """
        if servo not in (1, 2):
            raise ValueError("servo must be 1 or 2")
        if angle not in (0, 90):
            raise ValueError("angle must be 0 or 90")
        cmd = f"SET{servo}_{angle}\n"
        self.ser.write(cmd.encode())
        return self.ser.readline().decode().strip()

    def setLED(self, brightness):
        """
        brightness: 0–255
        Returns 'OK'.
        """
        if not (0 <= brightness <= 255):
            raise ValueError("brightness must be between 0 and 255")
        cmd = f"LED{brightness}\n"
        self.ser.write(cmd.encode())
        return self.ser.readline().decode().strip()

    def closePump(self):
        self.set_servo(2, 0)
        print("pump shutter closed")
    def closeProbe(self):
        self.set_servo(1, 0)
        print("probe shutter closed")
    def openPump(self):
        self.set_servo(2, 90)
        print("pump shutter opened")
    def openProbe(self):
        self.set_servo(1, 90)
        print("probe shutter opened")

    def close(self):
        """Close the serial connection."""
        self.setLED(0)
        self.ser.close()


if __name__ == "__main__":
    ctrl = SHUTTER()    # ← change to your port
    time.sleep(3)
    print("Servo 1 →", ctrl.set_servo(1, 90))
    time.sleep(.2)
    print("Servo 2 →", ctrl.set_servo(2, 90))
    time.sleep(.2)
    print("LED →",    ctrl.setLED(255))
    time.sleep(.2)
    ctrl.close()
