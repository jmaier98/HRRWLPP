import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import time
from collections import deque
import threading
import queue

try:
    from picosdk.ps5000a import ps5000a as ps
    from picosdk.functions import adc2mV, assert_pico_ok
    import ctypes
    
    # Handle different ways constants are defined across SDK versions
    try:
        PICO_OK = ps.PICO_OK
    except AttributeError:
        try:
            from picosdk.constants import PICO_OK
        except ImportError:
            PICO_OK = 0x00000000  # Standard PICO_OK value
    
    try:
        PS5000A_CHANNEL_A = ps.PS5000A_CHANNEL['PS5000A_CHANNEL_A']
        PS5000A_2V = ps.PS5000A_RANGE['PS5000A_2V']
        PS5000A_DC = ps.PS5000A_COUPLING['PS5000A_DC']
        PS5000A_RATIO_MODE_NONE = ps.PS5000A_RATIO_MODE['PS5000A_RATIO_MODE_NONE']
    except (AttributeError, KeyError):
        # Fallback to numeric values if constants not available
        PS5000A_CHANNEL_A = 0
        PS5000A_2V = 7
        PS5000A_DC = 1
        PS5000A_RATIO_MODE_NONE = 0
        
except ImportError:
    print("Warning: PicoScope SDK not installed. Using simulated data.")
    ps = None
    PICO_OK = 0

