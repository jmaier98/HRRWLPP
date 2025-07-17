from pyftdi.gpio import GpioSyncController
import time
import numpy as np

PIN_MASK = 0x1F  # D0–D4

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
    pin_sequence.append((0 << 0) | (0 << 1) | (1 << 2) | (1 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (1 << 2) | (0 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3) | (1 << 4))
    pin_sequence.append((1 << 0) | (1 << 1) | (0 << 2) | (1 << 3) | (1 << 4))
    pin_sequence.append((1 << 0) | (1 << 1) | (0 << 2) | (0 << 3) | (1 << 4))
    for i in range(16):
        xb = int(xbits[i])
        yb = int(ybits[i])
        pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (1 << 3) | (1 << 4))
        pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (0 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3) | (0 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3) | (0 << 4))
    
    # Write the entire sequence
    return bytes(pin_sequence)

def main():
    gpio = GpioSyncController()
    
    # Open in synchronous bit-bang at 1 MHz, low latency
    gpio.configure('ftdi:///1', direction=PIN_MASK,
                       frequency=500000)
    gpio._ftdi.set_latency_timer(1)
    try:
        print("Starting 40-byte transfer loop. Press Ctrl+C to stop.")
        xvals = np.linspace(-.1,.1,50)
        yvals = np.linspace(-.1,.1,50)
        for y in yvals:
            for x in xvals:
                frame=moveXY(x,y)
                gpio.exchange(frame)
                time.sleep(.01)
        frame=moveXY(0,0)
        gpio.exchange(frame)        
        # clocks out *all* bytes at 1 MHz
    except KeyboardInterrupt:
        pass
    finally:
        try:
            while True:
                # your work here
                gpio.exchange(moveXY(0,0))
                print("sent0")
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nInterrupted by user – exiting loop.")
            gpio.exchange(b'\x00')      # drive low
        gpio.close(freeze=True) 

if __name__ == '__main__':
    main()
