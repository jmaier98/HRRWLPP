import os, ctypes, platform, pprint

dll_dir = r"C:\Program Files\Pico Technology\PicoScope 7 T&M Stable"  # folder only
dll_name = "ps5000a.dll"
full     = os.path.join(dll_dir, dll_name)

print("Python bit-ness :", platform.architecture()[0])
print("DLL folder OK? :", os.path.isdir(dll_dir))
print("Full DLL path  :", full)
print()

# 1) Try loading by *full path* to see the real error
try:
    ctypes.WinDLL(full)
    print("✅ Full-path load succeeded.")
except OSError as e:
    print("❌ Full-path load FAILED:")
    print("   ", e)

# 2) Add the dir to the search list, then try name-only
os.add_dll_directory(dll_dir)

try:
    print('hello')
    ctypes.WinDLL("ps5000a")
    print("✅ Name-only load succeeded (DLL visible by PATH).")
except OSError as e:
    print("❌ Name-only load FAILED:")
    print("   ", e)
