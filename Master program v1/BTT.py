import serial
import time
PELLICLE = 60
EMPTY = 20
POWERMETER = 92

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

    def objectiveZ(self, dz, feed):
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
        
    def close(self):
        self.d1.close()
        self.d2.close()
        self.d3.close()
