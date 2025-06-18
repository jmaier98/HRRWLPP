import pygame
import serial
import time
import numpy as np

# Configuration
PORT_big = 'COM3'
PORT_small = 'COM4'
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

def move_small_motors(ser, joystick):
    motor1 = joystick.get_button(0)
    motor2 = joystick.get_button(1)

    if not hasattr(move_small_motors, "angle1"):
        move_small_motors.angle1 = 0
        move_small_motors.angle2 = 0
        move_small_motors.last_sent = ""

    updated = False

    if motor1:
        move_small_motors.angle1 = 90 if move_small_motors.angle1 == 0 else 0
        print(f"Servo 1 toggled to {move_small_motors.angle1}")
        updated = True
        time.sleep(0.2) 

    if motor2:
        move_small_motors.angle2 = 90 if move_small_motors.angle2 == 0 else 0
        print(f"Servo 2 toggled to {move_small_motors.angle2}")
        updated = True
        time.sleep(0.2)
        
    
    current_command = f"{move_small_motors.angle1},{move_small_motors.angle2}\n"
    if updated: #and current_command != move_small_motors.last_sent:
        ser.write(current_command.encode())
        print(current_command)
        move_small_motors.last_sent = current_command

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
        with serial.Serial(PORT_big, BAUD, timeout=1) as ser_big, \
             serial.Serial(PORT_small, BAUD, timeout=1) as ser_small:

            time.sleep(2)
            ser_big.reset_input_buffer()
            ser_small.reset_input_buffer()

            send_gcode(ser_big, 'G21')  # mm units
            send_gcode(ser_big, 'G91')  # relative positioning
            send_gcode(ser_big, 'G92 X0 Y0 Z0')  # zero current position

            while True:
                move_big_motors_from_controller(ser_big, joystick)
                move_small_motors(ser_small, joystick)
                
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        pygame.quit()

if __name__ == '__main__':
    main()
