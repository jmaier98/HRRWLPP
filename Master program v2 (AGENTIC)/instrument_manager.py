from drivers import ESP300, ESP301, SR830, BTT, SHUTTER, PM, Galvo_D2XX, Picoscope, Keithley2400, SP2500_v2 as SP2500, Lakeshore331, MIRRORS

class InstrumentManager:
    def __init__(self, state):
        self._instruments = {}
        self.state = state

    def _open(self, name, factory, required=True):
        """Open one instrument, reporting its name if it fails.

        A failed instrument is stored as None so the rest can still open.
        Returns the error message on failure (None on success)."""
        try:
            self._instruments[name] = factory()
            return None
        except Exception as e:
            self._instruments[name] = None
            msg = f"{name}: {type(e).__name__}: {e}"
            print(f"[{'ERROR' if required else 'WARNING'}] Failed to open {msg}")
            return msg if required else None

    def open_all(self):
        s = self.state
        steps = [
            ('BTT',          lambda: BTT.BTT(s)),
            ('ESP',          lambda: ESP300.ESP300Controller(s, "GPIB0::1::INSTR", timeout=2000)),
            ('SR830',        lambda: SR830.SR830()),
            ('Shutter',      lambda: SHUTTER.SHUTTER()),
            ('PM',           lambda: PM.PowerMeter(s)),
            ('Galvo',        lambda: Galvo_D2XX.GALVO(s)),
            ('Picoscope',    lambda: Picoscope.Picoscope()),
            ('Keithley2400', lambda: Keithley2400.Keithley2400(s)),
        ]
        failures = [self._open(name, f) for name, f in steps]
        # Mirrors before the SP2500: the spectrometer driver defaults to COM5,
        # which is now the mirror BTT, and would otherwise hold the port.
        self._open('Mirrors', lambda: MIRRORS.Mirrors(s), required=False)
        self._open('SP2500', lambda: SP2500.SP2500(), required=False)
        failures.append(self._open('Lakeshore331', lambda: Lakeshore331.LakeShore331()))

        failures = [f for f in failures if f]
        if failures:
            raise RuntimeError("Could not open instrument(s):\n  " + "\n  ".join(failures))

    def get(self, name):
        return self._instruments[name]

    def close_all(self):
        print("closing instruments")
        for inst in self._instruments.values():
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