class PicoScopeStreamer:
    def __init__(self, sample_rate=10000, buffer_duration=0.1):
        self.sample_rate = sample_rate
        self.buffer_duration = buffer_duration
        self.buffer_size = int(sample_rate * buffer_duration)  # 1000 samples for 0.1s at 10kHz
        
        # Data buffer - use deque for efficient append/pop operations
        self.data_buffer = deque(maxlen=self.buffer_size)
        self.time_buffer = deque(maxlen=self.buffer_size)
        
        # Threading
        self.data_queue = queue.Queue()
        self.running = False
        self.acquisition_thread = None
        
        # PicoScope variables
        self.chandle = None
        self.channel = PS5000A_CHANNEL_A
        self.range_val = PS5000A_2V
        
        # Latency measurement
        self.latency_measurements = []
        
        # Initialize PicoScope
        self.init_picoscope()
    
    def init_picoscope(self):
        """Initialize PicoScope connection"""
        if ps is None:
            print("Using simulated data mode")
            return
        
        try:
            # Create chandle and status ready for use
            self.chandle = ctypes.c_int16()
            
            # Try different OpenUnit function signatures
            try:
                # First try with resolution parameter (newer SDK)
                status = ps.ps5000aOpenUnit(ctypes.byref(self.chandle), None, 1)
            except TypeError:
                try:
                    # Try without resolution parameter (older SDK)
                    status = ps.ps5000aOpenUnit(ctypes.byref(self.chandle), None)
                except TypeError:
                    # Try with just chandle
                    status = ps.ps5000aOpenUnit(ctypes.byref(self.chandle))
            
            if status != PICO_OK:
                print(f"Failed to open PicoScope: {status}")
                self.chandle = None
                return
            
            # Set channel A
            status = ps.ps5000aSetChannel(
                self.chandle,
                self.channel,
                1,  # enabled
                PS5000A_DC,
                self.range_val,
                0   # analogue offset
            )
            
            # Use try/except for assert_pico_ok in case it's not available
            try:
                assert_pico_ok(status)
            except NameError:
                if status != PICO_OK:
                    raise Exception(f"ps5000aSetChannel failed with status {status}")
            
            print("PicoScope initialized successfully")
            
        except Exception as e:
            print(f"Error initializing PicoScope: {e}")
            print("Falling back to simulated data mode")
            self.chandle = None
    
    def get_sample_block(self):
        """Get a block of samples from PicoScope"""
        if self.chandle is None:
            # Simulate data with some noise and a slow sine wave
            num_samples = 100  # Get 100 samples per block (10ms at 10kHz)
            t = np.linspace(0, num_samples/self.sample_rate, num_samples)
            current_time = time.time()
            
            # Create realistic laser microscope signal: baseline + noise + occasional spikes
            baseline = 0.1
            noise = np.random.normal(0, 0.01, num_samples)
            slow_drift = 0.05 * np.sin(2 * np.pi * 0.1 * current_time)  # 0.1 Hz drift
            
            # Occasional "photon" spikes
            spikes = np.zeros(num_samples)
            if np.random.random() < 0.1:  # 10% chance of spike in this block
                spike_idx = np.random.randint(0, num_samples)
                spikes[spike_idx] = np.random.exponential(0.2)
            
            data = baseline + slow_drift + noise + spikes
            timestamps = current_time + t
            
            return data, timestamps
        
        try:
            # Use rapid block mode for faster acquisition
            preTriggerSamples = 0
            postTriggerSamples = 100  # Small block for low latency
            timebase = 199  # For ~10kHz sampling (depends on model)
            
            # Set up data buffer
            buffer = (ctypes.c_int16 * postTriggerSamples)()
            
            # Set data buffer before running
            status = ps.ps5000aSetDataBuffer(
                self.chandle,
                self.channel,
                ctypes.byref(buffer),
                postTriggerSamples,
                0,  # segmentIndex
                PS5000A_RATIO_MODE_NONE
            )
            
            if status != PICO_OK:
                return None, None
            
            # Run block
            timeIndisposedMs = ctypes.c_int32()
            status = ps.ps5000aRunBlock(
                self.chandle,
                preTriggerSamples,
                postTriggerSamples,
                timebase,
                ctypes.byref(timeIndisposedMs),
                0,  # segmentIndex
                None,  # lpReady callback
                None   # pParameter
            )
            
            if status != PICO_OK:
                return None, None
            
            # Wait for data to be ready
            ready = ctypes.c_int16(0)
            check = ctypes.c_int16(0)
            while ready.value == check.value:
                status = ps.ps5000aIsReady(self.chandle, ctypes.byref(ready))
            
            # Get values
            overflow = ctypes.c_int16()
            cmaxSamples = ctypes.c_int32(postTriggerSamples)
            status = ps.ps5000aGetValues(
                self.chandle,
                0,  # startIndex
                ctypes.byref(cmaxSamples),
                1,  # downSampleRatio
                PS5000A_RATIO_MODE_NONE,
                0,  # segmentIndex
                ctypes.byref(overflow)
            )
            
            if status != PICO_OK:
                return None, None
            
            # Convert to mV
            maxADC = ctypes.c_int16()
            status = ps.ps5000aMaximumValue(self.chandle, ctypes.byref(maxADC))
            
            if status == PICO_OK:
                data = adc2mV(buffer, self.range_val, maxADC)
                timestamps = np.linspace(0, len(data)/self.sample_rate, len(data)) + time.time()
                return data, timestamps
            else:
                return None, None
            
        except Exception as e:
            print(f"Error getting data: {e}")
            return None, None
    
    def acquisition_loop(self):
        """Main acquisition loop running in separate thread"""
        while self.running:
            start_time = time.time()
            
            data, timestamps = self.get_sample_block()
            
            if data is not None:
                # Measure latency
                acquisition_time = time.time() - start_time
                self.latency_measurements.append(acquisition_time)
                
                # Keep only last 100 latency measurements
                if len(self.latency_measurements) > 100:
                    self.latency_measurements.pop(0)
                
                # Put data in queue for main thread
                self.data_queue.put((data, timestamps))
            
            # Sleep to maintain approximately 10kHz effective rate
            time.sleep(0.001)  # 1ms sleep
    
    def start_streaming(self):
        """Start data acquisition in separate thread"""
        self.running = True
        self.acquisition_thread = threading.Thread(target=self.acquisition_loop)
        self.acquisition_thread.daemon = True
        self.acquisition_thread.start()
        print("Started streaming...")
    
    def stop_streaming(self):
        """Stop data acquisition"""
        self.running = False
        if self.acquisition_thread:
            self.acquisition_thread.join()
        print("Stopped streaming")
    
    def get_latest_data(self):
        """Get the latest data from buffer"""
        # Process any new data in queue
        while not self.data_queue.empty():
            try:
                data, timestamps = self.data_queue.get_nowait()
                for d, t in zip(data, timestamps):
                    self.data_buffer.append(d)
                    self.time_buffer.append(t)
            except queue.Empty:
                break
        
        if len(self.data_buffer) > 0:
            return np.array(self.time_buffer), np.array(self.data_buffer)
        else:
            return np.array([]), np.array([])
    
    def get_latency_stats(self):
        """Get latency statistics"""
        if len(self.latency_measurements) > 0:
            return {
                'mean': np.mean(self.latency_measurements) * 1000,  # ms
                'std': np.std(self.latency_measurements) * 1000,   # ms
                'max': np.max(self.latency_measurements) * 1000,   # ms
                'min': np.min(self.latency_measurements) * 1000    # ms
            }
        return None
    
    def close(self):
        """Clean up resources"""
        self.stop_streaming()
        if self.chandle and ps:
            try:
                ps.ps5000aCloseUnit(self.chandle)
                print("PicoScope closed")
            except:
                pass

