import json
import os
import tempfile
from pathlib import Path

class MicroscopeState:
    def __init__(self, path="microscope_state.json"):
        self._path = Path(path)
        self.settings = {
            "pm_reference": 0.0,
            "probe_power": 0.0,
            "pump_power": 0.0,
            "rot_1_angle": 0.0,
            "rot_2_angle": 0.0,
            "rot_3_angle": 0.0,
            "rot_4_angle": 0.0,
            "galvo_x_position": 0.0,
            "galvo_y_position": 0.0,
            "time zero pos": 10.0,
            "delay_stage_pos": 0.0,
            "delay_stage_speed": 0.0,
            "delay_ps": 0.0,
            "pump_shutter_open": False,
            "probe_shutter_open": False,
            "x_original_feature_pos": 0.0,
            "y_original_feature_pos": 0.0,
            "x_found_feature_pos": 0.0,
            "y_found_feature_pos": 0.0,
            "x_start_position": 0.0,
            "y_start_position": 0.0,
            "x_offset": 0.0,
            "y_offset": 0.0,
            "focus_position": 0.0,
            # Motorized mirror positions (microsteps). Mirror 1 = BTT X/Y
            # motors (x/y offset), mirror 2 = BTT Z/E motors (beam walk).
            "mirror_1_x_steps": 0,
            "mirror_1_y_steps": 0,
            "mirror_2_x_steps": 0,
            "mirror_2_y_steps": 0,
            # Keithley gate ramping: every voltage change walks to its target
            # in steps of this size, waiting the dwell between steps.
            # Traces recorded when the alignment origin was last set, used by
            # the "Recorded feature shape" fit: {"x": [...], "y": [...], "center": float}
            "align_template_h": None,
            "align_template_v": None,
            "keithley_ramp_step_v": 0.05,
            "keithley_ramp_dwell_s": 0.02,
            "temp_setpoint": 300.0,
            "temp_tolerance_k": 0.5,
            "temp_settle_s": 10.0,
            "temp_timeout_s": 900.0,
        }
        self.load()

    def load(self):
        if self._path.exists():
            with open(self._path, "r") as f:
                self.settings.update(json.load(f))
        else:
            print("could not find state file")

    def save(self):
        # Written atomically: save() is called on every objective move, so a
        # crash mid-write must not leave a truncated state file behind.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent or "."), prefix=".state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.settings, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
