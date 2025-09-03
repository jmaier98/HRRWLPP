from __future__ import annotations
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.axes_grid1 import make_axes_locatable
import ctypes, time, threading, math, numpy as np
from typing import Optional
from picosdk.ps5000a import ps5000a as ps
from picosdk.functions import assert_pico_ok

from PyQt6.QtCore    import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui     import QDoubleValidator
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QSizePolicy, QFileDialog
)


class GalvoWorker(QThread):
    finished = pyqtSignal()

    def __init__(self, galvo, x_vals: np.ndarray, y_vals: np.ndarray, dwell: float = 0.0005):
        super().__init__()
        self._galvo = galvo
        self._x_vals = x_vals
        self._y_vals = y_vals
        self._dwell  = dwell
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
        finally:
            time.sleep(0.3)
            self.finished.emit()

    def stop(self):
        self._stop = True


class PumpProbeOverlapTab(QWidget):
    """
    Tab that performs two back-to-back 2D scans:
      1) Pump image  (Probe shutter closed, Pump shutter open)
      2) Probe image (Pump shutter closed, Probe shutter open)
    Displays them side-by-side with linked draggable crosshairs in galvo coords,
    independent colorbars under each image, and a 'Go to crosshair' button.
    """

    # ----- acquisition phases
    PHASE_IDLE  = 0
    PHASE_PUMP  = 1
    PHASE_PROBE = 2

    def __init__(self, instrument_manager, state):
        super().__init__()
        self.IM    = instrument_manager
        self.state = state

        # instruments
        self.scope   = self.IM.get("Picoscope")
        self.galvo   = self.IM.get("Galvo")
        self.btt     = self.IM.get("BTT")
        self.shutter = self.IM.get("Shutter")

        # scope handles/buffers
        self.chandle = self.scope.get_chandle()
        self.overview_size = 8192
        self.buffer_a = np.zeros(self.overview_size, dtype=np.int16)
        self.buffer_d = np.zeros(self.overview_size, dtype=np.int16)
        self.c_callback = ps.StreamingReadyType(self._streaming_callback)
        self.stop_event = threading.Event()
        self.adc_values: list[int] = []
        self.digital_values: list[int] = []

        # images & vectors
        self.pump_image: Optional[np.ndarray]  = None
        self.probe_image: Optional[np.ndarray] = None
        self.x_vals: Optional[np.ndarray] = None
        self.y_vals: Optional[np.ndarray] = None
        self.nx = 0
        self.ny = 0

        # crosshair state (galvo coords, not pixels)
        self.crosshair_x = 0.0
        self.crosshair_y = 0.0

        # drag state
        self._dragging = False
        self._drag_axes = None  # ax_pump or ax_probe

        # phase
        self.phase = self.PHASE_IDLE
        self.worker: Optional[GalvoWorker] = None

        # build UI
        self._build_ui()

    # ---------------------------- UI
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

        # numeric inputs (match Imaging tab)
        self.x0_edit = self._make_line("X start (V):", ctrl, QDoubleValidator())
        self.x1_edit = self._make_line("X end   (V):", ctrl, QDoubleValidator())
        self.nx_edit = self._make_line("X points:",   ctrl, QDoubleValidator(1, 10000, 0))

        self.y0_edit = self._make_line("Y start (V):", ctrl, QDoubleValidator())
        self.y1_edit = self._make_line("Y end   (V):", ctrl, QDoubleValidator())
        self.ny_edit = self._make_line("Y points:",   ctrl, QDoubleValidator(1, 10000, 0))

        # sensible defaults
        self.x0_edit.setText("-.05"); self.x1_edit.setText(".05"); self.nx_edit.setText("50")
        self.y0_edit.setText("-.05"); self.y1_edit.setText(".05"); self.ny_edit.setText("50")

        # start/stop
        self.start_btn = QPushButton("Start (Pump → Probe)")
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        ctrl.addWidget(self.start_btn)
        ctrl.addWidget(self.stop_btn)

        # focus row (like Imaging tab)
        focus_row = QHBoxLayout()
        focus_label = QLabel("Change focus (Z):")
        self.focus_edit = QLineEdit(); self.focus_edit.setValidator(QDoubleValidator())
        self.focus_btn = QPushButton("Go")
        focus_row.addWidget(focus_label); focus_row.addWidget(self.focus_edit); focus_row.addWidget(self.focus_btn)
        ctrl.addLayout(focus_row)

        # crosshair readout + go button
        self.crosshair_lbl = QLabel("Crosshair (V): X=0.000, Y=0.000")
        self.goto_btn = QPushButton("Go to crosshair")
        ctrl.addWidget(self.crosshair_lbl)
        ctrl.addWidget(self.goto_btn)

        # save controls (combined figure + arrays)
        self.folder_edit = self._make_line("Folder:", ctrl, QDoubleValidator())  # allow any text; validator is cosmetic
        self.filename_edit = self._make_line("Base filename:", ctrl, QDoubleValidator())
        self.folder_btn = QPushButton("Browse…")
        self.save_btn   = QPushButton("Save (figure + arrays)")
        ctrl.addWidget(self.folder_btn)
        ctrl.addWidget(self.save_btn)

        ctrl.addStretch()

        # main layout
        main = QHBoxLayout(self)

        # narrow control bar as before
        ctrl_widget = QWidget(); ctrl_widget.setLayout(ctrl); ctrl_widget.setFixedWidth(170)
        main.addWidget(ctrl_widget, 0)

        # Matplotlib figure with two side-by-side axes
        self.fig = Figure()
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        main.addWidget(self.canvas, 1)

        # create two axes + colorbars beneath
        self.ax_pump  = self.fig.add_subplot(1, 2, 1)
        self.ax_probe = self.fig.add_subplot(1, 2, 2)
        self.ax_pump.set_title("Pump image")
        self.ax_probe.set_title("Probe image")

        # placeholders for images and colorbars
        self.im_pump = None
        self.im_probe = None
        self.cbar_pump = None
        self.cbar_probe = None

        # crosshair artists (2 axes)
        self._create_crosshair_artists()

        # connections
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.focus_btn.clicked.connect(self._on_change_focus)
        self.goto_btn.clicked.connect(self._on_goto_crosshair)
        self.folder_btn.clicked.connect(self._on_browse)
        self.save_btn.clicked.connect(self._on_save)

        # mouse interactions on canvas
        self.canvas.mpl_connect("button_press_event",  self._on_mouse_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)
        self.canvas.mpl_connect("button_release_event",self._on_mouse_release)

        self.canvas.draw()

    def _create_crosshair_artists(self):
        # pump
        self.xline_pump = self.ax_pump.axvline(self.crosshair_x, color='r', lw=1)
        self.yline_pump = self.ax_pump.axhline(self.crosshair_y, color='r', lw=1)
        # probe
        self.xline_probe = self.ax_probe.axvline(self.crosshair_x, color='r', lw=1)
        self.yline_probe = self.ax_probe.axhline(self.crosshair_y, color='r', lw=1)

    # ---------------------------- Inputs/validation
    def _validate_inputs(self):
        try:
            x0 = float(self.x0_edit.text()); x1 = float(self.x1_edit.text()); nx = int(float(self.nx_edit.text()))
            y0 = float(self.y0_edit.text()); y1 = float(self.y1_edit.text()); ny = int(float(self.ny_edit.text()))
        except ValueError:
            QMessageBox.critical(self, "Input Error", "All fields must be filled with numbers.")
            return None
        if nx < 1 or ny < 1:
            QMessageBox.critical(self, "Input Error", "Point counts must be ≥ 1.")
            return None
        return x0, x1, nx, y0, y1, ny

    def _alloc_images(self, ny: int, nx: int):
        self.pump_image  = np.zeros((ny, nx), dtype=np.float32)
        self.probe_image = np.zeros((ny, nx), dtype=np.float32)
        self.nx, self.ny = nx, ny

    # ---------------------------- Start/stop sequencing
    def _on_start(self):
        args = self._validate_inputs()
        if args is None: return
        x0, x1, nx, y0, y1, ny = args

        # vectors
        self.x_vals = np.linspace(x0, x1, nx, dtype=np.float32)
        self.y_vals = np.linspace(y0, y1, ny, dtype=np.float32)
        self._alloc_images(ny, nx)

        # initial crosshair = center
        self.crosshair_x = float((x0 + x1) / 2.0)
        self.crosshair_y = float((y0 + y1) / 2.0)
        self._update_crosshair_artists()
        self._update_crosshair_label()

        # lock UI
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # clear figures
        self._clear_images()

        # start sequence
        self._run_phase(self.PHASE_PUMP)

    def _on_stop(self):
        # stop any running worker + scope
        if self.worker is not None:
            self.worker.stop()
        self.stop_event.set()
        try:
            ps.ps5000aStop(self.chandle)
        except Exception:
            pass

        # safe shutters
        self._safe_shutters()

        # reset phase
        self.phase = self.PHASE_IDLE
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    # ---------------------------- Phase control
    def _run_phase(self, phase: int):
        self.phase = phase
        # shutter states
        if phase == self.PHASE_PUMP:
            self.shutter.closeProbe(); time.sleep(1)
            self.shutter.openPump();  time.sleep(0.5)
        elif phase == self.PHASE_PROBE:
            self.shutter.closePump();  time.sleep(1)
            self.shutter.openProbe();  time.sleep(0.5)

        # clear DAQ buffers
        self.adc_values.clear()
        self.digital_values.clear()
        self.stop_event.clear()

        # setup scope streaming (same as your Imaging tab)
        sample_interval  = ctypes.c_int32(10)    # 10 µs → 100 kHz
        time_units       = ps.PS5000A_TIME_UNITS['PS5000A_US']
        pre_trigger      = 0
        auto_stop        = 1
        downsample_ratio = 1
        ratio_mode       = ps.PS5000A_RATIO_MODE['PS5000A_RATIO_MODE_NONE']

        assert_pico_ok(ps.ps5000aSetDataBuffers(
            self.chandle,
            ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A'],
            self.buffer_a.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            None,
            self.overview_size,
            0,
            ratio_mode
        ))
        assert_pico_ok(ps.ps5000aSetDataBuffers(
            self.chandle,
            ps.PS5000A_CHANNEL['PS5000A_DIGITAL_PORT0'],
            self.buffer_d.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            None,
            self.overview_size,
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
            self.overview_size
        ))

        # start scope polling thread
        t = threading.Thread(target=self._scope_thread, daemon=True)
        t.start()

        # start galvo sweep
        dwell = 0.0005
        self.worker = GalvoWorker(self.galvo, self.x_vals, self.y_vals, dwell=dwell)
        self.worker.finished.connect(self._on_phase_finished)
        self.worker.start()

    def _on_phase_finished(self):
        # stop scope and parse samples to pixels
        self.stop_event.set()
        try:
            ps.ps5000aStop(self.chandle)
        except Exception:
            pass

        data_vals = self._extract_samples_to_pixels()
        arr = self._reshape_pixels(data_vals, self.ny, self.nx)

        if self.phase == self.PHASE_PUMP:
            self.pump_image = arr
            self._render_images()  # show pump early if you like
            # move to next phase
            self._run_phase(self.PHASE_PROBE)
        elif self.phase == self.PHASE_PROBE:
            self.probe_image = arr
            # close probe for safety
            self._safe_shutters()
            # final render
            self._render_images()
            # reset UI
            self.phase = self.PHASE_IDLE
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)

    # ---------------------------- Scope helpers
    def _scope_thread(self):
        # poll as fast as is sensible
        while not self.stop_event.is_set():
            ps.ps5000aGetStreamingLatestValues(self.chandle, self.c_callback, None)
            time.sleep(0.005)

    def _streaming_callback(self, handle, n_samples, start_index,
                            overflow, trigger_at, triggered,
                            auto_stop_flag, user_data):
        if overflow:
            print("⚠️ Overflow!")
        block_a = self.buffer_a[start_index:start_index + n_samples]
        block_d = self.buffer_d[start_index:start_index + n_samples]
        self.adc_values.extend(block_a.tolist())
        self.digital_values.extend(block_d.tolist())

    def _extract_samples_to_pixels(self) -> list[float]:
        # replicate your Imaging tab trigger picking logic
        vals = []
        i = 0
        while i < len(self.digital_values) and len(vals) < (self.nx * self.ny):
            if self.digital_values[i] == 1:
                i += 50  # small offset after digital edge
                if i < len(self.adc_values):
                    vals.append(self.adc_values[i])
            i += 1
        return vals

    @staticmethod
    def _reshape_pixels(data_vals: list[float], ny: int, nx: int) -> np.ndarray:
        expected = ny * nx
        actual   = len(data_vals)
        if actual < expected:
            data_vals = data_vals + [0.0] * (expected - actual)
        elif actual > expected:
            data_vals = data_vals[:expected]
        return np.array(data_vals, dtype=float).reshape((ny, nx))

    def _safe_shutters(self):
        try: self.shutter.closePump()
        except Exception: pass
        try: self.shutter.closeProbe()
        except Exception: pass

    # ---------------------------- Rendering
    def _clear_images(self):
        # 1) Remove existing colorbars first (before touching the image axes)
        self._remove_colorbar(self.cbar_pump);  self.cbar_pump  = None
        self._remove_colorbar(self.cbar_probe); self.cbar_probe = None

        # 2) Now clear the axes
        self.ax_pump.clear();  self.ax_pump.set_title("Pump image")
        self.ax_probe.clear(); self.ax_probe.set_title("Probe image")
        self.im_pump  = None
        self.im_probe = None

        # 3) Recreate crosshair artists on the fresh axes
        self._create_crosshair_artists()
        self.canvas.draw_idle()

    def _render_images(self):
        if self.pump_image is None or self.probe_image is None:
            # Render whatever is available (e.g., after first phase)
            pass

        # clear axes but keep titles
        self.ax_pump.clear();  self.ax_pump.set_title("Pump image")
        self.ax_probe.clear(); self.ax_probe.set_title("Probe image")

        # consistent extents so crosshair is in galvo coords
        x0, x1 = float(self.x_vals[0]), float(self.x_vals[-1])
        y0, y1 = float(self.y_vals[0]), float(self.y_vals[-1])
        extent = [x0, x1, y0, y1]

        if self.pump_image is not None:
            self.im_pump = self.ax_pump.imshow(
                self.pump_image, origin='lower', aspect='equal', extent=extent, cmap='viridis'
            )
        if self.probe_image is not None:
            self.im_probe = self.ax_probe.imshow(
                self.probe_image, origin='lower', aspect='equal', extent=extent, cmap='viridis'
            )

        # colorbars UNDER each image
        # pump
        self._remove_colorbar(self.cbar_pump);  self.cbar_pump  = None
        self._remove_colorbar(self.cbar_probe); self.cbar_probe = None

        if self.im_pump is not None:
            div1 = make_axes_locatable(self.ax_pump)
            cax1 = div1.append_axes("bottom", size="5%", pad=0.35)
            self.cbar_pump = self.fig.colorbar(self.im_pump, cax=cax1, orientation='horizontal')
        # probe
        if self.im_probe is not None:
            div2 = make_axes_locatable(self.ax_probe)
            cax2 = div2.append_axes("bottom", size="5%", pad=0.35)
            self.cbar_probe = self.fig.colorbar(self.im_probe, cax=cax2, orientation='horizontal')

        # re-create crosshair lines at current galvo coords
        self._create_crosshair_artists()
        self._update_crosshair_artists()

        self.canvas.draw()

    def _update_crosshair_artists(self):
        # update both axes to same galvo coords
        for xline in (self.xline_pump, self.xline_probe):
            xline.set_xdata([self.crosshair_x, self.crosshair_x])
        for yline in (self.yline_pump, self.yline_probe):
            yline.set_ydata([self.crosshair_y, self.crosshair_y])
        self.canvas.draw_idle()

    def _update_crosshair_label(self):
        self.crosshair_lbl.setText(f"Crosshair (V): X={self.crosshair_x:.3f}, Y={self.crosshair_y:.3f}")

    def _remove_colorbar(self, cbar):
        """Safely remove a Matplotlib Colorbar (and its Axes) if it exists."""
        if cbar is None:
            return
        try:
            cax = cbar.ax
            cbar.remove()  # removes the artists and disconnects from figure
            # In some mpl versions, the cax can linger; ensure it’s gone:
            if cax in self.fig.axes:
                self.fig.delaxes(cax)
        except Exception as e:
            print("Colorbar remove warning:", e)

    # ---------------------------- Mouse interaction (drag crosshair without moving galvo)
    def _in_axes(self, event, ax):
        return (event.inaxes is ax)

    def _on_mouse_press(self, event):
        if event.button != 1: return
        if event.inaxes not in (self.ax_pump, self.ax_probe): return
        self._dragging = True
        self._drag_axes = event.inaxes

    def _on_mouse_move(self, event):
        if not self._dragging: return
        if event.inaxes not in (self.ax_pump, self.ax_probe): return
        # clamp to extents
        x0, x1 = float(self.x_vals[0]), float(self.x_vals[-1])
        y0, y1 = float(self.y_vals[0]), float(self.y_vals[-1])
        x = min(max(event.xdata, x0), x1) if event.xdata is not None else self.crosshair_x
        y = min(max(event.ydata, y0), y1) if event.ydata is not None else self.crosshair_y
        self.crosshair_x = float(x); self.crosshair_y = float(y)
        self._update_crosshair_artists()
        self._update_crosshair_label()

    def _on_mouse_release(self, event):
        if event.button != 1: return
        self._dragging = False
        self._drag_axes = None

    # ---------------------------- Focus & goto crosshair
    def _on_change_focus(self):
        try:
            z = float(self.focus_edit.text())
        except ValueError:
            QMessageBox.critical(self, "Input Error", "Please enter a valid number for focus.")
            return
        try:
            self.btt.focus(z, feed=1000)
            QMessageBox.information(self, "Focus", f"Focus set to {z}")
        except Exception as e:
            QMessageBox.critical(self, "Focus Error", str(e))

    def _on_goto_crosshair(self):
        try:
            self.galvo.move(self.crosshair_x, self.crosshair_y)
        except Exception as e:
            QMessageBox.critical(self, "Galvo Move Error", str(e))

    # ---------------------------- Saving
    def _on_browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder to save")
        if folder:
            self.folder_edit.setText(folder)

    def _on_save(self):
        folder = self.folder_edit.text().strip()
        base   = self.filename_edit.text().strip()
        if not folder or not base:
            QMessageBox.critical(self, "Save Error", "Please set Folder and Base filename.")
            return
        if self.pump_image is None or self.probe_image is None:
            QMessageBox.critical(self, "Save Error", "No images to save yet.")
            return

        # combined figure (side-by-side)
        fig2 = Figure(figsize=(8, 4))
        from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvasAgg
        canv2 = FigureCanvasAgg(fig2)
        ax1 = fig2.add_subplot(1, 2, 1); ax2 = fig2.add_subplot(1, 2, 2)
        x0, x1 = float(self.x_vals[0]), float(self.x_vals[-1])
        y0, y1 = float(self.y_vals[0]), float(self.y_vals[-1])
        extent = [x0, x1, y0, y1]

        im1 = ax1.imshow(self.pump_image, origin='lower', aspect='equal', extent=extent, cmap='viridis')
        im2 = ax2.imshow(self.probe_image, origin='lower', aspect='equal', extent=extent, cmap='viridis')
        ax1.set_title("Pump image"); ax2.set_title("Probe image")

        # colorbars under each
        d1 = make_axes_locatable(ax1); cax1 = d1.append_axes("bottom", size="5%", pad=0.35)
        d2 = make_axes_locatable(ax2); cax2 = d2.append_axes("bottom", size="5%", pad=0.35)
        fig2.colorbar(im1, cax=cax1, orientation='horizontal')
        fig2.colorbar(im2, cax=cax2, orientation='horizontal')

        png_path = f"{folder}/{base}_pump_probe.png"
        fig2.savefig(png_path, dpi=200, bbox_inches='tight')

        # save arrays with headers
        header = (
            f"Pump-Probe Overlap data\n"
            f"X: {x0}..{x1} ({self.nx} pts), Y: {y0}..{y1} ({self.ny} pts)\n"
            f"Crosshair_V: X={self.crosshair_x:.6f}, Y={self.crosshair_y:.6f}\n"
        )
        pump_path  = f"{folder}/{base}_pump.txt"
        probe_path = f"{folder}/{base}_probe.txt"
        np.savetxt(pump_path,  self.pump_image,  header=header)
        np.savetxt(probe_path, self.probe_image, header=header)

        QMessageBox.information(self, "Saved",
                                f"Saved:\n{png_path}\n{pump_path}\n{probe_path}")
