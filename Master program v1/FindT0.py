# live_scan_avg_liveupdate.py

import sys, time
from PyQt6 import QtWidgets, QtCore
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from ESP300 import ESP300Controller
from SR830 import SR830

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Averaged Stage + Lock‑In Scans (Live)")

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

        form = QtWidgets.QFormLayout()
        dv = QDoubleValidator()
        iv = QIntValidator(1, 1000, self)
        self.start_edit = QtWidgets.QLineEdit("75"); self.start_edit.setValidator(dv)
        self.end_edit   = QtWidgets.QLineEdit("-75"); self.end_edit.setValidator(dv)
        self.speed_edit = QtWidgets.QLineEdit("10"); self.speed_edit.setValidator(dv)
        self.scans_edit = QtWidgets.QLineEdit("5");  self.scans_edit.setValidator(iv)
        form.addRow("Start pos (mm):", self.start_edit)
        form.addRow("End   pos (mm):", self.end_edit)
        form.addRow("Speed   (mm/s):", self.speed_edit)
        form.addRow("Num scans (>0):", self.scans_edit)
        vlay.addLayout(form)

        hl = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn  = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        hl.addWidget(self.start_btn); hl.addWidget(self.stop_btn)
        vlay.addLayout(hl)

        # — Plot —
        self.fig = Figure(figsize=(5,3))
        self.ax  = self.fig.add_subplot(111)
        self.canvas = FigureCanvas(self.fig)
        vlay.addWidget(self.canvas)

        self.setCentralWidget(w)

        self.avg_line, = self.ax.plot([], [], marker='o', linestyle='-')
        self.ax.set_xlabel("Stage pos (mm)")
        self.ax.set_ylabel("Lock‑in X (avg)")
        self.ax.grid(True)

        # — Timer —
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)

        # — State —
        self.phase      = 0
        self.axis       = 1
        self.scan_count = 0

        # — Signals —
        self.start_btn.clicked.connect(self.start_scan)
        self.stop_btn.clicked.connect(self.stop_scan)

    def start_scan(self):
        try:
            self.start_pos   = float(self.start_edit.text())
            self.end_pos     = float(self.end_edit.text())
            self.speed       = float(self.speed_edit.text())
            self.total_scans = int(self.scans_edit.text())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Input error", "Check your numbers.")
            return

        # reset
        self.scan_count = 0
        self.sum_y      = []  # accumulate y‑values across scans
        self.xdata      = []  # reference positions

        # disable UI
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # begin first scan
        self._begin_scan()

    def _begin_scan(self):
        self.phase      = 0
        self.current_y  = []
        # move to start
        self.stage.set_speed(self.axis, 25)
        self.stage.move_absolute(self.axis, self.start_pos)
        # start polling
        self.timer.start()

    def _poll(self):
        # phase 0: wait until at start, then trigger sweep
        if self.phase == 0:
            if not self.stage.is_moving(self.axis):
                self.phase = 1
                self.stage.set_speed(self.axis, self.speed)
                self.stage.move_absolute(self.axis, self.end_pos)
            return

        # phase 1: sweeping
        if self.phase == 1:
            # read stage position
            try:
                pos = self.stage.get_position(self.axis)
            except Exception as e:
                print("Stage read error (ignored):", e)
                pos = None

            # read lock‑in X
            try:
                x = self.lockin.read_x()
            except Exception as e:
                print("Lock‑in read timeout (ignored):", e)
                x = None

            if pos is not None and x is not None:
                # first scan: build xdata & sum_y
                if self.scan_count == 0:
                    self.xdata.append(pos)
                    self.sum_y.append(x)
                else:
                    idx = len(self.current_y)
                    if idx < len(self.sum_y):
                        self.sum_y[idx] += x

                self.current_y.append(x)

                # --- <=== live update of average here ===>
                # compute average across completed scans + this one
                runs_done = self.scan_count + 1
                avg_y = [s / runs_done for s in self.sum_y]
                self.avg_line.set_data(self.xdata, avg_y)
                self.ax.relim()
                self.ax.autoscale_view()
                self.canvas.draw_idle()
                # ------------------------------------------

            # check end of sweep
            if not self.stage.is_moving(self.axis):
                self.timer.stop()
                self._end_scan()

    def _end_scan(self):
        self.scan_count += 1

        if self.scan_count < self.total_scans:
            # small delay, then next sweep
            QtCore.QTimer.singleShot(200, self._begin_scan)
        else:
            self._finish_all()

    def stop_scan(self):
        if self.timer.isActive():
            self.timer.stop()
        self._finish_all()

    def _finish_all(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        QtWidgets.QMessageBox.information(
            self, "Done",
            f"Completed {self.scan_count} scan(s).\n"
            "Plot shows the point‑by‑point average."
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

