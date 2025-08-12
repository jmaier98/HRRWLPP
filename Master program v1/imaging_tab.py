"""
imaging_tab.py  – GUI tab for 2‑D scanning
-------------------------------------------------
• Left‑hand panel: scan parameters and Start/Stop buttons.
• Right‑hand panel: live (or final) image of the acquired data.

Threads
=======
UI thread        – all Qt widgets and painting.
ScopeWorker       – polls the Picoscope driver and emits new data blocks.
GalvoWorker       – sends the X/Y drive pattern to the galvo controller.

Both workers are independent and can be re‑used elsewhere.
"""

from __future__ import annotations

import time, math, queue, ctypes, numpy as np
from typing import List, Tuple

from PyQt6.QtCore    import Qt, QThread, QObject, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui     import QDoubleValidator, QImage, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QLineEdit, QPushButton, QMessageBox
)

# -----------------------------------------------------------------------------
#  Worker threads
# -----------------------------------------------------------------------------
class ScopeWorker(QThread):
    """Background thread that continuously pulls data from a Picoscope driver."""

    new_block = pyqtSignal(list, list)  # analog, digital

    def __init__(self, scope, poll_interval: float = 0.002):
        super().__init__()
        self._scope = scope
        self._poll  = poll_interval
        self._stop  = False
        

    def run(self):
        self._scope.start_stream()
        try:
            while not self._stop:
                a, d = self._scope.get_latest_values()
                if a:                       # only emit when we have data
                    self.new_block.emit(a, d)
                time.sleep(.002)      # wait before next poll
        finally:
            self._scope.stop_stream()

    def stop(self):
        self._stop = True


class GalvoWorker(QThread):
    """Sweeps the galvo through the provided X/Y grid once."""

    finished = pyqtSignal()
    time.sleep(0.1)
    def __init__(self, galvo, x_vals: np.ndarray, y_vals: np.ndarray, dwell: float = 0.0005):
        super().__init__()
        self._galvo = galvo
        self._x_vals = x_vals
        self._y_vals = y_vals
        self._dwell  = dwell    # seconds per pixel
        self._stop   = False

    def run(self):
        try:
            for y in self._y_vals:
                if self._stop: break
                for x in self._x_vals:
                    if self._stop: break
                    try:
                        self._galvo.move(float(x), float(y))
                    except Exception as exc:
                        print("Galvo error:", exc)
                    self.msleep(int(self._dwell * 1000))
                print(f"Galvo row {y} done.")
        finally:
            time.sleep(.5)
            self.finished.emit()

    def stop(self):
        self._stop = True


