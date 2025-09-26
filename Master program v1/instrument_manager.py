# instrument_manager.py
from drivers import ESP300, SR830, BTT, SHUTTER, PM, Galvo_D2XX, Picoscope, Keithley2400, SP2500

class InstrumentManager:
    def __init__(self, state):
        self._instruments = {}
        self.state = state

    def open_all(self):
        # e.g.:
        self._instruments['BTT'] = BTT.BTT(self.state)
        self._instruments['ESP'] = ESP300.ESP300Controller(self.state, "GPIB0::1::INSTR", timeout=2000)
        self._instruments['SR830'] = SR830.SR830()
        self._instruments['Shutter'] = SHUTTER.SHUTTER()
        self._instruments['PM'] = PM.PowerMeter(self.state)
        self._instruments['Galvo'] = Galvo_D2XX.GALVO()
        self._instruments['Picoscope'] = Picoscope.Picoscope()
        self._instruments['Keithley2400'] = Keithley2400.Keithley2400()
        self._instruments['SP2500'] = SP2500.SP2500()
        # …etc.

    def get(self, name):
        return self._instruments[name]

    def close_all(self):
        print("closing instruments")
        for inst in self._instruments.values():
            inst.close()
