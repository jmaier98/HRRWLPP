import json
from pathlib import Path

class MicroscopeState:
    def __init__(self, path="state.json"):
        self._path = Path(path)
        # default settings; add whatever you need
        self.settings = {
            "pm_reference": 0.0,
            "probe_power": 0.0,
            "pump_power": 0.0,
            "pump_polarizer_angle": 0.0,
            "probe_polarizer_angle": 0.0,
            "pump_waveplate_angle": 0.0,
            "probe_waveplate_angle": 0.0,
            "galvo_x_position": 0.0,
            "galvo_y_position": 0.0,
            "pump_shutter_open": False,
            "probe_shutter_open": False,
            # …
        }

    def load(self):
        if self._path.exists():
            with open(self._path, "r") as f:
                self.settings.update(json.load(f))

    def save(self):
        with open(self._path, "w") as f:
            json.dump(self.settings, f, indent=2)
