import tkinter as tk
import serial
import time

# Configuration
PORT_BIG = 'COM9'
BAUD = 115200

# Movement speeds (in units per minute for G-code)
HOMING_SEEK_RATE = 5000   # Seek rate for homing (mm/min)
HOMING_FEEDRATE = 5000    # Feed rate for homing (mm/min)
TRAVEL_FEED_RATE = 5000   # Feed rate for positional moves (mm/min)

# G-code sender
def send_gcode(ser, cmd, wait=0.05):
    """
    Send a G-code command over serial and wait briefly.
    """
    ser.write((cmd + '\n').encode())
    time.sleep(wait)

class MotorControlApp:
    def __init__(self, master, ser):
        self.master = master
        self.ser = ser
        master.title("Motor Control GUI")

        # Create buttons
        btn_home = tk.Button(master, text="Home", command=self.home)
        btn_x20 = tk.Button(master, text="Pellicle", command=lambda: self.move_to(60))
        btn_x10 = tk.Button(master, text="Power meter", command=lambda: self.move_to(92))
        btn_x0  = tk.Button(master, text="Empty",  command=lambda: self.move_to(20))

        # Layout
        btn_home.pack(fill='x', padx=10, pady=(10, 5))
        btn_x20.pack(fill='x', padx=10, pady=5)
        btn_x10.pack(fill='x', padx=10, pady=5)
        btn_x0.pack(fill='x',  padx=10, pady=(5, 10))

    def home(self):
        """Home the X axis."""
        send_gcode(self.ser, 'G28 Y')
        send_gcode(self.ser, 'G28 X')
        

    def move_to(self, position):
        """Move the X axis to the specified position."""
        # Use G0 for rapid positioning; adjust as needed
        send_gcode(self.ser, f'G0 Y{position} F{TRAVEL_FEED_RATE}')

def main():
    try:
        # Open serial connection
        ser = serial.Serial(PORT_BIG, BAUD, timeout=1)
        time.sleep(1)  # Wait for the connection to initialize
        ser.reset_input_buffer()

        # Set up GUI
        root = tk.Tk()
        app = MotorControlApp(root, ser)

        # Ensure serial is closed on exit
        def on_closing():
            ser.close()
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_closing)
        root.mainloop()

    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    main()

