import serial
import time
PELLICLE = 60
EMPTY = 20
POWERMETER = 92
OBJECTIVE_MOUNT = 3.6
class BTT:
    def __init__(self):
        super().__init__()
        self.baud = 115200
        self.d1 = serial.Serial('COM3', self.baud, timeout=1) #cryo and focus
        time.sleep(0.5)
        print("opened d1")
        self.d2 = serial.Serial('COM9', self.baud, timeout=1) #rails and 2 rots
        time.sleep(0.5)
        print("opened d2")
        self.d3 = serial.Serial('COM8', self.baud, timeout=1) #4 rots
        time.sleep(0.5)
        print("opened d3")
        
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

    def rotZ(self, dz, feed):
        self.send_gcode(1, f'G1 E{dz} F{feed:.1f}')
        
    def pellicles(self):
        self.send_gcode(2, f'G0 X{PELLICLE} Y{PELLICLE} F{5000}')

    def clear(self):
        self.send_gcode(2, f'G0 X{EMPTY} Y{EMPTY} F{5000}')

    def powermeter(self):
        self.send_gcode(2, f'G0 X{POWERMETER} Y{EMPTY} F{5000}')

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
        travel_mm = angle_x / OBJECTIVE_MOUNT   
        self.send_gcode(3, f'G0 A{travel_mm:.3f} F{feedrate}')

    def rot_2(self, angle_y, feedrate):
        travel_mm = angle_y / OBJECTIVE_MOUNT   
        self.send_gcode(3, f'G0 B{travel_mm:.3f} F{feedrate}')

    def rot_3(self, angle_z, feedrate):
        travel_mm = angle_z / OBJECTIVE_MOUNT   
        self.send_gcode(3, f'G0 C{travel_mm:.3f} F{feedrate}')
        
    def close(self):
        self.d1.close()
        self.d2.close()
        self.d3.close()
