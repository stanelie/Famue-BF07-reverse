#!/usr/bin/env python3
"""Dump the boot ROM over ADFU, for finding the flash-key-load routine.

SAFETY: never reads below 0x1000. `rm` dereferences the address, and reading
the low vector-table region faults the CPU *and* takes down the USB stack, so
even the boot-ROM recovery cannot answer -- only a physical reset clears it.
This cost a wedge; the floor below is not optional.

The ROM code of interest (nor_read, spinor vtable, launch helpers) lives in
0x1000-0x8000, all safely above the floor.
"""
import os, sys, struct, time
import os as _os
sys.path.insert(0, _os.environ.get(
    "BF07_TOOLS", _os.path.dirname(_os.path.abspath(__file__))))
import usb.core, usb.util

FLOOR = 0x1000
def rm(d, addr, n):
    if addr < FLOOR:
        raise SystemExit(f"refusing to read 0x{addr:x}: below the 0x{FLOOR:x} floor")
    out = b""
    while len(out) < n:
        chunk = min(0x1000, n - len(out))
        for _ in range(15):
            try: d.read(0x81, 512, 60)
            except usb.core.USBError: break
        p = bytearray(16); p[0:2] = b"rm"
        struct.pack_into("<I", p, 4, chunk); struct.pack_into("<I", p, 8, addr + len(out))
        d.write(0x02, bytes(p), 4000)
        b = b""
        while len(b) < chunk:
            try: c = bytes(d.read(0x81, min(chunk - len(b), 512), 2500))
            except usb.core.USBError: break
            if not c: break
            b += c
        if len(b) < chunk:
            raise SystemExit(f"short read at 0x{addr+len(out):x} -- ADFU may be wedged")
        out += b
    return out

def main():
    d = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
    if d is None:
        raise SystemExit("not in ADFU (enter via serial `dbg reboot adfu`, then run the payload)")
    # The payload owns the endpoints; re-selecting the already-active
    # configuration resets them under it and every raw packet then EIOs on
    # Linux. Configure only if nothing has. See docs/flashing.md.
    try: d.get_active_configuration()
    except usb.core.USBError: d.set_configuration()
    lo = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x1000
    hi = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x8000
    rom = rm(d, lo, hi - lo)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                       "reference", "rom", f"brom_{lo:04x}_{hi:04x}.bin")
    open(out, "wb").write(rom)
    print(f"dumped 0x{lo:x}-0x{hi:x} ({len(rom)} bytes) -> {out}")

if __name__ == "__main__":
    main()
