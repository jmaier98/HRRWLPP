import sys, time
import numpy as np
from PyQt6 import QtWidgets, QtCore
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from ESP300 import ESP300Controller
from SR830 import SR830

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Averaged Stage + Lock‑In Scans (Live + Sliding Gaussian)")

        # — Instruments —
        try:
            self.stage  = ESP300Controller("GPIB0::1::INSTR")
            self.lockin = SR830(gpib_addr=18)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Connection Error", f"{e}")
            sys.exit(1)

        # — GUI —
        w = QtWidgets.QWidget()
        vlay = QtWidgets.QVBoxLayout(w)

        # form inputs
        form = QtWidgets.QFormLayout()
        dv = QDoubleValidator()
        iv = QIntValidator(1, 1000, self)
        self.start_edit = QtWidgets.QLineEdit("75");  self.start_edit.setValidator(dv)
        self.end_edit   = QtWidgets.QLineEdit("-75"); self.end_edit.setValidator(dv)
        self.speed_edit = QtWidgets.QLineEdit("10");  self.speed_edit.setValidator(dv)
        self.scans_edit = QtWidgets.QLineEdit("5");   self.scans_edit.setValidator(iv)
        form.addRow("Start pos (mm):", self.start_edit)
        form.addRow("End   pos (mm):", self.end_edit)
        form.addRow("Speed   (mm/s):", self.speed_edit)
        form.addRow("Num scans (>0):", self.scans_edit)
        vlay.addLayout(form)

        # start / stop buttons
        hl = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn  = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        hl.addWidget(self.start_btn)
        hl.addWidget(self.stop_btn)
        vlay.addLayout(hl)

        # — SHOW TRACES CHECKBOX (new) —
        self.show_traces_checkbox = QtWidgets.QCheckBox("Show all traces")
        self.show_traces_checkbox.setToolTip("Toggle between raw scan traces and running average in the top plot")
        vlay.addWidget(self.show_traces_checkbox)

        # — Dual Plot —
        self.fig = Figure(figsize=(6,8))
        self.ax1 = self.fig.add_subplot(211)
        self.ax2 = self.fig.add_subplot(212, sharex=self.ax1)
        self.canvas = FigureCanvas(self.fig)
        vlay.addWidget(self.canvas)

        self.setCentralWidget(w)

        # — plot handles —
        self.avg_line,     = self.ax1.plot([], [], linestyle='-')
        self.sliding_line, = self.ax2.plot([], [], linestyle='-')
        self.ax1.set_ylabel("Lock‑in X (avg)")
        self.ax2.set_xlabel("Stage pos (mm)")
        self.ax2.set_ylabel("Gaussian avg (±10 pts)")
        self.ax1.grid(True)
        self.ax2.grid(True)

        # — Timer —
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)

        # — State —
        self.phase      = 0
        self.axis       = 1
        self.scan_count = 0
        # will hold raw-data for each completed scan
        self.runs_data  = []
        # will hold the Line2D objects for each raw-trace
        self.trace_lines = []

        # — Signals —
        self.start_btn.clicked.connect(self.start_scan)
        self.stop_btn.clicked.connect(self.stop_scan)
        # when the checkbox toggles, refresh the top plot
        self.show_traces_checkbox.stateChanged.connect(self._refresh_top_plot)

    def start_scan(self):
        try:
            self.start_pos   = float(self.start_edit.text())
            self.end_pos     = float(self.end_edit.text())
            self.speed       = float(self.speed_edit.text())
            self.total_scans = int(self.scans_edit.text())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Input error", "Check your numbers.")
            return

        # reset data
        self.scan_count   = 0
        self.sum_y        = []   # accumulate y across scans
        self.xdata        = []   # reference x positions
        self.runs_data    = []   # clear old runs
        self.trace_lines  = []   # clear old line artists
        self.ax1.clear()
        self.ax1.set_ylabel("Lock‑in X (avg)")
        self.ax1.grid(True)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self._begin_scan()

    def _begin_scan(self):
        self.phase     = 0
        self.current_y = []
        self.stage.set_speed(self.axis, 25)
        self.stage.move_absolute(self.axis, self.start_pos)
        self.timer.start()

    def _poll(self):
        if self.phase == 0:
            if not self.stage.is_moving(self.axis):
                self.phase = 1
                self.stage.set_speed(self.axis, self.speed)
                self.stage.move_absolute(self.axis, self.end_pos)
            return

        # sweeping
        try:
            pos = self.stage.get_position(self.axis)
            x   = self.lockin.read_x()
        except Exception:
            pos, x = None, None

        if pos is not None and x is not None:
            # build xdata & running sum
            if self.scan_count == 0:
                self.xdata.append(pos)
                self.sum_y.append(x)
            else:
                idx = len(self.current_y)
                if idx < len(self.sum_y):
                    self.sum_y[idx] += x

            self.current_y.append(x)

            # update sliding average in bottom plot
            runs = self.scan_count + 1
            avg_y = [s / runs for s in self.sum_y]
            N = len(avg_y)
            if N > 1:
                sigma = 10/2
                idxs = np.arange(N)
                smooth = [ (np.exp(-0.5*((idxs-i)/sigma)**2) / np.exp(-0.5*((idxs-i)/sigma)**2).sum() * np.array(avg_y)).sum()
                           for i in idxs ]
                self.sliding_line.set_data(self.xdata, smooth)

            # draw only the running-average if checkbox is off
            if not self.show_traces_checkbox.isChecked():
                self.avg_line.set_data(self.xdata, avg_y)

            # rescale & redraw
            self.ax1.relim(); self.ax1.autoscale_view()
            self.ax2.relim(); self.ax2.autoscale_view()
            self.canvas.draw_idle()

        if not self.stage.is_moving(self.axis):
            self.timer.stop()
            self._end_scan()

    def _end_scan(self):
        # store this scan's raw data
        self.runs_data.append(self.current_y.copy())

        # if we're in "show traces" mode, add a new line artist
        if self.show_traces_checkbox.isChecked():
            line, = self.ax1.plot(self.xdata, self.current_y, linestyle='-')
            self.trace_lines.append(line)
            self.canvas.draw_idle()

        self.scan_count += 1
        if self.scan_count < self.total_scans:
            QtCore.QTimer.singleShot(200, self._begin_scan)
        else:
            self._finish_all()

    def _refresh_top_plot(self):
        """Re-draw the top plot according to the checkbox."""
        self.ax1.clear()
        self.ax1.set_ylabel("Lock‑in X")
        self.ax1.grid(True)

        if self.show_traces_checkbox.isChecked():
            # plot every raw scan
            for run in self.runs_data:
                self.ax1.plot(self.xdata, run, linestyle='-')
        else:
            # plot only the running average
            runs = len(self.runs_data)
            if runs > 0:
                avg_y = [s / runs for s in self.sum_y]
                self.avg_line, = self.ax1.plot(self.xdata, avg_y, linestyle='-')
            self.ax1.set_ylabel("Lock‑in X (avg)")

        self.canvas.draw_idle()

    def stop_scan(self):
        if self.timer.isActive():
            self.timer.stop()
        self._finish_all()

    def _finish_all(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        QtWidgets.QMessageBox.information(
            self, "Done",
            f"Completed {self.scan_count} scan(s)."
        )

    def closeEvent(self, event):
        try:
            self.stage.close()
            self.lockin.close()
        except:
            pass
        super().closeEvent(event)


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


