

from __future__ import annotations
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.axes_grid1 import make_axes_locatable
import time, math, queue, ctypes, numpy as np, threading
from typing import List, Tuple
from picosdk.ps5000a import ps5000a as ps
import matplotlib.cm as cm
from picosdk.functions import assert_pico_ok
from PyQt6.QtCore    import Qt, QThread, QObject, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui     import QDoubleValidator, QImage, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QLineEdit, QPushButton, QMessageBox, QSizePolicy
)




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
        self.btt = self.IM.get("BTT")

        # image buffer & cursor
        self.image_data: np.ndarray | None = None
        self.cur_row = 0
        self.cur_col = 0

        self._prev_d = 0  # previous digital value for edge detection
        self.points = 0
        self._build_ui()
        overview_size    = 8192
        self.chandle = self.scope.get_chandle()
        self.buffer_a = np.zeros(overview_size, dtype=np.int16)
        self.buffer_d = np.zeros(overview_size, dtype=np.int16)
        self.adc_values = []
        self.digital_values = []
        self.c_callback = ps.StreamingReadyType(self.streaming_callback)
        self.stop_event = threading.Event()
        

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
        self.x0_edit.setText("-.05"); self.x1_edit.setText(".05"); self.nx_edit.setText("50")
        self.y0_edit.setText("-.05"); self.y1_edit.setText(".05"); self.ny_edit.setText("50")

        # start / stop
        self.start_btn = QPushButton("Start")
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        ctrl.addWidget(self.start_btn)
        ctrl.addWidget(self.stop_btn)
        ctrl.addStretch()

        # —– change focus input + button —–
        focus_row = QHBoxLayout()
        focus_label = QLabel("Change focus (Z):")
        self.focus_edit = QLineEdit()
        # only allow floats (you can tighten range if you like)
        self.focus_edit.setValidator(QDoubleValidator())
        self.focus_btn = QPushButton("Go")
        focus_row.addWidget(focus_label)
        focus_row.addWidget(self.focus_edit)
        focus_row.addWidget(self.focus_btn)
        ctrl.addLayout(focus_row)

        # connect the button
        self.focus_btn.clicked.connect(self._on_change_focus)

        # image display
        self.img_label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.img_label.setMinimumSize(400, 400)

        # split layout
        main_lay = QHBoxLayout(self)

        # wrap your ctrl layout in a QWidget and give it a fixed width
        ctrl_widget = QWidget()
        ctrl_widget.setLayout(ctrl)
        ctrl_widget.setFixedWidth(150)        # ← shrink control‑bar here
        main_lay.addWidget(ctrl_widget, 0)

        # — replace QLabel with a Matplotlib canvas —
        self.fig = Figure()
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        main_lay.addWidget(self.canvas, 1)

        # connections
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
    # --- Thread 1: PicoScope streaming at 10 kHz ---
    def scope_thread(self):
        print("starting daq")
        # polling as fast as possible (no sleep)
        while not self.stop_event.is_set():
            ps.ps5000aGetStreamingLatestValues(self.chandle, self.c_callback, None)
            time.sleep(0.005)
            #print("Polling scope...")
        print("Stopping scope")
        ps.ps5000aStop(self.chandle)
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
        self.adc_values = []
        self.digital_values = []
        self.stop_event.clear()
        # build scan vectors
        x_vals = np.linspace(x0, x1, nx, dtype=np.float32)
        y_vals = np.linspace(y0, y1, ny, dtype=np.float32)

        self.galvo_worker = GalvoWorker(self.galvo, x_vals, y_vals, dwell=0.001)
        self.galvo_worker.finished.connect(self._on_galvo_done)

        
        
        # start
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        # --- Streaming parameters for 100 kHz, auto-stop at 500k samples ---
        sample_interval  = ctypes.c_int32(10)    # 10 µs → 100 kHz
        time_units       = ps.PS5000A_TIME_UNITS['PS5000A_US']
        pre_trigger      = 0
        max_samples      = 500_000
        auto_stop        = 1
        downsample_ratio = 1
        ratio_mode       = ps.PS5000A_RATIO_MODE['PS5000A_RATIO_MODE_NONE']
        overview_size    = 8192

        # --- Set up analog buffer ---
        assert_pico_ok(ps.ps5000aSetDataBuffers(
            self.chandle,
            ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A'],
            self.buffer_a.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            None,
            overview_size,
            0,
            ratio_mode
        ))
        # --- Set up digital buffer for port D0–D7 ---
        assert_pico_ok(ps.ps5000aSetDataBuffers(
            self.chandle,
            ps.PS5000A_CHANNEL['PS5000A_DIGITAL_PORT0'],  # first digital port (D0–D7) :contentReference[oaicite:0]{index=0}
            self.buffer_d.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            None,
            overview_size,
            0,
            ratio_mode
        ))
        assert_pico_ok(ps.ps5000aRunStreaming(
            self.chandle,
            ctypes.byref(sample_interval),
            time_units,
            pre_trigger,
            0x3FFFFFFF,
            auto_stop,
            downsample_ratio,
            ratio_mode,
            overview_size
        ))
        t1 = threading.Thread(target=self.scope_thread, daemon=True)
        t1.start()
        self.galvo_worker.start()
    # --- Callback to grab each block of both analog and digital samples ---
    def streaming_callback(self,handle, n_samples, start_index,
                        overflow, trigger_at, triggered,
                        auto_stop_flag, user_data):
        global done
        if overflow:
            print("⚠️ Overflow!")
        # copy analog
        block_a = self.buffer_a[start_index:start_index + n_samples]
        self.adc_values.extend(block_a.tolist())
        # copy digital (each value is a bitmask for D0–D7) :contentReference[oaicite:1]{index=1}
        block_d = self.buffer_d[start_index:start_index + n_samples]
        self.digital_values.extend(block_d.tolist())
        

    def _on_stop(self):
        self.stop_event.set()
        print("Stopping scope thread...")
        print(len(self.adc_values), "analog samples received")
        i = 0
        data_vals = []
        while i < len(self.digital_values):
            if self.digital_values[i] == 1:
                i += 50
                data_vals.append(self.adc_values[i])
            i += 1

        print(f"Found triggers {len(data_vals)}")

        # ← call render_picture here
        self.render_picture(data_vals)

        self._finalize_scan()

    @pyqtSlot()
    def _on_change_focus(self):
        text = self.focus_edit.text()
        try:
            z = float(text)
        except ValueError:
            QMessageBox.critical(self, "Input Error", "Please enter a valid number for focus.")
            return
        # call your dummy focus setter
        self.btt.focus(z, feed=1000)  # adjust feed rate as needed
        # optionally give feedback
        QMessageBox.information(self, "Focus", f"Focus set to {z}")

    def render_picture(self, data_vals: list[float]):
        """Draw the scan as a viridis image + colorbar in the Matplotlib canvas."""
        if self.image_data is None:
            print("Warning: no image buffer allocated.")
            return

        ny, nx = self.image_data.shape
        expected = ny * nx
        actual   = len(data_vals)

        if actual != expected:
            print(f"Warning: expected {expected} pixels, got {actual}.", end=' ')
            if actual < expected:
                pad = expected - actual
                print(f"Padding with {pad} zeros.")
                data_vals = data_vals + [0] * pad
            else:
                extra = actual - expected
                print(f"Truncating extra {extra} points.")
                data_vals = data_vals[:expected]

        # reshape into 2‑D array
        arr = np.array(data_vals, dtype=float).reshape((ny, nx))
        self.image_data = arr

        # clear old figure and draw new image + colorbar
        self.fig.clear()
        ax = self.fig.add_subplot(1,1,1)
        im = ax.imshow(
            arr,
            cmap='viridis',
            aspect='equal',    # ← make each pixel square
            origin='lower'     # ← flip the y–axis so row 0 is at the bottom
        )
        cbar = self.fig.colorbar(im, ax=ax)
        

        # push to screen; the canvas will expand to fill the right‐hand panel
        self.canvas.draw()

    def _on_galvo_done(self):
        # when galvo finishes on its own, let the scope thread drain a little, then stop
        QTimer.singleShot(1000, self._on_stop)


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
        #self._update_image(live=False)
        self.galvo_worker = None
        self.image_data   = None