class LivePlotter:
    def __init__(self, streamer):
        self.streamer = streamer
        self.fig, (self.ax1, self.ax2) = plt.subplots(2, 1, figsize=(12, 8))
        self.line1, = self.ax1.plot([], [], 'b-', linewidth=0.8)
        self.line2, = self.ax2.plot([], [], 'r-', linewidth=0.8)
        
        # Setup plots
        self.ax1.set_title('Live Data Stream (Recent 0.1s)')
        self.ax1.set_xlabel('Time (s)')
        self.ax1.set_ylabel('Amplitude (V)')
        self.ax1.grid(True, alpha=0.3)
        
        self.ax2.set_title('Acquisition Latency')
        self.ax2.set_xlabel('Sample Number')
        self.ax2.set_ylabel('Latency (ms)')
        self.ax2.grid(True, alpha=0.3)
        
        # Text for displaying stats
        self.stats_text = self.ax1.text(0.02, 0.98, '', transform=self.ax1.transAxes, 
                                       verticalalignment='top', fontsize=10,
                                       bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
        
        plt.tight_layout()
    
    def update_plot(self, frame):
        """Update function for animation"""
        # Get latest data
        times, data = self.streamer.get_latest_data()
        
        if len(times) > 0:
            # Update main data plot
            # Convert absolute times to relative times for display
            rel_times = times - times[-1]  # Make most recent sample t=0
            self.line1.set_data(rel_times, data)
            
            # Auto-scale axes
            self.ax1.set_xlim(rel_times[0], rel_times[-1])
            self.ax1.set_ylim(20, 100)
            
            # Update stats
            latency_stats = self.streamer.get_latency_stats()
            if latency_stats:
                stats_str = f"Samples: {len(data)}\n"
                stats_str += f"Rate: {len(data)/0.1:.0f} Hz\n"
                stats_str += f"Latency: {latency_stats['mean']:.1f}±{latency_stats['std']:.1f} ms\n"
                stats_str += f"Range: {latency_stats['min']:.1f}-{latency_stats['max']:.1f} ms"
                self.stats_text.set_text(stats_str)
                
                # Update latency plot
                latencies = np.array(self.streamer.latency_measurements) * 1000
                self.line2.set_data(range(len(latencies)), latencies)
                if len(latencies) > 0:
                    self.ax2.set_xlim(0, len(latencies))
                    self.ax2.set_ylim(0, max(latencies) * 1.1)
        
        return self.line1, self.line2, self.stats_text
    
    def start_animation(self):
        """Start the live animation"""
        self.animation = FuncAnimation(self.fig, self.update_plot, interval=50, 
                                     blit=False, cache_frame_data=False)
        plt.show()

def main():
    """Main function to run the streaming example"""
    print("PicoScope 5444D Live Streaming Example")
    print("======================================")
    
    # Create streamer
    streamer = PicoScopeStreamer(sample_rate=10000, buffer_duration=0.1)
    
    # Start streaming
    streamer.start_streaming()
    
    # Create and start live plotter
    plotter = LivePlotter(streamer)
    
    try:
        print("Starting live plot... Close the plot window to stop.")
        plotter.start_animation()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        streamer.close()
        print("Done!")

if __name__ == "__main__":
    main()
