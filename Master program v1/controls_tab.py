import time
from functools import partial
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QMessageBox
)

# Default feedrate for angular motions (degrees/min)
DEFAULT_FEEDRATE = 720

class ControlsTab(QWidget):
    def __init__(self, instrument_manager):
        super().__init__()
        self.IM = instrument_manager
        # retrieve BTT controller
        try:
            self.btt = self.IM.get("BTT")
        except KeyError:
            raise RuntimeError("BTT controller not found in InstrumentManager")

        # top-level layout
        main_layout = QVBoxLayout(self)
        btn_layout = QHBoxLayout()
        ctrl_layout = QVBoxLayout()
        main_layout.addLayout(btn_layout)
        main_layout.addLayout(ctrl_layout)

        # shared controls
        self.move_all_btn = QPushButton("Move All")
        self.move_all_btn.clicked.connect(self.move_all)
        self.home_all_btn = QPushButton("Home All")
        self.home_all_btn.clicked.connect(self.home_all)
        btn_layout.addWidget(self.move_all_btn)
        btn_layout.addWidget(self.home_all_btn)
        btn_layout.addStretch()

        # per-controller widgets storage
        self.angle_entries = {}
        self.move_buttons = {}
        self.home_buttons = {}

        # build individual controller controls
        for idx in (1, 2, 3):
            # label + angle entry
            label = QLabel(f"Controller {idx} Angle (°):")
            angle_edit = QLineEdit()
            angle_edit.setPlaceholderText("0–360")
            # action buttons
            move_btn = QPushButton("Move")
            move_btn.clicked.connect(partial(self._move_one, idx))
            home_btn = QPushButton("Home")
            home_btn.clicked.connect(partial(self._home_one, idx))
            
            # store
            self.angle_entries[idx] = angle_edit
            self.move_buttons[idx] = move_btn
            self.home_buttons[idx] = home_btn

            # layout row
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(angle_edit)
            row.addWidget(move_btn)
            row.addWidget(home_btn)
            ctrl_layout.addLayout(row)

        ctrl_layout.addStretch()

    def move_all(self):
        """Move all controllers to their entered angles."""
        for idx in (1, 2, 3):
            self._move_one(idx)

    def home_all(self):
        """Home all controllers."""
        try:
            self.btt.home_rot1()
            self.btt.home_rot2()
            self.btt.home_rot3()
        except Exception as e:
            QMessageBox.critical(self, "Controllers Error", f"Home All failed: {e}")

    def _move_one(self, idx):
        """Move a single controller to the entered angle."""
        text = self.angle_entries[idx].text().strip()
        try:
            angle = float(text)
        except ValueError:
            return self._error(idx, "Enter a valid angle number.")
        if not 0 <= angle <= 360:
            return self._error(idx, "Angle must be between 0 and 360°.")

        try:
            # dispatch to BTT
            if idx == 1:
                self.btt.rot_1(angle, DEFAULT_FEEDRATE)
            elif idx == 2:
                self.btt.rot_2(angle, DEFAULT_FEEDRATE)
            else:
                self.btt.rot_3(angle, DEFAULT_FEEDRATE)
        except Exception as e:
            self._error(idx, f"Move failed: {e}")

    def _home_one(self, idx):
        """Home a single controller."""
        try:
            if idx == 1:
                self.btt.home_rot1()
            elif idx == 2:
                self.btt.home_rot2()
            else:
                self.btt.home_rot3()
        except Exception as e:
            self._error(idx, f"Home failed: {e}")

    def _error(self, idx, msg):
        """Show an error dialog for a specific controller."""
        QMessageBox.critical(self,
            "Controllers Error",
            f"Controller {idx}: {msg}"
        )
"""
