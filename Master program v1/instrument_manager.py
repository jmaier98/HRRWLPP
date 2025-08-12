# instrument_manager.py
import serial
import ESP300
import SR830
import BTT
import SHUTTER
import PM
import GALVO
import Picoscope

class InstrumentManager:
    def __init__(self, state):
        self._instruments = {}
        self.state = state

    def open_all(self):
        # e.g.:
        self._instruments['BTT'] = BTT.BTT(self.state)
        self._instruments['ESP'] = ESP300.ESP300Controller("GPIB0::1::INSTR", timeout=2000)
        self._instruments['SR830'] = SR830.SR830()
        self._instruments['Shutter'] = SHUTTER.SHUTTER()
        self._instruments['PM'] = PM.PowerMeter(self.state)
        self._instruments['Galvo'] = GALVO.GALVO()
        self._instruments['Picoscope'] = Picoscope.Picoscope()
        # …etc.

    def get(self, name):
        return self._instruments[name]

    def close_all(self):
        print("closing instruments")
        for inst in self._instruments.values():
            inst.close()
