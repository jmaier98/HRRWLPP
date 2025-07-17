import os, sys
PICO_DLL_DIR = r"C:\Program Files\Pico Technology\PicoScope 7 T&M Stable"
os.environ["PATH"] = PICO_DLL_DIR + os.pathsep + os.environ["PATH"]  # find ps5000a.dll
# ─── END boilerplate ──────────────────────────────

import ctypes
import numpy as np
import matplotlib.pyplot as plt
from picosdk.ps5000a import ps5000a as ps

# ------------------ open scope -------------------
handle = ctypes.c_int16()
# Open in the default (8-bit) resolution
status = ps.ps5000aOpenUnit(
    ctypes.byref(handle),      # ptr to the handle
    None,                      # serial number (None = first unit found)
    ps.PS5000A_DEVICE_RESOLUTION["PS5000A_DR_8BIT"]
)
assert status == 0, f"Device not found (status {status})."

# ----------------- channel setup -----------------
# Enable Channel A, DC coupling, ±2 V full-scale
status = ps.ps5000aSetChannel(
    handle,               # handle
    ps.PS5000A_CHANNEL["PS5000A_CHANNEL_A"],
    1,                    # enabled
    ps.PS5000A_COUPLING["PS5000A_DC"],
    ps.PS5000A_RANGE["PS5000A_2V"],
    0                     # analogue offset (V)
)
assert status == 0, f"SetChannel failed (status {status})."

# -------------- timebase & buffer ----------------
TIME_INTERVAL_NS = 1_000    # 1 µs → 1 MS/s
N_SAMPLES        = 10_000   # 10 k points → 10 ms span

timebase   = 8              # empirically: 1 µs @ 1 MS/s on 5444D
oversample = 0
time_int   = ctypes.c_float()
max_samples= ctypes.c_int32()

status = ps.ps5000aGetTimebase2(
    handle, timebase, N_SAMPLES, ctypes.byref(time_int), oversample,
    ctypes.byref(max_samples), 0
)
assert status == 0, f"Timebase not supported (status {status})."

# Allocate numpy buffer backed by ctypes array
bufftype = ctypes.c_int16 * N_SAMPLES
buffer   = bufftype()
stat = ps.ps5000aSetDataBuffer(
    handle, ps.PS5000A_CHANNEL["PS5000A_CHANNEL_A"],
    ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int16)),
    N_SAMPLES, 0, 0, ps.PS5000A_RATIO_MODE["PS5000A_RATIO_MODE_NONE"]
)
assert stat == 0, f"SetDataBuffer failed (status {stat})."

# ------------------- run block -------------------
segment = 0
stat = ps.ps5000aRunBlock(
    handle, 0,               # pre-trigger samples
    N_SAMPLES,               # post-trigger samples
    timebase, oversample, None, segment, None, None
)
assert stat == 0, f"RunBlock failed (status {stat})."

# Wait until capture is complete
ready = ctypes.c_int16(0)
while not ready.value:
    ps.ps5000aIsReady(handle, ctypes.byref(ready))

# Stop (not strictly necessary for single block)
ps.ps5000aStop(handle)

# ------------------ fetch data -------------------
sample_count = ctypes.c_int32(N_SAMPLES)
stat = ps.ps5000aGetValues(
    handle, 0, ctypes.byref(sample_count), 1,          # 1 = downsampling ratio
    ps.PS5000A_RATIO_MODE["PS5000A_RATIO_MODE_NONE"],
    0, None
)
assert stat == 0, f"GetValues failed (status {stat})."

# ---------------- convert & plot -----------------
# LSB (ADC count) size for 2 V range on 5444D = 2 V / 32768 ≈ 61 µV
ADC2V  = 2.0 / 32768
voltages = np.array(buffer[:N_SAMPLES]) * ADC2V
times = np.arange(N_SAMPLES) * time_int.value * 1e-9  # ns → s

plt.figure()
plt.plot(times * 1e3, voltages)         # time axis in ms
plt.xlabel("Time (ms)")
plt.ylabel("Voltage (V)")
plt.title("PicoScope 5444D Block Capture – Channel A")
plt.grid(True)
plt.tight_layout()
plt.show()

# ------------------ cleanup ----------------------
ps.ps5000aCloseUnit(handle)
