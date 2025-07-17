#!/usr/bin/env python3
import ctypes
import time
import threading
from collections import deque
import bisect

import numpy as np
from picosdk.ps5000a import ps5000a as ps
from picosdk.functions import adc2mV, assert_pico_ok

class PicoStreamer:
    def __init__(self,
                 channel=ps.PS5000A_CHANNEL["PS5000A_CHANNEL_A"],
                 vrange=ps.PS5000A_RANGE["PS5000A_200MV"],
                 coupling=ps.PS5000A_COUPLING["PS5000A_DC"],
                 sample_rate_hz=10_000,
                 chunk_size=10,
                 max_age_s=0.100):
        # Device handle + status dict
        self.chandle = ctypes.c_int16()
        self.status = {}
        # Open unit @ 12-bit
        res = ps.PS5000A_DEVICE_RESOLUTION["PS5000A_DR_12BIT"]
        self.status["open"] = ps.ps5000aOpenUnit(ctypes.byref(self.chandle), None, res)
        assert_pico_ok(self.status["open"])
        # If on USB bus-power only, may need to change power source
        if self.status["open"] in (282, 286):
            self.status["chgPS"] = ps.ps5000aChangePowerSource(self.chandle, self.status["open"])
            assert_pico_ok(self.status["chgPS"])
        # Channel config
        self.status["ch"] = ps.ps5000aSetChannel(
            self.chandle,
            channel,
            1,           # enabled
            coupling,
            vrange,
            0.0          # offset
        )
        assert_pico_ok(self.status["ch"])
        # ADC→mV conversion factor
        self.maxADC = ctypes.c_int16()
        self.status["max"] = ps.ps5000aMaximumValue(self.chandle, ctypes.byref(self.maxADC))
        assert_pico_ok(self.status["max"])
        # Streaming parameters
        self.sample_interval_s = 1.0 / sample_rate_hz
        interval_us = int(self.sample_interval_s * 1e6)
        self.interval = ctypes.c_int32(interval_us)
        self.units    = ps.PS5000A_TIME_UNITS["PS5000A_US"]
        self.CHUNK    = chunk_size
        # Set data buffer
        self.bufferA = (ctypes.c_int16 * self.CHUNK)()
        self.status["setBuf"] = ps.ps5000aSetDataBuffers(
            self.chandle,
            channel,
            self.bufferA,
            None,
            self.CHUNK,
            0,
            ps.PS5000A_RATIO_MODE["PS5000A_RATIO_MODE_NONE"]
        )
        assert_pico_ok(self.status["setBuf"])
        # Run streaming
        self.status["run"] = ps.ps5000aRunStreaming(
            self.chandle,
            ctypes.byref(self.interval),
            self.units,
            0,                  # pre-trigger samples
            self.CHUNK,         # post-trigger samples
            0,                  # auto-stop off
            1,                  # downsample ratio
            ps.PS5000A_RATIO_MODE["PS5000A_RATIO_MODE_NONE"],
            self.CHUNK          # overview buffer interval
        )
        assert_pico_ok(self.status["run"])
        print(f"Streaming @ {sample_rate_hz//1000} kHz…")
        # Rolling time‐stamped buffer
        self.buffer = deque()  # of (t, value_mV)
        self.lock   = threading.Lock()
        self.max_age = max_age_s
        # Prepare callback
        def _cb(handle, noOfSamples, startIndex, overflow, triggerAt, triggered, autoStop, param):
            # convert raw → mV
            raw = self.bufferA[startIndex : startIndex + noOfSamples]
            mv  = adc2mV(raw, vrange, self.maxADC)
            now = time.perf_counter()
            with self.lock:
                # stamp each sample, spacing them by sample_interval_s
                for i, v in enumerate(mv):
                    t = now - (noOfSamples - 1 - i) * self.sample_interval_s
                    self.buffer.append((t, v))
                # prune old
                cutoff = now - self.max_age
                while self.buffer and self.buffer[0][0] < cutoff:
                    self.buffer.popleft()
        self.cb_type = ps.StreamingReadyType(_cb)
        # Thread control
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._poller, daemon=True)

    def _poller(self):
        while not self.stop_event.is_set():
            ps.ps5000aGetStreamingLatestValues(self.chandle, self.cb_type, None)
            time.sleep(self.sample_interval_s)  # poll at sample_interval

    def start(self):
        """Begin background streaming / buffering."""
        self.thread.start()

    def get_value_at(self, t_query):
        """Return the mV value closest to t_query (perf_counter reference)."""
        with self.lock:
            if not self.buffer:
                raise RuntimeError("No data available yet")
            times = [t for t, _ in self.buffer]
        i = bisect.bisect_left(times, t_query)
        # clamp & pick nearest
        if i <= 0:
            return self.buffer[0][1]
        if i >= len(self.buffer):
            return self.buffer[-1][1]
        t0, v0 = self.buffer[i-1]
        t1, v1 = self.buffer[i]
        return v0 if abs(t_query - t0) <= abs(t1 - t_query) else v1

    def stop(self):
        """Halt streaming and clean up."""
        self.stop_event.set()
        self.thread.join(timeout=1.0)
        ps.ps5000aStop(self.chandle)
        ps.ps5000aCloseUnit(self.chandle)
        print("Streaming stopped.")

# ─── Example usage ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    streamer = PicoStreamer()
    streamer.start()
    try:
        # 1) Give the background thread a moment to fill the first chunk
        time.sleep(0.05)  

        while True:
            t_query = time.perf_counter() - 0.020
            try:
                val = streamer.get_value_at(t_query)
            except RuntimeError:
                # buffer was still empty—just try again on the next loop
                continue

            print(f"{val:.2f} mV @ {t_query:.6f}")
            time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        streamer.stop()
