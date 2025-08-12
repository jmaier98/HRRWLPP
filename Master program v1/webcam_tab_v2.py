import cv2
import pygame
import time
import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QLabel, QSlider, QMessageBox, QLineEdit 
)
from PyQt6.QtCore import (
    QTimer, Qt, QObject, QThread, pyqtSignal, QPoint
)
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QDoubleValidator

DEADZONE  = 0.15
SCALE_XY  = 2.0
SCALE_Z   = 0.15
SCALE_GALVO = 0.001


def move_big_motors_from_controller(BTT, galvo, joystick, state):
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
    
    x2_val = joystick.get_axis(2)
    y2_val = joystick.get_axis(3)
    if abs(x2_val) < DEADZONE: x2_val = 0
    if abs(y2_val) < DEADZONE: y2_val = 0

    dx2 = round(x2_val**3 * SCALE_GALVO*3, 5)
    dy2 = round(-1*y2_val**3 * SCALE_GALVO, 5)

    if dx2 != 0 or dy2 != 0:
        oldx = state.settings.get("galvo_x_position", 0.0)
        oldy = state.settings.get("galvo_y_position", 0.0)
        xnew = oldx + dx2
        ynew = oldy + dy2
        if xnew > .8:
            xnew = .8
        if xnew < -.8:  
            xnew = -.8
        if ynew > .8:
            ynew = .8
        if ynew < -.8:
            ynew = -.8
        state.settings["galvo_x_position"] = xnew
        state.settings["galvo_y_position"] = ynew
        galvo.move(xnew, ynew)
        print(f"Galvo moved to ({xnew}, {ynew})")
        time.sleep(0.01)  # allow galvo to settle
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


class MotorWorker(QObject):
    """
    Worker that runs in a separate thread to poll joystick & send G-code.
    """
    error = pyqtSignal(str)

    def __init__(self, instrument_manager, state):
        super().__init__()
        self.IM = instrument_manager
        self.btt = self.IM.get("BTT")
        self.shutter = self.IM.get("Shutter")
        self.galvo = self.IM.get("Galvo")
        self.state = state
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
            move_big_motors_from_controller(self.btt, self.galvo, self.joystick, self.state)
            time.sleep(0.001)  # adjust for responsiveness

        pygame.joystick.quit()
        pygame.quit()
        self.btt.clear()

    def stop(self):
        self._running = False
        
        


