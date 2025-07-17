from pyftdi.gpio import GpioSyncController
import threading, time
import numpy as np
import ctypes, time
from picosdk.ps5000a import ps5000a as ps
from picosdk.functions import assert_pico_ok
import matplotlib.pyplot as plt

PIN_MASK = 0x1F  # D0–D4

def moveXY(x,y):
    x = min(x, 1)
    x = max(x, -1)
    x = int(x * 32767) + 32768
    y = min(y, 1)
    y = max(y, -1)
    y = int(y * 32767) + 32768
    xbits = format(x, '016b')
    ybits = format(y, '016b')
    pin_sequence = []
    pin_sequence.append((0 << 0) | (0 << 1) | (1 << 2) | (1 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (1 << 2) | (0 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3) | (1 << 4))
    pin_sequence.append((1 << 0) | (1 << 1) | (0 << 2) | (1 << 3) | (1 << 4))
    pin_sequence.append((1 << 0) | (1 << 1) | (0 << 2) | (0 << 3) | (1 << 4))
    for i in range(16):
        xb = int(xbits[i])
        yb = int(ybits[i])
        pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (1 << 3) | (1 << 4))
        pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (0 << 3) | (1 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3) | (0 << 4))
    pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3) | (0 << 4))
    
    # Write the entire sequence
    return bytes(pin_sequence)

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

        
# Shared stop flag
stop_event = threading.Event()



# --- Thread 1: PicoScope streaming at 10 kHz ---
def scope_thread(chandle, callback):
    print("starting daq")
    # polling as fast as possible (no sleep)
    while not stop_event.is_set():
        ps.ps5000aGetStreamingLatestValues(chandle, callback, None)
        time.sleep(0.005)
    ps.ps5000aStop(chandle)
    
# --- Thread 2: Galvo sweep at 1 kHz pixel rate ---
def galvo_thread(gpio, xvals, yvals, pixel_interval_s):
    print("starting glavo")
    try:
        while not stop_event.is_set():
            for y in yvals:
                for x in xvals:
                    frame = moveXY(x, y)
                    gpio.exchange(frame)
                    # bring trigger low, if you need
                    gpio.exchange(b'\x00')
                    #time.sleep(pixel_interval_s)
            # stop after one full frame (or loop forever)
            break
    finally:
        gpio.exchange(b'\x00')





gpio = GpioSyncController()

# Open in synchronous bit-bang at 1 MHz, low latency
gpio.configure('ftdi:///1', direction=PIN_MASK,
                   frequency=1000000)
gpio._ftdi.set_latency_timer(1)

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
    ps.PS5000A_RANGE['PS5000A_200MV'],
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

# --- Initialize digital port D0–D7 with 1.5 V threshold ---
threshold_volts = 1.5
logic_level = int(threshold_volts / 5.0 * 32767)  # ≈9830
assert_pico_ok(ps.ps5000aSetDigitalPort(
    chandle,
    ps.PS5000A_CHANNEL['PS5000A_DIGITAL_PORT0'],  # D0–D7
    1,                                            # enable port
    logic_level                                  # threshold count
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
xvals = np.linspace(-.1,.1,100)
yvals = np.linspace(-.1,.1,100)
t1 = threading.Thread(target=scope_thread, args=(chandle, c_callback), daemon=True)
t2 = threading.Thread(target=galvo_thread, args=(gpio, xvals, yvals, 0.001), daemon=True)

t1.start()
t2.start()

# Let them run for however long you like...
time.sleep(21)

# Signal both to stop
stop_event.set()

# Wait for clean exit
t1.join()
t2.join()

gpio.exchange(b'\x00')      # drive low
gpio.close(freeze=True)

print(f"Analog samples: {len(adc_values)}")
print(f"Digital samples: {len(digital_values)}")

i = 0
data_vals = []
while i < len(digital_values):
    if digital_values[i] == 1:
        i+=100
        data_vals.append(adc_values[i])
    i += 1
    
print(f"Found triggers {len(data_vals)}")
data_vals.append(0)
data_vals.append(0)
data_vals.append(0)
data_vals.append(0)
data_vals.append(0)


data = np.array(data_vals[0:10000]).reshape((100, 100))

# plot
plt.figure(figsize=(5, 5))
plt.imshow(data,
           aspect='equal',       # ensure square pixels
           interpolation='nearest')  # no smoothing
plt.colorbar(label='Value')
plt.title('100×100 Color Map')
plt.xlabel('X pixel')
plt.ylabel('Y pixel')
plt.tight_layout()
plt.show()

