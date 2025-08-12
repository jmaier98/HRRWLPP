# imaging_tab.py  – rewritten to avoid missed pixels
from __future__ import annotations
import math, numpy as np
from typing import List, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui  import QDoubleValidator, QImage, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QLineEdit, QPushButton, QMessageBox
)

# --------------------------------------------------------------------------
#  Worker that polls the scope – honours the poll_interval you ask for
# --------------------------------------------------------------------------
class ScopeWorker(QThread):
    new_block = pyqtSignal(list, list)          # analog, digital

    def __init__(self, scope, poll_interval: float = 0.001):
        super().__init__()
        self._scope = scope
        self._poll  = max(poll_interval, 0.0002)  # never <200 µs
        self._stop  = False

    def run(self):
        self._scope.start_stream()
        try:
            poll_ms = int(self._poll * 1000)
            while not self._stop:
                a, d = self._scope.get_latest_values()
                if a:                                   # only emit when data present
                    self.new_block.emit(a, d)
                self.msleep(poll_ms)                    # non‑blocking sleep
        finally:
            self._scope.stop_stream()

    def stop(self):
        self._stop = True


# --------------------------------------------------------------------------
#  Unchanged: GalvoWorker (included for completeness)
# --------------------------------------------------------------------------
class GalvoWorker(QThread):
    finished = pyqtSignal()
    def __init__(self, galvo, x_vals: np.ndarray, y_vals: np.ndarray,
                 dwell: float = 0.0005):
        super().__init__()
        self._galvo  = galvo
        self._x_vals = x_vals
        self._y_vals = y_vals
        self._dwell  = dwell
        self._stop   = False

    def run(self):
        for y in self._y_vals:
            if self._stop:
                break
            for x in self._x_vals:
                if self._stop:
                    break
                try:
                    self._galvo.move(float(x), float(y))
                except Exception as exc:
                    print("Galvo error:", exc)
                self.msleep(int(self._dwell * 1000))
            print(f"Galvo row {y} done.")
        self.finished.emit()

    def stop(self): self._stop = True


