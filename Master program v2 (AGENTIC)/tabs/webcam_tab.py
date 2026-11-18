# tabs/webcam_tab.py
import time
import io
import base64
import numpy as np

import cv2
import pygame

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QLabel, QSlider, QMessageBox, QLineEdit,
    QGroupBox, QGridLayout, QRadioButton, QButtonGroup,
)
from PyQt6.QtCore import (
    QTimer, Qt, QObject, QThread, pyqtSignal, pyqtSlot, QPoint, QMetaObject,
)
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

# The webcam image is mirrored top-to-bottom, so the displayed frame is flipped
# vertically.  Flip the cryo-Y command sign to match, keeping "up" on screen the
# same as "up" on the joystick / jog pad.
CRYO_Y_DIR = -1

# ── Mirror joystick mode (toggled with the A button) ─────────────────────────
# Right stick → mirror 1 (BTT X/Y motors, x/y offset); left stick → mirror 2
# (BTT Z/E motors, beam walk). D-pad nudges mirror 1 by a single microstep.
# B cycles the joystick speed.
BUTTON_A        = 0
BUTTON_B        = 1
# One move at a time: each joystick move is sent with M400 and the next is not
# sent until the board reports it finished, so commands can never pile up.
# Motion accumulated while a move runs goes out in the next one.
MIRROR_PERIOD   = 0.1    # s per move, so at most ~10 commands/s
MIRROR_POLL     = 0.02   # s between stick reads while idle
# Full-deflection speed in microsteps/s. The stick response is cubic, so half
# deflection gives 1/8 of this.
MIRROR_SPEEDS   = {"Fine": 100.0, "Medium": 400.0, "Rough": 2000.0}
MIRROR_DEFAULT_SPEED = "Fine"
# Flip any of these to -1 if a motor moves opposite to the stick.
MIRROR_DIR      = {"X": 1, "Y": -1, "Z": 1, "E": -1}


# ── Joystick movement logic ───────────────────────────────────────────────────

def _move_from_joystick(btt, galvo, joystick, state):
    pygame.event.pump()

    # Left stick → cryo XY
    ax = joystick.get_axis(0)
    ay = joystick.get_axis(1)
    if abs(ax) < DEADZONE: ax = 0
    if abs(ay) < DEADZONE: ay = 0
    dx = round(ax ** 3 * SCALE_XY, 3)
    dy = round(CRYO_Y_DIR * ay ** 3 * SCALE_XY, 3)
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


def _stick(joystick, axis):
    v = joystick.get_axis(axis)
    return 0.0 if abs(v) < DEADZONE else v


class _MirrorJoystick:
    """Turns stick deflection into gentle, microstep-resolution mirror moves.

    Fractional microsteps carry over between cycles, so even a barely
    deflected stick creeps the mirror along one microstep at a time.
    """

    def __init__(self, mirrors):
        self.mirrors   = mirrors
        self.speed     = MIRROR_SPEEDS[MIRROR_DEFAULT_SPEED]
        self._carry    = {ax: 0.0 for ax in MIRROR_DIR}
        self._nudge    = {}
        self._last_hat = (0, 0)
        self._t_last   = time.time()
        self._t_sent   = 0.0

    def reset(self):
        self._carry = {ax: 0.0 for ax in MIRROR_DIR}
        self._nudge = {}
        self._t_last = time.time()

    def step(self, joystick):
        now = time.time()
        dt = max(0.0, min(now - self._t_last, 2 * MIRROR_PERIOD))
        self._t_last = now

        sticks = {"X": _stick(joystick, 2), "Y": _stick(joystick, 3),
                  "Z": _stick(joystick, 0), "E": _stick(joystick, 1)}
        for ax, v in sticks.items():
            if v == 0:
                self._carry[ax] = 0.0           # stick released: stop, drop unsent motion
            else:
                self._carry[ax] += MIRROR_DIR[ax] * v ** 3 * self.speed * dt

        # D-pad: one microstep per press on mirror 1
        hat = joystick.get_hat(0) if joystick.get_numhats() else (0, 0)
        if hat != self._last_hat:
            if hat[0]:
                self._nudge["X"] = self._nudge.get("X", 0) + MIRROR_DIR["X"] * hat[0]
            if hat[1]:
                # hat y is +1 for up, stick y is -1 for up
                self._nudge["Y"] = self._nudge.get("Y", 0) - MIRROR_DIR["Y"] * hat[1]
            self._last_hat = hat

        # Motion accumulates between sends; send it once the controller is ready.
        whole = {ax: int(c) for ax, c in self._carry.items() if int(c)}   # toward zero
        deltas = dict(whole)
        for ax, n in self._nudge.items():
            deltas[ax] = deltas.get(ax, 0) + n
        deltas = {ax: d for ax, d in deltas.items() if d}
        if not any(sticks.values()) and not deltas:
            self._t_sent = now                  # idle: next move times from first input
        elapsed = now - self._t_sent
        if deltas and elapsed >= MIRROR_PERIOD:
            self._t_sent = now
            for ax, n in whole.items():
                self._carry[ax] -= n
            self._nudge = {}
            try:
                # Blocks until the board confirms the move has finished.
                self.mirrors.move_by(deltas, duration=MIRROR_PERIOD,
                                     wait=True)
            except TimeoutError as e:
                print(f"Mirror move not confirmed: {e}")
            return
        if not deltas:
            self._nudge = {}
        time.sleep(MIRROR_POLL)


