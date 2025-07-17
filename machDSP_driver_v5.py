from pyftdi.ftdi import Ftdi
import time
import numpy as np
PIN_MASK = 0x0F  # D0–D3
    
def moveXY(x,y):
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
    return bytes(pin_sequence)
    
def main():
    ftdi = Ftdi()
    try:
        ftdi.open_from_url('ftdi:///1')  # adapt this if needed
        ftdi.set_bitmode(0x0F, Ftdi.BitMode.SYNCBB)
        ftdi.set_latency_timer(2)
        ftdi.write_data_set_chunksize(65536)
        ftdi.read_data_set_chunksize(65536)
        ftdi.set_baudrate(1000000)  # 3 MHz = 333 ns per byte

        print("Starting 40-byte transfer loop. Press Ctrl+C to stop.")

        for i in range(100):
            x = i/200
            frame = moveXY(x,0)
            ftdi.write_data(frame)
            print("sent:")
            print(frame)
            time.sleep(0.1)  # Delay between packets (1 ms)
    
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        ftdi.close()

if __name__ == '__main__':
    main()
