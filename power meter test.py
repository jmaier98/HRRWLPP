#!/usr/bin/env python3
"""
Live-plotting script for the Thorlabs PM16-122 USB power meter.
Displays both a scrolling plot of power vs. time and the
current reading (mW) in large text.
"""

import time
import numpy as np
import matplotlib.pyplot as plt
from PM16 import PM16

# ——— USER CONFIG ———
# Replace this with your actual USBTMC resource
# e.g. on Linux: "/dev/usbtmc0"
#     on Windows: "USB0::0x1313::0x8073::PM16-122::INSTR"
RESOURCE = "/dev/usbtmc0"

# Polling interval (seconds)
INTERVAL = 0.1

# Number of points to keep on screen
BUFFER_SIZE = 200
# —————————————

def main():
    # Initialize power meter
    pm = PM16(RESOURCE)
    # If your sensor head needs a specific wavelength setting, uncomment:
    # pm.set_wavelength(1550)  # in nm

    # Prepare plot
    plt.ion()
    fig, ax = plt.subplots()
    ax.set_title("PM16-122 Live Power")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Power (mW)")

    # Data buffers
    times = np.zeros(BUFFER_SIZE)
    powers = np.zeros(BUFFER_SIZE)

    # Plot objects
    line, = ax.plot(times, powers, '-o', markersize=4)
    text = ax.text(0.02, 0.95, "", transform=ax.transAxes, fontsize=14,
                   bbox=dict(facecolor='white', alpha=0.8))

    start = time.time()
    idx = 0

    try:
        while True:
            # Read power (returns W)
            p_w = pm.power()
            p_mw = p_w * 1e3

            t = time.time() - start

            # Update circular buffer
            times[idx % BUFFER_SIZE] = t
            powers[idx % BUFFER_SIZE] = p_mw
            idx += 1

            # Update line data
            if idx < BUFFER_SIZE:
                line.set_data(times[:idx], powers[:idx])
                ax.set_xlim(0, times[idx-1] + 0.1)
            else:
                # roll display window
                window = times[(idx - BUFFER_SIZE) % BUFFER_SIZE]
                line.set_data(times, powers)
                ax.set_xlim(window, window + times.max() - times.min())

            # Autoscale Y
            ax.relim()
            ax.autoscale_view(True, True, True)

            # Update text
            text.set_text(f"{p_mw:6.2f} mW")

            # Redraw
            fig.canvas.draw()
            fig.canvas.flush_events()

            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.")

if __name__ == "__main__":
    main()
