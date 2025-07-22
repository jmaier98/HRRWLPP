import sys
from functools import partial
from math import ceil
from PyQt6.QtWidgets import (
    QApplication, QWidget, QMainWindow,
    QLabel, QLineEdit, QPushButton,
    QGridLayout, QHBoxLayout, QVBoxLayout
)
DEFAULT_FEEDRATE = 1000
class DynamicControlsTab(QWidget):
    def __init__(self, instrument_manager, parent=None):
        super().__init__(parent)
        self.IM = instrument_manager
        try:
            self.btt = self.IM.get("BTT")
        except KeyError:
            raise RuntimeError("BTT controller not found in InstrumentManager")
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
            'rail 2'
        ]
        self.button_labels = [
            'open pump shutter',
            'open probe shutter',
            'close pump shutter',
            'close probe shutter',
            'home rail 1',
            'home rail 2',
            'home pump polarizer',
            'home probe polarizer',
            'home pump waveplate',
            'home probe waveplate',
            'home detector polarizer',
            'home beamsplitter waveplate'
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
        main_layout.addLayout(button_grid)
        self.setLayout(main_layout)

    def on_control_activated(self, idx):
        label = self.entry_labels[idx]
        value = float(self.entries[idx].text().strip())
        print(f"[Entry {idx}] {label} → '{value}'")
        if label == 'pump polarizer':
            self.btt.rot_1(value, DEFAULT_FEEDRATE)
        if label == 'probe polarizer':
            self.btt.rot_2(value, DEFAULT_FEEDRATE)
            

    def on_plain_command(self, jdx):
        cmd = self.button_labels[jdx]
        print(f"[Button {jdx}] Command: {cmd}")
        if cmd == 'home pump polarizer':
            self.btt.home_rot1()
        if cmd == 'home probe polarizer':
            self.btt.home_rot2()
            
