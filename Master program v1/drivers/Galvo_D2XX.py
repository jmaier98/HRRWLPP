# drivers/Galvo_D2XX.py
import time
from ftd2xx import ftd2xx as ftd

PIN_MASK = 0x1F  # D0..D4 used
PURGE_RX = 0x01  # D2XX purge flags: RX=1
PURGE_TX = 0x02  # TX=2

def _to_str(x):
    return x.decode(errors="ignore") if isinstance(x, (bytes, bytearray)) else x

def moveXY(x, y):
    x = max(min(x, 1.0), -1.0)
    y = max(min(y, 1.0), -1.0)
    x = int(x * 131071) + 131072
    y = int(y * 131071) + 131072
    xbits = f"{x:018b}"
    ybits = f"{y:018b}"

    seq = []
    # enable / preamble
    seq.append((1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4))
    seq.append((1 << 0) | (1 << 1) | (1 << 2) | (0 << 3) | (1 << 4))
    # 18 bits with CLK hi/lo per bit
    for i in range(18):
        xb = int(xbits[i]); yb = int(ybits[i])
        seq.append((yb << 0) | (xb << 1) | (0 << 2) | (1 << 3) | (1 << 4))  # clk=1
        seq.append((yb << 0) | (xb << 1) | (0 << 2) | (0 << 3) | (1 << 4))  # clk=0
    # post / disable
    seq.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3) | (0 << 4))
    seq.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3) | (0 << 4))
    return bytes(seq)  # exactly 40 bytes

class GALVO:
    """
    FTDI D2XX-based galvo driver (async bit-bang on ADBUS).
    - Auto-selects the FT232H "Single RS232-HS" (your index 0).
    - No libusb/pyusb, so it coexists with the SP2500 on VCP.
    """
    def __init__(self, *, frequency=200_000, pin_mask=PIN_MASK,
                 prefer_type=8, prefer_desc_substr="Single RS232-HS",
                 force_index=None):
        self.pin_mask = pin_mask & 0xFF
        self.frequency = int(frequency)
        self.dev = self._open_device(prefer_type, prefer_desc_substr, force_index)

        # Low-latency config (wrap “nice-to-have” calls)
        self._safe(self.dev.resetDevice)
        self._safe(self.dev.purge, PURGE_RX | PURGE_TX)
        self._safe(self.dev.setTimeouts, 100, 100)       # read, write (ms)
        self._safe(self.dev.setLatencyTimer, 1)          # many devices accept 1–16ms; ignore error if not
        self._safe(self.dev.setUSBParameters, 64, 64)    # TX/RX USB packet sizes; ignore if not supported

        # Async bit-bang on ADBUS (mask selects outputs), mode=0x01
        self.dev.setBitMode(self.pin_mask, 0x01)

        # BAUD ≈ frequency*16 (async bit-bang updates pins at BAUD/16)
        target_baud = int(min(max(self.frequency * 16, 9600), 12_000_000))
        self.dev.setBaudRate(target_baud)

        # Initialize pins low
        self.write_gpio(0x00)
        print(f"Galvo (D2XX) online | baud={target_baud} (~{target_baud//16} Hz) | mask=0x{self.pin_mask:02X}")

    def _open_device(self, prefer_type, prefer_desc_substr, force_index):
        n = ftd.createDeviceInfoList()
        if n <= 0:
            raise RuntimeError("FTDI: No devices found for GALVO (D2XX).")

        if force_index is not None:
            idx = int(force_index)
            if not (0 <= idx < n):
                raise IndexError(f"FTDI index out of range: {idx} (found {n})")
            return ftd.open(idx)

        # Score devices: prefer FT232H (type==8) and description match
        best_idx, best_score, infos = None, -1, []
        for i in range(n):
            info = ftd.getDeviceInfoDetail(i)
            dev_type = info.get('type')
            desc = _to_str(info.get('description') or '')
            serial = _to_str(info.get('serial') or '')
            loc = info.get('location')

            score = 0
            if dev_type == prefer_type:  # FT232H
                score += 3
            if prefer_desc_substr and prefer_desc_substr.lower() in desc.lower():
                score += 2
            if serial == "":  # your galvo shows empty serial
                score += 1

            infos.append((i, dev_type, desc, serial, loc, score))
            if score > best_score:
                best_idx, best_score = i, score

        if best_idx is None:
            best_idx = 0

        try:
            i, dev_type, desc, serial, loc, score = next(x for x in infos if x[0] == best_idx)
            print(f"Selected FTDI index {i} (type={dev_type}, desc='{desc}', serial='{serial}', location={loc}, score={score})")
        except Exception:
            pass

        return ftd.open(best_idx)

    # ---- GPIO ----
    def write_gpio(self, value: int):
        self.dev.write(bytes([value & self.pin_mask]))

    # ---- Frame I/O ----
    def send_frame_atomic(self, frame_data: bytes) -> bool:
        if len(frame_data) != 40:
            raise ValueError(f"Frame must be exactly 40 bytes, got {len(frame_data)}")
        self._safe(self.dev.purge, PURGE_TX)
        written = self.dev.write(frame_data)
        return written == 40

    def send_frame_with_verification(self, frame_data: bytes) -> bool:
        if len(frame_data) != 40:
            raise ValueError(f"Frame must be exactly 40 bytes, got {len(frame_data)}")
        self._safe(self.dev.purge, PURGE_TX)
        t0 = time.perf_counter()
        ok = self.send_frame_atomic(frame_data)
        dt_ms = (time.perf_counter() - t0) * 1e3
        if dt_ms > 5.0:
            print(f"Warning: 40B transfer took {dt_ms:.2f} ms.")
        return ok

    # ---- Public API ----
    def move(self, x: float, y: float) -> bool:
        return self.send_frame_with_verification(moveXY(x, y))

    def close(self):
        # Return FTDI to normal mode and release
        self._safe(self.dev.setBitMode, 0x00, 0x00)   # reset to normal
        self._safe(self.dev.purge, PURGE_RX | PURGE_TX)
        self._safe(self.dev.close)

    # ---- helpers ----
    @staticmethod
    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return None


