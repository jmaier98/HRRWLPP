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

# One colour per tuning phase so the traces are visually separate
TUNER_PHASE_COLORS = {
    "Rough sweep":       "#2980b9",   # blue
    "Medium scan":       "#e67e22",   # orange
    "Fine scan":         "#27ae60",   # green
    "Final":             "#c0392b",   # red
    "Calibration sweep": "#2980b9",   # blue
}
TUNER_DEFAULT_COLOR = "#8e44ad"       # purple, for any unknown phase


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

        # Waveplate controls
        root.addWidget(self._build_waveplate_group())

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

        # ── Power-tuner live view ─────────────────────────────────────────────
        # While a pump/probe tuner (or calibration sweep) runs, this tab is
        # brought to the front and plots angle vs power as readings arrive.
        # Each scan phase (rough / medium / fine / final) gets its own line so
        # the traces are not connected across phases.
        self._tuner_active = False
        self._tuner_lines: list = []           # Line2D objects, one per phase
        self._tuner_cur = None                 # line receiving current readings
        self._tuner_all_angles: list[float] = []
        self._tuner_all_powers: list[float] = []

        sigs = controller.scan_signals
        sigs.tuner_started.connect(self._on_tuner_started,
                                   Qt.ConnectionType.QueuedConnection)
        sigs.tuner_phase.connect(self._on_tuner_phase,
                                 Qt.ConnectionType.QueuedConnection)
        sigs.tuner_reading.connect(self._on_tuner_reading,
                                   Qt.ConnectionType.QueuedConnection)
        sigs.tuner_finished.connect(self._on_tuner_finished,
                                    Qt.ConnectionType.QueuedConnection)

    # ── Waveplate controls ────────────────────────────────────────────────────

    def _build_waveplate_group(self) -> QGroupBox:
        """
        Two rows — one for pump waveplate (Rot 1), one for probe waveplate (Rot 3).
        These are the mounts the power tuners drive, so they are what you reach
        for when setting power by hand.
        Each row has a slider for coarse dragging and a spin-box for precise entry.
        Both update the motor and state in real time.
        """
        box   = QGroupBox("Waveplates")
        grid  = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        SLIDER_SCALE = 10      # slider ticks per degree → 0.1° resolution
        ANGLE_MIN, ANGLE_MAX = 0.0, 360.0

        for row, (label, rot_fn, state_key) in enumerate([
            ("Pump waveplate (Rot 1):",
             lambda a: self.btt.rot_1(a, 5000) if self.btt else None,
             "rot_1_angle"),
            ("Probe waveplate (Rot 3):",
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
                self._send_waveplate(_rot, _key, angle)

            def _on_spin(angle, _slider=slider, _rot=rot_fn, _key=state_key):
                _slider.blockSignals(True)
                _slider.setValue(int(angle * SLIDER_SCALE))
                _slider.blockSignals(False)
                self._send_waveplate(_rot, _key, angle)

            slider.valueChanged.connect(_on_slider)
            spin.valueChanged.connect(_on_spin)

            grid.addWidget(lbl,    row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(spin,   row, 2)

        return box

    def _send_waveplate(self, rot_fn, state_key: str, angle: float):
        """Move a rotation mount and update state."""
        try:
            rot_fn(angle)
            self.state.settings[state_key] = angle
        except Exception as e:
            self.controller.log(f"Waveplate move error: {e}")

    # ── Power-tuner live plotting ─────────────────────────────────────────────

    def _clear_tuner_lines(self):
        """Remove all per-phase tuning traces and the legend from the axes."""
        for line in self._tuner_lines:
            try:
                line.remove()
            except ValueError:
                pass
        self._tuner_lines = []
        self._tuner_cur = None
        self._tuner_all_angles = []
        self._tuner_all_powers = []
        legend = self.ax.get_legend()
        if legend is not None:
            legend.remove()

    def _on_tuner_started(self, label: str):
        # Pause the rolling live view — the tuner owns the power meter now.
        # (No btt.clear() here: the tuner needs the power-meter rail in the beam.)
        if self._timer.isActive():
            self._timer.stop()
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
        self._tuner_active = True
        self._clear_tuner_lines()
        self._line.set_data([], [])
        self.ax.set_title(label)
        self.ax.set_xlabel("Waveplate angle (°)")
        self.canvas.draw_idle()

    def _on_tuner_phase(self, name: str):
        if not self._tuner_active:
            return
        color = TUNER_PHASE_COLORS.get(name, TUNER_DEFAULT_COLOR)
        if name == "Final":
            # Single averaged point at the target angle — big standalone marker
            line, = self.ax.plot([], [], "o", markersize=9, color=color,
                                 label=name, zorder=5)
        else:
            line, = self.ax.plot([], [], "o-", markersize=3.5, linewidth=1.2,
                                 color=color, label=name)
        self._tuner_lines.append(line)
        self._tuner_cur = line
        self.ax.legend(loc="best", fontsize=8)
        self.canvas.draw_idle()

    def _on_tuner_reading(self, angle: float, power: float):
        if not self._tuner_active:
            return
        if self._tuner_cur is None:
            self._on_tuner_phase("Scan")
        xs = list(self._tuner_cur.get_xdata()) + [angle]
        ys = list(self._tuner_cur.get_ydata()) + [power]
        self._tuner_cur.set_data(xs, ys)

        self._tuner_all_angles.append(angle)
        self._tuner_all_powers.append(power)
        lo, hi = min(self._tuner_all_angles), max(self._tuner_all_angles)
        pad = max(1.0, 0.05 * (hi - lo))
        self.ax.set_xlim(lo - pad, hi + pad)
        pmax = max(self._tuner_all_powers)
        self.ax.set_ylim(0, pmax * 1.15 if pmax > 0 else 1.0)
        self.canvas.draw_idle()
        self.current_lbl.setText(f"{power:.2f} µW")

    def _on_tuner_finished(self):
        if not self._tuner_active:
            return
        self._tuner_active = False
        self.ax.set_title(self.ax.get_title() + " — done")
        self.canvas.draw_idle()

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _start(self):
        if self._tuner_active:
            self.controller.log(
                "Power meter: tuner running — live view not started")
            return
        if self.btt is not None:
            self.btt.powermeter()
        self._t0 = time.time()
        self._idx = 0
        self._times.fill(0)
        self._powers.fill(0)
        self._clear_tuner_lines()
        self._line.set_data([], [])
        self.ax.set_title("Live power")
        self.ax.set_xlabel("Time (s)")
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
