import serial
import time
PELLICLE = 60
EMPTY = 20
POWERMETER = 92
ROTATION_MOUNT = 3.6
class BTT:
    def __init__(self, state):
        super().__init__()
        self.baud = 115200
        self.state = state
        self.d1 = serial.Serial('COM3', self.baud, timeout=1) #cryo and focus
        time.sleep(0.5)
        self.d1.write(('G21\n').encode())
        time.sleep(0.05)
        self.d1.write(('G91\n').encode())
        time.sleep(0.05)
        self.d1.write(('G92 X0 Y0 Z0\n').encode())
        time.sleep(0.05)
        self.d1.write(('M302 S0\n').encode())
        time.sleep(0.05)
        self.d1.write(('M17 E\n').encode())
        time.sleep(0.05)
        print("Stepper Controller 1 online")
        self.d2 = serial.Serial('COM9', self.baud, timeout=1) #rails and 2 rots
        time.sleep(0.5)
        print("Stepper Controller 2 online")
        self.d3 = serial.Serial('COM8', self.baud, timeout=1) #4 rots
        time.sleep(0.5)
        self.d3.write(('M302 S0\n').encode())
        time.sleep(0.05)
        self.d3.write(('M17 E\n').encode())
        time.sleep(0.05)
        print("Stepper Controller 3 online")

    def send_gcode(self, driver, cmd, wait=0.05):
        if driver == 1:
            self.d1.write((cmd + '\n').encode())
        if driver == 2:
            self.d2.write((cmd + '\n').encode())
        if driver == 3:
            self.d3.write((cmd + '\n').encode())
        time.sleep(wait)

    def cryoXY(self, dx, dy, feed):
        self.send_gcode(1, f'G1 X{dx} Y{dy} F{feed:.1f}')

    def cryoZ(self, dz, feed):
        self.send_gcode(1, f'G1 Z{dz} F{feed:.1f}')

    def focus(self, dz, feed):
        self.send_gcode(1, f'G1 E{dz} F{feed:.1f}')

    def rotZ(self, dz, feed):
        self.send_gcode(1, f'G1 E{dz} F{feed:.1f}')
        
    def pellicles(self):
        self.send_gcode(2, f'G0 X{PELLICLE} Y{PELLICLE} F{3000}')

    def clear(self):
        self.send_gcode(2, f'G0 X{EMPTY} Y{EMPTY} F{3000}')

    def powermeter(self):
        self.send_gcode(2, f'G0 X{POWERMETER} Y{EMPTY} F{3000}')

    def homeRails(self):
        self.send_gcode(2, 'G28 X')
        self.send_gcode(2, 'G28 Y')
        
    def home_rot1(self):  
        self.send_gcode(3, 'G28 X')
       
    def home_rot2(self):
        self.send_gcode(3, 'G28 Y')

    def home_rot3(self):
         self.send_gcode(3, 'G28 Z')

    def home_all_rot(self):
        self.home_rot1()
        self.home_rot2()
        self.home_rot3()
        
    def rot_1(self, angle_x, feedrate):
        self.state.settings["pump_polarizer_angle"] = angle_x

        travel_mm = angle_x / ROTATION_MOUNT
        self.send_gcode(3, f'G0 X{travel_mm:.3f} F{feedrate}')

    def rot_2(self, angle_y, feedrate):
        self.state.settings["probe_polarizer_angle"] = angle_y

        travel_mm = angle_y / ROTATION_MOUNT   
        self.send_gcode(3, f'G0 Y{travel_mm:.3f} F{feedrate}')

    def rot_3(self, angle_z, feedrate):
        self.state.settings["pump_waveplate_angle"] = angle_z

        travel_mm = angle_z / (ROTATION_MOUNT*5)  
        self.send_gcode(3, f'G0 Z{travel_mm:.3f} F{feedrate}')


    def rot_4(self, angle_e, feedrate):
        self.state.settings["probe_waveplate_angle"] = angle_e

        travel_mm = angle_e / (ROTATION_MOUNT*1.161)
        self.send_gcode(3, f'G0 E{travel_mm:.3f} F{feedrate}')
        

        
    def close(self):
        self.d1.close()
        self.d2.close()
        self.d3.close()
