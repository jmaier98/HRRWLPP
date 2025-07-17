import sys
import cv2
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSlider
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QImage, QPixmap

class WebcamTab(QWidget):
    def __init__(self):
        super().__init__()
        # Defer webcam capture initialization to speed up startup
        self.cap = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)

        # Main layout for this tab
        layout = QHBoxLayout(self)
        control_layout = QVBoxLayout()
        video_layout = QVBoxLayout()

        # Controls on the left
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_capture)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_capture)
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(-100, 100)
        self.brightness_slider.setValue(0)
        self.brightness_slider.setTickInterval(10)
        self.brightness_slider.setTickPosition(QSlider.TickPosition.TicksBelow)

        control_layout.addWidget(self.start_btn)
        control_layout.addWidget(self.stop_btn)
        control_layout.addWidget(QLabel("Brightness"))
        control_layout.addWidget(self.brightness_slider)
        control_layout.addStretch()

        # Video display on the right
        self.video_label = QLabel()
        self.video_label.setFixedSize(640, 480)
        video_layout.addWidget(self.video_label)

        layout.addLayout(control_layout)
        layout.addLayout(video_layout)

    def start_capture(self):
        if self.cap is None:
            # Specify backend for faster init on Windows; remove for other OS or choose your preferred backend
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        self.timer.start(30)  # Update every 30 ms (~33 FPS)

    def stop_capture(self):
        self.timer.stop()
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def update_frame(self):
        if self.cap is None:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        # Adjust brightness based on slider
        brightness = self.brightness_slider.value()
        frame = cv2.convertScaleAbs(frame, alpha=1, beta=brightness)
        # Convert color for Qt
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame.shape
        bytes_per_line = ch * w
        qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        self.video_label.setPixmap(QPixmap.fromImage(qimg))

    def closeEvent(self, event):
        if self.cap is not None:
            self.cap.release()
        event.accept()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multi-Tab PyQt6 Example with Webcam")
        self.resize(800, 600)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Create dummy tabs
        for i in range(1, 4):
            tab = QWidget()
            layout = QVBoxLayout(tab)
            layout.addWidget(QLabel(f"Placeholder content for Tab {i}"))
            self.tabs.addTab(tab, f"Tab {i}")

        # Create the webcam tab
        webcam_tab = WebcamTab()
        self.tabs.addTab(webcam_tab, "Webcam")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
