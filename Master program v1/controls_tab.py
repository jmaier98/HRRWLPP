from functools import partial
from typing import Callable

from PyQt6.QtWidgets import (
    QHBoxLayout, QLineEdit, QMessageBox, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget, QGroupBox, QFormLayout, QLabel
)

DEGREE_SYMBOL = "\N{DEGREE SIGN}"
DEFAULT_FEEDRATE = 720

class ControlsTab(QWidget):
    _KEYS = {1: "Mount1", 2: "Mount2", 3: "Mount3"}

    def __init__(self, instrument_manager):
        super().__init__()
        self.IM = instrument_manager
        # store controls for external access
        self.angle_edits = {}
        self.move_buttons = {}
        self.home_buttons = {}

        main_layout = QVBoxLayout(self)
        # top button bar
        btn_bar = QHBoxLayout()
        main_layout.addLayout(btn_bar)
        self.home_all_btn = QPushButton("Home All")
        self.home_all_btn.clicked.connect(self.home_all)
        btn_bar.addWidget(self.home_all_btn)
        self.stop_all_btn = QPushButton("Stop All")
        self.stop_all_btn.setEnabled(False)
        self.stop_all_btn.clicked.connect(self.stop_all)
        btn_bar.addWidget(self.stop_all_btn)
        btn_bar.addStretch()

        # control panels
        ctrl_col = QVBoxLayout()
        main_layout.addLayout(ctrl_col)
        for idx in (1, 2, 3):
            # create widgets
            angle_edit = QLineEdit()
            angle_edit.setPlaceholderText(f"Target angle (0–360{DEGREE_SYMBOL})")
            move_btn = QPushButton("Move")
            home_btn = QPushButton("Home")
            # store
            self.angle_edits[idx] = angle_edit
            self.move_buttons[idx] = move_btn
            self.home_buttons[idx] = home_btn
            # connect
            move_btn.clicked.connect(partial(self._move_clicked, idx))
            home_btn.clicked.connect(partial(self._home_clicked, idx))
            # build panel
            panel = QGroupBox(f"Rotation Mount {idx}")
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            form = QFormLayout(panel)
            form.addRow(QLabel("Target Angle"), angle_edit)
            row = QHBoxLayout()
            row.addWidget(move_btn); row.addWidget(home_btn)
            form.addRow(row)
            ctrl_col.addWidget(panel)
        ctrl_col.addStretch()

    def home_all(self):
        for idx in (1,2,3): self._home_clicked(idx)
        self.stop_all_btn.setEnabled(True)

    def stop_all(self):
        self.stop_all_btn.setEnabled(False)

    def _move_clicked(self, idx: int):
        text = self.angle_edits[idx].text().strip()
        try:
            angle = float(text)
        except ValueError:
            return self._error(idx, "Enter a valid number for the angle.")
        if not 0 <= angle <= 360:
            return self._error(idx, f"Angle must be 0–360{DEGREE_SYMBOL}.")
        mount = self._mount(idx)
        if not mount: return
        self._invoke(mount, "move_to_angle", idx, angle, DEFAULT_FEEDRATE)

    def _home_clicked(self, idx: int):
        mount = self._mount(idx)
        if not mount: return
        self._invoke(mount, "home", idx)

    def _mount(self, idx: int):
        key = self._KEYS[idx]
        try:
            return self.IM[key]
        except KeyError:
            self._error(idx, f"Missing '{key}' in InstrumentManager.")
            return None

    def _invoke(self, mount, method: str, idx: int, *args):
        try:
            func: Callable = getattr(mount, method)
        except AttributeError:
            return self._error(idx, f"Driver lacks '{method}()'.")
        try:
            func(*args)
        except Exception as e:
            self._error(idx, f"Driver error: {e}")

    def _error(self, idx: int, msg: str):
        QMessageBox.critical(self, "Rotation Mount Error", f"Mount {idx}: {msg}")
