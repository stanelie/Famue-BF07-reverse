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
import serialport

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

def enter_adfu(port=None):
    import glob
    port = serialport.find(port)
    s = serial.Serial(port, 2000000, timeout=0.2)
    s.write(b"dbg reboot adfu\r\n"); s.flush(); time.sleep(1.2); s.close()
    for _ in range(15):
        time.sleep(1)
        if in_adfu(): return True
    return False

def reset_via_payload():
    """Reset with the RUNNING payload's own write-memory op.

    The stub-upload path below only works on a FRESH ADFU entry: once the
    payload is running, the boot ROM's CBW protocol is gone and uploading
    anything wedges ADFU. But the payload exposes `wm`, and the reset is only
    two register writes -- no code upload needed at all.

        RTC_REMAIN3 (0x4000c03c) = 0x42520000   clear the "boot to ADFU" request
        WATCHDOG    (0x4000c020) = 0x5f         arm it; the reset follows

    Verified on hardware: device left ADFU and booted normally.
    """
    d = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
    if d is None:
        return False
    # The payload owns the endpoints; re-selecting the already-active
    # configuration resets them under it and every raw packet then EIOs on
    # Linux. Configure only if nothing has. See docs/flashing.md.
    try:
        d.get_active_configuration()
    except usb.core.USBError:
        d.set_configuration()
    for _ in range(15):
        try:
            d.read(0x81, 512, 60)
        except usb.core.USBError:
            break

    def wm(addr, val):
        p = bytearray(16)
        p[0:2] = b"wm"
        struct.pack_into("<I", p, 4, 4)
        struct.pack_into("<I", p, 8, addr)
        d.write(0x02, bytes(p), 3000)
        d.write(0x02, struct.pack("<I", val), 3000)
        try:
            return bytes(d.read(0x81, 4, 2000))[:1] == b"\xaa"
        except usb.core.USBError:
            return False

    if not wm(0x4000c03c, 0x42520000):
        return False
    wm(0x4000c020, 0x5f)              # no ack expected: it resets mid-command
    time.sleep(3)
    return not in_adfu()


if __name__ == "__main__":
    if not in_adfu():
        print("not in ADFU; entering...")
        if not enter_adfu():
            print("RESULT: could not reach ADFU"); sys.exit(1)
    print("in ADFU -- trying the payload's wm first")
    if reset_via_payload():
        print("RESULT: rebooted (via payload wm)")
        sys.exit(0)
    print("payload route unavailable -- uploading reset stub")
    blob = STUB + b"\x00" * (256 - len(STUB) % 256)
    a = Adfu(timeout=8000)
    print("  write:", a.write(OP_WRITE, STUB_ADDR, blob))
    print("  exec :", a.cmd(OP_EXEC1, STUB_ADDR))
    usb.util.dispose_resources(a.d); del a
    time.sleep(3)
    print("still in ADFU?" , in_adfu(), " (False means it rebooted)")