class DraggableVideoLabel(QLabel):
    """
    QLabel subclass that displays the video frame and
    allows dragging a crosshair target, emitting its coords.
    """
    moved = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.crosshair_pos = None
        self.dragging = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.crosshair_pos is None:
            w = event.size().width()
            h = event.size().height()
            self.crosshair_pos = QPoint(w // 2, h // 2)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.crosshair_pos = event.pos()
            self.moved.emit(self.crosshair_pos.x(), self.crosshair_pos.y())
            self.update()

    def mouseMoveEvent(self, event):
        if self.dragging:
            pos = event.pos()
            # constrain within widget bounds
            x = min(max(0, pos.x()), self.width() - 1)
            y = min(max(0, pos.y()), self.height() - 1)
            self.crosshair_pos = QPoint(x, y)
            self.moved.emit(x, y)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.crosshair_pos:
            painter = QPainter(self)
            pen = QPen(Qt.GlobalColor.red)
            pen.setWidth(1)
            painter.setPen(pen)
            x = self.crosshair_pos.x()
            y = self.crosshair_pos.y()
            # full-length crosshair lines
            painter.drawLine(x, 0, x, self.height())
            painter.drawLine(0, y, self.width(), y)
            painter.end()


class WebcamTab(QWidget):
    def __init__(self, instrument_manager, state):
        super().__init__()
        self.IM = instrument_manager
        self.shutter = self.IM.get("Shutter")
        self.stage = self.IM.get("ESP")
        self.galvo = self.IM.get("Galvo")
        self.state = state
        self.cap      = None
        self.timer    = QTimer()
        self.timer.timeout.connect(self._update_frame)



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
        
        galvo_row = QHBoxLayout()
        galvo_row.addWidget(QLabel("Galvo X (V):"))
        self.galvo_x_edit = QLineEdit("0.0")
        self.galvo_x_edit.setValidator(QDoubleValidator(-.1, .1, 4))
        galvo_row.addWidget(self.galvo_x_edit)

        galvo_row.addWidget(QLabel("Galvo Y (V):"))
        self.galvo_y_edit = QLineEdit("0.0")
        self.galvo_y_edit.setValidator(QDoubleValidator(-.1, .1, 4))
        galvo_row.addWidget(self.galvo_y_edit)

        self.galvo_go_btn = QPushButton("Go")
        self.galvo_go_btn.clicked.connect(self._on_galvo_go)
        galvo_row.addWidget(self.galvo_go_btn)

        ctrl.addLayout(galvo_row)
        # --------------------------------------------------------------------

        # --- Stage Controls -------------------------------------------------
        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Stage Pos (mm):"))
        self.stage_pos_edit = QLineEdit("0.0")
        self.stage_pos_edit.setValidator(QDoubleValidator(-75, 75, 3))
        pos_row.addWidget(self.stage_pos_edit)
        self.stage_go_btn = QPushButton("Go")
        self.stage_go_btn.clicked.connect(self._on_stage_go)
        pos_row.addWidget(self.stage_go_btn)
        ctrl.addLayout(pos_row)

        spd_row = QHBoxLayout()
        spd_row.addWidget(QLabel("Stage Speed (mm/s):"))
        self.stage_speed_edit = QLineEdit("30")  # default/example
        self.stage_speed_edit.setValidator(QDoubleValidator(0.0, 25, 2))
        spd_row.addWidget(self.stage_speed_edit)
        self.stage_speed_set_btn = QPushButton("Set")
        self.stage_speed_set_btn.clicked.connect(self._on_stage_speed_set)
        spd_row.addWidget(self.stage_speed_set_btn)
        ctrl.addLayout(spd_row)
        # --------------------------------------------------------------------
        
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

        # Video label with draggable crosshair
        self.video_label = DraggableVideoLabel()
        self.video_label.setFixedSize(640, 480)
        vid.addWidget(self.video_label)

        # Coordinate display
        self.coord_label = QLabel("Coords: (0, 0)")
        vid.addWidget(self.coord_label)

        # Connect crosshair movement
        self.video_label.moved.connect(self._on_target_moved)

        layout.addLayout(ctrl)
        layout.addLayout(vid)

    def _on_target_moved(self, x, y):
        self.coord_label.setText(f"Coords: ({x}, {y})")

    def _on_stage_go(self):
        pos = float(self.stage_pos_edit.text())
        self.stage.move_absolute(1, pos)

    def _on_stage_speed_set(self):
        speed = float(self.stage_speed_edit.text())
        self.stage.set_speed(1, speed)

    def _on_galvo_go(self):
        try:
            x = float(self.galvo_x_edit.text())
            y = float(self.galvo_y_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Please enter valid numbers for X and Y.")
            return

        try:
            # Replace move_xy with whatever your galvo API uses:
            self.galvo.move(x, y)
        except Exception as e:
            QMessageBox.warning(self, "Galvo Error", f"Couldn’t move galvo:\n{e}")
        
    def start_all(self):
        # 1) camera
        if self.cap is None:
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        self.timer.start(30)  # ~33 Hz

        # 2) motors — make a brand-new thread+worker every time
        self.motor_thread = QThread(self)
        self.motor_worker = MotorWorker(self.IM, self.state)
        self.motor_worker.moveToThread(self.motor_thread)
        self.motor_worker.error.connect(self._on_motor_error)
        self.motor_thread.started.connect(self.motor_worker.start)
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
        self.motor_worker.deleteLater()
        self.motor_thread.deleteLater()

        # toggle buttons
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _on_brightness_changed(self, val: int):
        try:
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
