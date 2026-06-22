import serial
import threading
import time
import re

class SP2500:
    """
    Python driver for the spectrometer over RS-232.
    Provides methods to get the nm reading, set nm/min, and set nm.
    """
    def __init__(self, port="COM5", baudrate=9600, timeout=1):
        """
        Initialize the serial connection to the spectrometer.
        """
        try:
            self.ser = serial.Serial(port, baudrate, timeout=timeout)
            time.sleep(2)  # Allow time for the connection to establish
        except Exception as e:
            print("\033[31m" + "SP2500 offline" + "\033[0m")
            self.ser = None
        self.lock = threading.Lock()

    def send_command(self, command, wait=0.5, read_bytes=100):
        """
        Send a command to the spectrometer and return the response.
        
        Args:
            command (str): The command string to send (without termination).
            wait (float): Time in seconds to wait after sending before reading.
            read_bytes (int): Number of bytes to attempt to read.
        
        Returns:
            str: The decoded response.
        """
        with self.lock:
            full_command = f"{command}\r"
            print(f"Sending: {full_command.strip()}")
            self.ser.write(full_command.encode())
            time.sleep(wait)
            response = self.ser.read(read_bytes)
            decoded = response.decode()
            print(f"Response: {decoded}")
            return decoded

    def get_nm(self):
        """
        Query the spectrometer for the current nm value.
        
        Returns:
            float: The nm value, if found.
        """
        response = self.send_command("?NM")
        matches = re.findall(r"\d+\.\d+", response)
        if matches:
            return float(matches[0])
        return None

    def set_nm_min(self, x):
        """
        Set the nm/MIN value on the spectrometer.
        
        Args:
            x (int or float): The value to set.
        
        Returns:
            The value that was set.
        """
        cmd = f"{x} NM/MIN"
        self.send_command(cmd)
        return x

    def set_nm(self, x):
        """
        Set the nm value on the spectrometer and wait for an 'ok' response.
        
        Args:
            x (float): The nm value to set.
        
        Returns:
            The value that was set.
        """
        cmd = f"{x:.2f} NM"
        with self.lock:
            full_command = f"{cmd}\r"
            print(f"Sending: {full_command.strip()}")
            self.ser.write(full_command.encode())
            time.sleep(0.5)
            attempts = 0
            timeout_attempts = 100
            while attempts < timeout_attempts:
                response = self.ser.read(100).decode()
                print(f"Response: {response}")
                if "ok" in response.lower():
                    print("Command executed successfully.")
                    break
                else:
                    print("Waiting for acknowledgment...")
                    time.sleep(0.5)
                    attempts += 1
        return x
    
    def print_installed_gratings(self):
        """
        Query and print the list of all gratings currently installed
        (groove densities, blaze wavelengths, and which one is active).
        """
        response = self.send_command("?GRATINGS", wait=0.5, read_bytes=500)
        print("Installed gratings:")
        for line in response.splitlines():
            # lines typically look like:
            # " 1: 1200 g/mm, blaze 500nm"
            # or "-> 2:  600 g/mm, blaze 750nm"
            print(f"  {line.strip()}")

    def print_selected_grating(self):
        """
        Query and print the number of the currently selected grating.
        """
        response = self.send_command("?GRATING", wait=0.2, read_bytes=50)
        # Expecting something like "3" or " 3"
        match = re.search(r"\d+", response)
        if match:
            current = int(match.group())
            print(f"Current selected grating: {current}")
        else:
            print("Could not parse current grating from response.")

    def switch_grating(self, grating_number, turret=None):
        """
        Switch to the specified grating (1–9). Optionally switch to a given turret (1–3) first.
        """
        if turret is not None:
            # select the turret
            self.send_command(f"{turret} TURRET")
        # now place the desired grating into the beam
        self.send_command(f"{grating_number} GRATING")
        # verify
        time.sleep(5)
        response = self.send_command("?GRATING", wait=0.2, read_bytes=50)
        match = re.search(r"\d+", response)
        if match and int(match.group()) == grating_number:
            print(f"✅ Switched to grating {grating_number}")
        else:
            print(f"⚠️  Failed to switch—response was: {response.strip()}")
            
    def close(self):
        """
        Close the serial connection.
        """
        if self.ser is not None:
            self.ser.close()


# Example usage:
if __name__ == "__main__":
    spec = SP2500(port="COM5", baudrate=9600, timeout=1)
    
    nm_value = spec.get_nm()
    print(f"Current nm: {nm_value}")
    #spec.switch_grating(3)
    #time.sleep(5)
    #spec.print_selected_grating()
    spec.print_installed_gratings()
    #spec.set_nm_min(5000)
    spec.set_nm(800)
    
    spec.close()