# live_lockin_tab.py
from __future__ import annotations
import time
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QGroupBox
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator, QFont


class _ReaderWorker(QThread):
    """Continuously polls the lock-in and emits (raw_value, resistance) tuples."""
    reading = pyqtSignal(float, float)      # raw_value, resistance
    finished = pyqtSignal(bool, str)        # user_abort, message

    def __init__(self, lockin, shunt_ohms: float, applied_voltage: float, interval_ms: int = 100):
        super().__init__()
        self._lockin = lockin
        self._Rshunt = float(shunt_ohms)
        self._interval = max(10, int(interval_ms)) / 1000.0
        self._applied_voltage = float(applied_voltage)
        self._stop = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _calc_resistance(lockin_value: float, Rshunt: float, applied_voltage: float) -> float:
        """
        DUMMY FORMULA — REPLACE WITH YOUR OWN.
        Currently treats the lock-in reading as a current in Amps and returns
        R = V/I with V assumed to be (I * Rshunt) -> so returns Rshunt / max(eps, I).
        Edit this to match your actual wiring/measurement model.
        """
        eps = 1e-12  # avoid division by zero
        V = abs(lockin_value)
        return Rshunt*(V/max(applied_voltage-V,eps)) 

    def run(self):
        try:
            while not self._stop:
                try:
                    # Choose the lock-in quantity you want to display:
                    # val = float(self._lockin.read_r())   # magnitude
                    val = float(self._lockin.read_x())     # in-phase (default)
                except Exception as e:
                    self.finished.emit(True, f"Lock-in read error: {e}")
                    return

                Rcalc = self._calc_resistance(val, self._Rshunt, self._applied_voltage)
                self.reading.emit(val, Rcalc)
                time.sleep(self._interval)

            self.finished.emit(True, "Stopped by user.")
        except Exception as e:
            self.finished.emit(True, f"Unexpected error: {e}")


