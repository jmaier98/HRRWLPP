from pyftdi.gpio import GpioSyncController
import time
import numpy as np

PIN_MASK = 0x0F  # D0–D3

def moveXY(x, y):
    x = min(max(x, -1), 1)
    y = min(max(y, -1), 1)
    xi = int(x * 32767) + 32768
    yi = int(y * 32767) + 32768
    xbits = format(xi,  '016b')
    ybits = format(yi,  '016b')
    seq = []
    # --- preamble
    seq += [
        (0<<0)|(0<<1)|(1<<2)|(1<<3),
        (0<<0)|(0<<1)|(1<<2)|(0<<3),
        (0<<0)|(0<<1)|(0<<2)|(1<<3),
        (0<<0)|(0<<1)|(0<<2)|(0<<3),
        (1<<0)|(1<<1)|(0<<2)|(1<<3),
        (1<<0)|(1<<1)|(0<<2)|(0<<3),
    ]
    # --- 16 data bits
    for i in range(16):
        xb = int(xbits[i])
        yb = int(ybits[i])
        seq.append((yb<<0)|(xb<<1)|(0<<2)|(1<<3))
        seq.append((yb<<0)|(xb<<1)|(0<<2)|(0<<3))
    # --- postamble
    seq += [
        (0<<0)|(0<<1)|(0<<2)|(1<<3),
        (0<<0)|(0<<1)|(0<<2)|(0<<3),
    ]
    return bytes(seq)

def main():
    gpio = GpioSyncController()
    gpio.configure('ftdi:///1', direction=PIN_MASK, frequency=1_000_000)

    # smaller grid for timing
    xvals = np.linspace(-.1, .1, 5)
    yvals = np.linspace(-.1, .1, 5)

    print("Timing each build vs exchange (5×5 grid):")
    try:
        for y in yvals:
            for x in xvals:
                t0 = time.perf_counter()
                frame = moveXY(x, y)
                t1 = time.perf_counter()
                gpio.exchange(frame)
                t2 = time.perf_counter()

                print(f"x={x:+.3f}, y={y:+.3f} | "
                      f"build: {(t1-t0)*1e3:7.3f} ms, "
                      f"xchg: {(t2-t1)*1e3:7.3f} ms, "
                      f"total: {(t2-t0)*1e3:7.3f} ms")
    except KeyboardInterrupt:
        pass
    finally:
        gpio.exchange(b'\x00')
        gpio.close(freeze=True)

if __name__ == '__main__':
    main()
