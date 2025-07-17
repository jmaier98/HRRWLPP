import sys
import time
import cv2
import numpy as np
import pyvisa
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSlider
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QImage, QPixmap
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

# ——— USER CONFIG ———
INTERVAL    = 0.05      # polling interval (s)
BUFFER_SIZE = 200       # points in your rolling window
# ——————————————

class WebcamTab(QWidget):
    def __init__(self):
        super().__init__()
        self.cap = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)

        layout = QHBoxLayout(self)
        control_layout = QVBoxLayout()
        video_layout = QVBoxLayout()

        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_capture)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_capture)
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(-100, 100)
        self.brightness_slider.setValue(0)
        self.brightness_slider.setTickInterval(10)
        self.brightness_slider.setTickPosition(QSlider.TickPosition.TicksBelow)

        control_layout.addWidget(self.start_btn)
        control_layout.addWidget(self.stop_btn)
        control_layout.addWidget(QLabel("Brightness"))
        control_layout.addWidget(self.brightness_slider)
        control_layout.addStretch()

        self.video_label = QLabel()
        self.video_label.setFixedSize(640, 480)
        video_layout.addWidget(self.video_label)

        layout.addLayout(control_layout)
        layout.addLayout(video_layout)

    def start_capture(self):
        if self.cap is None:
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        self.timer.start(30)

    def stop_capture(self):
        self.timer.stop()
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def update_frame(self):
        if self.cap is None:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        brightness = self.brightness_slider.value()
        frame = cv2.convertScaleAbs(frame, alpha=1, beta=brightness)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame.shape
        bytes_per_line = ch * w
        qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        self.video_label.setPixmap(QPixmap.fromImage(qimg))

    def closeEvent(self, event):
        if self.cap is not None:
            self.cap.release()
        event.accept()

class PowerMeterTab(QWidget):
    def __init__(self):
        super().__init__()
        self.pm = None

        # — Layouts ——————————————————————————————
        main_layout = QVBoxLayout(self)
        btn_layout  = QHBoxLayout()
        main_layout.addLayout(btn_layout)

        # — Start/Stop Buttons —————————————————————
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_measurement)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_measurement)
        self.stop_btn.setEnabled(False)

        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addStretch()

        # — Matplotlib Canvas —————————————————————
        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        self.ax     = self.figure.add_subplot(111)
        self.ax.set_title("PM16-122 Live Power")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Power (uW)")

        main_layout.addWidget(self.canvas)

        # — Numeric Readout ——————————————————————
        self.current_lbl = QLabel("Current: -- uW")
        main_layout.addWidget(self.current_lbl)

        # — Data Buffers & Plot Objects ———————————
        self.times  = np.zeros(BUFFER_SIZE)
        self.powers = np.zeros(BUFFER_SIZE)
        self.line, = self.ax.plot(self.times, self.powers, 'o', markersize=4)
        self.text   = self.ax.text(0.02, 0.95, "", transform=self.ax.transAxes,
                                   fontsize=14, bbox=dict(facecolor='white', alpha=0.8))

        # — Timer (but don’t start yet) ————————————
        self.timer = QTimer()
        self.timer.timeout.connect(self._update_plot)

    def start_measurement(self):
        # Initialize instrument on first start
        if self.pm is None:
            try:
                rm = pyvisa.ResourceManager('@py')
                self.pm = rm.open_resource("USB0::4883::32891::250604411::0::INSTR")
                self.pm.write("SENS:POW:UNIT W")
                self.pm.write("SENS:POW:RANG AUTO")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open PM16-122:\n{e}")
                return

        # Reset data
        self.start_time = time.time()
        self.idx = 0
        self.times.fill(0)
        self.powers.fill(0)
        self.line.set_data(self.times, self.powers)
        self.ax.set_xlim(0, 1)
        self.ax.set_ylim(0, 1)

        # Toggle buttons
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # Start timer
        self.timer.start(int(INTERVAL * 1000))

    def stop_measurement(self):
        self.timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _update_plot(self):
        # Query power meter
        try:
            p_w  = float(self.pm.query("READ?"))
        except Exception:
            return
        p_mw = p_w * 1e6
        t    = time.time() - self.start_time

        # Update buffer
        self.times[self.idx % BUFFER_SIZE]  = t
        self.powers[self.idx % BUFFER_SIZE] = p_mw
        self.idx += 1

        # Update line
        if self.idx < BUFFER_SIZE:
            xs = self.times[:self.idx]
            ys = self.powers[:self.idx]
            self.line.set_data(xs, ys)
            self.ax.set_xlim(0, xs.max() + 0.1)
        else:
            window = self.times[(self.idx - BUFFER_SIZE) % BUFFER_SIZE]
            self.line.set_data(self.times, self.powers)
            self.ax.set_xlim(window, window + self.times.max() - self.times.min())

        # Autoscale & redraw
        self.ax.relim()
        self.ax.autoscale_view(True, True, True)
        self.text.set_text(f"{p_mw:6.2f} uW")
        self.current_lbl.setText(f"Current: {p_mw:6.2f} uW")
        self.canvas.draw()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multi-Tab PyQt6 Example")
        self.resize(900, 700)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Tab 1: placeholder
        tab1 = QWidget()
        layout1 = QVBoxLayout(tab1)
        layout1.addWidget(QLabel("Placeholder content for Tab 1"))
        self.tabs.addTab(tab1, "Tab 1")

        # Tab 2: power meter
        power_tab = PowerMeterTab()
        self.tabs.addTab(power_tab, "Power Meter")

        # Tab 3: placeholder
        tab3 = QWidget()
        layout3 = QVBoxLayout(tab3)
        layout3.addWidget(QLabel("Placeholder content for Tab 3"))
        self.tabs.addTab(tab3, "Tab 3")

        # Tab 4: webcam
        webcam_tab = WebcamTab()
        self.tabs.addTab(webcam_tab, "Webcam")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


