import usb.backend.libusb1 as libusb1
backend = libusb1.get_backend()
print("libusb1 backend:", backend)
