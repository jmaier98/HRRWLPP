from pyftdi.ftdi import Ftdi
from pyftdi.gpio import GpioAsyncController
import time
import struct
import numpy as np


class MachDSPController:
    def __init__(self, clock_rate=1000000):
        """
        Initialize the FTDI FT232H controller for Mach DSP communication
        
        Args:
            clock_rate: Clock rate in Hz (default 1MHz)
        """
        self.clock_rate = clock_rate
        self.ftdi = None
        self.gpio = None
        self.setup_ftdi()
    
    def setup_ftdi(self):
        """Initialize FTDI device in GPIO mode for bit-banging"""
        try:
            # Initialize GPIO controller
            self.gpio = GpioAsyncController()
            
            # Open FTDI device (FT232H) - first available device
            # If you have multiple FTDI devices, you might need to specify the serial number
            self.gpio.open_from_url('ftdi:///1')
            
            # Configure pins D0-D3 as outputs
            # D0 = Frame Sync (pin 5), D1 = Clock (pin 6), D2 = X-data (pin 4), D3 = Y-data (pin 3)
            pin_config = 0x0F  # Pins 0-3 as outputs (bits 0-3 = 1)
            self.gpio.set_direction(pins=pin_config, direction=pin_config)
            
            # Initialize all pins low
            self.gpio.write_port(0x00)
            time.sleep(0.001)  # Small delay for initialization
            
            print("FTDI FT232H initialized successfully")
            
        except Exception as e:
            raise Exception(f"Failed to initialize FTDI device: {e}")
    
    def write_pins_with_timing(self, pin_states):
        """
        Write a sequence of pin states with proper timing
        
        Args:
            pin_states: List of pin state values to write sequentially
        """
        for state in pin_states:
            self.gpio.write_port(state)
            # Small delay to maintain clock timing - adjust if needed
            time.sleep(1.0 / (self.clock_rate * 2))  # Half period delay
    
    def moveXY(self,x,y):
        x = min(x, 1)
        x = max(x, -1)
        x = int(x * 32767) + 32768
        y = min(y, 1)
        y = max(y, -1)
        y = int(y * 32767) + 32768
        xbits = format(x, '016b')
        ybits = format(y, '016b')
        pin_sequence = []
        pin_sequence.append((0 << 0) | (0 << 1) | (1 << 2) | (1 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (1 << 2) | (0 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3))
        pin_sequence.append((1 << 0) | (1 << 1) | (0 << 2) | (1 << 3))
        pin_sequence.append((1 << 0) | (1 << 1) | (0 << 2) | (0 << 3))
        for i in range(16):
            xb = int(xbits[i])
            yb = int(ybits[i])
            pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (1 << 3))
            pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (0 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3))
        
        # Write the entire sequence
        self.write_pins_with_timing(pin_sequence)
    
    def close(self):
        """Close FTDI connection"""
        try:
            if self.gpio:
                # Set all pins low before closing
                self.gpio.write_port(0x00)
                self.gpio.close()
                self.gpio = None
                print("FTDI connection closed")
        except Exception as e:
            print(f"Error closing FTDI: {e}")



def square_scan_test():
    """Demonstrate galvo control by drawing a square pattern"""
    
    controller = None
    
    
    try:
        # Initialize controller
        print("Initializing Mach DSP Controller...")
        controller = MachDSPController(clock_rate=100000)  # 1MHz clock
        
        xvals = np.linspace(-.1,.1,20)
        yvals = np.linspace(-.1,.1,20)
        for y in yvals:
            for x in xvals:
                controller.moveXY(x,y)
                time.sleep(0.01)  # Wait 0.01 seconds at each point
        
        print("Square demo completed!")
        
    except Exception as e:
        print(f"Error during demo: {e}")
    
    finally:
        # Always close the connection
        if controller:
            controller.close()
# Alternative simple test function
def x_input_test():
    """Simple test to move to a few positions"""
    controller = None
    
    try:
        controller = MachDSPController()
        x = 0
        while abs(x) < 1:
            controller.moveXY(x,0)
            print("enter an x value")
            x = float(input())
        
            
        print("Test completed!")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if controller:
            controller.close()

if __name__ == "__main__":
    # Run the demo
    print("Choose test:")
    print("1. x input demo")
    print("2. Scan test")
    
    choice = input("Enter choice (1 or 2): ").strip()
    
    if choice == "1":
        x_input_test()
    elif choice == "2":
        square_scan_test()

        
