#!/usr/bin/env python3
from pyftdi.gpio import GpioSyncController
import tkinter as tk
from threading import Thread, Event, Lock
import numpy as np
import pico_get_val as pico
import random
import time

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

PIN_MASK = 0x0F  # D0–D3
streamer = pico.PicoStreamer()
streamer.start()
gpio = GpioSyncController()
    
# Open in synchronous bit-bang at 1 MHz, low latency
gpio.configure('ftdi:///1', direction=PIN_MASK,
                   frequency=1000000)
gpio._ftdi.set_latency_timer(1)


class ScanApp:
    def __init__(self, master):
        self.master = master
        master.title("Live 2D Scan")

        # Shared data buffer
        self.data = np.zeros((50, 50), dtype=float)
        self.lock = Lock()

        # Control events
        self.stop_event = Event()

        # Matplotlib Figure
        self.fig = Figure(figsize=(5, 5))
        self.ax = self.fig.add_subplot(111)
        self.im = self.ax.imshow(self.data, vmin=0, vmax=1, 
                                 origin='lower', interpolation='nearest')
        self.ax.set_title("Live Scan Data")
        self.fig.colorbar(self.im, ax=self.ax)

        # Embed in Tk
        self.canvas = FigureCanvasTkAgg(self.fig, master=master)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Start / Stop buttons
        btn_frame = tk.Frame(master)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.start_btn = tk.Button(btn_frame, text="Start Scan", command=self.start_scan)
        self.start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5, pady=5)

        self.stop_btn = tk.Button(btn_frame, text="Stop Scan", command=self.stop_scan, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=5, pady=5)

        # schedule first plot update
        self.master.after(100, self.update_plot)

        
    def moveXY(self,x,y):
        x = min(x, 1)
        x = max(x, -1)
        x = int(x * 32767) + 32768
        y = min(y, 1)
        y = max(y, -1)
        y = int(y * 32767) + 32768
        xbits = format(x, '016b')
        ybits = format(y, '016b')
        pin_sequence = []
        pin_sequence.append((0 << 0) | (0 << 1) | (1 << 2) | (1 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (1 << 2) | (0 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3))
        pin_sequence.append((1 << 0) | (1 << 1) | (0 << 2) | (1 << 3))
        pin_sequence.append((1 << 0) | (1 << 1) | (0 << 2) | (0 << 3))
        for i in range(16):
            xb = int(xbits[i])
            yb = int(ybits[i])
            pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (1 << 3))
            pin_sequence.append((yb << 0) | (xb << 1) | (0 << 2) | (0 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (1 << 3))
        pin_sequence.append((0 << 0) | (0 << 1) | (0 << 2) | (0 << 3))
        
        # Write the entire sequence
        return bytes(pin_sequence)

    def scan_loop(self):
        """Background scan thread: walks through 50×50 grid, fills data."""
        while not self.stop_event.is_set():
            xvals = np.linspace(-.1,.1,50)
            yvals = np.linspace(-.1,.1,50)
            for y in range(50):
                for x in range(50):
                    if self.stop_event.is_set():
                        return

                    frame=self.moveXY(xvals[x],yvals[y])
                    gpio.exchange(frame)
                    t_query = time.now() -.002 
                    val = streamer.get_value_at(t_query)

                    time.sleep(.001)
                    # dummy measurement
                    # --------------------------------------
                    with self.lock:
                        self.data[y, x] = val
            self.stop_event.set()
            # Optionally repeat endlessly

    def start_scan(self):
        self.stop_event.clear()
        self.thread = Thread(target=self.scan_loop, daemon=True)
        self.thread.start()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

    def stop_scan(self):
        self.stop_event.set()
        self.thread.join(timeout=1.0)
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    def update_plot(self):
        """Called in the GUI thread ~10 Hz to redraw the image."""
        with self.lock:
            # autoscale to the min/max of all non-zero pixels
            nonzero = self.data[self.data != 0]
            if nonzero.size > 0:
                self.im.set_clim(nonzero.min(), nonzero.max())
            self.im.set_data(self.data)
        self.canvas.draw_idle()
        self.master.after(100, self.update_plot)

if __name__ == "__main__":
    root = tk.Tk()
    app = ScanApp(root)
    root.mainloop()
