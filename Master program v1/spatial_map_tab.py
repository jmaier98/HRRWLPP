# imaging_tab.py

import time
import numpy as np

from PyQt6.QtCore    import Qt, QThread, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtGui     import QDoubleValidator, QImage, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QLineEdit, QPushButton, QMessageBox
)




class ImagingTab(QWidget):
    """
    Tab for live 2D imaging via a PicoScope + galvo.
    Controls on the left, image on the right.
    """
    def __init__(self, instrument_manager, state):
        super().__init__()
        self.IM    = instrument_manager
        self.state = state

        # get drivers from InstrumentManager
        self.scope = self.IM.get("Picoscope")
        self.galvo = self.IM.get("Galvo")

        # image buffer & cursor
        self.image_data = None
        self.cur_row    = 0
        self.cur_col    = 0

        self._build_ui()

        self._prev_d = 0
        '''example code, this all works'''
        self.scope.start_stream()
        time.sleep(0.1)
        analog, digital = self.scope.get_latest_values()
        print(len(analog), "new analog samples",len(digital), "new digital samples")
        self.scope.stop_stream()
        #-----------------------------

    def _build_ui(self):
        ctrl = QVBoxLayout()

        # six numeric inputs
        self.x0_edit = self._make_line("X start (V):", ctrl, QDoubleValidator())
        self.x1_edit = self._make_line("X end   (V):", ctrl, QDoubleValidator())
        self.nx_edit = self._make_line("X points:", ctrl, QDoubleValidator(1, 10000, 0))

        self.y0_edit = self._make_line("Y start (V):", ctrl, QDoubleValidator())
        self.y1_edit = self._make_line("Y end   (V):", ctrl, QDoubleValidator())
        self.ny_edit = self._make_line("Y points:", ctrl, QDoubleValidator(1, 10000, 0))

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

    def _make_line(self, label, layout, validator):
        row = QHBoxLayout()
        lbl = QLabel(label)
        edt = QLineEdit()
        edt.setValidator(validator)
        row.addWidget(lbl)
        row.addWidget(edt)
        layout.addLayout(row)
        return edt

    

    

    
