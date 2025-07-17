from __future__ import annotations

from functools import partial
from typing import Callable

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
DEFAULT_FEEDRATE = 720  # ° min⁻¹ — tune for your hardware


class ControlsTab(QWidget):
    """GUI tab controlling three rotation mounts via an InstrumentManager."""

    _KEYS = {1: "Mount1", 2: "Mount2", 3: "Mount3"}

    # ------------------------------------------------------------------
    # construction — mirrors powermeter_tab.py
    # ------------------------------------------------------------------

    def __init__(self, instrument_manager):
        super().__init__()
        self.IM = instrument_manager

        # ─── overall vertical layout ───────────────────────────────────
        main_layout = QVBoxLayout(self)

        # ─── 1. button bar (Horizontal) ────────────────────────────────
        btn_layout = QHBoxLayout()
        main_layout.addLayout(btn_layout)

        self.home_all_btn = QPushButton("Home All")
        self.home_all_btn.clicked.connect(self.home_all)
        btn_layout.addWidget(self.home_all_btn)

        self.stop_all_btn = QPushButton("Stop All")
        self.stop_all_btn.setEnabled(False)  # reserved for future use
        self.stop_all_btn.clicked.connect(self.stop_all)
        btn_layout.addWidget(self.stop_all_btn)

        btn_layout.addStretch()

        # ─── 2. control column (Vertical) ──────────────────────────────
        ctrl_layout = QVBoxLayout()
        main_layout.addLayout(ctrl_layout)

        for idx in (1, 2, 3):
            ctrl_layout.addWidget(self._build_panel(idx))

        ctrl_layout.addStretch()

        # (powermeter_tab shows a plot + large numeric label here; we skip.)

    # ------------------------------------------------------------------
    # build each mount sub‑panel
    # ------------------------------------------------------------------

    def _build_panel(self, idx: int) -> QGroupBox:
        box = QGroupBox(f"Rotation Mount {idx}")
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        form = QFormLayout(box)

        angle_edit = QLineEdit()
        angle_edit.setPlaceholderText(f"Target angle (0–360{DEGREE_SYMBOL})")

        move_btn = QPushButton("Move")
        move_btn.clicked.connect(partial(self._move_clicked, idx, angle_edit))

        home_btn = QPushButton("Home")
        home_btn.clicked.connect(partial(self._home_clicked, idx))

        # lay out widgets
        form.addRow(QLabel("Target Angle"), angle_edit)
        btn_row = QHBoxLayout(); btn_row.addWidget(move_btn); btn_row.addWidget(home_btn)
        form.addRow(btn_row)

        return box

    # ------------------------------------------------------------------
    # top‑bar actions
    # ------------------------------------------------------------------

    def home_all(self):
        for idx in (1, 2, 3):
            self._home_clicked(idx)
        self.stop_all_btn.setEnabled(True)

    def stop_all(self):
        # placeholder; if mounts supported stop/abort we'd call it here.
        self.stop_all_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # per‑mount actions
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
        key = self._KEYS[idx]
        try:
            return self.IM.get(key)
        except KeyError:
            self._error(idx, f"InstrumentManager is missing '{key}'.")
            return None

    def _invoke(self, mount, method_name: str, idx: int, *args):
        try:
            method: Callable = getattr(mount, method_name)
        except AttributeError:
            self._error(idx, f"Mount driver lacks '{method_name}()'.")
            return
        try:
            method(*args)
        except Exception as exc:  # noqa: BLE001
            self._error(idx, f"Driver error: {exc}")

    # ------------------------------------------------------------------
    # message helper
    # ------------------------------------------------------------------

    def _error(self, idx: int, msg: str) -> None:
        QMessageBox.critical(self, "Rotation Mount Error", f"Mount {idx}: {msg}")
