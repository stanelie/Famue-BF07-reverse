"""Catch a boot-looping BF07 in ADFU.

A crash loop still runs the shell briefly on each cycle, so hammering
`dbg reboot adfu` down the UART lands eventually; the USB route is tried too.
"""
import glob, sys, time
import os as _os
sys.path.insert(0, _os.environ.get(
    "BF07_TOOLS", _os.path.dirname(_os.path.abspath(__file__))))
import usb.core, serial
import serialport

def in_adfu():
    return usb.core.find(idVendor=0x10D6, idProduct=0x10D6) is not None

print("hammering for ADFU (power the device on / let it loop)...")
t0 = time.time()
port = serialport.find()
while time.time() - t0 < 120:
    if in_adfu():
        print(f"ADFU reached after {time.time()-t0:.0f}s")
        sys.exit(0)
    if port:
        try:
            s = serial.Serial(port, 2000000, timeout=0.05)
            for _ in range(10):
                s.write(b"dbg reboot adfu\r\n"); s.flush(); time.sleep(0.05)
            s.close()
        except Exception:
            pass
    time.sleep(0.2)
print("RESULT: never reached ADFU")
sys.exit(1)
