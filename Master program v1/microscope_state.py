import json
from pathlib import Path

class MicroscopeState:
    def __init__(self, path="microscope_state.json"):
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
            "time zero pos": 10.0,
            "delay_stage_pos": 0.0,
            "delay_stage_speed": 0.0,
            "delay_ps": 0.0,
            "pump_shutter_open": False,
            "probe_shutter_open": False,
            # …
        }
        self.load()

    def load(self):
        if self._path.exists():
            with open(self._path, "r") as f:
                self.settings.update(json.load(f))
        else:
            print("could not find state file")

    def save(self):
        with open(self._path, "w") as f:
            json.dump(self.settings, f, indent=2)