# ── Motor worker ──────────────────────────────────────────────────────────────

class MotorWorker(QObject):
    error = pyqtSignal(str)
    mirror_mode_changed = pyqtSignal(bool)
    mirror_speed_cycle  = pyqtSignal()

    def __init__(self, btt, galvo, state, mirrors=None):
        super().__init__()
        self.btt     = btt
        self.galvo   = galvo
        self.state   = state
        self.mirrors = mirrors
        self.mirror_mode = False
        self.mirror_js = _MirrorJoystick(mirrors) if mirrors is not None else None
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
            self.btt.rail_2(50)
        except Exception as e:
            self.error.emit(str(e))
            return

        self._running = True
        a_was_down = b_was_down = False
        while self._running:
            try:
                pygame.event.pump()
                a_down = bool(self.joystick.get_button(BUTTON_A))
                if a_down and not a_was_down:
                    self._toggle_mirror_mode()
                a_was_down = a_down
                b_down = bool(self.joystick.get_button(BUTTON_B))
                if b_down and not b_was_down and self.mirrors is not None:
                    self.mirror_speed_cycle.emit()
                b_was_down = b_down

                if self.mirror_mode:
                    self.mirror_js.step(self.joystick)
                else:
                    _move_from_joystick(self.btt, self.galvo, self.joystick, self.state)
            except Exception:
                pass
            time.sleep(0.001)

        if self.mirrors is not None:
            self.mirrors.save_positions()
        pygame.joystick.quit()
        pygame.quit()
        self.btt.clear()

    def _toggle_mirror_mode(self):
        if self.mirrors is None:
            return
        self.mirror_mode = not self.mirror_mode
        if self.mirror_mode:
            self.mirror_js.reset()
        else:
            self.mirrors.save_positions()
        self.mirror_mode_changed.emit(self.mirror_mode)

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
        self.mirrors = controller.get_instrument("Mirrors")

        self.cap   = None
        self.timer = QTimer()
        self.timer.timeout.connect(self._update_frame)
        self.motor_thread  = None
        self.motor_worker  = None
        self._last_frame   = None   # latest RGB frame (for agent capture)

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

        # Motorized mirrors
        mbox = QGroupBox("Motorized mirrors (joystick: A = toggle, B = speed)")
        mlay = QGridLayout(mbox)
        self.mirror_mode_label = QLabel()
        mlay.addWidget(self.mirror_mode_label, 0, 0, 1, 4)
        mlay.addWidget(QLabel("Joystick speed:"), 1, 0)
        self._mirror_speed_grp = QButtonGroup(self)
        for i, name in enumerate(MIRROR_SPEEDS):
            rb = QRadioButton(name)
            rb.setChecked(name == MIRROR_DEFAULT_SPEED)
            rb.toggled.connect(self._on_mirror_speed)
            self._mirror_speed_grp.addButton(rb, i)
            mlay.addWidget(rb, 1, 1 + i)
        mlay.addWidget(QLabel("Mirror 1 X:"), 2, 0)
        self.mirror_x_edit = QLineEdit("0")
        self.mirror_x_edit.setValidator(QDoubleValidator())
        mlay.addWidget(self.mirror_x_edit, 2, 1)
        mlay.addWidget(QLabel("Y:"), 2, 2)
        self.mirror_y_edit = QLineEdit("0")
        self.mirror_y_edit.setValidator(QDoubleValidator())
        mlay.addWidget(self.mirror_y_edit, 2, 3)
        mirror_go = QPushButton("Go (µsteps)")
        mirror_go.clicked.connect(self._on_mirror_go)
        mlay.addWidget(mirror_go, 3, 0, 1, 2)
        mirror_zero = QPushButton("Zero both mirrors here")
        mirror_zero.clicked.connect(self._on_mirror_zero)
        mlay.addWidget(mirror_zero, 3, 2, 1, 2)
        self.mirror_pos_label = QLabel()
        mlay.addWidget(self.mirror_pos_label, 4, 0, 1, 4)
        mbox.setEnabled(self.mirrors is not None)
        ctrl.addWidget(mbox)
        self._set_mirror_mode_label(False)

        self._mirror_timer = QTimer(self)
        self._mirror_timer.timeout.connect(self._update_mirror_label)
        self._mirror_timer.start(200)
        self._update_mirror_label()

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

    @pyqtSlot()
    def _start_all(self):
        if self.cap is None:
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        self.timer.start(30)
        self.brightness_slider.setValue(self.brightness_slider.maximum())

        if self.btt is not None and self.galvo is not None:
            self.motor_thread  = QThread(self)
            self.motor_worker  = MotorWorker(self.btt, self.galvo, self.state, self.mirrors)
            self._on_mirror_speed()
            self.motor_worker.moveToThread(self.motor_thread)
            self.motor_worker.error.connect(self._on_motor_error)
            self.motor_worker.mirror_mode_changed.connect(self._set_mirror_mode_label)
            self.motor_worker.mirror_speed_cycle.connect(self._cycle_mirror_speed)
            self.motor_thread.started.connect(self.motor_worker.start)
            self.motor_thread.start()
        elif self.btt is not None:
            # No joystick thread (galvo unavailable) — nothing will re-home
            # rail 2, so move it directly.
            try:
                self.btt.rail_2(50)
            except Exception as e:
                QMessageBox.warning(self, "Rail Error", str(e))

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    @pyqtSlot()
    def _stop_all(self):
        self.timer.stop()
        if self.cap:
            self.cap.release()
            self.cap = None
        self._last_frame = None
        self.brightness_slider.setValue(0)

        if self.motor_worker is not None:
            self.motor_worker.stop()
        if self.motor_thread is not None:
            self.motor_thread.quit()
            self.motor_thread.wait()
            self.motor_worker.deleteLater()
            self.motor_thread.deleteLater()
            self.motor_worker = None
            self.motor_thread = None
        self._set_mirror_mode_label(False)

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # ── Frame update ──────────────────────────────────────────────────────────

    def _update_frame(self):
        if not self.cap:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        frame = cv2.flip(frame, 0)          # camera image is mirrored vertically
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self._last_frame = frame
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
        self.btt.cryoXY(round(dx_sign * step, 4),
                        round(CRYO_Y_DIR * dy_sign * step, 4), feed)

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

    # ── Mirrors ───────────────────────────────────────────────────────────────

    def _set_mirror_mode_label(self, on: bool):
        if self.mirrors is None:
            self.mirror_mode_label.setText("Mirror controller (COM5) not connected")
        elif on:
            self.mirror_mode_label.setText(
                "<b style='color:#c0392b'>Joystick → MIRRORS</b> "
                "(right = mirror 1, left = mirror 2, D-pad = 1 µstep on mirror 1, "
                "B = speed)")
        else:
            self.mirror_mode_label.setText("Joystick → cryo stage / galvo")

    def _update_mirror_label(self):
        if self.mirrors is None:
            return
        p = self.mirrors.pos
        self.mirror_pos_label.setText(
            f"Mirror 1: X {p['X']}  Y {p['Y']}    Mirror 2: X {p['Z']}  Y {p['E']}  (µsteps)")

    def _cycle_mirror_speed(self):
        n = len(MIRROR_SPEEDS)
        nxt = (self._mirror_speed_grp.checkedId() + 1) % n
        self._mirror_speed_grp.button(nxt).setChecked(True)   # → _on_mirror_speed

    def _on_mirror_speed(self, *_):
        if self.motor_worker is None or self.motor_worker.mirror_js is None:
            return
        name = list(MIRROR_SPEEDS)[self._mirror_speed_grp.checkedId()]
        self.motor_worker.mirror_js.speed = MIRROR_SPEEDS[name]

    def _on_mirror_go(self):
        if self.mirrors is None:
            return
        try:
            x = float(self.mirror_x_edit.text())
            y = float(self.mirror_y_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Invalid mirror values.")
            return
        try:
            self.mirrors.move_to({"X": x, "Y": y})
            self.mirrors.save_positions()
            self.controller.log(f"Mirror 1 moved to ({x:.0f}, {y:.0f}) µsteps")
        except Exception as e:
            QMessageBox.warning(self, "Mirror Error", str(e))

    def _on_mirror_zero(self):
        if self.mirrors is None:
            return
        try:
            self.mirrors.zero()
            self.controller.log("Mirror positions zeroed")
        except Exception as e:
            QMessageBox.warning(self, "Mirror Error", str(e))

    def _on_crosshair_moved(self, x, y):
        self.coord_label.setText(f"Crosshair: ({x}, {y})")

    def _on_motor_error(self, msg):
        QMessageBox.critical(self, "Motor / Joystick Error", msg)
        self._stop_all()

    # ── Agent API ─────────────────────────────────────────────────────────────

    _JOG_NAMES = ["rough", "medium", "fine"]

    def get_settings(self) -> dict:
        idx = self._step_grp.checkedId()
        return {
            "camera_running":     self.cap is not None,
            "joystick_running":   self.motor_worker is not None,
            "led_brightness":     self.brightness_slider.value(),
            "galvo_x":            self.galvo_x_edit.text(),
            "galvo_y":            self.galvo_y_edit.text(),
            "stage_position_mm":  self.stage_pos_edit.text(),
            "stage_speed_mm_s":   self.stage_speed_edit.text(),
            "jog_step":           self._JOG_NAMES[idx] if 0 <= idx < 3 else None,
            "pump_shutter_open":  self.state.settings.get("pump_shutter_open"),
            "probe_shutter_open": self.state.settings.get("probe_shutter_open"),
            "galvo_limit":        GALVO_LIMIT,
            "jog_step_options":   self._JOG_NAMES,
            "jog_step_sizes_xy":  JOG_STEPS_XY,
            "jog_step_sizes_z":   JOG_STEPS_Z,
            "mirrors_connected":  self.mirrors is not None,
            "joystick_mirror_mode": bool(self.motor_worker and self.motor_worker.mirror_mode),
            "mirror_positions_usteps": dict(self.mirrors.pos) if self.mirrors else None,
        }

    def apply_settings(self, settings: dict) -> list[str]:
        """Apply a subset of webcam-tab controls. Returns a list of warnings."""
        warnings: list[str] = []

        # ── LED brightness ────────────────────────────────────────────────────
        if "led_brightness" in settings:
            try:
                v = max(0, min(255, int(round(float(settings["led_brightness"])))))
                if self.shutter is not None:
                    self.shutter.setLED(v)
                self.brightness_slider.blockSignals(True)
                self.brightness_slider.setValue(v)
                self.brightness_slider.blockSignals(False)
                self.controller.log(f"Agent set LED brightness {v}")
            except Exception as e:
                warnings.append(f"led_brightness: {e}")

        # ── Galvo position ────────────────────────────────────────────────────
        if "galvo_x" in settings or "galvo_y" in settings:
            if self.galvo is None:
                warnings.append("Galvo not available")
            else:
                try:
                    x = float(settings.get("galvo_x", self.galvo_x_edit.text()))
                    y = float(settings.get("galvo_y", self.galvo_y_edit.text()))
                    x = max(min(x, GALVO_LIMIT), -GALVO_LIMIT)
                    y = max(min(y, GALVO_LIMIT), -GALVO_LIMIT)
                    self.galvo.move_xy(x, y)
                    self.state.settings["galvo_x_position"] = x
                    self.state.settings["galvo_y_position"] = y
                    self.galvo_x_edit.setText(str(x))
                    self.galvo_y_edit.setText(str(y))
                    self.controller.log(f"Agent galvo -> ({x}, {y})")
                except Exception as e:
                    warnings.append(f"galvo: {e}")

        # ── Delay stage position ──────────────────────────────────────────────
        if "stage_position_mm" in settings:
            if self.stage is None:
                warnings.append("Stage not available")
            else:
                try:
                    pos = float(settings["stage_position_mm"])
                    self.stage.move_absolute(1, pos)
                    self.stage_pos_edit.setText(str(pos))
                    self.controller.log(f"Agent stage -> {pos} mm")
                except Exception as e:
                    warnings.append(f"stage_position_mm: {e}")

        # ── Delay stage speed ─────────────────────────────────────────────────
        if "stage_speed_mm_s" in settings:
            if self.stage is None:
                warnings.append("Stage not available")
            else:
                try:
                    spd = float(settings["stage_speed_mm_s"])
                    self.stage.set_speed(1, spd)
                    self.stage_speed_edit.setText(str(spd))
                    self.controller.log(f"Agent stage speed -> {spd} mm/s")
                except Exception as e:
                    warnings.append(f"stage_speed_mm_s: {e}")

        # ── Jog step selection ────────────────────────────────────────────────
        if "jog_step" in settings:
            key = str(settings["jog_step"]).lower()
            if key in self._JOG_NAMES:
                self._step_grp.button(self._JOG_NAMES.index(key)).setChecked(True)
            else:
                warnings.append("jog_step must be one of rough/medium/fine")

        # ── Shutters ──────────────────────────────────────────────────────────
        if "pump_shutter_open" in settings:
            if self.shutter is None:
                warnings.append("Shutter not available")
            else:
                (self._open_pump if bool(settings["pump_shutter_open"])
                 else self._close_pump)()
        if "probe_shutter_open" in settings:
            if self.shutter is None:
                warnings.append("Shutter not available")
            else:
                (self._open_probe if bool(settings["probe_shutter_open"])
                 else self._close_probe)()

        # ── Camera / joystick live view (run on the GUI thread) ───────────────
        if "camera_on" in settings:
            slot = "_start_all" if bool(settings["camera_on"]) else "_stop_all"
            QMetaObject.invokeMethod(self, slot, Qt.ConnectionType.QueuedConnection)

        return warnings

    def jog(self, axis: str, direction: str, count: int = 1,
            step: str | None = None) -> dict:
        """Jog the cryo stage. axis = x/y/z, direction = '+'/'-'."""
        if self.btt is None:
            return {"error": "BTT (cryo stage) not available"}
        if step is not None:
            key = str(step).lower()
            if key in self._JOG_NAMES:
                self._step_grp.button(self._JOG_NAMES.index(key)).setChecked(True)
            else:
                return {"error": "step must be one of rough/medium/fine"}

        ax   = str(axis).lower()
        sign = 1 if str(direction).strip().startswith(("+", "u", "p")) else -1
        n    = max(1, int(count))
        if ax not in ("x", "y", "z"):
            return {"error": "axis must be x, y, or z"}

        for _ in range(n):
            if ax == "x":
                self._jog_xy(sign, 0)
            elif ax == "y":
                self._jog_xy(0, sign)
            else:
                self._jog_z(sign)

        idx = self._step_grp.checkedId()
        self.controller.log(f"Agent jog {ax}{'+' if sign > 0 else '-'} x{n}")
        return {"status": "ok", "axis": ax, "direction": "+" if sign > 0 else "-",
                "count": n, "step": self._JOG_NAMES[idx] if 0 <= idx < 3 else None}

    def capture_frame_b64(self) -> str | None:
        """Grab the current webcam frame as a base64 PNG, or None if unavailable."""
        frame = None
        if self.cap is not None and self._last_frame is not None:
            frame = self._last_frame
        else:
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            try:
                ok, f = False, None
                for _ in range(5):          # warm up auto-exposure
                    ok, f = cap.read()
                if ok and f is not None:
                    frame = cv2.cvtColor(cv2.flip(f, 0), cv2.COLOR_BGR2RGB)
            finally:
                cap.release()
        if frame is None:
            return None
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(frame).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._stop_all()
        event.accept()
