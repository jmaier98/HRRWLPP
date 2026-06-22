# tabs/webcam_tab.py
import time
import numpy as np

import cv2
import pygame

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QLabel, QSlider, QMessageBox, QLineEdit,
    QGroupBox, QGridLayout, QRadioButton, QButtonGroup,
)
from PyQt6.QtCore import QTimer, Qt, QObject, QThread, pyqtSignal, QPoint
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QDoubleValidator

# ── Joystick constants ────────────────────────────────────────────────────────
DEADZONE     = 0.15
SCALE_XY     = 2.0
SCALE_Z      = 0.15
SCALE_GALVO  = 1.0    # joystick → milliunit increments per poll cycle

JOG_STEPS_XY = [1.0,  0.1,  0.01]
JOG_STEPS_Z  = [0.1,  0.01, 0.001]
JOG_FEEDS_XY = [1200, 300,  60]
JOG_FEEDS_Z  = [120,  30,   10]

# Galvo public limit in milliunits (must match driver)
GALVO_LIMIT = 500


# ── Joystick movement logic ───────────────────────────────────────────────────

def _move_from_joystick(btt, galvo, joystick, state):
    pygame.event.pump()

    # Left stick → cryo XY
    ax = joystick.get_axis(0)
    ay = joystick.get_axis(1)
    if abs(ax) < DEADZONE: ax = 0
    if abs(ay) < DEADZONE: ay = 0
    dx = round(ax ** 3 * SCALE_XY, 3)
    dy = round(ay ** 3 * SCALE_XY, 3)
    if dx != 0 or dy != 0:
        btt.cryoXY(dx, dy, np.hypot(dx, dy) * 1200)
        return

    # Right stick → galvo XY
    ax2 = joystick.get_axis(2)
    ay2 = joystick.get_axis(3)
    if abs(ax2) < DEADZONE: ax2 = 0
    if abs(ay2) < DEADZONE: ay2 = 0
    dgx = round(ax2 ** 3 * SCALE_GALVO, 5)
    dgy = round(-ay2 ** 3 * SCALE_GALVO, 5)
    if dgx != 0 or dgy != 0:
        gx = state.settings.get("galvo_x_position", 0.0) + dgx
        gy = state.settings.get("galvo_y_position", 0.0) + dgy
        gx = max(min(gx, GALVO_LIMIT), -GALVO_LIMIT)
        gy = max(min(gy, GALVO_LIMIT), -GALVO_LIMIT)
        state.settings["galvo_x_position"] = gx
        state.settings["galvo_y_position"] = gy
        galvo.move_xy(gx, gy)
        time.sleep(0.01)
        return

    # Triggers → cryo Z
    lt = max(0.0, (joystick.get_axis(4) + 1) / 2)
    rt = max(0.0, (joystick.get_axis(5) + 1) / 2)
    if lt < 0.1: lt = 0
    if rt < 0.1: rt = 0
    dz = round((lt ** 3 - rt ** 3) * SCALE_Z, 3)
    if dz != 0:
        btt.cryoZ(dz, abs(dz) * 1200)


# ── Motor worker ──────────────────────────────────────────────────────────────

class MotorWorker(QObject):
    error = pyqtSignal(str)

    def __init__(self, btt, galvo, state):
        super().__init__()
        self.btt     = btt
        self.galvo   = galvo
        self.state   = state
        self._running = False

    def start(self):
        try:
            pygame.init()
            pygame.joystick.init()
            if pygame.joystick.get_count() == 0:
                raise RuntimeError("No joystick detected.")
            self.joystick = pygame.joystick.Joystick(0)
            self.joystick.init()
            self.btt.pellicles()
        except Exception as e:
            self.error.emit(str(e))
            return

        self._running = True
        while self._running:
            try:
                _move_from_joystick(self.btt, self.galvo, self.joystick, self.state)
            except Exception:
                pass
            time.sleep(0.001)

        pygame.joystick.quit()
        pygame.quit()
        self.btt.clear()

    def stop(self):
        self._running = False


# ── Draggable crosshair video label ──────────────────────────────────────────

