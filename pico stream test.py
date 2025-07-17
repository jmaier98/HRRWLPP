#!/usr/bin/env python3
import ctypes
import numpy as np
import time
import threading
from collections import deque

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

from picosdk.ps5000a import ps5000a as ps
from picosdk.functions import adc2mV, assert_pico_ok

# ─── DEVICE SETUP ──────────────────────────────────────────────────────────────
chandle = ctypes.c_int16()
status = {}

# 12-bit resolution
res = ps.PS5000A_DEVICE_RESOLUTION["PS5000A_DR_12BIT"]
status["open"] = ps.ps5000aOpenUnit(ctypes.byref(chandle), None, res)
assert_pico_ok(status["open"])

# fix under-power if needed
if status["open"] in (282, 286):
    status["chgPS"] = ps.ps5000aChangePowerSource(chandle, status["open"])
    assert_pico_ok(status["chgPS"])

# ─── CHANNEL A CONFIG ──────────────────────────────────────────────────────────
enabled  = 1
coupling = ps.PS5000A_COUPLING["PS5000A_DC"]
vrange   = ps.PS5000A_RANGE["PS5000A_200MV"]   # ±2 V
offset   = 0.0

status["chA"] = ps.ps5000aSetChannel(
    chandle,
    ps.PS5000A_CHANNEL["PS5000A_CHANNEL_A"],
    enabled,
    coupling,
    vrange,
    offset
)
assert_pico_ok(status["chA"])

# get ADC max for mV conversion
maxADC = ctypes.c_int16()
status["max"] = ps.ps5000aMaximumValue(chandle, ctypes.byref(maxADC))
assert_pico_ok(status["max"])

# ─── STREAMING PARAMETERS ─────────────────────────────────────────────────────
CHUNK      = 10     # samples per callback → callback every 10 ms
WINDOW     = 1000   # rolling window size (1 s at 1 kHz)

# 1 kHz → 1 ms = 1000 µs
interval_us = ctypes.c_int32(1000)
units       = ps.PS5000A_TIME_UNITS["PS5000A_US"]

# no pre-trigger, post-trigger = CHUNK, auto-stop off
status["setBuf"] = ps.ps5000aSetDataBuffers(
    chandle,
    ps.PS5000A_CHANNEL["PS5000A_CHANNEL_A"],
    (ctypes.c_int16 * CHUNK)(),  # driver’s raw buffer
    None,
    CHUNK,
    0,  # memory segment
    ps.PS5000A_RATIO_MODE["PS5000A_RATIO_MODE_NONE"]
)
assert_pico_ok(status["setBuf"])

status["run"] = ps.ps5000aRunStreaming(
    chandle,
    ctypes.byref(interval_us),
    units,
    0,        # pre-trigger
    CHUNK,    # post-trigger
    0,        # auto-stop off
    1,        # downsample ratio
    ps.PS5000A_RATIO_MODE["PS5000A_RATIO_MODE_NONE"],
    CHUNK
)
assert_pico_ok(status["run"])
print("Streaming @ 1 kHz...")

# ─── ROLLING BUFFER ────────────────────────────────────────────────────────────
data_q = deque([0.0]*WINDOW, maxlen=WINDOW)

# ─── STREAMING CALLBACK ───────────────────────────────────────────────────────
# bufferA must match what we passed to SetDataBuffers
bufferA = (ctypes.c_int16 * CHUNK)()

def streaming_callback(handle, noOfSamples, startIndex, overflow, triggerAt, triggered, autoStop, param):
    raw = bufferA[startIndex : startIndex + noOfSamples]
    mv  = adc2mV(raw, vrange, maxADC)
    for v in mv:
        data_q.append(v)

cptr = ps.StreamingReadyType(streaming_callback)

# ─── BACKGROUND POLLING THREAD ─────────────────────────────────────────────────
stop_event = threading.Event()

def poller():
    while not stop_event.is_set():
        # triggers callback if data ready
        ps.ps5000aGetStreamingLatestValues(chandle, cptr, None)
        # tiny sleep to avoid CPU spin
        time.sleep(0.001)

thread = threading.Thread(target=poller, daemon=True)
thread.start()

# ─── LIVE PLOT ─────────────────────────────────────────────────────────────────
fig, ax = plt.subplots()
x = np.linspace(-WINDOW/1000, 0, WINDOW)
line, = ax.plot(x, list(data_q))
ax.set_xlabel("Time (s)")
ax.set_ylabel("Channel A (mV)")
ax.set_ylim(-vrange*100*1.1, vrange*100*1.1)
ax.set_title("Last 1000 samples @ 1 kHz")

def update(_):
    line.set_ydata(list(data_q))
    return line,

# redraw ~60 times/sec for smooth UI
ani = FuncAnimation(fig, update, interval=16, blit=True, cache_frame_data=False)

try:
    plt.show()
finally:
    # stop polling & streaming, then close
    stop_event.set()
    thread.join(timeout=1)
    ps.ps5000aStop(chandle)
    ps.ps5000aCloseUnit(chandle)
    print("Stopped.")

