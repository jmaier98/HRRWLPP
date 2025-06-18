import serial
import time

# Update this to match your Arduino's port
arduino = serial.Serial(port='COM4', baudrate=9600, timeout=1)
time.sleep(2)  # Wait for Arduino to initialize

def set_servo_angle(angle1, angle2):
    angle1 = max(0, min(180, angle1))  # clamp between 0–180
    angle2 = max(0, min(180, angle2)) 
    arduino.write(f"{angle1}, {angle2}\n".encode())
    print(f"Moved to angle {angle1}")
    time.sleep(0.1)

# Example: Sweep from 0 to 180 and back
for angle in range(0, 181, 30):
    set_servo_angle(angle, angle)
    time.sleep(0.5)

for angle in range(180, -1, -30):
    set_servo_angle(angle, angle)
    time.sleep(0.5)


