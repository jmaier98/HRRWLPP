import pyvisa
import time

class PowerMeter:
    def __init__(self, state):
        self.state = state
        self.resource_name = state.settings["pm_reference"]
        self.rm = None
        self.inst = None
        self.open()
        time.sleep(0.05)
        #print(self.read_power())
        
    def find_pm16(self):
        rm = pyvisa.ResourceManager('@py')       # use pyvisa-py backend
        resources = rm.list_resources('USB?*::?*::?*::INSTR')          # list all INSTR resources :contentReference[oaicite:1]{index=1}
        for res in resources:
            try:
                inst = rm.open_resource(res)
                idn = inst.query('*IDN?')
                if 'PM16-122' in idn:
                    print(f"Found PM16-122 on {res}")
                    self.state.settings["pm_reference"] = res
                    self.state.save()
                    return res
            except Exception:
                continue
        print("Could not find PM16-122")
        print("\033[31m" + "PM offline" + "\033[0m")
        return "Could not find PM16-122"
    
    def open(self):
        """Open the VISA session if not already open."""
        if self.inst is None:
            self.rm = pyvisa.ResourceManager('@py')
            try:
                self.inst = self.rm.open_resource(self.resource_name)
                self.inst.write("SENS:POW:UNIT W")
                self.inst.write("SENS:POW:RANG AUTO")
                print("acquired PM")
            except Exception as e:
                print("searching for PM")
                res_name = self.find_pm16()
                if res_name != "Could not find PM16-122":
                    self.inst = self.rm.open_resource(res_name)
                    self.inst.write("SENS:POW:UNIT W")
                    self.inst.write("SENS:POW:RANG AUTO")

    def close(self):
        """Close the VISA session."""
        if self.inst is not None:
            self.inst.close()
            self.inst = None
            self.rm = None

    def set_range(self, reading_range):
        """
        Set the power measurement range.

        reading_range: a numeric value (Watts) or 'AUTO' for auto-range.
        """
        self.inst.write(f"SENS:POW:RANG {reading_range}")

    def set_wavelength(self, wavelength_nm):
        """
        Set the calibration wavelength in nanometers.

        wavelength_nm: numeric wavelength in nm.
        """
        self.inst.write(f"SENS:POW:WAV {wavelength_nm}")

    def read_power(self):
        """
        Trigger a measurement and return the power reading in Watts.
        """
        resp = self.inst.query("READ?")
        try:
            return float(resp)*10**6
        except ValueError:
            raise RuntimeError(f"Unexpected response from power meter: {resp}")
