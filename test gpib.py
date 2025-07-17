import pyvisa

def list_gpib_devices():
    # Create a VISA resource manager
    rm = pyvisa.ResourceManager()

    # List all connected VISA resources
    resources = rm.list_resources()

    # Filter for GPIB devices
    gpib_devices = [res for res in resources if "GPIB" in res]

    if gpib_devices:
        print("GPIB devices found:")
        for device in gpib_devices:
            print(f"  - {device}")
    else:
        print("No GPIB devices found.")

if __name__ == "__main__":
    list_gpib_devices()
