from PyQt6.QtCore import QThread, pyqtSignal
from time import sleep
DEFAULT_FEEDRATE = 1000
class ProbeTuner(QThread):
    """Tunes the probe power by rotating the probe waveplate."""
    reading_ready = pyqtSignal(float, float)   # emits each new power reading
    finished       = pyqtSignal()       # emits when done

    def __init__(self, target, pm, btt, shutter, initial_wp):
        super().__init__()
        self.target     = target
        self.pm         = pm
        self.btt        = btt
        self.shutter    = shutter
        self.initial_wp = initial_wp

    def run(self):
        # 1) Prep
        self.shutter.closePump()
        self.shutter.openProbe()
        self.btt.powermeter()
        sleep(2)  # let the powermeter settle

        # 2) First reading
        reading = self.pm.read_power()
        self.reading_ready.emit(self.initial_wp, reading)

        # 3) Decide direction
        diff = abs(self.target - reading)
        if diff > self.target*.1:
            dTheta = 4
            self.btt.rot_4(self.initial_wp + dTheta, DEFAULT_FEEDRATE)
            sleep(0.2)
            reading = self.pm.read_power()
            self.reading_ready.emit(self.initial_wp+dTheta, reading)

            # step sign
            newdiff = abs(self.target - reading)
            theta_step = 2 if newdiff < diff else -2
        else:
            dTheta = 0
            theta_step = 0

        # 4) Main tuning loop
        while abs(self.target - reading) > self.target*.1 and abs(dTheta) < 180:
            dTheta += theta_step
            self.btt.rot_4(self.initial_wp + dTheta, DEFAULT_FEEDRATE)
            sleep(0.1)
            reading = self.pm.read_power()
            self.reading_ready.emit(self.initial_wp + dTheta, reading)
        # 5) fine tuning loop
        dTheta -= 2*theta_step
        print("fine tuning")
        while abs(self.target - reading) > self.target*.01 and abs(dTheta) < 180:
            dTheta += theta_step / 20
            self.btt.rot_4(self.initial_wp + dTheta, DEFAULT_FEEDRATE)
            sleep(0.05)
            reading = self.pm.read_power()
            self.reading_ready.emit(self.initial_wp + dTheta, reading)

        if abs(dTheta) >= 180:
            print("Reached maximum waveplate angle, stopping adjustment.")

        self.btt.clear()
        self.finished.emit()
class PumpTuner(QThread):
    """Tunes the pump power by rotating the pump waveplate."""
    reading_ready = pyqtSignal(float, float)   # emits each new power reading
    finished       = pyqtSignal()       # emits when done

    def __init__(self, target, pm, btt, shutter, initial_wp):
        super().__init__()
        self.target     = target
        self.pm         = pm
        self.btt        = btt
        self.shutter    = shutter
        self.initial_wp = initial_wp

    def run(self):
        # 1) Prep
        self.shutter.openPump()
        self.shutter.closeProbe()
        self.btt.powermeter()
        sleep(2)  # let the powermeter settle

        # 2) First reading
        reading = self.pm.read_power()
        self.reading_ready.emit(self.initial_wp, reading)

        # 3) Decide direction
        diff = abs(self.target - reading)
        if diff > self.target*.1:
            dTheta = 4
            self.btt.rot_3(self.initial_wp + dTheta, DEFAULT_FEEDRATE)
            sleep(0.2)
            reading = self.pm.read_power()
            self.reading_ready.emit(self.initial_wp+dTheta, reading)

            # step sign
            newdiff = abs(self.target - reading)
            theta_step = 2 if newdiff < diff else -2
        else:
            dTheta = 0
            theta_step = 0

        # 4) Main tuning loop
        while abs(self.target - reading) > self.target*.1 and abs(dTheta) < 180:
            dTheta += theta_step
            self.btt.rot_3(self.initial_wp + dTheta, DEFAULT_FEEDRATE)
            sleep(0.1)
            reading = self.pm.read_power()
            self.reading_ready.emit(self.initial_wp + dTheta, reading)
        # 5) fine tuning loop
        dTheta -= 2*theta_step
        print("fine tuning")
        while abs(self.target - reading) > self.target*.01 and abs(dTheta) < 180:
            dTheta += theta_step / 20
            self.btt.rot_3(self.initial_wp + dTheta, DEFAULT_FEEDRATE)
            sleep(0.05)
            reading = self.pm.read_power()
            self.reading_ready.emit(self.initial_wp + dTheta, reading)

        if abs(dTheta) >= 180:
            print("Reached maximum waveplate angle, stopping adjustment.")

        self.btt.clear()
        self.finished.emit()