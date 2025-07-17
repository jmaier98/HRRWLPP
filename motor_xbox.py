import xbox
import pygame
import serial
import time
import numpy as np



# Configuration
PORT_big = 'COM3'
BAUD = 115200
PORT_small = 'COM4'

# G-code sender
def send_gcode(ser, cmd, wait=0.05):
    ser.write((cmd + '\n').encode())
    time.sleep(wait)
    #while ser.in_waiting:
        #print('<', ser.readline().decode(errors='ignore').strip())



def move_big_motors_from_controller(ser, joystick, scale=1, deadzone=0.15):
    
    """Read controller joystick position and move X/Y motors.
    
    Parameters:
        ser        : serial.Serial object
        joystick   : pygame joystick object
        scale      : multiplier to convert joystick input to mm
        deadzone   : threshold below which input is ignored
        feedrate   : G-code feedrate in mm/min
    """
    pygame.event.pump()
    x_val = joystick.get_axis(0)  # Left stick X
    y_val = joystick.get_axis(1)  # Left stick Y (inverted)

    # Apply deadzone
    if abs(x_val) < deadzone: x_val = 0
    if abs(y_val) < deadzone: y_val = 0

        
    # Convert to motion in mm
    dx = round(x_val**3 * scale, 3)
    dy = round(y_val**3 * scale, 3)
    dr = np.sqrt(dx**2 + dy**2)
    feedrate = dr*1200
     
    # Only move if there's meaningful input
    if dx != 0 or dy != 0:
        #print(f"Joystick X: {x_val:.2f}, Y: {y_val:.2f}  →  Moving X: {dx} mm, Y: {dy} mm")
        cmd = f'G1 X{dx} Y{dy} F{feedrate}'
        send_gcode(ser, cmd)

def move_small_motors(ser, joystick):
    new_angle1 = 0
    new_angle2 = 0
    motor1 = joystick.get_button(0)  # e.g., A button
    motor2 = joystick.get_button(1)  # e.g., B button
    
    
    if not hasattr(move_small_motors, "angle1"):
        move_small_motors.angle1 = 0
        move_small_motors.angle2 = 0

    if motor1:
        new_angle1 = 90 if move_small_motors.angle1 == 0 else 0
        ser.write(f"{new_angle1},{new_angle2}\n".encode())
        move_small_motors.angle1 = new_angle1
        print(f"Servo 1 toggled to {new_angle1}")
        time.sleep(0.2)  # Debounce delay

    if motor2:
        new_angle2 = 90 if move_small_motors.angle2 == 0 else 0
        ser.write(f"{new_angle2},{new_angle2}\n".encode())
        move_small_motors.angle2 = new_angle2
        print(f"Servo 2 toggled to {new_angle2}")
        time.sleep(0.2)  # Debounce delay

    
        
          
        


def main():
    pygame.init()
    pygame.joystick.init()

    # Wait for controller
    while pygame.joystick.get_count() == 0:
        print("Waiting for controller...")
        pygame.joystick.quit()
        pygame.joystick.init()

    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    print(f"Connected: {joystick.get_name()}")

    # Open both serial connections at once
    with serial.Serial(PORT_small, BAUD, timeout=1) as ser_small:

        time.sleep(2)
        ser_big.reset_input_buffer()
        ser_small.reset_input_buffer()

        # Optional G-code setup for big motors
        send_gcode(ser_big, 'G21')  # mm units
        send_gcode(ser_big, 'G91')  # relative positioning
        send_gcode(ser_big, 'G92 X0 Y0 Z0')  # zero current position

        try:
            while True:
                move_big_motors_from_controller(ser_big, joystick)
                move_small_motors(ser_small, joystick)
                 
        except KeyboardInterrupt:
            print("Stopped by user.")
        finally:
            pygame.quit()

if __name__ == '__main__':
    main()
