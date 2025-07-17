# instrument_manager.py
import serial
import ESP300
import SR830
import BTT
import SHUTTER

class InstrumentManager:
    def __init__(self):
        self._instruments = {}

    def open_all(self):
        # e.g.:
        self._instruments['BTT'] = BTT.BTT()
        self._instruments['ESP'] = ESP300.ESP300Controller("GPIB0::1::INSTR", timeout=2000)
        self._instruments['lockin'] = SR830.SR830()
        self._instruments['Shutter'] = SHUTTER.SHUTTER()
        # …etc.

    def get(self, name):
        return self._instruments[name]

    def close_all(self):
        for inst in self._instruments.values():
            inst.close()
