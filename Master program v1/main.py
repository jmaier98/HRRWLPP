# main.py
import sys
from PyQt6.QtWidgets import QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QLabel

# import your tabs
from webcam_tab_v2     import WebcamTab
from powermeter_tab import PowerMeterTab
from live_lockin_tab import LiveLockinTab
from dynamicControlsTabv2 import DynamicControlsTab
from instrument_manager import InstrumentManager
from microscope_state import MicroscopeState
from imaging_tab_v3 import ImagingTab
from averaged_scan_tab import AveragedScanTab
from photocurrent_scan_tab import PhotocurrentScanTab
from PumpProbeOverlapTab import PumpProbeOverlapTab
from iv_curve_tab import IVCurveTab
from four_map_scan_tab import FourMapScanTab
from one_d_scan_tab import OneDScanTab

class MainWindow(QMainWindow):
    def __init__(self,instrument_manager, state):
        super().__init__()
        self.setWindowTitle("HRRWLPP Microscope")
        self.resize(900, 700)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        self.instrument_manager = instrument_manager
        self.state = state

        # Tab 1: Live Lock-in
        tabs.addTab(LiveLockinTab(instrument_manager), "Live Lock-in")

        # Tab 2: power meter
        tabs.addTab(PowerMeterTab(instrument_manager, state), "Power Meter")

        # Tab 3: placeholder
        tab3 = QWidget()
        v3 = QVBoxLayout(tab3)
        v3.addWidget(QLabel("Dynamic controls"))
        tabs.addTab(DynamicControlsTab(instrument_manager,state), "All Controls")

        # Tab 4: webcam
        tabs.addTab(WebcamTab(instrument_manager, state), "Webcam")

        # Tab 5: picoscope
        tabs.addTab(ImagingTab(instrument_manager, state), "Imaging")

        tabs.addTab(OneDScanTab(instrument_manager, state), "1D Scan")

        tabs.addTab(PhotocurrentScanTab(instrument_manager, state), "Photocurrent Scan")

        tabs.addTab(PumpProbeOverlapTab(instrument_manager, state), "Pump-Probe Overlap")

        tabs.addTab(IVCurveTab(instrument_manager, state), "IV Curve")

        tabs.addTab(FourMapScanTab(instrument_manager, state), "Four Map Scan")
        
        tabs.addTab(AveragedScanTab(instrument_manager, state), "Averaged Scan")

    def closeEvent(self, event):
        # save state when the window is closing
        self.state.save()
        super().closeEvent(event)

if __name__ == "__main__":
    state = MicroscopeState("microscope_state.json")
    state.load()
    instrument_manager = InstrumentManager(state)
    instrument_manager.open_all()
    app = QApplication(sys.argv)
    w   = MainWindow(instrument_manager, state)
    w.show()
    app.aboutToQuit.connect(instrument_manager.close_all)
    app.aboutToQuit.connect(state.save)
    sys.exit(app.exec())
