# drivers/Galvo_D2XX.py
import time
import threading
from ftd2xx import ftd2xx as ftd

PIN_MASK = 0x1F   # D0..D4 used as outputs
PURGE_RX = 0x01
PURGE_TX = 0x02

# Public API uses milliunits (1 mu = 0.001 V).
# Hardware accepts ±0.5 V, so the public limit is ±500 mu.
GALVO_LIMIT_MU = 500    # public hard limit in milliunits
_GALVO_LIMIT_V = 0.5    # internal hardware limit in volts


def _to_str(x):
    return x.decode(errors="ignore") if isinstance(x, (bytes, bytearray)) else x


def _build_frame(x_v: float, y_v: float) -> bytes:
    """
    Encode a (x, y) position in **volts** (already clamped) as a 40-byte
    FTDI bit-bang frame.  Internal only — callers must clamp before calling.
    """
    x_v = max(min(float(x_v), _GALVO_LIMIT_V), -_GALVO_LIMIT_V)
    y_v = max(min(float(y_v), _GALVO_LIMIT_V), -_GALVO_LIMIT_V)
    x, y = x_v, y_v
    xi = int(x * 131071) + 131072
    yi = int(y * 131071) + 131072
    xb = f"{xi:018b}"
    yb = f"{yi:018b}"

    seq = []
    seq.append((1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4))  # preamble hi
    seq.append((1 << 0) | (1 << 1) | (1 << 2) | (0 << 3) | (1 << 4))  # preamble lo
    for i in range(18):
        xbit = int(xb[i])
        ybit = int(yb[i])
        seq.append((ybit << 0) | (xbit << 1) | (0 << 2) | (1 << 3) | (1 << 4))  # clk hi
        seq.append((ybit << 0) | (xbit << 1) | (0 << 2) | (0 << 3) | (1 << 4))  # clk lo
    seq.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3) | (0 << 4))  # post hi
    seq.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3) | (0 << 4))  # post lo
    return bytes(seq)  # exactly 40 bytes


class GALVO:
    """
    FTDI D2XX-based galvo driver.

    Position state is kept in ``state.settings["galvo_x_position"]`` and
    ``state.settings["galvo_y_position"]`` so the rest of the program always
    has an accurate view of where the galvo is.

    Public API (three methods only):
        move_xy(x, y)  — move both axes
        move_x(x)      — move X only; reads current Y from state
        move_y(y)      — move Y only; reads current X from state
    """

    def __init__(self, state, *, frequency=5_000, pin_mask=PIN_MASK,
                 prefer_type=8, prefer_desc_substr="Single RS232-HS",
                 force_index=None):
        self._state    = state
        self.pin_mask  = pin_mask & 0xFF
        self.frequency = int(frequency)
        self._lock     = threading.RLock()

        self.dev = self._open_device(prefer_type, prefer_desc_substr, force_index)

        self._safe(self.dev.resetDevice)
        self._safe(self.dev.purge, PURGE_RX | PURGE_TX)
        self._safe(self.dev.setTimeouts, 100, 100)
        self._safe(self.dev.setLatencyTimer, 1)
        self._safe(self.dev.setUSBParameters, 64, 64)

        self.dev.setBitMode(self.pin_mask, 0x01)  # async bit-bang

        target_baud = int(min(max(frequency * 16, 9600), 12_000_000))
        self.dev.setBaudRate(target_baud)

        self._write_gpio(0x00)
        print(f"Galvo (D2XX) online | baud={target_baud} (~{target_baud // 16} Hz) | "
              f"mask=0x{self.pin_mask:02X}")

    # ── Public API ────────────────────────────────────────────────────────────

    def move_xy(self, x_mu: float, y_mu: float) -> bool:
        """
        Move both axes.  x_mu and y_mu are in **milliunits** (1 mu = 0.001 V).
        Valid range: ±500 mu  (= ±0.5 V hardware limit).
        State is updated with the clamped milliunit values.
        """
        x_mu = max(min(float(x_mu), GALVO_LIMIT_MU), -GALVO_LIMIT_MU)
        y_mu = max(min(float(y_mu), GALVO_LIMIT_MU), -GALVO_LIMIT_MU)
        x_v  = x_mu / 1000.0
        y_v  = y_mu / 1000.0
        with self._lock:
            frame = _build_frame(x_v, y_v)
            self._safe(self.dev.purge, PURGE_TX)
            t0 = time.perf_counter()
            written = self.dev.write(frame)
            dt_ms = (time.perf_counter() - t0) * 1e3
            if dt_ms > 5.0:
                print(f"[Galvo] Warning: 40 B transfer took {dt_ms:.2f} ms.")
            ok = (written == 40)
            if ok:
                self._state.settings["galvo_x_position"] = x_mu
                self._state.settings["galvo_y_position"] = y_mu
            return ok

    def move_x(self, x_mu: float) -> bool:
        """Move X only (milliunits); current Y is read from state."""
        y_mu = float(self._state.settings.get("galvo_y_position", 0.0))
        return self.move_xy(x_mu, y_mu)

    def move_y(self, y_mu: float) -> bool:
        """Move Y only (milliunits); current X is read from state."""
        x_mu = float(self._state.settings.get("galvo_x_position", 0.0))
        return self.move_xy(x_mu, y_mu)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _write_gpio(self, value: int):
        self.dev.write(bytes([value & self.pin_mask]))

    def _open_device(self, prefer_type, prefer_desc_substr, force_index):
        n = ftd.createDeviceInfoList()
        if n <= 0:
            raise RuntimeError("FTDI: no devices found for GALVO (D2XX).")

        if force_index is not None:
            idx = int(force_index)
            if not (0 <= idx < n):
                raise IndexError(f"FTDI index out of range: {idx} (found {n})")
            return ftd.open(idx)

        best_idx, best_score, infos = None, -1, []
        for i in range(n):
            info     = ftd.getDeviceInfoDetail(i)
            dev_type = info.get("type")
            desc     = _to_str(info.get("description") or "")
            serial   = _to_str(info.get("serial") or "")
            loc      = info.get("location")

            score = 0
            if dev_type == prefer_type:
                score += 3
            if prefer_desc_substr and prefer_desc_substr.lower() in desc.lower():
                score += 2
            if serial == "":
                score += 1

            infos.append((i, dev_type, desc, serial, loc, score))
            if score > best_score:
                best_idx, best_score = i, score

        if best_idx is None:
            best_idx = 0

        i, dev_type, desc, serial, loc, _ = next(x for x in infos if x[0] == best_idx)
        print(f"[Galvo] Selected FTDI index {i} "
              f"(type={dev_type}, desc='{desc}', serial='{serial}', loc={loc})")
        return ftd.open(best_idx)

    @staticmethod
    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return None

    def close(self):
        self._safe(self.dev.setBitMode, 0x00, 0x00)
        self._safe(self.dev.purge, PURGE_RX | PURGE_TX)
        self._safe(self.dev.close)
