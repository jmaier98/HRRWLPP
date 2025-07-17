# list_ftdi_devices.py
from pyftdi.ftdi import Ftdi

def main():
    devices = list(Ftdi.list_devices())
    if not devices:
        print("No FTDI devices found.")
    else:
        print("Found FTDI devices:")
        for url, desc in devices:
            print(f"  {url} — {desc}")

if __name__ == '__main__':
    main()
