#!/usr/bin/env python3
import time
import numpy as np
import matplotlib.pyplot as plt
import pyvisa

# ——— USER CONFIG ———
INTERVAL    = 0.05      # polling interval (s)
BUFFER_SIZE = 200      # points in your rolling window
# ——————————————

def find_pm16():
    rm = pyvisa.ResourceManager('@py')       # use pyvisa-py backend
    '''resources = rm.list_resources()          # list all INSTR resources :contentReference[oaicite:1]{index=1}
    for res in resources:
        try:
            inst = rm.open_resource(res)
            idn = inst.query('*IDN?')
            if 'PM16-122' in idn:
                print(f"Found PM16-122 on {res}")
                return inst
        except Exception:
            continue
    raise IOError("Could not find PM16-122")'''
    return rm.open_resource("USB0::4883::32891::250604411::0::INSTR")

def main():
    pm = find_pm16()
    # Ensure power unit is watts
    pm.write("SENS:POW:UNIT W")
    pm.write("SENS:POW:RANG AUTO")

    plt.ion()
    fig, ax = plt.subplots()
    ax.set_title("PM16-122 Live Power")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Power (mW)")

    times  = np.zeros(BUFFER_SIZE)
    powers = np.zeros(BUFFER_SIZE)
    line, = ax.plot(times, powers, 'o', markersize=4)
    txt  = ax.text(0.02, 0.95, "", transform=ax.transAxes,
                   fontsize=14, bbox=dict(facecolor='white', alpha=0.8))

    start = time.time()
    idx   = 0

    try:
        while True:
            # Query a single reading (in W) and convert to mW
            p_w  = float(pm.query("READ?"))
            p_mw = p_w * 1e3
            t    = time.time() - start

            # Circular buffer update
            times[idx % BUFFER_SIZE]  = t
            powers[idx % BUFFER_SIZE] = p_mw
            idx += 1

            # Update plot data & axes
            if idx < BUFFER_SIZE:
                line.set_data(times[:idx], powers[:idx])
                ax.set_xlim(0, times[idx-1] + 0.1)
            else:
                window = times[(idx - BUFFER_SIZE) % BUFFER_SIZE]
                line.set_data(times, powers)
                ax.set_xlim(window, window + times.max() - times.min())

            ax.relim()
            ax.autoscale_view(True, True, True)
            txt.set_text(f"{p_mw:6.2f} mW")

            fig.canvas.draw()
            fig.canvas.flush_events()
            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("\nExiting…")

if __name__ == "__main__":
    main()