class DraggableVideoLabel(QLabel):
    moved = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.crosshair_pos = None
        self.dragging = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.crosshair_pos is None:
            self.crosshair_pos = QPoint(event.size().width() // 2,
                                        event.size().height() // 2)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.crosshair_pos = event.pos()
            self.moved.emit(self.crosshair_pos.x(), self.crosshair_pos.y())
            self.update()

    def mouseMoveEvent(self, event):
        if self.dragging:
            x = min(max(0, event.pos().x()), self.width()  - 1)
            y = min(max(0, event.pos().y()), self.height() - 1)
            self.crosshair_pos = QPoint(x, y)
            self.moved.emit(x, y)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.crosshair_pos:
            p = QPainter(self)
            pen = QPen(Qt.GlobalColor.red)
            pen.setWidth(1)
            p.setPen(pen)
            x, y = self.crosshair_pos.x(), self.crosshair_pos.y()
            p.drawLine(x, 0, x, self.height())
            p.drawLine(0, y, self.width(), y)
            p.end()


# ── Tab ───────────────────────────────────────────────────────────────────────

class WebcamTab(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.state   = controller.state
        self.btt     = controller.get_instrument("BTT")
        self.galvo   = controller.get_instrument("Galvo")
        self.stage   = controller.get_instrument("ESP")
        self.shutter = controller.get_instrument("Shutter")

        self.cap   = None
        self.timer = QTimer()
        self.timer.timeout.connect(self._update_frame)
        self.motor_thread  = None
        self.motor_worker  = None

        # ── Layout ────────────────────────────────────────────────────────────
        layout = QHBoxLayout(self)
        ctrl = QVBoxLayout()
        vid  = QVBoxLayout()

        # Start / Stop
        self.start_btn = QPushButton("Start camera + joystick")
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start_all)
        self.stop_btn.clicked.connect(self._stop_all)
        ctrl.addWidget(self.start_btn)
        ctrl.addWidget(self.stop_btn)

        # LED brightness
        ctrl.addWidget(QLabel("LED Brightness:"))
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(0, 255)
        self.brightness_slider.setValue(0)
        self.brightness_slider.setTickInterval(50)
        self.brightness_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.brightness_slider.valueChanged.connect(self._on_brightness)
        ctrl.addWidget(self.brightness_slider)

        # Galvo
        gbox = QGroupBox("Galvo position")
        grow = QGridLayout(gbox)
        grow.addWidget(QLabel("X:"), 0, 0)
        self.galvo_x_edit = QLineEdit("0")
        self.galvo_x_edit.setValidator(QDoubleValidator(-GALVO_LIMIT, GALVO_LIMIT, 1))
        grow.addWidget(self.galvo_x_edit, 0, 1)
        grow.addWidget(QLabel("Y:"), 1, 0)
        self.galvo_y_edit = QLineEdit("0")
        self.galvo_y_edit.setValidator(QDoubleValidator(-GALVO_LIMIT, GALVO_LIMIT, 1))
        grow.addWidget(self.galvo_y_edit, 1, 1)
        galvo_go = QPushButton("Go")
        galvo_go.clicked.connect(self._on_galvo_go)
        grow.addWidget(galvo_go, 0, 2, 2, 1)
        ctrl.addWidget(gbox)

        # Stage
        sbox = QGroupBox("Delay stage")
        slay = QGridLayout(sbox)
        slay.addWidget(QLabel("Position (mm):"), 0, 0)
        self.stage_pos_edit = QLineEdit("0.0")
        self.stage_pos_edit.setValidator(QDoubleValidator(-75, 75, 3))
        slay.addWidget(self.stage_pos_edit, 0, 1)
        stage_go = QPushButton("Go")
        stage_go.clicked.connect(self._on_stage_go)
        slay.addWidget(stage_go, 0, 2)
        slay.addWidget(QLabel("Speed (mm/s):"), 1, 0)
        self.stage_speed_edit = QLineEdit("30")
        self.stage_speed_edit.setValidator(QDoubleValidator(0.0, 25, 2))
        slay.addWidget(self.stage_speed_edit, 1, 1)
        speed_set = QPushButton("Set")
        speed_set.clicked.connect(self._on_stage_speed)
        slay.addWidget(speed_set, 1, 2)
        ctrl.addWidget(sbox)

        # Cryo jog
        jog_group = QGroupBox("Cryo stage jog")
        jlay = QVBoxLayout(jog_group)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step:"))
        self._step_grp = QButtonGroup(self)
        for i, label in enumerate(["Rough\n1 / 0.1 mm",
                                    "Medium\n0.1 / 0.01 mm",
                                    "Fine\n0.01 / 0.001 mm"]):
            rb = QRadioButton(label)
            rb.setChecked(i == 1)
            self._step_grp.addButton(rb, i)
            step_row.addWidget(rb)
        jlay.addLayout(step_row)

        xy_pad = QGridLayout()
        xy_pad.setSpacing(2)
        for btn_text, r, c, dx, dy in [
            ("Y+", 0, 1,  0,  1),
            ("X-", 1, 0, -1,  0),
            ("X+", 1, 2,  1,  0),
            ("Y-", 2, 1,  0, -1),
        ]:
            b = QPushButton(btn_text)
            b.setFixedSize(52, 36)
            b.clicked.connect(lambda _, _dx=dx, _dy=dy: self._jog_xy(_dx, _dy))
            xy_pad.addWidget(b, r, c)
        jlay.addLayout(xy_pad)

        z_row = QHBoxLayout()
        z_row.addStretch()
        z_row.addWidget(QLabel("Z:"))
        for label, sign in [("Z+", 1), ("Z-", -1)]:
            b = QPushButton(label)
            b.setFixedSize(52, 36)
            b.clicked.connect(lambda _, s=sign: self._jog_z(s))
            z_row.addWidget(b)
        z_row.addStretch()
        jlay.addLayout(z_row)
        ctrl.addWidget(jog_group)

        # Shutter buttons
        shutter_box = QGroupBox("Shutters")
        shut_lay = QGridLayout(shutter_box)
        for r, c, label, fn in [
            (0, 0, "Open pump",   self._open_pump),
            (0, 1, "Close pump",  self._close_pump),
            (1, 0, "Open probe",  self._open_probe),
            (1, 1, "Close probe", self._close_probe),
        ]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            shut_lay.addWidget(b, r, c)
        ctrl.addWidget(shutter_box)

        ctrl.addStretch()

        # Video
        self.video_label = DraggableVideoLabel()
        self.video_label.setFixedSize(640, 480)
        self.video_label.moved.connect(self._on_crosshair_moved)
        self.coord_label = QLabel("Crosshair: (0, 0)")
        vid.addWidget(self.video_label)
        vid.addWidget(self.coord_label)

        layout.addLayout(ctrl)
        layout.addLayout(vid)

    # ── Camera / joystick start-stop ──────────────────────────────────────────

    def _start_all(self):
        if self.cap is None:
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        self.timer.start(30)

        if self.btt is not None and self.galvo is not None:
            self.motor_thread  = QThread(self)
            self.motor_worker  = MotorWorker(self.btt, self.galvo, self.state)
            self.motor_worker.moveToThread(self.motor_thread)
            self.motor_worker.error.connect(self._on_motor_error)
            self.motor_thread.started.connect(self.motor_worker.start)
            self.motor_thread.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _stop_all(self):
        self.timer.stop()
        if self.cap:
            self.cap.release()
            self.cap = None

        if self.motor_worker is not None:
            self.motor_worker.stop()
        if self.motor_thread is not None:
            self.motor_thread.quit()
            self.motor_thread.wait()
            self.motor_worker.deleteLater()
            self.motor_thread.deleteLater()
            self.motor_worker = None
            self.motor_thread = None

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # ── Frame update ──────────────────────────────────────────────────────────

    def _update_frame(self):
        if not self.cap:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame.shape
        qimg = QImage(frame.data, w, h, ch * w, QImage.Format.Format_RGB888)
        self.video_label.setPixmap(QPixmap.fromImage(qimg))

    # ── Control handlers ──────────────────────────────────────────────────────

    def _on_brightness(self, val: int):
        if self.shutter:
            try:
                self.shutter.setLED(val)
            except Exception as e:
                QMessageBox.warning(self, "LED Error", str(e))

    def _on_galvo_go(self):
        if self.galvo is None:
            return
        try:
            x = float(self.galvo_x_edit.text())
            y = float(self.galvo_y_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Invalid galvo values.")
            return
        try:
            self.galvo.move_xy(x, y)
            self.state.settings["galvo_x_position"] = x
            self.state.settings["galvo_y_position"] = y
            self.controller.log(f"Galvo moved to ({x}, {y})")
        except Exception as e:
            QMessageBox.warning(self, "Galvo Error", str(e))

    def _on_stage_go(self):
        if self.stage is None:
            return
        try:
            self.stage.move_absolute(1, float(self.stage_pos_edit.text()))
        except Exception as e:
            QMessageBox.warning(self, "Stage Error", str(e))

    def _on_stage_speed(self):
        if self.stage is None:
            return
        try:
            self.stage.set_speed(1, float(self.stage_speed_edit.text()))
        except Exception as e:
            QMessageBox.warning(self, "Stage Error", str(e))

    def _jog_xy(self, dx_sign, dy_sign):
        if self.btt is None:
            return
        idx  = self._step_grp.checkedId()
        step = JOG_STEPS_XY[idx]
        feed = JOG_FEEDS_XY[idx]
        self.btt.cryoXY(round(dx_sign * step, 4), round(dy_sign * step, 4), feed)

    def _jog_z(self, sign):
        if self.btt is None:
            return
        idx  = self._step_grp.checkedId()
        step = JOG_STEPS_Z[idx]
        feed = JOG_FEEDS_Z[idx]
        self.btt.cryoZ(round(sign * step, 4), feed)

    def _open_pump(self):
        if self.shutter:
            self.shutter.openPump()
            self.state.settings["pump_shutter_open"] = True
            self.controller.log("Pump shutter opened (webcam tab)")

    def _close_pump(self):
        if self.shutter:
            self.shutter.closePump()
            self.state.settings["pump_shutter_open"] = False
            self.controller.log("Pump shutter closed (webcam tab)")

    def _open_probe(self):
        if self.shutter:
            self.shutter.openProbe()
            self.state.settings["probe_shutter_open"] = True
            self.controller.log("Probe shutter opened (webcam tab)")

    def _close_probe(self):
        if self.shutter:
            self.shutter.closeProbe()
            self.state.settings["probe_shutter_open"] = False
            self.controller.log("Probe shutter closed (webcam tab)")

    def _on_crosshair_moved(self, x, y):
        self.coord_label.setText(f"Crosshair: ({x}, {y})")

    def _on_motor_error(self, msg):
        QMessageBox.critical(self, "Motor / Joystick Error", msg)
        self._stop_all()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._stop_all()
        event.accept()
