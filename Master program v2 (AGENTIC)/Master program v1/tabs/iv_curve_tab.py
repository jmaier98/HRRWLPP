# iv_curve_tab.py
import os, time, numpy as np
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QPushButton,
    QFileDialog, QMessageBox, QLabel
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QDoubleValidator

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

DWELL_S_DEFAULT = 0.02  # settle time at each voltage before reading (s)
# Safe resistance display limits
R_MIN_OHM = 1e-3      # clamp |R| to at least 1 mΩ
R_MAX_OHM = 1e14     # clamp |R| to at most 1 TΩ
I_FLOOR_A = 1e-14     # treat |I| < I_FLOOR as ~0 to avoid div-by-zero

class IVWorker(QThread):
    progress = pyqtSignal(float, float)  # (I, V)
    finished = pyqtSignal(np.ndarray, np.ndarray, str)  # V, I, status
    status   = pyqtSignal(str)

    def __init__(self, k2400, start_V: float, stop_V: float, step_V: float, dwell_s: float = DWELL_S_DEFAULT):
        super().__init__()
        self.k = k2400
        self.start_V = float(start_V)
        self.stop_V  = float(stop_V)
        self.step_V  = abs(float(step_V)) if step_V != 0 else 0.01
        self.dwell_s = float(dwell_s)
        self._abort  = False

    def abort(self):
        self._abort = True

    def _build_sweep(self) -> np.ndarray:
        if self.start_V <= self.stop_V:
            step = +self.step_V
        else:
            step = -self.step_V
        # include stop point
        n = int(np.floor((self.stop_V - self.start_V) / step)) if step != 0 else 0
        pts = [self.start_V + i*step for i in range(n+1)]
        if len(pts) == 0 or abs(pts[-1] - self.stop_V) > 1e-12:
            pts.append(self.stop_V)
        return np.array(pts, dtype=float)

    def run(self):
        try:
            self.status.emit("Configuring instrument…")
            self.k.set_source_voltage_mode()
            self.k.output_on()

            sweep = self._build_sweep()
            V = np.zeros_like(sweep)
            I = np.zeros_like(sweep)

            self.status.emit(f"Starting sweep with {len(sweep)} points…")
            for idx, v in enumerate(sweep):
                if self._abort:
                    self.finished.emit(I[:idx], V[:idx], "aborted")
                    return
                self.k.set_voltage(float(v))
                time.sleep(self.dwell_s)
                iv = self.k.read_iv()
                if iv is None:
                    self.finished.emit(I[:idx], V[:idx], "read_error")
                    return
                curr, v_meas = iv  # format is CURR, VOLT
                V[idx] = v_meas
                I[idx] = curr
                self.progress.emit(I[idx], V[idx])

            self.finished.emit(V, I, "ok")
        except Exception as e:
            self.status.emit(f"Error: {e}")
            self.finished.emit(np.array([]), np.array([]), "exception")


