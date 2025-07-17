"""
mach_dsp_ft232h.py
------------------
Drive a Mach-DSP galvo controller from a FT232H running in synchronous
bit-bang mode.

WIRING  (FT232H 1×8 female header -> Mach-DSP 6-pin “Digital Command Input”)
─────────────────────────────────────────────────────────────────────────────
 FT232H   Mach-DSP   Function
 ────────────────────────────────────────────────────────────────────────────
 D0       Pin 3      Y-axis serial data    (LSB = 0 V, MSB = 3 V3)
 D1       Pin 4      X-axis serial data
 D2       Pin 5      FRAME_SYNC            (active-high “late FS”)
 D3       Pin 6      CLOCK                (idle low, data change on ↑edge)
 GND      Pin 1      Signal ground
 (Pin 2 “enable” is left un-driven here; tie high or handle elsewhere.)

If you wired D0-D3 in a different order, edit the _BITMASK and the
byte-assembly code in _build_frame().
"""
import time
from pyftdi.ftdi import Ftdi

# ------------- LOW-LEVEL FTDI INITIALISATION -------------
_BITMASK = 0x0F                 # D0..D3 are outputs
_CLOCK_BIT = 1 << 3             # D3
_FS_BIT    = 1 << 2             # D2
_X_BIT     = 1 << 1             # D1
_Y_BIT     = 1 << 0             # D0

class MachDSPDriver:
    def __init__(self,
                 url: str = 'ftdi:///1',
                 clk_hz: int = 1_000_000):
        """
        url     : PyFtdi URL identifying the FT232H
        clk_hz  : bit-bang sample rate (twice the Mach-DSP clock rate)
        """
        self.ft = Ftdi()
        self.ft.open_from_url(url)
        self.ft.set_frequency(clk_hz)          # 1 MHz → 1 µs per sample
        self.ft.set_bitmode(_BITMASK, Ftdi.BITMODE_SYNCBB)
        self.ft.write_data(bytearray([0x00]))  # all lines low
        # optional: latency timer tweak (smaller = lower write latency)
        self.ft.set_latency_timer(4)

    # ---------- HIGH-LEVEL “MOVE” API ----------
    def move(self, x: float, y: float) -> None:
        """
        x, y  : user coordinates ∈ [-1.0, +1.0]
                –1 → full-left/bottom, 0 → centre, +1 → full-right/top
        """
        frame = self._build_frame(x, y)
        self.ft.write_data(frame)

    # ---------- FRAME GENERATION ----------
    def _build_frame(self, x: float, y: float) -> bytearray:
        """
        Produce 40 samples (20 clocks × 2 half-cycles) for one XY2-100 frame.
        Control bits = 0b001 → 16-bit position.
        """
        # --- Clip and map user coords → 16-bit unsigned positions ----------
        def to_u16(v):
            v = max(-1.0, min(1.0, v))
            return int(round((v + 1.0) * 32767.5))  # 0…65535
        x_val, y_val = to_u16(x), to_u16(y)

        # --- Build bit lists: [C2,C1,C0,P15,…,P0] (MSB first) -------------
        ctrl = (0, 0, 1)
        x_bits = list(ctrl) + [(x_val >> i) & 1 for i in range(15, -1, -1)]
        y_bits = list(ctrl) + [(y_val >> i) & 1 for i in range(15, -1, -1)]
        assert len(x_bits) == len(y_bits) == 19

        buf = bytearray()

        for bit_idx in range(20):                      # 20 clocks
            # Data for clocks 0-18; clock 19 is a “dummy” with FS low
            x_bit = x_bits[bit_idx] if bit_idx < 19 else 0
            y_bit = y_bits[bit_idx] if bit_idx < 19 else 0
            fs    = 1 if bit_idx < 19 else 0           # FS high until last clock

            # Assemble lines |CLK|FS|X|Y| on D3..D0
            base = (y_bit << 0) | (x_bit << 1) | (fs << 2)

            # half-cycle 1: CLK = 0, data valid
            buf.append(base)
            # half-cycle 2: CLK rises to 1 → Mach-DSP latches on next fall
            buf.append(base | _CLOCK_BIT)

        return buf

    # ---------- HOUSE-KEEPING ----------
    def close(self):
        self.ft.set_bitmode(0, 0)  # reset
        self.ft.close()


# ---------------- EXAMPLE — DRAW A 4-POINT SQUARE ----------------
if __name__ == '__main__':
    galvo = MachDSPDriver()                 # open FT232H @ 1 MHz

    try:
        square = [(-1, -1), (+1, -1), (+1, +1), (-1, +1)]
        while True:                         # repeat forever (Ctrl-C to stop)
            for x, y in square:
                galvo.move(x, y)            # jump to corner
                time.sleep(0.5)             # dwell 0.5 s
    finally:
        galvo.close()
