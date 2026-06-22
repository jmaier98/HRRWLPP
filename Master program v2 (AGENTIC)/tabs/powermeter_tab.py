# tabs/powermeter_tab.py
import time
import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGroupBox, QGridLayout,
    QSlider, QDoubleSpinBox,
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

POLL_INTERVAL_MS = 50    # timer interval
BUFFER_SIZE      = 300   # rolling window length


class PowerMeterTab(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.state   = controller.state
        self.pm      = controller.get_instrument("PM")
        self.btt     = controller.get_instrument("BTT")
        self.shutter = controller.get_instrument("Shutter")

        # ── Layout ────────────────────────────────────────────────────────────
        root = QVBoxLayout(self)

        # Top row: Start/Stop + shutter controls
        top = QHBoxLayout()

        self.start_btn = QPushButton("Start")
        self.stop_btn  = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        top.addWidget(self.start_btn)
        top.addWidget(self.stop_btn)
        top.addStretch()

        shutter_box = QGroupBox("Shutters")
        sg = QGridLayout(shutter_box)
        for r, c, label, fn in [
            (0, 0, "Open pump",   self._open_pump),
            (0, 1, "Close pump",  self._close_pump),
            (1, 0, "Open probe",  self._open_probe),
            (1, 1, "Close probe", self._close_probe),
        ]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            sg.addWidget(b, r, c)
        top.addWidget(shutter_box)

        root.addLayout(top)

        # Polarizer controls
        root.addWidget(self._build_polarizer_group())

        # Large numeric readout
        self.current_lbl = QLabel("-- µW")
        self.current_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        f = QFont()
        f.setPointSize(48)
        f.setBold(True)
        self.current_lbl.setFont(f)
        root.addWidget(self.current_lbl)

        # Live plot
        self.fig = Figure(figsize=(8, 3), tight_layout=True, facecolor="#f5f5f5")
        self.ax  = self.fig.add_subplot(111)
        self.ax.set_facecolor("#ffffff")
        self.ax.set_title("Live power")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Power (µW)")
        self.ax.grid(True, alpha=0.3)
        self.canvas = FigureCanvas(self.fig)
        root.addWidget(self.canvas, stretch=1)

        # Runtime state
        self._times  = np.zeros(BUFFER_SIZE)
        self._powers = np.zeros(BUFFER_SIZE)
        self._idx    = 0
        self._t0     = 0.0
        self._line,  = self.ax.plot([], [], "o-", markersize=3, linewidth=1.2,
                                    color="#2980b9")

        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)

    # ── Polarizer controls ────────────────────────────────────────────────────

    def _build_polarizer_group(self) -> QGroupBox:
        """
        Two rows — one for pump polarizer (Rot 4), one for probe polarizer (Rot 3).
        Each row has a slider for coarse dragging and a spin-box for precise entry.
        Both update the motor and state in real time.
        """
        box   = QGroupBox("Polarizers")
        grid  = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        SLIDER_SCALE = 10      # slider ticks per degree → 0.1° resolution
        ANGLE_MIN, ANGLE_MAX = 0.0, 360.0

        for row, (label, rot_fn, state_key) in enumerate([
            ("Pump polarizer (Rot 4):",
             lambda a: self.btt.rot_4(a, 5000) if self.btt else None,
             "rot_4_angle"),
            ("Probe polarizer (Rot 3):",
             lambda a: self.btt.rot_3(a, 5000) if self.btt else None,
             "rot_3_angle"),
        ]):
            lbl = QLabel(label)
            lbl.setFixedWidth(170)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(int(ANGLE_MIN * SLIDER_SCALE),
                            int(ANGLE_MAX * SLIDER_SCALE))
            slider.setFixedWidth(280)
            slider.setTickInterval(SLIDER_SCALE * 10)   # tick every 10°
            slider.setTickPosition(QSlider.TickPosition.TicksBelow)

            spin = QDoubleSpinBox()
            spin.setRange(ANGLE_MIN, ANGLE_MAX)
            spin.setDecimals(2)
            spin.setSingleStep(0.05)
            spin.setSuffix(" °")
            spin.setFixedWidth(90)

            # Seed from state
            current = float(self.state.settings.get(state_key, 0.0))
            slider.setValue(int(current * SLIDER_SCALE))
            spin.setValue(current)

            # Closure captures the mutable objects
            def _on_slider(ticks, _spin=spin, _rot=rot_fn, _key=state_key):
                angle = ticks / SLIDER_SCALE
                _spin.blockSignals(True)
                _spin.setValue(angle)
                _spin.blockSignals(False)
                self._send_polarizer(_rot, _key, angle)

            def _on_spin(angle, _slider=slider, _rot=rot_fn, _key=state_key):
                _slider.blockSignals(True)
                _slider.setValue(int(angle * SLIDER_SCALE))
                _slider.blockSignals(False)
                self._send_polarizer(_rot, _key, angle)

            slider.valueChanged.connect(_on_slider)
            spin.valueChanged.connect(_on_spin)

            grid.addWidget(lbl,    row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(spin,   row, 2)

        return box

    def _send_polarizer(self, rot_fn, state_key: str, angle: float):
        """Move a rotation mount and update state."""
        try:
            rot_fn(angle)
            self.state.settings[state_key] = angle
        except Exception as e:
            self.controller.log(f"Polarizer move error: {e}")

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _start(self):
        if self.btt is not None:
            self.btt.powermeter()
        self._t0 = time.time()
        self._idx = 0
        self._times.fill(0)
        self._powers.fill(0)
        self._line.set_data([], [])
        self.ax.set_xlim(0, 10)
        self.ax.set_ylim(0, 100)
        self.canvas.draw_idle()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._timer.start(POLL_INTERVAL_MS)
        self.controller.log("Power meter started")

    def _stop(self):
        self._timer.stop()
        if self.btt is not None:
            self.btt.clear()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.controller.log("Power meter stopped")

    # ── Timer tick ────────────────────────────────────────────────────────────

    def _tick(self):
        if self.pm is None:
            return
        try:
            p_uw = self.pm.read_power()
        except Exception:
            return

        t = time.time() - self._t0
        slot = self._idx % BUFFER_SIZE
        self._times[slot]  = t
        self._powers[slot] = p_uw
        self._idx += 1

        n = min(self._idx, BUFFER_SIZE)
        if self._idx <= BUFFER_SIZE:
            xs = self._times[:n]
            ys = self._powers[:n]
        else:
            # Reorder the circular buffer for a smooth scrolling plot
            start = self._idx % BUFFER_SIZE
            xs = np.roll(self._times,  -start)
            ys = np.roll(self._powers, -start)

        self._line.set_data(xs, ys)
        self.ax.set_xlim(xs.min(), xs.max() + 0.1 if len(xs) > 1 else xs.min() + 10)
        ymax = float(ys.max()) * 1.15 if ys.max() > 0 else 100
        self.ax.set_ylim(0, ymax)
        self.canvas.draw_idle()

        self.current_lbl.setText(f"{p_uw:.2f} µW")

    # ── Shutter helpers ───────────────────────────────────────────────────────

    def _open_pump(self):
        if self.shutter:
            self.shutter.openPump()
            self.state.settings["pump_shutter_open"] = True
            self.controller.log("Pump shutter opened (power meter tab)")

    def _close_pump(self):
        if self.shutter:
            self.shutter.closePump()
            self.state.settings["pump_shutter_open"] = False
            self.controller.log("Pump shutter closed (power meter tab)")

    def _open_probe(self):
        if self.shutter:
            self.shutter.openProbe()
            self.state.settings["probe_shutter_open"] = True
            self.controller.log("Probe shutter opened (power meter tab)")

    def _close_probe(self):
        if self.shutter:
            self.shutter.closeProbe()
            self.state.settings["probe_shutter_open"] = False
            self.controller.log("Probe shutter closed (power meter tab)")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._timer.stop()
        event.accept()
