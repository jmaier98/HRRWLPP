import serial
import time

# Update this to match your Arduino's port
arduino = serial.Serial(port='COM4', baudrate=9600, timeout=1)
time.sleep(2)  # Wait for Arduino to initialize

def set_servo_angle(angle1):
    arduino.write(f"{angle1}\n".encode())
    print(f"Moved to angle {angle}")
    time.sleep(0.1)