# --------------------------------------------------------------------------
#  Main tab
# --------------------------------------------------------------------------
class ImagingTab(QWidget):
    def __init__(self, instrument_manager, state):
        super().__init__()
        self.IM, self.state = instrument_manager, state
        self.scope = self.IM.get("Picoscope")
        self.galvo = self.IM.get("Galvo")

        self.image_data: np.ndarray | None = None
        self._prev_d = 0                # previous bit‑0 state
        self._points = 0
        self._build_ui()

    # ----------------------------- UI helpers (unchanged)
    def _make_line(self, label, layout, validator):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        edt = QLineEdit(); edt.setValidator(validator)
        row.addWidget(edt)
        layout.addLayout(row)
        return edt

    def _build_ui(self):
        ctrl = QVBoxLayout()
        self.x0_edit = self._make_line("X start (V):", ctrl, QDoubleValidator())
        self.x1_edit = self._make_line("X end   (V):", ctrl, QDoubleValidator())
        self.nx_edit = self._make_line("X points:",    ctrl, QDoubleValidator(1, 10000, 0))
        self.y0_edit = self._make_line("Y start (V):", ctrl, QDoubleValidator())
        self.y1_edit = self._make_line("Y end   (V):", ctrl, QDoubleValidator())
        self.ny_edit = self._make_line("Y points:",    ctrl, QDoubleValidator(1, 10000, 0))
        for w,d in [(self.x0_edit,"0.0"),(self.x1_edit,"0.1"),(self.nx_edit,"50"),
                    (self.y0_edit,"0.0"),(self.y1_edit,"0.1"),(self.ny_edit,"50")]:
            w.setText(d)

        self.start_btn, self.stop_btn = QPushButton("Start"), QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        ctrl.addWidget(self.start_btn); ctrl.addWidget(self.stop_btn); ctrl.addStretch()

        self.img_label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.img_label.setMinimumSize(400, 400)

        main = QHBoxLayout(self); main.addLayout(ctrl, 0); main.addWidget(self.img_label, 1)
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)

    # ----------------------------- scan start/stop
    def _validate_inputs(self):
        try:
            x0,x1,nx = float(self.x0_edit.text()), float(self.x1_edit.text()), int(float(self.nx_edit.text()))
            y0,y1,ny = float(self.y0_edit.text()), float(self.y1_edit.text()), int(float(self.ny_edit.text()))
            assert nx>0 and ny>0
            return x0,x1,nx,y0,y1,ny
        except Exception:
            QMessageBox.critical(self,"Input Error","Fill in all numeric fields (points ≥ 1).")
            return None

    def _alloc_image(self, ny,nx):
        self.image_data = np.full((ny,nx), np.nan, np.float32)
        self._cur_row = self._cur_col = 0
        self._points_needed = ny*nx
        self._points = 0

    def _on_start(self):
        p = self._validate_inputs(); # noqa: E275
        if p is None: return
        x0,x1,nx,y0,y1,ny = p
        self._alloc_image(ny,nx)
        x_vals,y_vals = (np.linspace(x0,x1,nx, dtype=np.float32),
                         np.linspace(y0,y1,ny, dtype=np.float32))

        self.scope_worker = ScopeWorker(self.scope, poll_interval=0.0008)
        self.scope_worker.new_block.connect(self._consume_scope_data)
        self.galvo_worker  = GalvoWorker(self.galvo,x_vals,y_vals,dwell=0.0005)
        self.galvo_worker.finished.connect(lambda: QTimer.singleShot(1000,self._on_stop))

        self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.galvo_worker.start();        self.scope_worker.start()

    def _on_stop(self):
        if hasattr(self,'galvo_worker'):  self.galvo_worker.stop()
        if hasattr(self,'scope_worker'):
            self.scope_worker.stop(); self.scope_worker.wait()
        self._finalize_scan()

    # ----------------------------- high‑speed edge processing
    @pyqtSlot(list, list)
    def _consume_scope_data(self, analog: List[int], digital: List[int]):
        if self.image_data is None or not analog:
            return

        # make same‑length NumPy vectors (very fast)
        n      = min(len(analog), len(digital))
        a_arr  = np.array(analog[:n],  dtype=np.int16)
        d_bits = (np.array(digital[:n],dtype=np.uint8) & 1)

        # detect rising edges of D0
        edges  = np.flatnonzero(np.diff(np.concatenate(([self._prev_d], d_bits))) == 1)
        self._prev_d = int(d_bits[-1])

        # store every pixel the galvo generated since last call
        for v in a_arr[edges]:
            if self._cur_row >= self.image_data.shape[0]:
                break                                   # image full
            self.image_data[self._cur_row, self._cur_col] = v
            self._cur_col += 1
            if self._cur_col >= self.image_data.shape[1]:
                self._cur_col  = 0
                self._cur_row += 1
                self._update_image(live=True)
            self._points += 1

        if self._points >= self._points_needed:
            self._on_stop()

    # ----------------------------- render helpers (unchanged but faster)
    def _update_image(self, live=False):
        if self.image_data is None: return
        if not live and self._points < self._points_needed: return
        data = self.image_data
        finite = data[np.isfinite(data)]
        if finite.size == 0: img8 = np.zeros_like(data, np.uint8)
        else:
            vmin,vmax = float(finite.min()), float(finite.max())
            if math.isclose(vmin,vmax): vmax = vmin+1
            img8 = np.clip((data-vmin)/(vmax-vmin),0,1)*255
            img8 = img8.astype(np.uint8)

        h,w = img8.shape
        qimg = QImage(img8.data, w,h,w,QImage.Format.Format_Grayscale8)
        pm   = QPixmap.fromImage(qimg).scaled(
                 self.img_label.size(),
                 Qt.AspectRatioMode.KeepAspectRatio,
                 Qt.TransformationMode.SmoothTransformation)
        self.img_label.setPixmap(pm)
        print(f"{self._points} / {self._points_needed} pixels")

    def _finalize_scan(self):
        self.start_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        self._update_image(live=False)
        self.scope_worker = self.galvo_worker = None
        self.image_data   = None