class IVCurveTab(QWidget):
    """
    Drop-in tab:
      - Inputs: start V, stop V, step V, compliance (A), folder, filename
      - Buttons: Set Compliance, Browse, Start, Abort, Save
      - Live plot of I vs V during sweep
      - Saves: PNG of plot + TXT (np.savetxt) with metadata header
    """
    def __init__(self, instrument_manager, state, parent=None):
        super().__init__(parent)
        self.IM = instrument_manager
        self.state = state
        self.k = self.IM.get("Keithley2400")

        self._worker = None
        self._have_data = False
        self._V = np.array([])
        self._I = np.array([])

        # --- UI: controls ---
        self.start_edit = QLineEdit("0.0");   self._set_num(self.start_edit)
        self.stop_edit  = QLineEdit("1.0");   self._set_num(self.stop_edit)
        self.step_edit  = QLineEdit("0.05");  self._set_num(self.step_edit)

        self.comp_edit  = QLineEdit("1e-3");  self._set_num(self.comp_edit)
        self.comp_btn   = QPushButton("Set Compliance")

        self.folder_edit = QLineEdit("")
        self.browse_btn  = QPushButton("Browse…")
        self.file_edit   = QLineEdit("iv_sweep")

        self.start_btn   = QPushButton("Start Sweep")
        self.abort_btn   = QPushButton("Abort")
        self.save_btn    = QPushButton("Save")
        self.status_lbl  = QLabel("Idle")
        self.res_lbl = QLabel("R: —")

        self.abort_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

        form = QFormLayout()
        form.addRow("Start V", self.start_edit)
        form.addRow("Stop V",  self.stop_edit)
        form.addRow("Step V",  self.step_edit)

        comp_row = QHBoxLayout()
        comp_row.addWidget(self.comp_edit)
        comp_row.addWidget(self.comp_btn)
        form.addRow("Compliance (A)", comp_row)

        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit)
        folder_row.addWidget(self.browse_btn)
        form.addRow("Folder", folder_row)

        form.addRow("Filename (no ext.)", self.file_edit)

        btns = QHBoxLayout()
        btns.addWidget(self.start_btn)
        btns.addWidget(self.abort_btn)
        btns.addWidget(self.save_btn)

        left = QVBoxLayout()
        left.addLayout(form)
        left.addLayout(btns)
        left.addWidget(self.status_lbl)
        left.addWidget(self.res_lbl)

        # --- Plot ---
        self.fig = Figure(figsize=(5,4), dpi=100)
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Voltage (V)")
        self.ax.set_ylabel("Current (A)")
        self.line, = self.ax.plot([], [], marker='o', linestyle='-')
        self.ax.grid(True)

        layout = QHBoxLayout(self)
        layout.addLayout(left, stretch=0)
        layout.addWidget(self.canvas, stretch=1)
        self.setLayout(layout)

        # --- Signals ---
        self.browse_btn.clicked.connect(self._on_browse)
        self.comp_btn.clicked.connect(self._on_set_compliance)
        self.start_btn.clicked.connect(self._on_start)
        self.abort_btn.clicked.connect(self._on_abort)
        self.save_btn.clicked.connect(self._on_save)

    # ---------------- helpers ----------------
    def _set_num(self, line: QLineEdit):
        v = QDoubleValidator(); v.setNotation(QDoubleValidator.Notation.StandardNotation)
        line.setValidator(v)

    def _on_browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", self.folder_edit.text() or "")
        if folder:
            self.folder_edit.setText(folder)

    def _on_set_compliance(self):
        try:
            c = float(self.comp_edit.text())
            if self.k is None:
                QMessageBox.warning(self, "Keithley", "Keithley2400 not found.")
                return
            self.k.set_current_compliance(c)
            QMessageBox.information(self, "Keithley", f"Compliance set to {c:.3e} A")
        except Exception as e:
            QMessageBox.critical(self, "Compliance Error", str(e))

    def _on_start(self):
        if self.k is None:
            QMessageBox.warning(self, "Keithley", "Keithley2400 not found.")
            return
        try:
            sv = float(self.start_edit.text())
            ev = float(self.stop_edit.text())
            st = float(self.step_edit.text())
            if st == 0:
                QMessageBox.warning(self, "Input", "Step must be non-zero.")
                return
        except ValueError:
            QMessageBox.warning(self, "Input", "Please enter valid numbers.")
            return

        # UI state
        self._have_data = False
        self._V = np.array([]); self._I = np.array([])
        self.save_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.abort_btn.setEnabled(True)
        self.status_lbl.setText("Running…")
        self.res_lbl.setText("R: —")

        # reset plot
        self.line.set_data([], [])
        self.ax.relim(); self.ax.autoscale_view()
        self.canvas.draw_idle()

        # start worker
        self._worker = IVWorker(self.k, sv, ev, st, DWELL_S_DEFAULT)
        self._worker.progress.connect(self._on_point)
        self._worker.status.connect(self._set_status)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_abort(self):
        if self._worker and self._worker.isRunning():
            self._worker.abort()
            self._set_status("Aborting…")
            self.abort_btn.setEnabled(False)

    def _on_point(self, v, i):
        # append and update plot
        self._V = np.append(self._V, v)  # x-axis (Voltage)
        self._I = np.append(self._I, i)  # y-axis (Current)
        self.line.set_data(self._V, self._I)
        self.ax.relim(); self.ax.autoscale_view()
        self.canvas.draw_idle()

        # update resistance indicator safely
        r = self._safe_resistance(v, i)
        self.res_lbl.setText(f"R: {self._fmt_ohms(r)}")

    def _on_finished(self, V, I, status):
        self._worker = None
        self.start_btn.setEnabled(True)
        self.abort_btn.setEnabled(False)
        self._have_data = (len(V) > 0)
        self.save_btn.setEnabled(self._have_data)

        if status == "ok":
            self._set_status("Done.")
        elif status == "aborted":
            self._set_status("Aborted.")
        elif status == "read_error":
            self._set_status("Stopped (read error).")
        else:
            self._set_status("Stopped.")

        # ensure plot reflects final arrays
        if len(V) and len(I):
            self._V, self._I = np.array(V), np.array(I)
            self.line.set_data(self._I, self._V)
            self.ax.relim(); self.ax.autoscale_view()
            self.canvas.draw_idle()

    def _set_status(self, text: str):
        self.status_lbl.setText(text)

    def _on_save(self):
        if not self._have_data or len(self._V) == 0:
            QMessageBox.information(self, "Save", "No data to save yet.")
            return

        folder = self.folder_edit.text().strip()
        fname  = self.file_edit.text().strip() or "iv_sweep"
        if not folder:
            QMessageBox.warning(self, "Save", "Please select a folder.")
            return
        os.makedirs(folder, exist_ok=True)

        txt_path = os.path.join(folder, f"{fname}.txt")
        png_path = os.path.join(folder, f"{fname}.png")

        # metadata header (np.savetxt will prefix with '# ')
        try:
            idn = self.k.identify() if self.k else "Unknown"
        except Exception:
            idn = "Unknown"

        header_lines = [
            f"IV sweep saved: {datetime.now().isoformat()}",
            f"Instrument: {idn}",
            f"Start_V={self.start_edit.text()}",
            f"Stop_V={self.stop_edit.text()}",
            f"Step_V={self.step_edit.text()}",
            f"Dwell_s={DWELL_S_DEFAULT}",
            f"Compliance_A={self.comp_edit.text()}",
            "Columns: V (V), I (A)"
        ]
        header = "\n".join(header_lines)

        try:
            data = np.column_stack([self._V, self._I])
            np.savetxt(txt_path, data, header=header)
            self.fig.savefig(png_path, dpi=150, bbox_inches="tight")
            print(f"Saved:\n{txt_path}\n{png_path}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))
    def _fmt_ohms(self, r: float) -> str:
        """Engineering format for ohms with sign."""
        if not np.isfinite(r):
            return "—"
        s = "-" if r < 0 else ""
        x = abs(r)
        if x >= 1e12: return f"{s}{x/1e12:.3g} TΩ"
        if x >= 1e9:  return f"{s}{x/1e9:.3g} GΩ"
        if x >= 1e6:  return f"{s}{x/1e6:.3g} MΩ"
        if x >= 1e3:  return f"{s}{x/1e3:.3g} kΩ"
        return f"{s}{x:.3g} Ω"

    def _safe_resistance(self, v: float, i: float) -> float:
        """
        Compute R = V/I safely:
        - if |I| < I_FLOOR_A → use sign(v) * R_MAX_OHM
        - clamp |R| into [R_MIN_OHM, R_MAX_OHM]
        - return finite number; caller formats it
        """
        if not (np.isfinite(v) and np.isfinite(i)):
            return np.nan
        if abs(i) < I_FLOOR_A:
            r = (1.0 if v >= 0 else -1.0) * R_MAX_OHM
        else:
            r = v / i
        # clamp magnitude
        mag = abs(r)
        if mag < R_MIN_OHM:
            r = (1.0 if r >= 0 else -1.0) * R_MIN_OHM
        elif mag > R_MAX_OHM:
            r = (1.0 if r >= 0 else -1.0) * R_MAX_OHM
        return r