# -----------------------------------------------------------------------------
#  Main ImagingTab widget
# -----------------------------------------------------------------------------
class ImagingTab(QWidget):
    """Tab for live 2‑D imaging via a PicoScope + galvo."""

    def __init__(self, instrument_manager, state):
        super().__init__()
        self.IM    = instrument_manager
        self.state = state

        # --- drivers ---
        self.scope = self.IM.get("Picoscope")
        self.galvo = self.IM.get("Galvo")

        # image buffer & cursor
        self.image_data: np.ndarray | None = None
        self.cur_row = 0
        self.cur_col = 0

        self._prev_d = 0  # previous digital value for edge detection
        self.points = 0
        self._build_ui()

    # --------------------------------------------------------------------- UI
    def _make_line(self, label: str, layout, validator):
        row = QHBoxLayout()
        lbl = QLabel(label)
        edt = QLineEdit()
        edt.setValidator(validator)
        row.addWidget(lbl)
        row.addWidget(edt)
        layout.addLayout(row)
        return edt

    def _build_ui(self):
        ctrl = QVBoxLayout()

        # numeric inputs
        self.x0_edit = self._make_line("X start (V):", ctrl, QDoubleValidator())
        self.x1_edit = self._make_line("X end   (V):", ctrl, QDoubleValidator())
        self.nx_edit = self._make_line("X points:",   ctrl, QDoubleValidator(1, 10000, 0))

        self.y0_edit = self._make_line("Y start (V):", ctrl, QDoubleValidator())
        self.y1_edit = self._make_line("Y end   (V):", ctrl, QDoubleValidator())
        self.ny_edit = self._make_line("Y points:",   ctrl, QDoubleValidator(1, 10000, 0))

        # default values for convenience
        self.x0_edit.setText("0.0"); self.x1_edit.setText("0.1"); self.nx_edit.setText("50")
        self.y0_edit.setText("0.0"); self.y1_edit.setText("0.1"); self.ny_edit.setText("50")

        # start / stop
        self.start_btn = QPushButton("Start")
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        ctrl.addWidget(self.start_btn)
        ctrl.addWidget(self.stop_btn)
        ctrl.addStretch()

        # image display
        self.img_label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.img_label.setMinimumSize(400, 400)

        # split layout
        main_lay = QHBoxLayout(self)
        main_lay.addLayout(ctrl, 0)
        main_lay.addWidget(self.img_label, 1)

        # connections
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)

    # ---------------------------------------------------------- scan helpers
    def _validate_inputs(self):
        try:
            x0 = float(self.x0_edit.text())
            x1 = float(self.x1_edit.text())
            nx = int(float(self.nx_edit.text()))
            y0 = float(self.y0_edit.text())
            y1 = float(self.y1_edit.text())
            ny = int(float(self.ny_edit.text()))
        except ValueError:
            QMessageBox.critical(self, "Input Error", "All fields must be filled with numbers.")
            return None
        if nx < 1 or ny < 1:
            QMessageBox.critical(self, "Input Error", "Point counts must be >= 1.")
            return None
        return x0, x1, nx, y0, y1, ny

    def _alloc_image(self, ny, nx):
        self.image_data = np.zeros((ny, nx), dtype=np.float32)
        self.cur_row = 0
        self.cur_col = 0

    # ------------------------------------------------------------ UI slots
    def _on_start(self):
        args = self._validate_inputs()
        if args is None:
            return
        x0, x1, nx, y0, y1, ny = args

        # allocate buffer
        self._alloc_image(ny, nx)

        # build scan vectors
        x_vals = np.linspace(x0, x1, nx, dtype=np.float32)
        y_vals = np.linspace(y0, y1, ny, dtype=np.float32)

        # --- Scope worker ---
        self.scope_worker = ScopeWorker(self.scope, poll_interval=0.006)
        self.scope_worker.new_block.connect(self._consume_scope_data)
        # --- Galvo worker ---
        time.sleep(0.3)
        self.galvo_worker = GalvoWorker(self.galvo, x_vals, y_vals, dwell=0.0005)
        self.galvo_worker.finished.connect(self._on_galvo_done)

        

        # start
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.galvo_worker.start()
        self.scope_worker.start()

    def _on_stop(self):
        if hasattr(self, 'galvo_worker'):
            self.galvo_worker.stop()
        if hasattr(self, 'scope_worker'):
            self.scope_worker.stop()
            self.scope_worker.wait()
        self._finalize_scan()

    def _on_galvo_done(self):
        # when galvo finishes on its own, let the scope thread drain a little, then stop
        QTimer.singleShot(1000, self._on_stop)

    # --------------------------------------------- data handling / imaging
    @pyqtSlot(list, list)
    def _consume_scope_data(self, analog: List[int], digital: List[int]):
        if self.image_data is None:
            return  # scan not started

        for a, d in zip(analog, digital):
            rising_edge = (self._prev_d == 0) and (d & 0x01)
            self._prev_d = d & 0x01
            if not rising_edge:
                continue
            self.points += 1

            # store pixel
            if self.cur_row < self.image_data.shape[0]:
                self.image_data[self.cur_row, self.cur_col] = a
                self.cur_col += 1
                if self.cur_col >= self.image_data.shape[1]:
                    self.cur_col = 0
                    self.cur_row += 1

            # once per row, update the on‑screen image for feedback
            if self.cur_col == 0:
                self._update_image(live=True)

            # stop early if buffer filled
            if self.cur_row >= self.image_data.shape[0]:
                self._on_stop()
                print(f"Scan complete: {self.points} points acquired.")
                break

    def _update_image(self, live=False):
        if self.image_data is None:
            return
        data = self.image_data
        if not live and (self.cur_row < data.shape[0]):
            return  # don't render final until full
        img8 = self._normalise_to_u8(data)
        h, w = img8.shape
        qimg = QImage(img8.data, w, h, w, QImage.Format.Format_Grayscale8)
        pm   = QPixmap.fromImage(qimg).scaled(self.img_label.size(),
                                             Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
        self.img_label.setPixmap(pm)
        print(self.points, "points acquired so far.")

    def _normalise_to_u8(self, arr: np.ndarray) -> np.ndarray:
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros_like(arr, dtype=np.uint8)
        vmin, vmax = float(finite.min()), float(finite.max())
        if math.isclose(vmin, vmax):
            vmax = vmin + 1.0
        norm = np.clip((arr - vmin) / (vmax - vmin), 0, 1)
        return (norm * 255).astype(np.uint8)

    def _finalize_scan(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._update_image(live=False)
        self.galvo_worker = None
        self.scope_worker = None
        self.image_data   = None
