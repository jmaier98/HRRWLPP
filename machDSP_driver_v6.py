from pyftdi.gpio import GpioSyncController
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
    gpio = GpioSyncController()
    
    # Open in synchronous bit-bang at 1 MHz, low latency
    gpio.configure('ftdi:///1', direction=PIN_MASK,
                       frequency=1000000)
    gpio._ftdi.set_latency_timer(1)
    try:
        print("Starting 40-byte transfer loop. Press Ctrl+C to stop.")
        xvals = np.linspace(-.1,.1,50)
        yvals = np.linspace(-.1,.1,50)
        for y in yvals:
            for x in xvals:
                frame=moveXY(x,y)
                gpio.exchange(frame)
        frame=moveXY(0,0)
        gpio.exchange(frame)        
        # clocks out *all* bytes at 1 MHz
    except KeyboardInterrupt:
        pass
    finally:
        gpio.exchange(b'\x00')      # drive low
        gpio.close(freeze=True) 

if __name__ == '__main__':
    main()
