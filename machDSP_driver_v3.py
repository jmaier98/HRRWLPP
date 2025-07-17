from pyftdi.ftdi import Ftdi
from pyftdi.gpio import GpioAsyncController
import time
import struct
import numpy as np


class MachDSPController:
    def __init__(self, clock_rate=100000):
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
    
    def send_bit_sequence(self, frame_sync, x_bits, y_bits):
        """
        Send a complete 20-bit sequence to the Mach DSP
        
        Args:
            frame_sync: Frame sync bit (1 for start of frame)
            x_bits: List of 19 bits for X-axis data (MSB first)
            y_bits: List of 19 bits for Y-axis data (MSB first)
        """
        # Pin mapping: D2=Frame Sync, D3=Clock, D1=X-data, D0=Y-data
        pin_sequence = []
        for i in range(5):
            pin_value_low = (0 << 0) | (0 << 1) | (0 << 2) | (0 << 3)
            pin_sequence.append(pin_value_low)
            pin_value_high = (0 << 0) | (0 << 1) | (0 << 2) | (1 << 3)
            pin_sequence.append(pin_value_high)
        for bit_index in range(20):
            # Determine bit values for this clock cycle
            if bit_index == 0:
                fs_bit = frame_sync
                x_bit = x_bits[0] if len(x_bits) > 0 else 0
                y_bit = y_bits[0] if len(y_bits) > 0 else 0
            elif bit_index < 20 and bit_index-1 < len(x_bits):
                # Frame sync can stay high for first 19 clocks, must be low by clock 20
                fs_bit = frame_sync if bit_index < 19 else 0
                x_bit = x_bits[bit_index-1] if bit_index-1 < len(x_bits) else 0
                y_bit = y_bits[bit_index-1] if bit_index-1 < len(y_bits) else 0
            else:
                fs_bit = 0
                x_bit = 0
                y_bit = 0
            
            # Clock low phase - set up data (data changes on rising edge)
            pin_value_low = (y_bit << 0) | (x_bit << 1) | (fs_bit << 2) | (0 << 3)
            pin_sequence.append(pin_value_low)
            
            # Clock high phase - data is latched on falling edge
            pin_value_high = (y_bit << 0) | (x_bit << 1) | (fs_bit << 2) | (1 << 3)
            pin_sequence.append(pin_value_high)
        for i in range(5):
            pin_value_low = (0 << 0) | (0 << 1) | (0 << 2) | (0 << 3)
            pin_sequence.append(pin_value_low)
            pin_value_high = (0 << 0) | (0 << 1) | (0 << 2) | (1 << 3)
            pin_sequence.append(pin_value_high)
        # Add final state with frame sync low
        pin_sequence.append(0x00)
        
        # Write the entire sequence
        self.write_pins_with_timing(pin_sequence)
    
    def position_to_bits(self, position, use_18bit=False):
        """
        Convert normalized position (-1 to 1) to bit array
        
        Args:
            position: Float value from -1.0 to 1.0
            use_18bit: Use 18-bit mode (True) or 16-bit mode (False)
        
        Returns:
            List of bits representing the position data (19 bits total including control)
        """
        # Clamp position to valid range
        position = max(-1.0, min(1.0, position))
        
        if use_18bit:
            # 18-bit mode: 0 to 262143, middle = 131072
            control_bits = [1, 0, 0]  # Control bits for 18-bit mode (binary 100)
            max_val = 262143
            mid_val = 131072
            # Map -1..1 to 0..262143
            position_val = int(mid_val + (position * mid_val))
            position_val = max(0, min(max_val, position_val))
            # Convert to 18 bits, MSB first
            data_bits = [(position_val >> i) & 1 for i in range(17, -1, -1)]
        else:
            # 16-bit mode: 0 to 65535, middle = 32768
            control_bits = [0, 0, 1]  # Control bits for 16-bit mode (binary 001)
            max_val = 65535
            mid_val = 32768
            # Map -1..1 to 0..65535
            position_val = int(mid_val + (position * mid_val))
            position_val = max(0, min(max_val, position_val))
            # Convert to 16 bits, MSB first, then pad to 18 bits
            data_bits = [(position_val >> i) & 1 for i in range(15, -1, -1)]
            data_bits = [0, 0] + data_bits  # Pad to 18 bits
        
        # Return control bits + data bits (total 19 bits)
        return control_bits + data_bits
    
    def Move(self, x, y):
        """
        Move galvo mirrors to specified position
        
        Args:
            x: X position from -1.0 to 1.0
            y: Y position from -1.0 to 1.0
        """
        try:
            # Convert positions to bit sequences
            x_bits = self.position_to_bits(x, use_18bit=False)
            y_bits = self.position_to_bits(y, use_18bit=False)
            
            # Debug output
            print(f"Moving to X={x:.3f}, Y={y:.3f}")
            
            # Send the command
            self.send_bit_sequence(frame_sync=1, x_bits=x_bits, y_bits=y_bits)
            
        except Exception as e:
            print(f"Error in Move function: {e}")
    
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

# Example usage - Square pattern
def galvo_square_demo():
    """Demonstrate galvo control by drawing a square pattern"""
    
    controller = None
    
    try:
        # Initialize controller
        print("Initializing Mach DSP Controller...")
        controller = MachDSPController(clock_rate=100000)  # 1MHz clock
        
        print("Starting galvo square demo...")
        
        # Define square corners (normalized coordinates -1 to 1)
        square_points = [
            (0.0, 0.0),    # Start at center
            (-0.1, -0.1),  # Bottom left
            (0.1, -0.1),   # Bottom right
            (0.1, 0.1),    # Top right
            (-0.1, 0.1),   # Top left
            (-0.1, -0.1),  # Back to bottom left
            (0.0, 0.0)     # Return to center
        ]
        
        # Move through each point
        for i, (x, y) in enumerate(square_points):
            print(f"Moving to point {i+1}: ({x:.1f}, {y:.1f})")
            controller.Move(x, y)
            time.sleep(0.5)  # Wait 0.5 seconds at each point
        
        print("Square demo completed!")
        
    except Exception as e:
        print(f"Error during demo: {e}")
    
    finally:
        # Always close the connection
        if controller:
            controller.close()

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
                controller.Move(x,y)
                time.sleep(0.01)  # Wait 0.01 seconds at each point
        
        print("Square demo completed!")
        
    except Exception as e:
        print(f"Error during demo: {e}")
    
    finally:
        # Always close the connection
        if controller:
            controller.close()
# Alternative simple test function
def simple_move_test():
    """Simple test to move to a few positions"""
    controller = None
    
    try:
        controller = MachDSPController()
        
        
            
        print("Test completed!")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if controller:
            controller.close()

if __name__ == "__main__":
    # Run the demo
    print("Choose test:")
    print("1. Square demo")
    print("2. Simple move test")
    
    choice = input("Enter choice (1 or 2): ").strip()
    
    if choice == "1":
        galvo_square_demo()
    elif choice == "2":
        simple_move_test()
    elif choice == "3":
        square_scan_test()
    else:
        print("Running square demo by default...")
        galvo_square_demo()
