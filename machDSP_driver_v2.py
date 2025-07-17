# mach_dsp_ft232h_v6.py

import time
from pyftdi.ftdi import Ftdi, BitMode

# D0..D3 mask and line bits
_BITMASK    = 0x0F        # D0, D1, D2, D3 as outputs
_CLOCK_BIT  = 1 << 3      # D3 = clock
_FS_BIT     = 1 << 2      # D2 = frame-sync
_X_BIT      = 1 << 1      # D1 = X data
_Y_BIT      = 1 << 0      # D0 = Y data

class MachDSPDriver:
    def __init__(self,
                 url: str = 'ftdi:///1',
                 clk_hz: int = 500_000,
                 latency: int = 4):
        """
        url     : FTDI URL (e.g. 'ftdi:///1')
        clk_hz  : bit-bang clock rate in Hz (500 kHz → ~1 562 full frames/sec)
        latency : USB latency timer in ms
        """
        self.ft = Ftdi()
        # 1) Open the device
        self.ft.open_from_url(url)
        # 2) In SYNCBB, baudrate sets the bit-clock
        self.ft.set_baudrate(clk_hz)
        # 3) Enable synchronous bit-bang on D0..D3 using the enum
        self.ft.set_bitmode(_BITMASK, BitMode.SYNCBB)
        # 4) Lower latency for smoother toggling
        self.ft.set_latency_timer(latency)
        # 5) Idle all lines low
        self.ft.write_data(b'\x00')

    def move(self, x: float, y: float) -> None:
        """
        Send one 20-clock XY2-100 frame for (x,y) in [–1, +1].
        Add time.sleep(0.001) here if you want to cap at 1000 frames/sec.
        """
        frame = self._build_frame(x, y)
        self.ft.write_data(frame)

    def _build_frame(self, x: float, y: float) -> bytearray:
        # Map –1..+1 → 0..65535
        def to_u16(v):
            v = max(-1.0, min(1.0, v))
            return int(round((v + 1.0) * 32767.5))

        xv, yv = to_u16(x), to_u16(y)
        ctrl = (0, 0, 1)  # control bits = 0b001 for 16-bit mode

        x_bits = list(ctrl) + [(xv >> i) & 1 for i in range(15, -1, -1)]
        y_bits = list(ctrl) + [(yv >> i) & 1 for i in range(15, -1, -1)]

        buf = bytearray()
        for clk in range(20):
            xb = x_bits[clk] if clk < 19 else 0
            yb = y_bits[clk] if clk < 19 else 0
            fs = 1 if clk < 19 else 0
            base = (yb << 0) | (xb << 1) | (fs << 2)
            buf.append(base)              # CLK low
            buf.append(base | _CLOCK_BIT) # CLK high
        return buf

    def close(self):
        """Turn off bit-bang and close."""
        self.ft.set_bitmode(0, 0)
        self.ft.close()


# ─── Example: drive a square at 0.5 s dwell ───
if __name__ == '__main__':
    galvo = MachDSPDriver(url='ftdi:///1',
                          clk_hz=500_000,
                          latency=4)
    try:
        square = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        for x, y in square:
            galvo.move(x, y)
            time.sleep(0.5)
    finally:
        galvo.close()
