import ctypes
import time
import numpy as np

from picosdk.ps5000a import ps5000a as ps
from picosdk.functions import assert_pico_ok

# --- Open the scope ---
chandle = ctypes.c_int16()
# 12-bit resolution
resolution = ps.PS5000A_DEVICE_RESOLUTION['PS5000A_DR_12BIT']
status = ps.ps5000aOpenUnit(ctypes.byref(chandle), None, resolution)
assert_pico_ok(status)

# --- Enable Channel A ---
enabled = 1
coupling = ps.PS5000A_COUPLING['PS5000A_DC']
voltage_range = ps.PS5000A_RANGE['PS5000A_2V']
status = ps.ps5000aSetChannel(
    chandle,
    ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A'],
    enabled,
    coupling,
    voltage_range,
    0.0  # analogue offset
)
assert_pico_ok(status)

# --- Prepare streaming parameters ---
sample_interval = ctypes.c_int32(100)               # 100 µs → 10 kHz
time_units     = ps.PS5000A_TIME_UNITS['PS5000A_US']
pre_trigger    = 0
auto_stop      = 0                                  # we’ll stop in software
downsample_ratio = 1
ratio_mode       = ps.PS5000A_RATIO_MODE['PS5000A_RATIO_MODE_NONE']
overview_size    = 1024                             # samples per callback

# --- Set up a buffer for Channel A ---
buffer_a = np.zeros(overview_size, dtype=np.int16)
status = ps.ps5000aSetDataBuffers(
    chandle,
    ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A'],
    buffer_a.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
    None,
    overview_size,
    0,
    ratio_mode
)
assert_pico_ok(status)

# --- Storage for all samples ---
adc_values = []

# --- Callback to copy each block into our list ---
def streaming_callback(handle, n_samples, start_index, overflow, trigger_at, triggered, auto_stop_flag, user_data):
    global adc_values
    if overflow:
        print("⚠️ Overflow!")

    block = buffer_a[start_index:start_index + n_samples]
    adc_values.extend(block.tolist())

# Convert to C-callable pointer
c_callback = ps.StreamingReadyType(streaming_callback)

# --- Start streaming ---
status = ps.ps5000aRunStreaming(
    chandle,
    ctypes.byref(sample_interval),
    time_units,
    pre_trigger,
    0x3FFFFFFF,      # effectively “infinite” total samples
    auto_stop,
    downsample_ratio,
    ratio_mode,
    overview_size
)
assert_pico_ok(status)

# --- Poll for 5 seconds ---
start_time = time.time()
while time.time() - start_time < 15.01:
    ps.ps5000aGetStreamingLatestValues(chandle, c_callback, None)
    time.sleep(0.005)

# --- Stop and clean up ---
ps.ps5000aStop(chandle)
ps.ps5000aCloseUnit(chandle)

print(f"Collected {len(adc_values)} samples")
