import os, sys
PICO_DLL_DIR = r"C:\Program Files\Pico Technology\PicoScope 7 T&M Stable"

# prepend the folder to PATH *before* importing picosdk
os.environ["PATH"] = PICO_DLL_DIR + os.pathsep + os.environ["PATH"]

from picosdk.ps5000a import ps5000a as ps     # correct wrapper for 5444D
# ─── END boilerplate ────────────────────────────────────────────────────

# sanity-check (optional)
import ctypes
ctypes.WinDLL("ps5000a")          # will raise if the DLL still isn’t visible

res = ps.PS5000A_DEVICE_RESOLUTION['PS5000A_DR_8BIT']


CHANNEL = ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A']
COUPLING = ps.PS5000A_COUPLING['PS5000A_DC']
RANGE = ps.PS5000A_RANGE['PS5000A_2V']  # ±2V
SAMPLES = 1000
TIMEBASE = 8  # Adjust as needed
OVERSAMPLE = 1
SEGMENTS = 1

# Step 1: Open device
handle = ctypes.c_int16()
status = ps.ps5000aOpenUnit(ctypes.byref(handle), None, res)
assert status == 0, f"Device not found. Status: {status}"

# Step 2: Set channel
status = ps.ps5000aSetChannel(handle, CHANNEL, 1, COUPLING, RANGE, 0)
assert status == 0, "Failed to set channel"

# Step 3: Set simple trigger (optional, can remove to use no trigger)
status = ps.ps5000aSetSimpleTrigger(handle, 1, CHANNEL, mV2adc(500, RANGE, 32767), ps.PS5000A_THRESHOLD_DIRECTION['PS5000A_RISING'], 0, 1000, 0)
assert status == 0, "Failed to set trigger"

# Step 4: Set data buffer
buffer = (ctypes.c_int16 * SAMPLES)()
overflow = ctypes.c_int16()
status = ps.ps5000aSetDataBuffers(handle, CHANNEL, ctypes.byref(buffer), None, SAMPLES, 0, ps.PS5000A_RATIO_MODE['PS5000A_RATIO_MODE_NONE'])
assert status == 0, "Failed to set data buffer"

# Step 5: Run block
pre_trigger = 0
post_trigger = SAMPLES
time_indisposed = ctypes.c_int32()
status = ps.ps5000aRunBlock(handle, pre_trigger, post_trigger, TIMEBASE, OVER_SAMPLE, ctypes.byref(time_indisposed), 0, None, None)
assert status == 0, "Failed to start capture"

# Step 6: Poll for readiness
ready = ctypes.c_int16(0)
while not ready.value:
    ps.ps5000aIsReady(handle, ctypes.byref(ready))

# Step 7: Get values
status = ps.ps5000aGetValues(handle, 0, ctypes.byref(ctypes.c_uint32(SAMPLES)), 1, ps.PS5000A_RATIO_MODE['PS5000A_RATIO_MODE_NONE'], 0, ctypes.byref(overflow))
assert status == 0, "Failed to get values"

# Step 8: Convert ADC to mV
voltage = np.array([adc2mV(val, RANGE, 32767) / 1000 for val in buffer])  # in Volts

# Step 9: Stop and close
ps.ps5000aStop(handle)
ps.ps5000aCloseUnit(handle)

# Step 10: Plot
plt.plot(voltage)
plt.title("PicoScope 5444D: Channel A Voltage")
plt.xlabel("Sample Index")
plt.ylabel("Voltage (V)")
plt.grid(True)
plt.show()
