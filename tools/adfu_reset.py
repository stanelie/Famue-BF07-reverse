"""Reboot the BF07 out of ADFU with no button press.

The notes say ADFU has no working reset command -- but ADFU lets us EXECUTE
code (cd 20), and the CPU can always reset itself: writing 0x05FA0004 to
AIRCR (0xE000ED0C) raises SYSRESETREQ. So upload a six-instruction stub and
run it.

    ldr r0, [pc, #4]     ; 0xE000ED0C   (AIRCR)
    ldr r1, [pc, #8]     ; 0x05FA0004   (VECTKEY | SYSRESETREQ)
    str r1, [r0]
    b   .
"""
import struct, sys, time
import os as _os
sys.path.insert(0, _os.environ.get(
    "BF07_TOOLS", _os.path.dirname(_os.path.abspath(__file__))))
import usb.core, usb.util, serial
from lark_cd import Adfu, OP_EXEC1, OP_WRITE

STUB_ADDR = 0x0101C000
# SYSRESETREQ alone lands straight back in ADFU: the boot ROM re-reads the
# reboot type, which `dbg reboot adfu` left as GOTO_ADFU. So clear
# RTC_REMAIN3 to magic|NORMAL first, then let the WATCHDOG do the reset.
# NOTE: the stub RETURNS (bx lr) instead of spinning. An earlier version ended
# in `b .`, so when the reset did not take, the CPU sat in that loop and the
# device hung -- no shell, no ADFU, needing a power cycle. A stub that returns
# leaves ADFU intact and the experiment repeatable.
STUB = bytes([0x04,0x48, 0x05,0x49, 0x01,0x60,
              0x05,0x48, 0x5f,0x21, 0x01,0x60,
              0x70,0x47, 0x00,0x00, 0x00,0x00,0x00,0x00]) + \
       struct.pack("<III", 0x4000c03c, 0x42520000, 0x4000c020)

def in_adfu():
    return usb.core.find(idVendor=0x10D6, idProduct=0x10D6) is not None

def enter_adfu(port="/dev/cu.usbserial-AV7K776E"):
    import glob
    port = (glob.glob("/dev/cu.usbserial-*") or [port])[0]
    s = serial.Serial(port, 2000000, timeout=0.2)
    s.write(b"dbg reboot adfu\r\n"); s.flush(); time.sleep(1.2); s.close()
    for _ in range(15):
        time.sleep(1)
        if in_adfu(): return True
    return False

if __name__ == "__main__":
    if not in_adfu():
        print("not in ADFU; entering...")
        if not enter_adfu():
            print("RESULT: could not reach ADFU"); sys.exit(1)
    print("in ADFU -- uploading reset stub")
    blob = STUB + b"\x00" * (256 - len(STUB) % 256)
    a = Adfu(timeout=8000)
    print("  write:", a.write(OP_WRITE, STUB_ADDR, blob))
    print("  exec :", a.cmd(OP_EXEC1, STUB_ADDR))
    usb.util.dispose_resources(a.d); del a
    time.sleep(3)
    print("still in ADFU?" , in_adfu(), " (False means it rebooted)")
