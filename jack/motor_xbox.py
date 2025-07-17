import pygame
import serial
import time
import numpy as np

# Configuration
PORT_big = 'COM3'
BAUD = 115200

# G-code sender
def send_gcode(ser, cmd, wait=0.05):
    ser.write((cmd + '\n').encode())
    time.sleep(wait)

def move_big_motors_from_controller(ser, joystick, scale=1, deadzone=0.15):
    pygame.event.pump()
    x_val = joystick.get_axis(0)
    y_val = joystick.get_axis(1)

    if abs(x_val) < deadzone: x_val = 0
    if abs(y_val) < deadzone: y_val = 0

    dx = round(x_val**3 * scale, 3)
    dy = round(y_val**3 * scale, 3)
    dr = np.sqrt(dx**2 + dy**2)
    feedrate = dr * 1200

    if dx != 0 or dy != 0:
        cmd = f'G1 X{dx} Y{dy} F{feedrate}'
        send_gcode(ser, cmd)
    else:

        # --- Z axis control (only when X/Y are idle) ---
        # read raw trigger axes (adjust indices if your controller maps differently)
        lt_raw = joystick.get_axis(4)  # left trigger
        rt_raw = joystick.get_axis(5)  # right trigger
        lt = (lt_raw + 1) / 2
        rt = (rt_raw + 1) / 2

        # apply deadzone
        if lt < .1: lt = 0
        if rt < .1: rt = 0

        # cubic scaling, positive = Z up, negative = Z down
        dz = round((lt**3 - rt**3) * scale*.15, 3)
        #print(dz)
        if dz != 0:
            feedrate_z = abs(dz) * 1200
            cmd = f'G1 Z{dz} F{feedrate_z:.1f}'
            send_gcode(ser, cmd)



def main():
    pygame.init()
    pygame.joystick.init()

    while pygame.joystick.get_count() == 0:
        print("Waiting for controller...")
        pygame.joystick.quit()
        pygame.joystick.init()

    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    print(f"Connected: {joystick.get_name()}")

    try:
        with serial.Serial(PORT_big, BAUD, timeout=1) as ser_big:
            
            time.sleep(2)
            ser_big.reset_input_buffer()

            send_gcode(ser_big, 'G21')  # mm units
            send_gcode(ser_big, 'G91')  # relative positioning
            send_gcode(ser_big, 'G92 X0 Y0 Z0')  # zero current position

            while True:
                move_big_motors_from_controller(ser_big, joystick)
                
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        pygame.quit()

if __name__ == '__main__':
    main()
