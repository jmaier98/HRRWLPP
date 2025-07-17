# webcam_tab.py

import cv2
import pygame
import time
import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QLabel, QSlider, QMessageBox
)
from PyQt6.QtCore import (
    QTimer, Qt, QObject, QThread, pyqtSignal
)
from PyQt6.QtGui import QImage, QPixmap

DEADZONE  = 0.15
SCALE_XY  = 2.0
SCALE_Z   = 0.15


def move_big_motors_from_controller(BTT, joystick):
    pygame.event.pump()
    # X/Y axes
    x_val = joystick.get_axis(0)
    y_val = joystick.get_axis(1)
    if abs(x_val) < DEADZONE: x_val = 0
    if abs(y_val) < DEADZONE: y_val = 0

    dx = round(x_val**3 * SCALE_XY, 3)
    dy = round(y_val**3 * SCALE_XY, 3)

    if dx != 0 or dy != 0:
        dr = np.hypot(dx, dy)
        feedrate = dr * 1200
        BTT.cryoXY(dx, dy, feedrate)
        return  # skip Z when XY are moving

    # Z axis (triggers)
    lt_raw = joystick.get_axis(4)
    rt_raw = joystick.get_axis(5)
    lt = (lt_raw + 1) / 2
    rt = (rt_raw + 1) / 2
    if lt < .1: lt = 0
    if rt < .1: rt = 0

    dz = round((lt**3 - rt**3) * SCALE_Z, 3)
    if dz != 0:
        feedrate_z = abs(dz) * 1200
        BTT.cryoZ(dz, feedrate_z)

# ————————————————————————————————————————————————————————

class MotorWorker(QObject):
    """
    Worker that runs in a separate thread to poll joystick & send G-code.
    """
    error = pyqtSignal(str)

    def __init__(self, instrument_manager):
        super().__init__()
        self.IM = instrument_manager
        self.btt = self.IM.get("BTT")
        self.shutter = self.IM.get("Shutter")
        self._running = False

    def start(self):
        try:
            pygame.init()
            pygame.joystick.init()
            if pygame.joystick.get_count() == 0:
                raise RuntimeError("No joystick detected")
            self.joystick = pygame.joystick.Joystick(0)
            self.joystick.init()
            
            self.btt.pellicles()
        except Exception as e:
            self.error.emit(str(e))
            return

        self._running = True
        while self._running:
            move_big_motors_from_controller(self.btt, self.joystick)
            time.sleep(0.001)  # adjust for responsiveness

    def stop(self):
        self._running = False
        self.btt.clear()
        pygame.joystick.quit()
        pygame.quit()

class WebcamTab(QWidget):
    def __init__(self, instrument_manager):
        super().__init__()
        self.IM = instrument_manager
        self.shutter = self.IM.get("Shutter")
        self.cap      = None
        self.timer    = QTimer()
        self.timer.timeout.connect(self._update_frame)

        # prepare motor-thread but don't start
        self.motor_thread = QThread(self)
        self.motor_worker = MotorWorker(self.IM)
        self.motor_worker.moveToThread(self.motor_thread)
        self.motor_worker.error.connect(self._on_motor_error)
        self.motor_thread.started.connect(self.motor_worker.start)

        # — UI —
        layout = QHBoxLayout(self)
        ctrl  = QVBoxLayout()
        vid   = QVBoxLayout()

        # Start / Stop buttons
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_all)
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_all)
        self.stop_btn.setEnabled(False)

        # Brightness slider
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(0, 255)
        self.brightness_slider.setValue(0)
        self.brightness_slider.setTickInterval(50)
        self.brightness_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.brightness_slider.valueChanged.connect(self._on_brightness_changed)

        ctrl.addWidget(self.start_btn)
        ctrl.addWidget(self.stop_btn)
        ctrl.addWidget(QLabel("LED Brightness"))
        ctrl.addWidget(self.brightness_slider)
        ctrl.addStretch()

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

        # Video label
        self.video_label = QLabel()
        self.video_label.setFixedSize(640, 480)
        vid.addWidget(self.video_label)

        layout.addLayout(ctrl)
        layout.addLayout(vid)

    def start_all(self):
        # 1) camera
        if self.cap is None:
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        self.timer.start(30)  # ~33 Hz

        # 2) motors
        self.motor_thread.start()

        # toggle buttons
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def stop_all(self):
        # stop camera
        self.timer.stop()
        if self.cap:
            self.cap.release()
            self.cap = None

        # stop motors
        self.motor_worker.stop()
        self.motor_thread.quit()
        self.motor_thread.wait()

        # toggle buttons
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _on_brightness_changed(self, val: int):
        """
        Called with val between 0 and 255 whenever the slider is moved.
        Replace `self.IM.get("LED").set_brightness(val)` with your actual function.
        """
        try:
            # Example: call your LED‐driver here
            self.shutter.setLED(val)
        except Exception as e:
            QMessageBox.warning(self, "LED Error", f"Couldn’t set brightness:\n{e}")
    def _update_frame(self):
        if not self.cap:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        frame = cv2.convertScaleAbs(frame, alpha=1,
                                    beta=0)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame.shape
        bytes_per_line = ch * w
        qimg = QImage(frame.data, w, h, bytes_per_line,
                      QImage.Format.Format_RGB888)
        self.video_label.setPixmap(QPixmap.fromImage(qimg))

    def _on_motor_error(self, msg):
        QMessageBox.critical(self, "Motor Error", msg)
        self.stop_all()

    def closeEvent(self, event):
        # ensure threads and devices are cleaned up on window close
        self.stop_all()
        event.accept()