class LiveLockinTab(QWidget):
    """
    Live lock-in readout with resistance calculation.
    Required InstrumentManager key:
      • "SR830" (or adapt name below) exposing .read_x() or .read_r()
    """
    def __init__(self, instrument_manager, state=None):
        super().__init__()
        self.IM = instrument_manager
        self.state = state

        # --- Instruments ----------------------------------------------------
        try:
            self.lockin = self.IM.get("SR830")
        except KeyError:
            # Fall back to another common key name if you use a different label
            try:
                self.lockin = self.IM.get("Lockin")
            except KeyError as err:
                raise RuntimeError(
                    f"InstrumentManager is missing a lock-in (tried 'SR830' and 'Lockin'). "
                    f"Add it before constructing LiveLockinTab. Missing: {err.args[0]}"
                )

        # --- UI -------------------------------------------------------------
        outer = QVBoxLayout(self)

        # Controls
        controls_box = QGroupBox("Controls")
        controls_layout = QFormLayout(controls_box)

        dv_pos = QDoubleValidator(bottom=0.0, top=1e12, decimals=9, parent=self)
        dv_any = QDoubleValidator(bottom=-1e300, top=1e300, decimals=9, parent=self)
        iv_ms  = QIntValidator(10, 60000, self)  # 10 ms .. 60 s

        self.shunt_entry = QLineEdit("1000000")   # Ohms
        self.shunt_entry.setValidator(dv_pos)
        self.shunt_entry.setToolTip("Shunt resistance in ohms (used by your calculation).")

        self.appliedV_entry = QLineEdit("0.01")   # volts
        self.appliedV_entry.setValidator(dv_pos)
        self.appliedV_entry.setToolTip("Applied voltage across the device (V).")

        self.period_ms_entry = QLineEdit("100")  # poll every 100 ms
        self.period_ms_entry.setValidator(iv_ms)
        self.period_ms_entry.setToolTip("Polling interval in milliseconds.")

        buttons_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        buttons_row.addWidget(self.start_btn)
        buttons_row.addWidget(self.stop_btn)

        controls_layout.addRow("Shunt R (Ω):", self.shunt_entry)
        controls_layout.addRow("Applied Voltage (V):", self.appliedV_entry)
        controls_layout.addRow("Read period (ms):", self.period_ms_entry)
        controls_layout.addRow(buttons_row)

        # Readouts
        readout_box = QGroupBox("Live Readout")
        readout_layout = QVBoxLayout(readout_box)

        # Big number styles
        big_font = QFont()
        big_font.setPointSize(56)
        big_font.setBold(True)

        # Raw lock-in value
        self.raw_label_title = QLabel("Lock-in (X):")
        self.raw_label_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.raw_label = QLabel("–")
        self.raw_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.raw_label.setFont(big_font)
        self.raw_units = QLabel("V")
        self.raw_units.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Calculated resistance
        self.R_label_title = QLabel("Calculated R:")
        self.R_label_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.R_label = QLabel("–")
        self.R_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.R_label.setFont(big_font)
        self.R_units = QLabel("Ω")
        self.R_units.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Assemble readouts
        readout_layout.addWidget(self.raw_label_title)
        readout_layout.addWidget(self.raw_label)
        readout_layout.addWidget(self.raw_units)
        readout_layout.addSpacing(12)
        readout_layout.addWidget(self.R_label_title)
        readout_layout.addWidget(self.R_label)
        readout_layout.addWidget(self.R_units)

        # Status line
        self.status_lbl = QLabel("Idle.")
        self.status_lbl.setStyleSheet("color: #555;")

        outer.addWidget(controls_box)
        outer.addWidget(readout_box)
        outer.addWidget(self.status_lbl)

        # --- Runtime --------------------------------------------------------
        self.worker: _ReaderWorker | None = None

        # Connect buttons
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #
    def _start(self):
        # Parse inputs
        try:
            Rshunt = float(self.shunt_entry.text())
            period_ms = int(self.period_ms_entry.text())
            appliedV = float(self.appliedV_entry.text())
        except ValueError:
            QMessageBox.warning(self, "Input error", "Please enter valid numbers.")
            return

        if Rshunt <= 0:
            QMessageBox.warning(self, "Input error", "Shunt resistance must be > 0.")
            return

        # Start worker
        self.worker = _ReaderWorker(self.lockin, Rshunt, applied_voltage=appliedV, interval_ms=period_ms)
        self.worker.reading.connect(self._on_reading, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Reading…")

    def _stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.status_lbl.setText("Stopping…")

    def _on_reading(self, raw_value: float, Rcalc: float):
        # Format thoughtfully for readability
        self.raw_label.setText(self._fmt_eng(raw_value)+"V")
        self.R_label.setText(self._fmt_eng(Rcalc)+"Ω")

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self.worker = None

    @staticmethod
    def _fmt_eng(x: float) -> str:
        """Engineering notation for clean big-number display."""
        if x == 0 or not (x == x):  # NaN safe
            return "0"
        absx = abs(x)
        # Choose unit prefix
        prefixes = [
            (1e-12, "p"), (1e-9, "n"), (1e-6, "µ"), (1e-3, "m"),
            (1, ""), (1e3, "k"), (1e6, "M"), (1e9, "G"), (1e12, "T")
        ]
        # Find closest bucket
        for scale, _ in prefixes:
            pass
        # Determine exponent bucket
        exp = 0
        if absx > 0:
            import math
            exp = int(math.floor(math.log10(absx) / 3) * 3)
            exp = max(-12, min(12, exp))
        value = x / (10 ** exp)
        # Map exponent to prefix
        prefix_map = {
            -12: "p", -9: "n", -6: "µ", -3: "m",
             0: "",   3: "k",  6: "M",  9: "G", 12: "T"
        }
        prefix = prefix_map.get(exp, "")
        # 3 sig figs is a nice compromise for live readouts
        return f"{value:.3g} {prefix}"

    # Ensure the worker is stopped on close
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
        event.accept()
