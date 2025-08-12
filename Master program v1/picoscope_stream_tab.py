import numpy as np
import threading

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QPushButton, QMessageBox
)

# If you prefer Matplotlib, you can swap pyqtgraph out—but pyqtgraph is faster for live plots.
import pyqtgraph as pg


class PicoscopeStreamTab(QWidget):
    """
    A tab that streams & plots PicoScope data.

    Expects InstrumentManager to return the scope instance with IM.get("Picoscope").
    The scope wrapper should match the interface described in the docstring above.
    """

    # Display labels → Pico range keys used by your wrapper
    RANGE_MAP = {
        "20 mV":  "20MV",
        "50 mV":  "50MV",
        "100 mV": "100MV",
        "200 mV": "200MV",
        "500 mV": "500MV",
        "1 V":    "1V",
        "2 V":    "2V",
        "5 V":    "5V",
        "10 V":   "10V",
        "20 V":   "20V",
        "50 V":   "50V",
    }

    # Seconds of history to keep/plot
    TIME_SPANS = ["0.1 s", "0.5 s", "1 s", "2 s", "5 s", "10 s"]

    # Pico channels
    CHANNELS = ["A", "B", "C", "D"]

    # Supported vertical resolutions (requires reopen)
    RESOLUTIONS = ["8-bit", "12-bit", "14-bit", "15-bit", "16-bit"]

    def __init__(self, IM, parent=None):
        super().__init__(parent)
        self.IM = IM

        # ---- Acquisition parameters ----
        self._sr_hz = 100_000.0          # 10 µs sample interval; change if you want a different timebase
        self._sample_interval_us = 10
        self._time_span_s = 1.0          # default plot window
        self._max_plot_points = 2000      # decimate to keep UI responsive

        # ---- Ring buffer for analog samples ----
        self._lock = threading.RLock()
        self._N = int(self._sr_hz * self._time_span_s)
        self._buf = np.full(self._N, np.nan, dtype=np.float32)
        self._idx = 0
        self._filled = False

        # ---- UI ----
        self._build_ui()

        # Plot timer
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 FPS
        self._timer.timeout.connect(self._update_plot)

        self._streaming = False

    # ------------- UI -----------------
    def _build_ui(self):
        lay = QVBoxLayout(self)

        # Top controls
        ctrl = QHBoxLayout()

        ctrl.addWidget(QLabel("Channel"))
        self.cmb_ch = QComboBox()
        self.cmb_ch.addItems(self.CHANNELS)
        ctrl.addWidget(self.cmb_ch)

        ctrl.addWidget(QLabel("Vertical Res."))
        self.cmb_res = QComboBox()
        self.cmb_res.addItems(self.RESOLUTIONS)
        self.cmb_res.setCurrentText("12-bit")  # common default
        self.cmb_res.currentTextChanged.connect(self._on_resolution_change)
        ctrl.addWidget(self.cmb_res)

        ctrl.addWidget(QLabel("Voltage Range"))
        self.cmb_rng = QComboBox()
        self.cmb_rng.addItems(list(self.RANGE_MAP.keys()))
        self.cmb_rng.setCurrentText("200 mV")
        ctrl.addWidget(self.cmb_rng)

        ctrl.addWidget(QLabel("Time Range"))
        self.cmb_time = QComboBox()
        self.cmb_time.addItems(self.TIME_SPANS)
        self.cmb_time.setCurrentText("1 s")
        self.cmb_time.currentTextChanged.connect(self._on_time_span_changed)
        ctrl.addWidget(self.cmb_time)

        self.btn_start = QPushButton("Start")
        self.btn_start.clicked.connect(self.start_stream)
        ctrl.addWidget(self.btn_start)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self.stop_stream)
        self.btn_stop.setEnabled(False)
        ctrl.addWidget(self.btn_stop)

        ctrl.addStretch()
        lay.addLayout(ctrl)

        # Plot
        self.plot = pg.PlotWidget()
        self.plot.setTitle("PicoScope Channel")
        self.plot.setLabel("left", "Voltage", units="V (ADC counts scaled)")
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.curve = self.plot.plot([], [], pen=pg.mkPen(width=2))
        lay.addWidget(self.plot)

    # ------------- Acquisition control -------------
    def _get_pico(self):
        pico = self.IM.get("Picoscope")
        if pico is None:
            QMessageBox.warning(self, "PicoScope", "Picoscope not found in InstrumentManager as 'Picoscope'.")
        return pico

    def start_stream(self):
        if self._streaming:
            return

        pico = self._get_pico()
        if pico is None:
            return

        # Configure channel/range
        ch = self.cmb_ch.currentText()
        rng_label = self.cmb_rng.currentText()
        vrange = self.RANGE_MAP[rng_label]

        try:
            pico.configure_channel(channel=ch, vrange=vrange, coupling="DC", offset=0.0, enabled=True)
        except Exception as e:
            QMessageBox.critical(self, "PicoScope", f"Failed to configure channel: {e}")
            return

        # Reset buffers for the chosen time span
        with self._lock:
            self._N = int(self._sr_hz * self._time_span_s)
            self._buf = np.full(self._N, np.nan, dtype=np.float32)
            self._idx = 0
            self._filled = False

        # Hook the data callback
        def on_data(a_block: np.ndarray, d_block: np.ndarray):
            # Convert raw ADC to float; if you want real volts, multiply by ADC->V factor per range
            self._append_samples(a_block.astype(np.float32))

        pico.on_data = on_data

        # Start streaming
        try:
            pico.start_streaming(
                sample_interval_us=self._sample_interval_us,
                overview_size=131072,
                downsample_ratio=1,
                auto_stop=False
            )
        except Exception as e:
            QMessageBox.critical(self, "PicoScope", f"Failed to start streaming: {e}")
            return

        self._timer.start()
        self._streaming = True
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def stop_stream(self):
        if not self._streaming:
            return
        pico = self._get_pico()
        if pico:
            try:
                pico.idle()  # stops acquisition but keeps device open
            except Exception as e:
                QMessageBox.warning(self, "PicoScope", f"Stop/idle error: {e}")

        self._timer.stop()
        self._streaming = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    # ------------- Buffer & plotting -------------
    def _append_samples(self, arr: np.ndarray):
        if arr.size == 0:
            return
        with self._lock:
            n = arr.size
            if n >= self._N:
                # Take latest window
                self._buf[:] = arr[-self._N:]
                self._idx = 0
                self._filled = True
                return

            end = self._idx + n
            if end <= self._N:
                self._buf[self._idx:end] = arr
            else:
                first = self._N - self._idx
                self._buf[self._idx:] = arr[:first]
                self._buf[:n - first] = arr[first:]
            self._idx = (self._idx + n) % self._N
            if self._idx == 0:
                self._filled = True

    def _read_window(self):
        with self._lock:
            if self._filled:
                # Return buffer ordered oldest→newest over full window
                y = np.concatenate((self._buf[self._idx:], self._buf[:self._idx]))
                t = np.arange(self._N, dtype=np.float32) / self._sr_hz
            else:
                # Only the portion up to idx is valid
                y = self._buf[:self._idx].copy()
                t = np.arange(y.size, dtype=np.float32) / self._sr_hz
        return t, y

    def _update_plot(self):
        t, y = self._read_window()
        if y.size == 0 or np.all(np.isnan(y)):
            return

        # Decimate to keep <= self._max_plot_points
        n = y.size
        if n > self._max_plot_points:
            step = int(np.ceil(n / self._max_plot_points))
            y = y[::step]
            t = t[::step]

        self.curve.setData(t, y)
        # Keep x range tied to time span
        self.plot.setXRange(0.0, self._time_span_s, padding=0)

    # ------------- UI handlers -------------
    def _on_time_span_changed(self, label: str):
        # "1 s" → 1.0
        val = float(label.split()[0])
        self._time_span_s = val
        with self._lock:
            self._N = int(self._sr_hz * self._time_span_s)
            # Resize while preserving latest data
            old_t, old_y = self._read_window()
            new_buf = np.full(self._N, np.nan, dtype=np.float32)
            if old_y.size > 0:
                if old_y.size >= self._N:
                    new_buf[:] = old_y[-self._N:]
                    self._idx = 0
                    self._filled = True
                else:
                    new_buf[:old_y.size] = old_y
                    self._idx = old_y.size % self._N
                    self._filled = (self._idx == 0 and old_y.size == self._N)
            self._buf = new_buf

    def _on_resolution_change(self, label: str):
        """
        Changing vertical resolution requires reopening the device.
        This handler tries IM.reopen_picoscope(bits). If not available, it shows a hint.
        """
        bits = label.replace("-bit", "")
        if self._streaming:
            self.stop_stream()

        if hasattr(self.IM, "reopen_picoscope"):
            try:
                self.IM.reopen_picoscope(bits)
            except Exception as e:
                QMessageBox.warning(
                    self, "PicoScope",
                    f"Attempted to reopen with {bits}-bit resolution but failed:\n{e}"
                )
        else:
            QMessageBox.information(
                self, "PicoScope",
                "Vertical resolution change selected.\n\n"
                "Please implement InstrumentManager.reopen_picoscope(bits) to reopen\n"
                "the PicoScope with the chosen resolution, or restart the app with\n"
                "the desired resolution configured at startup."
            )
