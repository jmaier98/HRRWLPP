import ftd2xx
n = ftd2xx.createDeviceInfoList()
print(n, "FTDI devices found")
for i in range(n):
    info = ftd2xx.getDeviceInfoDetail(i)
    print(info)

from ftd2xx import ftd2xx as ftd
n = ftd.createDeviceInfoList()
for i in range(n):
    info = ftd.getDeviceInfoDetail(i)
    print(i, {k: (v.decode(errors="ignore") if isinstance(v, bytes) else v) for k,v in info.items()})