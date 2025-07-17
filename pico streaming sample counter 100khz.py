import ctypes, time
import numpy as np

from picosdk.ps5000a import ps5000a as ps
from picosdk.functions import assert_pico_ok

# --- Open the scope ---
chandle = ctypes.c_int16()
assert_pico_ok(ps.ps5000aOpenUnit(
    ctypes.byref(chandle), None,
    ps.PS5000A_DEVICE_RESOLUTION['PS5000A_DR_12BIT']
))

# --- Enable Channel A at ±2 V DC ---
assert_pico_ok(ps.ps5000aSetChannel(
    chandle,
    ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A'],
    1,
    ps.PS5000A_COUPLING['PS5000A_DC'],
    ps.PS5000A_RANGE['PS5000A_2V'],
    0.0
))

# --- Streaming parameters for 100 kHz, auto-stop at 500k samples ---
sample_interval  = ctypes.c_int32(10)    # 10 µs → 100 kHz
time_units       = ps.PS5000A_TIME_UNITS['PS5000A_US']
pre_trigger      = 0
max_samples      = 500_000
auto_stop        = 1
downsample_ratio = 1
ratio_mode       = ps.PS5000A_RATIO_MODE['PS5000A_RATIO_MODE_NONE']
overview_size    = 8192

# --- Set up analog buffer ---
buffer_a = np.zeros(overview_size, dtype=np.int16)
assert_pico_ok(ps.ps5000aSetDataBuffers(
    chandle,
    ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A'],
    buffer_a.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
    None,
    overview_size,
    0,
    ratio_mode
))

# --- Set up digital buffer for port D0–D7 ---
buffer_d = np.zeros(overview_size, dtype=np.int16)
assert_pico_ok(ps.ps5000aSetDataBuffers(
    chandle,
    ps.PS5000A_CHANNEL['PS5000A_DIGITAL_PORT0'],  # first digital port (D0–D7) :contentReference[oaicite:0]{index=0}
    buffer_d.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
    None,
    overview_size,
    0,
    ratio_mode
))

# --- Storage for samples ---
adc_values     = []
digital_values = []
done = False

# --- Callback to grab each block of both analog and digital samples ---
def streaming_callback(handle, n_samples, start_index,
                       overflow, trigger_at, triggered,
                       auto_stop_flag, user_data):
    global done
    if overflow:
        print("⚠️ Overflow!")
    # copy analog
    block_a = buffer_a[start_index:start_index + n_samples]
    adc_values.extend(block_a.tolist())
    # copy digital (each value is a bitmask for D0–D7) :contentReference[oaicite:1]{index=1}
    block_d = buffer_d[start_index:start_index + n_samples]
    digital_values.extend(block_d.tolist())
    if auto_stop_flag:
        done = True

c_callback = ps.StreamingReadyType(streaming_callback)

# --- Start streaming ---
assert_pico_ok(ps.ps5000aRunStreaming(
    chandle,
    ctypes.byref(sample_interval),
    time_units,
    pre_trigger,
    0x3FFFFFFF,
    auto_stop,
    downsample_ratio,
    ratio_mode,
    overview_size
))

# --- Poll until auto-stop ---
start_time = time.time()
while time.time() - start_time < 3:
    ps.ps5000aGetStreamingLatestValues(chandle, c_callback, None)
    time.sleep(0.005)

# --- Stop and close ---
ps.ps5000aStop(chandle)
ps.ps5000aCloseUnit(chandle)

print(f"Analog samples: {len(adc_values)}")
print(f"Digital samples: {len(digital_values)}")
print(digital_values[0:100])
