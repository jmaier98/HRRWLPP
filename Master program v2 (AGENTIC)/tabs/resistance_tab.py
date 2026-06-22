# tabs/resistance_tab.py
from __future__ import annotations
import math
import time

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLineEdit, QLabel, QMessageBox, QGroupBox, QComboBox,
)
from PyQt6.QtGui import QDoubleValidator, QIntValidator, QFont


# ── Worker ────────────────────────────────────────────────────────────────────

class _ReaderWorker(QThread):
    """Continuously polls the lock-in and emits (raw_value, resistance) pairs."""
    reading  = pyqtSignal(float, float)   # raw_value, resistance
    finished = pyqtSignal(bool, str)      # user_abort, message

    def __init__(self, read_fn, shunt_ohms: float, applied_voltage: float,
                 interval_ms: int = 100):
        super().__init__()
        self._read_fn       = read_fn
        self._Rshunt        = float(shunt_ohms)
        self._applied_V     = float(applied_voltage)
        self._interval      = max(10, int(interval_ms)) / 1000.0
        self._stop_flag     = False

    def stop(self):
        self._stop_flag = True

    @staticmethod
    def _calc_resistance(lockin_value: float, Rshunt: float,
                         applied_voltage: float) -> float:
        eps = 1e-12
        V = abs(lockin_value)
        return Rshunt * (V / max(applied_voltage - V, eps))

    def run(self):
        try:
            while not self._stop_flag:
                try:
                    val = float(self._read_fn())
                except Exception as e:
                    self.finished.emit(True, f"Lock-in read error: {e}")
                    return
                Rcalc = self._calc_resistance(val, self._Rshunt, self._applied_V)
                self.reading.emit(val, Rcalc)
                time.sleep(self._interval)
            self.finished.emit(True, "Stopped by user.")
        except Exception as e:
            self.finished.emit(True, f"Unexpected error: {e}")


# ── Tab ───────────────────────────────────────────────────────────────────────

class ResistanceTab(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.lockin     = controller.get_instrument("SR830")

        outer = QVBoxLayout(self)

        # ── Controls ──────────────────────────────────────────────────────────
        ctrl_box = QGroupBox("Controls")
        form = QFormLayout(ctrl_box)

        dv_pos = QDoubleValidator(0.0,  1e12, 9, self)
        iv_ms  = QIntValidator(10, 60000, self)

        self.lockin_combo = QComboBox()
        self.lockin_combo.addItems(["Lock-in 1 (GPIB 18)", "Lock-in 2 (GPIB 8)"])

        self.shunt_entry   = QLineEdit("1000000")
        self.shunt_entry.setValidator(dv_pos)
        self.shunt_entry.setToolTip("Shunt resistance in ohms.")

        self.appliedV_entry = QLineEdit("0.01")
        self.appliedV_entry.setValidator(dv_pos)
        self.appliedV_entry.setToolTip("Applied voltage across the device (V).")

        self.period_entry  = QLineEdit("100")
        self.period_entry.setValidator(iv_ms)
        self.period_entry.setToolTip("Polling interval in milliseconds.")

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)

        form.addRow("Lock-in:",             self.lockin_combo)
        form.addRow("Shunt R (Ohm):",       self.shunt_entry)
        form.addRow("Applied voltage (V):", self.appliedV_entry)
        form.addRow("Read period (ms):",    self.period_entry)
        form.addRow(btn_row)

        # ── Readout ───────────────────────────────────────────────────────────
        readout_box = QGroupBox("Live Readout")
        readout_lay = QVBoxLayout(readout_box)

        big = QFont()
        big.setPointSize(56)
        big.setBold(True)

        self.raw_title = QLabel("Lock-in (X):")
        self.raw_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.raw_lbl   = QLabel("–")
        self.raw_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.raw_lbl.setFont(big)
        self.raw_units = QLabel("V")
        self.raw_units.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.R_title = QLabel("Calculated R:")
        self.R_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.R_lbl   = QLabel("–")
        self.R_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.R_lbl.setFont(big)
        self.R_units = QLabel("Ohm")
        self.R_units.setAlignment(Qt.AlignmentFlag.AlignCenter)

        readout_lay.addWidget(self.raw_title)
        readout_lay.addWidget(self.raw_lbl)
        readout_lay.addWidget(self.raw_units)
        readout_lay.addSpacing(12)
        readout_lay.addWidget(self.R_title)
        readout_lay.addWidget(self.R_lbl)
        readout_lay.addWidget(self.R_units)

        self.status_lbl = QLabel("Idle.")
        self.status_lbl.setStyleSheet("color: #555;")

        outer.addWidget(ctrl_box)
        outer.addWidget(readout_box)
        outer.addWidget(self.status_lbl)

        # ── Connections ───────────────────────────────────────────────────────
        self.worker: _ReaderWorker | None = None
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _start(self):
        if self.lockin is None:
            QMessageBox.warning(self, "No lock-in", "SR830 not available.")
            return
        try:
            Rshunt    = float(self.shunt_entry.text())
            period_ms = int(self.period_entry.text())
            appliedV  = float(self.appliedV_entry.text())
        except ValueError:
            QMessageBox.warning(self, "Input error", "Please enter valid numbers.")
            return
        if Rshunt <= 0:
            QMessageBox.warning(self, "Input error", "Shunt resistance must be > 0.")
            return

        read_fn = self.lockin.read_x if self.lockin_combo.currentIndex() == 0 \
                  else self.lockin.read_x2

        self.worker = _ReaderWorker(read_fn, Rshunt, appliedV, period_ms)
        self.worker.reading.connect(self._on_reading,  Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Reading…")

    def _stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.status_lbl.setText("Stopping…")

    # ── Worker handlers ───────────────────────────────────────────────────────

    def _on_reading(self, raw: float, R: float):
        self.raw_lbl.setText(self._fmt_eng(raw) + "V")
        self.R_lbl.setText(self._fmt_eng(R) + "Ohm")

    def _on_finished(self, user_abort: bool, message: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText(message)
        self.worker = None

    # ── Formatting ────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_eng(x: float) -> str:
        if x == 0 or x != x:
            return "0"
        absx = abs(x)
        exp  = int(math.floor(math.log10(absx) / 3) * 3)
        exp  = max(-12, min(12, exp))
        val  = x / (10 ** exp)
        prefix = {-12: "p", -9: "n", -6: "u", -3: "m",
                   0: "", 3: "k", 6: "M", 9: "G", 12: "T"}.get(exp, "")
        return f"{val:.3g} {prefix}"

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
        event.accept()
