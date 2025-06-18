print("hello world")


import serial
import time

# ——— CONFIGURE THESE ———
PORT = 'COM3'       # ← your SKR’s COM port
BAUD = 115200       # ← firmware baud
SIDE = 20           # ← square side length in mm
FEEDRATE = 1500     # ← movement speed (mm/min)
# ————————————————————

def send_gcode(ser, cmd, wait=0.1):
    """Send one G-code line and print any response."""
    ser.write((cmd + '\n').encode())
    time.sleep(wait)
    # Read & print all available lines
    while ser.in_waiting:
        line = ser.readline().decode(errors='ignore').strip()
        print(f"< {line}")

def main():
    # Open serial port
    with serial.Serial(PORT, BAUD, timeout=1) as ser:
        Z_FACTOR = 80 / 400
        # Give the board a couple seconds to initialize
        time.sleep(2)
        ser.reset_input_buffer()

        # — Initialization G-codes —
        send_gcode(ser, 'G21')             # set units to mm
        send_gcode(ser, 'G91')             # relative positioning
        send_gcode(ser, 'G92 X0 Y0 Z0')    # zero current position

        # — Draw a square —
        send_gcode(ser, f'G1 X{SIDE} F{FEEDRATE}')
        send_gcode(ser, f'G1 Z{SIDE * Z_FACTOR} F{FEEDRATE}')
        send_gcode(ser, f'G1 X-{SIDE} F{FEEDRATE}')
        send_gcode(ser, f'G1 Z-{SIDE * Z_FACTOR} F{FEEDRATE}')

        send_gcode(ser, 'M503')
        #send_gcode(ser, 'G1 Y10 F1000')
        # Done
        print("Square complete.")

if __name__ == '__main__':
    main()
