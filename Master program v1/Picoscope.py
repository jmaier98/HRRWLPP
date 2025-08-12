# picoscope.py
"""
Driver for a PicoScope 5444D MSO in streaming‑mode with:
  • 1 × analog channel (Ch A, ±5 V range, DC‑coupled)
  • 1 × digital port (D0–D7, 1.5 V logic threshold)

The driver is *idle* until you call `start_stream()`, and it keeps the USB
connection open for the life of your program.
"""
import ctypes, time, threading, numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from picosdk.ps5000a import ps5000a as ps
from picosdk.functions import assert_pico_ok


class Picoscope(QObject):
    """
    Wrapper that looks like your other instrument drivers:
      Picoscope.start_stream()   → begin emitting data
      Picoscope.stop_stream()    → halt streaming
      Picoscope.close()          → final shutdown (called by InstrumentManager)
    Signals:
      new_block(analog, digital) where both args are 1‑D numpy arrays
      stopped()                  when streaming stops (manual or auto‑stop)
    """
    new_block = pyqtSignal(np.ndarray, np.ndarray)
    stopped   = pyqtSignal()

    # ---- constructor / destructor ---------------------------------------
    def __init__(self, parent=None):
        super().__init__(parent) 
        # --- Open the scope ---
        chandle = ctypes.c_int16()
        assert_pico_ok(ps.ps5000aOpenUnit(
            ctypes.byref(chandle), None,
            ps.PS5000A_DEVICE_RESOLUTION['PS5000A_DR_12BIT']
        ))
        self.chandle = chandle
        
        # --- Enable Channel A at ±2 V DC ---
        assert_pico_ok(ps.ps5000aSetChannel(
            chandle,
            ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A'],
            1,
            ps.PS5000A_COUPLING['PS5000A_DC'],
            ps.PS5000A_RANGE['PS5000A_10V'],
            0.0
        ))
        # --- Initialize digital port D0–D7 with 1.5 V threshold ---
        threshold_volts = 1.5
        logic_level = int(threshold_volts / 5.0 * 32767)  # ≈9830
        assert_pico_ok(ps.ps5000aSetDigitalPort(
            chandle,
            ps.PS5000A_CHANNEL['PS5000A_DIGITAL_PORT0'],  # D0–D7
            1,                                            # enable port
            logic_level                                  # threshold count
        ))


        print("PicoScope initialized")
    def get_chandle(self):
        """Return the C handle for this scope."""
        return self.chandle
    # ---- internal helpers -------------------------------------------------
    def _streaming_callback(self, handle, n_samples, start_idx,
                            overflow, trigger_at, triggered,
                            auto_stop_flag, user_data):
        """C‑level callback that copies the new slice into Python lists."""
        if n_samples == 0:
            return
        # pull the new samples out of the DMA buffers
        a_slice = self._buf_a[start_idx:start_idx + n_samples]
        d_slice = self._buf_d[start_idx:start_idx + n_samples]
        with self._lock:
            self._analog.extend(a_slice.tolist())
            self._digital.extend(d_slice.tolist())
        if auto_stop_flag:
            self._done = True


    def _poll_loop(self):
        """Background thread that keeps polling GetStreamingLatestValues()."""
        while not self._stop_event.is_set() and not self._done:
            ps.ps5000aGetStreamingLatestValues(self.chandle,
                                            self._c_cb, None)
            time.sleep(self._poll_int)               # 1–10 ms is typical

        ps.ps5000aStop(self.chandle)                 # clean shutdown
        self.stopped.emit()
    # ---- public control ---------------------------------------------------
    def start_stream(self,
                    fs_hz=100_000,
                    auto_stop_samples=500_000,
                    poll_interval=0.005):
        """
        Begin streaming.  Typical usage:
            scope.start_stream()
        Parameters
        ----------
        fs_hz : int
            Sample‑rate in Hertz (analog *and* digital).
        auto_stop_samples : int
            Stop automatically after this many samples (0 ⇒ run forever).
        poll_interval : float
            Seconds between driver polls for new data.
        """
        # already running?
        if hasattr(self, '_poll_thread') and self._poll_thread.is_alive():
            return

        # thread‑safe containers for new samples
        self._lock       = threading.Lock()
        self._analog     = []          # raw ADC counts (int16)
        self._digital    = []          # 0‑255 bitmasks
        self._done       = False
        self._poll_int   = poll_interval
        self._stop_event = threading.Event()

        # build C‑compatible callback once
        self._c_cb = ps.StreamingReadyType(self._streaming_callback)

        # configure timing
        sample_interval_us = int(1e6 // fs_hz)          # µs between samples
        sample_interval    = ctypes.c_int32(sample_interval_us)

        assert_pico_ok(ps.ps5000aRunStreaming(
            self.chandle,
            ctypes.byref(sample_interval),
            ps.PS5000A_TIME_UNITS['PS5000A_US'],
            0,                                # pre‑trigger samples
            auto_stop_samples if auto_stop_samples else 0x3FFFFFFF,
            1 if auto_stop_samples else 0,    # auto‑stop flag
            1,                                # down‑sample ratio
            ps.PS5000A_RATIO_MODE['PS5000A_RATIO_MODE_NONE'],
            len(self._buf_a)                  # overview buffer size
        ))

        # launch poller
        self._poll_thread = threading.Thread(target=self._poll_loop,
                                            daemon=True)
        self._poll_thread.start()


    def get_latest_values(self):
        """
        Fetch **and clear** everything received since the previous call.
        Returns
        -------
        analog  : list[int]   (ADC counts from Ch A)
        digital : list[int]   (bit‑masks for D0–D7)
        """
        with self._lock:
            a = self._analog[:]
            d = self._digital[:]
            self._analog.clear()
            self._digital.clear()
        return a, d


    def stop_stream(self):
        """Request the background thread to halt and wait for it."""
        if not hasattr(self, '_stop_event'):
            return
        self._stop_event.set()
        self._poll_thread.join()
    def close(self):
        ps.ps5000aStop(self.chandle)
        ps.ps5000aCloseUnit(self.chandle)
        print("PicoScope closed")


