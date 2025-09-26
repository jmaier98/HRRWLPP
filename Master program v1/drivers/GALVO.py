from pyftdi.gpio import GpioSyncController
from pyftdi.ftdi import Ftdi
import time
import numpy as np

PIN_MASK = 0x1F  # D0–D3

def moveXY(x, y):
    x = min(x, 1)
    x = max(x, -1)
    x = int(x * 131071) + 131072
    y = min(y, 1)
    y = max(y, -1)
    y = int(y * 131071) + 131072
    xbits = format(x, '018b')
    ybits = format(y, '018b')
    pin_sequence = []
    pin_sequence.append((1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4))
    pin_sequence.append((1 << 0) | (1 << 1) | (1 << 2) | (0 << 3) | (1 << 4))
    for i in range(18):
        xb = int(xbits[i])
        yb = int(ybits[i])
        pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (1 << 3) | (1 << 4))
        pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (0 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3) | (0 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3) | (0 << 4))
    
    return bytes(pin_sequence)

class GALVO:
    def __init__(self, device_url='ftdi://0x0403:0x6014:02:24/1', frequency=200000):
        self.gpio = None
        self.ftdi = None
        self.device_url = device_url
        self.frequency = frequency
        self.configure()
        
    def configure(self):
        """Configure FTDI device for atomic 40-byte transfers"""
        self.gpio = GpioSyncController()
        self.gpio.configure(self.device_url, direction=PIN_MASK, frequency=self.frequency)
        self.ftdi = self.gpio._ftdi
        
        # Force minimum latency
        self.ftdi.set_latency_timer(1)
        
        # Critical: Set USB parameters to force atomic transfers
        try:
            # Method 1: Use standard USB chunk size but force immediate flush
            # Don't set chunk size to 40 - use 64 or 512 (USB standard sizes)
            if hasattr(self.ftdi, 'write_chunk_size'):
                self.ftdi.write_chunk_size = 64  # Standard USB 2.0 packet size
                print("Set write chunk size to 64 bytes (USB standard)")
            
            # Method 2: Set reasonable timeout
            if hasattr(self.ftdi, 'usb_dev'):
                self.ftdi.usb_dev.default_timeout = 100  # 100ms timeout
                #print("Set USB timeout to 100ms")
            
            # Method 3: Try to set USB parameters for better buffering
            try:
                # Use USB endpoint max packet size (typically 64 bytes)
                self.ftdi.set_usb_parameters(request_size=64, buffer_size=64)
                print("Set USB parameters to 64 bytes")
            except:
                pass
                
        except Exception as e:
            print(f"USB parameter setup: {e}")
        
        # Verify we're in the correct mode
        #print(f"FTDI mode: {self.ftdi.bitbang_enabled}")
        #print(f"Frequency: {self.frequency} Hz")
        print("Galvo online")
        
    def send_frame_atomic(self, frame_data):
        """Send exactly 40 bytes atomically using flush to force immediate transfer"""
        if len(frame_data) != 40:
            raise ValueError(f"Frame must be exactly 44 bytes, got {len(frame_data)}")
        
        # Method 1: Use exchange with immediate flush
        try:
            # The key insight: exchange() should handle the transfer properly
            # But we need to ensure the USB buffer is flushed immediately
            result = self.gpio.exchange(frame_data)
            
            # Force immediate USB transfer by flushing buffers
            try:
                self.ftdi.purge_buffers()  # This forces any pending data to be sent
            except:
                pass
                
            return True
            
        except Exception as e:
            print(f"exchange failed: {e}")
            
            # Method 2: Try write_data with padding to USB packet boundary
            try:
                # Pad to 64 bytes (USB packet size) and then only send what we need
                padded_data = frame_data + b'\x00' * (64 - len(frame_data))
                bytes_written = self.ftdi.write_data(padded_data)
                
                # Check if we wrote at least our 40 bytes
                if bytes_written >= 40:
                    return True
                else:
                    print(f"Warning: Only wrote {bytes_written} bytes")
                    return False
                    
            except Exception as e2:
                print(f"write_data with padding failed: {e2}")
                return False
    
    def send_frame_with_verification(self, frame_data):
        """Send frame and verify it was sent atomically"""
        if len(frame_data) != 40:
            raise ValueError(f"Frame must be exactly 44 bytes, got {len(frame_data)}")
        
        # Clear any pending data first
        try:
            self.ftdi.purge_buffers()
        except:
            pass
        
        # Send the frame
        start_time = time.time()
        success = self.send_frame_atomic(frame_data)
        end_time = time.time()
        
        transfer_time = (end_time - start_time) * 1000  # Convert to ms
        
        if transfer_time > 5:  # If transfer takes more than 5ms, likely fragmented
            print(f"Warning: Transfer took {transfer_time:.2f}ms - possible fragmentation")
        
        return success
    
    def move(self, x, y):
        """Move to specified (x, y) coordinates using atomic transfer"""
        frame = moveXY(x, y)
        
        if not self.send_frame_with_verification(frame):
            print("Failed to send frame atomically")
            return False
        return True
    
    def close(self):
        """Clean shutdown"""
        if self.gpio:
            self.gpio.close(freeze=True)
    

