import pyvisa

ADDRESS_1 = 18
ADDRESS_2 = 8
class SR830:
    """
    Driver for the Stanford Research Systems SR830 Lock-in Amplifier via GPIB.
    Uses 'SNAP?' to fetch multiple parameters in one query for minimal latency.
    """

    def __init__(self, gpib_addr: int = 18, timeout: int = 2000):
        """
        Open a connection to the SR830 on the given GPIB address.
        :param gpib_addr: your instrument’s GPIB primary address (default 8)
        :param timeout: VISA timeout in milliseconds
        """
        self._rm = pyvisa.ResourceManager()
        self._inst1 = self._rm.open_resource(f'GPIB::{ADDRESS_1}::INSTR')
        print("SR830 onlineA")
        self._inst2 = self._rm.open_resource(f'GPIB::{ADDRESS_2}::INSTR')
        print("SR830 onlineB")

    def read_x(self):
        """
        Read the x value from the middle lockin amplifier.
        (Assumes that the command "OUTP? 1" returns x.)
        """
        try:
            return float(self._inst1.query("OUTP? 1"))
        except Exception as e:
            print(f"Error in readx1: {e}")
            return None
    def read_y(self):
        """
        Read the x value from the middle lockin amplifier.
        (Assumes that the command "OUTP? 1" returns x.)
        """
        try:
            return float(self._inst1.query("OUTP? 2"))
        except Exception as e:
            print(f"Error in readx1: {e}")
            return None
    def read_xy(self) -> tuple[float, float]:
        """
        Fastest read of X and Y: SNAP?1,2 → "xval,yval"
        Returns (X, Y) as floats.
        """
        resp = self._inst1.query("SNAP?1,2")
        x_str, y_str = resp.strip().split(',')
        return float(x_str), float(y_str)
    
    def read_x2(self):
        """
        Read the x value from the middle lockin amplifier.
        (Assumes that the command "OUTP? 1" returns x.)
        """
        try:
            return float(self._inst2.query("OUTP? 1"))
        except Exception as e:
            print(f"Error in readx1: {e}")
            return None
    def read_y2(self):
        """
        Read the x value from the middle lockin amplifier.
        (Assumes that the command "OUTP? 1" returns x.)
        """
        try:
            return float(self._inst2.query("OUTP? 2"))
        except Exception as e:
            print(f"Error in readx1: {e}")
            return None
    def read_xy2(self) -> tuple[float, float]:
        """
        Fastest read of X and Y: SNAP?1,2 → "xval,yval"
        Returns (X, Y) as floats.
        """
        resp = self._inst2.query("SNAP?1,2")
        x_str, y_str = resp.strip().split(',')
        return float(x_str), float(y_str)
    def readX_A(self) -> tuple[float, float]:
        """
        Return the X-channel output and Aux 1 input from lockin 1.
        Uses SNAP?1,5 → X (1), Aux In 1 (5)
        Returns (X, Aux1) as floats.
        """
        try:
            resp = self._inst1.query("SNAP?1,5")
            x_str, aux1_str = resp.strip().split(',')
            return float(x_str), float(aux1_str)
        except Exception as e:
            print(f"Error in readX_A: {e}")
            return None, None
    
    def close(self):
 # restore display updates if needed
        self._inst1.close()
        self._inst2.close()
        self._rm.close()


if __name__ == "__main__":

    lockin = SR830(gpib_addr=18, timeout=2000)

    # single read:
    x = lockin.read_x()
    print(f"Single read → X={x:+.6e}")
    y = lockin.read_y()
    print(f"Single read → Y={y:+.6e}")
    x, y = lockin.read_xy()
    print(f"Single read → X={x:+.6e}, Y={y:+.6e}")
    x = lockin.read_x2()
    print(f"Single read → X2={x:+.6e}")


    lockin.close()
