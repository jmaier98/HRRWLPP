# averaged_scan_tab.py
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLineEdit, QMessageBox
)
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QDoubleValidator, QIntValidator

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


class AveragedScanTab(QWidget):
    """
    Tab that performs repeated stage sweeps while reading SR830 X and
    live-plots both the current sweep and the running average.
    ------------------------------------------------------------------
    Required InstrumentManager keys (rename if you use different ones):
        • "ESP"   → ESP300Controller   (must expose .set_speed, .move_absolute,
                                        .is_moving, .get_position)
        • "SR830" → SR830 lock-in      (must expose .read_x)
    """
    def __init__(self, instrument_manager, state):
        super().__init__()
        self.IM     = instrument_manager
        self.state  = state  # not used yet, but handy for prefs

        # --- Instruments ----------------------------------------------------
        try:
            self.stage  = self.IM.get("ESP")     # ESP300Controller
            self.lockin = self.IM.get("SR830")   # SR830
        except KeyError as err:
            raise RuntimeError(
                f"InstrumentManager is missing “{err.args[0]}”. "
                "Add it before constructing AveragedScanTab."
            )

        # --- UI --------------------------------------------------------------
        outer = QVBoxLayout(self)

        # --- Scan parameters -------------------------------------------------
        form = QFormLayout()
        dv = QDoubleValidator(bottom=-999.0, top=999.0, decimals=3, parent=self)
        iv = QIntValidator(1, 1000, self)

        self.start_edit = QLineEdit("-10");   self.start_edit.setValidator(dv)
        self.end_edit   = QLineEdit("20");  self.end_edit.setValidator(dv)
        self.speed_edit = QLineEdit(".25");   self.speed_edit.setValidator(dv)
        self.scans_edit = QLineEdit("5");    self.scans_edit.setValidator(iv)

        form.addRow("Start time (ps):", self.start_edit)
        form.addRow("End   time (ps):", self.end_edit)
        form.addRow("Speed   (mm/s):", self.speed_edit)
        form.addRow("Num scans (>0):", self.scans_edit)
        outer.addLayout(form)

        # --- Start / Stop buttons -------------------------------------------
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start"); self.start_btn.clicked.connect(self._start_scan)
        self.stop_btn  = QPushButton("Stop");  self.stop_btn.clicked.connect(self._stop_scan)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn); btn_row.addWidget(self.stop_btn)
        outer.addLayout(btn_row)

        # --- Dual matplotlib plot -------------------------------------------
        self.fig = Figure(figsize=(6, 8))
        self.ax_avg = self.fig.add_subplot(211)
        self.ax_cur = self.fig.add_subplot(212, sharex=self.ax_avg)
        self.canvas = FigureCanvas(self.fig)
        outer.addWidget(self.canvas)

        # line objects
        self.avg_line, = self.ax_avg.plot([], [], lw=1.5, label="Running average")
        self.scan_lines = []  # one Line2D per scan

        self.ax_avg.set_ylabel("Lock-in X (avg)")
        self.ax_cur.set_xlabel("Delay time (ps)")
        self.ax_cur.set_ylabel("Lock-in X (scan)")
        self.ax_avg.grid(True); self.ax_cur.grid(True)

        # --- Timer for polling instruments ----------------------------------
        self.timer = QTimer(self)
        self.timer.setInterval(50)           # 20 Hz
        self.timer.timeout.connect(self._poll)

        # --- State variables -------------------------------------------------
        self.phase = 0            # 0 = positioning to start, 1 = scanning
        self.axis  = 1            # ESP300 axis (change to 2/3 if needed)
        self.scan_idx = 0         # 0-based index of current scan

        # data arrays
        self.xdata   = []         # reference positions (filled during first scan)
        self.sum_y   = []         # running sum for averaging
        self.cur_y   = []         # current scan trace

    # --------------------------------------------------------------------- #
    #  High-level control slots                                             #
    # --------------------------------------------------------------------- #
    def _start_scan(self):
        try:
            self.start_pos   = float(self.start_edit.text())
            self.end_pos     = float(self.end_edit.text())
            self.speed       = float(self.speed_edit.text())
            self.n_scans_req = int(self.scans_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Input error", "Please enter valid numbers.")
            return

        # reset state
        self.scan_idx = 0
        self.xdata.clear()
        self.sum_y.clear()
        self.avg_line.set_data([], [])
        self.ax_cur.clear(); self.ax_cur.set_xlabel("Delay time (ps)");self.ax_cur.set_ylabel("Lock-in X (scan)"); self.ax_cur.grid(True)
        self.scan_lines.clear()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._begin_single_scan()

    def _stop_scan(self):
        if self.timer.isActive():
            self.timer.stop()
        self._finish_all_scans(user_abort=True)

    # --------------------------------------------------------------------- #
    #  Scan life-cycle helpers                                              #
    # --------------------------------------------------------------------- #
    def to_ps(self, x):
        return (x-4.37)*(-6.67)
    def to_mm(self, x):
        return (x/(-6.67))+4.37
    
    def _begin_single_scan(self):
        """Move to start position and kick off QTimer polling."""
        self.phase = 0
        self.cur_y = []
        line, = self.ax_cur.plot([], [], lw=1)
        self.scan_lines.append(line)

        # go to start (fast) -------------------------------------------------
        try:
            self.stage.set_speed(self.axis, 25)
            self.stage.move_absolute(self.axis, self.to_mm(self.start_pos))
        except Exception as e:
            QMessageBox.critical(self, "Stage error", str(e))
            self._stop_scan()
            return

        self.timer.start()

    def _poll(self):
        """Called every 50 ms while a scan is running."""
        # Phase 0: waiting at start until motion completed -------------------
        if self.phase == 0:
            if not self.stage.is_moving(self.axis):
                # reached start → begin sweep
                try:
                    self.stage.set_speed(self.axis, self.speed)
                    self.stage.move_absolute(self.axis, self.to_mm(self.end_pos))
                except Exception as e:
                    QMessageBox.critical(self, "Stage error", str(e))
                    self._stop_scan()
                    return
                self.phase = 1
            return

        # Phase 1: sweeping --------------------------------------------------
        pos = None
        x   = None
        try:
            pos = self.to_ps(self.stage.get_position(self.axis))
        except Exception:
            pass
        try:
            x = self.lockin.read_x()
        except Exception:
            pass

        if (pos is not None) and (x is not None):
            # first scan builds x-axis --------------------------------------
            if self.scan_idx == 0:
                self.xdata.append(pos)
                self.sum_y.append(x)
            else:
                i = len(self.cur_y)
                if i < len(self.sum_y):
                    self.sum_y[i] += x

            # append to this scan’s trace
            self.cur_y.append(x)

            # --- live plot updates ----------------------------------------
            runs = self.scan_idx + 1
            # average (handle tail shorter than first scan)
            avg_y = [
                s / (runs if i < len(self.cur_y) else runs - 1)
                for i, s in enumerate(self.sum_y)
            ]
            self.avg_line.set_data(self.xdata, avg_y)

            # current scan
            if self.scan_lines:
                cur_line = self.scan_lines[-1]
                n = min(len(self.xdata), len(self.cur_y))
                cur_line.set_data(self.xdata[:n], self.cur_y[:n])

            # autoscale
            self.ax_avg.relim(); self.ax_avg.autoscale_view()
            self.ax_cur.relim(); self.ax_cur.autoscale_view()
            self.canvas.draw_idle()

        # finished sweep?
        if not self.stage.is_moving(self.axis):
            self.timer.stop()
            self._end_single_scan()

    def _end_single_scan(self):
        """Called after each sweep finishes."""
        self.scan_idx += 1
        if self.scan_idx < self.n_scans_req:
            # small pause so the stage settles, then start next scan
            QTimer.singleShot(200, self._begin_single_scan)
        else:
            self._finish_all_scans(user_abort=False)

    def _finish_all_scans(self, *, user_abort: bool):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        msg = "Scan sequence aborted." if user_abort else f"Completed {self.scan_idx} scan(s)."
        print(msg)

    # --------------------------------------------------------------------- #
    #  House-keeping                                                        #
    # --------------------------------------------------------------------- #
    def closeEvent(self, event):
        """Make sure the timer stops when the user closes this tab."""
        if self.timer.isActive():
            self.timer.stop()
        event.accept()
