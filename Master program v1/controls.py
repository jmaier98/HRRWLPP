from __future__ import annotations

from functools import partial
from typing import Callable, Mapping

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

DEGREE_SYMBOL = "\N{DEGREE SIGN}"
DEFAULT_FEEDRATE = 720  # ° min⁻¹ – tweak to taste


class RotationMountTab(QWidget):
    """PyQt tab controlling three rotation mounts via an InstrumentManager."""

    _MOUNT_KEYS: Mapping[int, str] = {1: "Mount1", 2: "Mount2", 3: "Mount3"}

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def __init__(self, instrument_manager: Mapping[str, object], parent: QWidget | None = None):
        super().__init__(parent)
        self.im = instrument_manager

        self.setWindowTitle("Rotation Mount Control")

        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # build three identical panels
        for idx in (1, 2, 3):
            main_layout.addWidget(self._build_panel(idx))

        main_layout.addStretch()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _build_panel(self, idx: int) -> QGroupBox:
        box = QGroupBox(f"Rotation Mount {idx}")
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        form = QFormLayout(box)

        angle_edit = QLineEdit()
        angle_edit.setPlaceholderText(f"Target angle (0–360{DEGREE_SYMBOL})")

        # buttons
        move_btn = QPushButton("Move")
        home_btn = QPushButton("Home")

        # wiring
        move_btn.clicked.connect(partial(self._move_clicked, idx, angle_edit))
        home_btn.clicked.connect(partial(self._home_clicked, idx))

        # layout
        btn_row = QHBoxLayout()
        btn_row.addWidget(move_btn)
        btn_row.addWidget(home_btn)

        form.addRow(QLabel("Target Angle"), angle_edit)
        form.addRow(btn_row)

        return box

    # ------------------------------------------------------------------
    # slot implementations
    # ------------------------------------------------------------------

    def _move_clicked(self, idx: int, angle_edit: QLineEdit) -> None:
        text = angle_edit.text().strip()
        try:
            angle = float(text)
        except ValueError:
            self._error(idx, "Enter a valid number for the angle.")
            return

        if not 0 <= angle <= 360:
            self._error(idx, f"Angle must be between 0 and 360{DEGREE_SYMBOL}.")
            return

        mount = self._mount(idx)
        if mount is None:
            return

        self._invoke(mount, "move_to_angle", idx, angle, DEFAULT_FEEDRATE)

    def _home_clicked(self, idx: int) -> None:
        mount = self._mount(idx)
        if mount is None:
            return

        self._invoke(mount, "home", idx)

    # ------------------------------------------------------------------
    # low‑level helpers
    # ------------------------------------------------------------------

    def _mount(self, idx: int):
        key = self._MOUNT_KEYS[idx]
        try:
            return self.im[key]
        except KeyError:
            self._error(idx, f"InstrumentManager lacks entry '{key}'.")
            return None

    def _invoke(self, mount, method_name: str, idx: int, *args):
        try:
            method: Callable = getattr(mount, method_name)
        except AttributeError:
            self._error(idx, f"Mount driver lacks method '{method_name}()'.")
            return

        try:
            method(*args)
        except Exception as exc:  # noqa: BLE001
            self._error(idx, f"Driver error: {exc}")

    # ------------------------------------------------------------------
    # message box helper
    # ------------------------------------------------------------------

    def _error(self, idx: int, msg: str) -> None:
        QMessageBox.critical(self, "Rotation Mount Error", f"Mount {idx}: {msg}")
