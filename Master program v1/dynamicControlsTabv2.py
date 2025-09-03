import sys
from functools import partial
from math import ceil
from PyQt6.QtWidgets import (
    QApplication, QWidget, QMainWindow,
    QLabel, QLineEdit, QPushButton,
    QGridLayout, QHBoxLayout, QVBoxLayout, QGroupBox, QFormLayout
)
from PyQt6.QtCore import QThread, pyqtSignal
from time import sleep
from power_tuners import ProbeTuner, PumpTuner
DEFAULT_FEEDRATE = 1000






class DynamicControlsTab(QWidget):
    def __init__(self, instrument_manager, state, parent=None):
        super().__init__(parent)
        self.IM = instrument_manager
        self.state = state
        self.PM = self.IM.get("PM")
        self.shutter = self.IM.get("Shutter")
        self.Keithley2400 = self.IM.get("Keithley2400")
        try:
            self.btt = self.IM.get("BTT")
        except KeyError:
            raise RuntimeError("BTT controller not found in InstrumentManager")
        self._tuner_thread = None
        self.entry_labels = [
            'pump polarizer',
            'probe polarizer',
            'pump half waveplate',
            'probe half waveplate',
            'pump power',
            'probe power',
            'pump longpass',
            'probe longpass',
            'pump shortpass',
            'probe shortpass',
            'detector longpass',
            'detector shortpass',
            'detector polarizer',
            'delay stage pos',
            'temperature',
            'galvo x position',
            'galvo y position',
            'move cryo x',
            'move cryo y',
            'move cryo z',
            'move objective stage',
            'rail 1',
            'rail 2',
            'ramp gate voltage (v)'
        ]
        self.button_labels = [
            'open pump shutter',
            'open probe shutter',
            'close pump shutter',
            'close probe shutter',
            'home rails',
            'home pump polarizer',
            'home probe polarizer',
            'home pump waveplate',
            'home probe waveplate',
            'home detector polarizer',
            'home beamsplitter waveplate',
            'enable keithley output',
            'disable keithley output'
        ]
        self.entries = []
        self.go_buttons = []
        self.plain_buttons = []

        # — Layout for entry+Go controls —
        columns = 6
        rows = ceil(len(self.entry_labels) / columns)
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(10)

        for idx, text in enumerate(self.entry_labels):
            row = idx // columns
            col = idx % columns

            lbl   = QLabel(text + ":")
            entry = QLineEdit()
            entry.setPlaceholderText("value…")
            btn   = QPushButton("Go")
            btn.clicked.connect(partial(self.on_control_activated, idx))

            self.entries.append(entry)
            self.go_buttons.append(btn)

            # stack vertically within the grid cell
            cell = QWidget()
            vbox = QVBoxLayout(cell)
            vbox.setContentsMargins(0,0,0,0)
            vbox.addWidget(lbl)
            vbox.addWidget(entry)
            vbox.addWidget(btn)

            grid.addWidget(cell, row, col)

        # stretch to push everything up if there's spare space
        grid.setRowStretch(rows, 1)

        # — Display current state variables —
        self.state_group = QGroupBox("Current State")
        self.state_layout = QFormLayout()
        self.state_group.setLayout(self.state_layout)

        # keep refs to the QLabel widgets so we can update them later
        self.state_labels = {}
        for key, val in self.state.settings.items():
            name_lbl  = QLabel(f"{key}:")
            value_lbl = QLabel(str(val))
            self.state_labels[key] = value_lbl
            self.state_layout.addRow(name_lbl, value_lbl)

        # — Layout for plain buttons —
        btn_columns = 6
        btn_rows = ceil(len(self.button_labels) / btn_columns)
        button_grid = QGridLayout()
        button_grid.setHorizontalSpacing(15)
        button_grid.setVerticalSpacing(10)

        for jdx, cmd in enumerate(self.button_labels):
            row = jdx // btn_columns
            col = jdx % btn_columns
            pb = QPushButton(cmd)
            pb.clicked.connect(partial(self.on_plain_command, jdx))
            self.plain_buttons.append(pb)
            button_grid.addWidget(pb, row, col)

        

        # — Combine both layouts —
        main_layout = QVBoxLayout(self)
        main_layout.addLayout(grid)
        main_layout.addWidget(self.state_group)
        main_layout.addLayout(button_grid)
        self.setLayout(main_layout)

    def update_state_display(self):
        for key, lbl in self.state_labels.items():
            val = self.state.settings.get(key, "")
            if isinstance(val, float):
                # format *any* float to two decimal places
                text = f"{val:.2f}"
            else:
                text = str(val)
            lbl.setText(text)
        QApplication.processEvents()

    def on_control_activated(self, idx):
        label = self.entry_labels[idx]
        value = float(self.entries[idx].text().strip())
        print(f"[Entry {idx}] {label} → '{value}'")
        if label == 'pump polarizer':
            self.btt.rot_1(value, 5000)
        if label == 'probe polarizer':
            self.btt.rot_2(value, DEFAULT_FEEDRATE)
        if label == 'pump half waveplate':
            self.btt.rot_3(value, DEFAULT_FEEDRATE)
        if label == 'probe half waveplate':
            self.btt.rot_4(value, DEFAULT_FEEDRATE)
        if label == 'probe power':
            tuner = ProbeTuner(
                target       = value,
                pm           = self.PM,
                btt          = self.btt,
                shutter      = self.shutter,
                initial_wp   = self.state.settings["probe_waveplate_angle"],
            )

            # 1) If there’s already a tuner running, stop it cleanly:
            if self._tuner_thread and self._tuner_thread.isRunning():
                self._tuner_thread.quit()
                self._tuner_thread.wait()

            # 2) Keep a reference so it isn't GC’d:
            self._tuner_thread = tuner

            # 3) Connect signals
            tuner.reading_ready.connect(self._on_probe_update)

            # once the thread really finishes, delete the object and clear your ref:
            tuner.finished.connect(tuner.deleteLater)
            tuner.finished.connect(lambda: setattr(self, '_tuner_thread', None))

            # 4) Start it
            tuner.start()
        if label == 'pump power':
            tuner = PumpTuner(
                target       = value,
                pm           = self.PM,
                btt          = self.btt,
                shutter      = self.shutter,
                initial_wp   = self.state.settings["pump_waveplate_angle"],
            )

            # 1) If there’s already a tuner running, stop it cleanly:
            if self._tuner_thread and self._tuner_thread.isRunning():
                self._tuner_thread.quit()
                self._tuner_thread.wait()

            # 2) Keep a reference so it isn't GC’d:
            self._tuner_thread = tuner

            # 3) Connect signals
            tuner.reading_ready.connect(self._on_pump_update)

            # once the thread really finishes, delete the object and clear your ref:
            tuner.finished.connect(tuner.deleteLater)
            tuner.finished.connect(lambda: setattr(self, '_tuner_thread', None))

            # 4) Start it
            tuner.start()
        if label == 'ramp gate voltage (v)':
            self.Keithley2400.ramp_voltage(value, verbose = True)  # uses driver defaults for step/dwell

    def _on_probe_update(self, angle, power):
        # update both waveplate‑angle & power in your state
        self.state.settings["probe_waveplate_angle"] = angle
        self.state.settings["probe_power"]            = power
        self.update_state_display() 
    def _on_pump_update(self, angle, power):
        # update both waveplate‑angle & power in your state
        self.state.settings["pump_waveplate_angle"] = angle
        self.state.settings["pump_power"]            = power
        self.update_state_display() 
            

    def on_plain_command(self, jdx):
        cmd = self.button_labels[jdx]
        print(f"[Button {jdx}] Command: {cmd}")
        if cmd == 'home pump polarizer':
            self.btt.home_rot1()
        if cmd == 'home probe polarizer':
            self.btt.home_rot2()
        if cmd == 'home pump waveplate':
            self.btt.home_rot3()
        if cmd == 'home rails':
            self.btt.homeRails()
        if cmd == 'enable keithley output':
            self.Keithley2400.output_on()
        if cmd == 'disable keithley output':
            self.Keithley2400.output_off()
            
