# main.py
import sys
from PyQt6.QtWidgets import QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QLabel

# import your tabs
from webcam_tab_v2     import WebcamTab
from powermeter_tab import PowerMeterTab
from controls_tab import ControlsTab
from instrument_manager import InstrumentManager

class MainWindow(QMainWindow):
    def __init__(self,instrument_manager):
        super().__init__()
        self.setWindowTitle("Multi-Tab PyQt6 Example")
        self.resize(900, 700)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        self.instrument_manager = instrument_manager
        
        # Tab 1: placeholder
        tabs.addTab(ControlsTab(instrument_manager), "Controls")

        # Tab 2: power meter
        tabs.addTab(PowerMeterTab(instrument_manager), "Power Meter")

        # Tab 3: placeholder
        tab3 = QWidget()
        v3 = QVBoxLayout(tab3)
        v3.addWidget(QLabel("Placeholder content for Tab 3"))
        tabs.addTab(tab3, "Tab 3")

        # Tab 4: webcam
        tabs.addTab(WebcamTab(instrument_manager), "Webcam")

if __name__ == "__main__":
    instrument_manager = InstrumentManager()
    instrument_manager.open_all()
    app = QApplication(sys.argv)
    w   = MainWindow(instrument_manager)
    w.show()
    app.aboutToQuit.connect(instrument_manager.close_all)
    sys.exit(app.exec())
