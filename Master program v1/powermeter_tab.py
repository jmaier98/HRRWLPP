# powermeter_tab.py
import time
import numpy as np
import pyvisa
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import Qt
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
import serial

# match the same constants
INTERVAL    = 0.05
BUFFER_SIZE = 200
PM = 92
Empty = 20
class PowerMeterTab(QWidget):
    def __init__(self, instrument_manager, state):
        super().__init__()
        self.pm = None
        self.IM = instrument_manager
        self.state = state
        self.btt = self.IM.get("BTT")
        self.shutter = self.IM.get("Shutter")
        self.pm = self.IM.get("PM")
        main_layout = QVBoxLayout(self)
        btn_layout  = QHBoxLayout()
        ctrl  = QVBoxLayout()
        main_layout.addLayout(btn_layout)
        main_layout.addLayout(ctrl)
        

        # Start/Stop
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_measurement)
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_measurement)
        self.stop_btn.setEnabled(False)

        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addStretch()

        # Shutter control buttons
        self.open_pump_btn = QPushButton("Open Pump Shutter")
        self.open_pump_btn.clicked.connect(self.shutter.openPump)
        ctrl.addWidget(self.open_pump_btn)

        self.close_pump_btn = QPushButton("Close Pump Shutter")
        self.close_pump_btn.clicked.connect(self.shutter.closePump)
        ctrl.addWidget(self.close_pump_btn)

        self.open_probe_btn = QPushButton("Open Probe Shutter")
        self.open_probe_btn.clicked.connect(self.shutter.openProbe)
        ctrl.addWidget(self.open_probe_btn)

        self.close_probe_btn = QPushButton("Close Probe Shutter")
        self.close_probe_btn.clicked.connect(self.shutter.closeProbe)
        ctrl.addWidget(self.close_probe_btn)
        
        # Canvas
        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.add_subplot(111)
        self.ax.set_title("PM16-122 Live Power")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Power (uW)")
        main_layout.addWidget(self.canvas)

        # Numeric label
        self.current_lbl = QLabel("-- uW")
        main_layout.addWidget(self.current_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)
        font = self.current_lbl.font()
        font.setPointSize(60)      # e.g. 18-point type
        font.setBold(True)
        self.current_lbl.setFont(font)

        # data buffers
        self.times  = np.zeros(BUFFER_SIZE)
        self.powers = np.zeros(BUFFER_SIZE)
        self.line, = self.ax.plot(self.times, self.powers, 'o', markersize=4)

        self.timer = QTimer()
        self.timer.timeout.connect(self._update_plot)
    


    def start_measurement(self):
        self.btt.powermeter()
        self.start_time = time.time()
        self.idx = 0
        self.times.fill(0)
        self.powers.fill(0)
        self.line.set_data(self.times, self.powers)
        self.ax.set_xlim(0,10); self.ax.set_ylim(0,20000)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.timer.start(int(INTERVAL * 1000))

    def stop_measurement(self):
        self.timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.btt.clear()

    def _update_plot(self):
        try:
            p_w = self.pm.read_power()
        except:
            return
        p_mw = p_w 
        t = time.time() - self.start_time

        self.times[self.idx % BUFFER_SIZE]  = t
        self.powers[self.idx % BUFFER_SIZE] = p_mw
        self.idx += 1

        if self.idx < BUFFER_SIZE:
            xs, ys = self.times[:self.idx], self.powers[:self.idx]
            self.line.set_data(xs, ys)
            self.ax.set_xlim(0, xs.max() + 0.1)
            ymax = ys.max() * 1.1 if ys.max() > 0 else 20000
            self.ax.set_ylim(0, ymax)
        else:
            window = self.times[(self.idx - BUFFER_SIZE) % BUFFER_SIZE]
            self.line.set_data(self.times, self.powers)
            self.ax.set_xlim(window, window + self.times.max() - self.times.min())
            ymax = self.powers.max() * 1.1 if self.powers.max() > 0 else 20000
            self.ax.set_ylim(0, ymax)

        self.ax.relim()
        self.ax.autoscale_view(True, True, True)
        self.current_lbl.setText(f"{p_mw:6.2f} uW")
        self.canvas.draw()
