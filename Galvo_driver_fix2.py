from pyftdi.gpio import GpioSyncController
import time
import numpy as np
from pyftdi.ftdi import Ftdi
PIN_MASK = 0x0F  # D0–D4

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
    ftdi = Ftdi()
    ftdi.open_bitbang(
        vendor=0x0403,            # FTDI VID
        product=0x6014,           # FT232H PID
        interface=1,              # usually 1 for the single port
        direction=PIN_MASK,       # which pins are outputs
        initial=0x00,             # initial output state
        frequency=500_000.0,      # GPIO clock in Hz
        latency=1,                # USB latency in ms
        debug=False               # set True to trace commands
    )

    try:
        xvals = np.linspace(-.1, .1, 50)
        yvals = np.linspace(-.1, .1, 50)
        for y in yvals:
            for x in xvals:
                frame = moveXY_frame(x, y)
                ftdi.write_data(frame)         # single contiguous transfer
                time.sleep(0.01)
    finally:
        # home back to zero
        ftdi.write_data(moveXY_frame(0, 0))
        ftdi.close()
if __name__ == "__main__":
    main()
        
