# SP2500.py
import serial
import threading
import time
import re
from typing import Optional

# ---------- Editable defaults ----------
PORT_DEFAULT       = "COM5"
BAUD_DEFAULT       = 9600
TIMEOUT_S_DEFAULT  = 1.0

class SP2500:
    """
    Minimal driver for the Acton SP2500 monochromator over RS-232.
    Provides nm position, slew rate control, wavelength set, and grating control.
    Thread-safe for multi-threaded use.
    """

    def __init__(
        self,
        port: str = PORT_DEFAULT,
        baudrate: int = BAUD_DEFAULT,
        timeout_s: float = TIMEOUT_S_DEFAULT
    ):
        try:
            self._ser = serial.Serial(port, baudrate, timeout=timeout_s)
            time.sleep(2.0)  # Allow link to settle
        except Exception as e:
            print(f"SP2500 init error: {e}")
            raise

        self._lock = threading.Lock()
        print(f"SP2500 online")

    # ---------------- Basic identity / raw I/O ----------------
    def identify(self) -> str:
        """Query instrument identity if supported (returns empty if not)."""
        try:
            return self.query("*IDN?")
        except Exception:
            # Many Acton spectrometers don’t implement *IDN?
            return "SP2500"

    def write(self, cmd: str):
        """Raw command write (with CR termination)."""
        with self._lock:
            self._ser.write(f"{cmd}\r".encode())

    def query(self, cmd: str, wait: float = 0.5, read_bytes: int = 200) -> str:
        """Send command and return response string."""
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(f"{cmd}\r".encode())
            time.sleep(wait)
            resp = self._ser.read(read_bytes).decode(errors="ignore")
            return resp.strip()

    # ---------------- Wavelength control ----------------
    def get_wavelength_nm(self) -> Optional[float]:
        """Return current wavelength in nm, or None if parse fails."""
        resp = self.query("?NM")
        m = re.search(r"\d+\.\d+", resp)
        return float(m.group()) if m else None

    def set_slew_rate_nm_per_min(self, rate: float) -> float:
        """Set wavelength slew rate in nm/min."""
        self.write(f"{rate} NM/MIN")
        return rate

    def set_wavelength_nm(self, nm: float, timeout_attempts: int = 100) -> float:
        """Command wavelength move and block until 'ok' received."""
        cmd = f"{nm:.2f} NM"
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(f"{cmd}\r".encode())
            time.sleep(0.5)
            for attempt in range(timeout_attempts):
                resp = self._ser.read(100).decode(errors="ignore").lower()
                if "ok" in resp:
                    return nm
                time.sleep(0.5)
        raise TimeoutError(f"SP2500 did not confirm move to {nm:.2f} nm.")

    # ---------------- Grating control ----------------
    def list_gratings(self) -> str:
        """Return text listing of all installed gratings."""
        return self.query("?GRATINGS", wait=0.5, read_bytes=500)

    def get_selected_grating(self) -> Optional[int]:
        """Return currently selected grating number (1–9)."""
        resp = self.query("?GRATING", wait=0.2, read_bytes=50)
        m = re.search(r"\d+", resp)
        return int(m.group()) if m else None

    def switch_grating(self, grating_number: int, turret: Optional[int] = None) -> bool:
        """Switch grating (and optionally turret). Returns True if successful."""
        if turret is not None:
            self.write(f"{turret} TURRET")
            time.sleep(1.0)
        self.write(f"{grating_number} GRATING")
        time.sleep(5.0)
        return self.get_selected_grating() == grating_number

    # ---------------- Cleanup ----------------
    def close(self):
        try:
            self._ser.close()
        except Exception:
            pass


# Example usage
if __name__ == "__main__":
    spec = SP2500(port="COM5")
    print("ID:", spec.identify())
    wl = spec.get_wavelength_nm()
    print(f"Current wavelength: {wl} nm")
    # spec.set_wavelength_nm(600.0)
    # spec.switch_grating(2)
    spec.close()

