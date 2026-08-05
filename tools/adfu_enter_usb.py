#!/usr/bin/env python3
"""Put the BF07 into ADFU mode over **USB only** — no serial cable required.

Until now the only known way in was `dbg reboot adfu` on the UART shell. But the
device's own mass-storage stack implements the classic Actions ADFU-switch
handshake:

  * `ACTIONSUSBD` lives at `0x101978bc` in `fw0_sys`, immediately beside the MSC
    descriptor strings (`'Actions'`, `'MSC-Sample'`, `'0123456798AB'`).
  * `switch_to_adfu()` at `0x100e3d08` does `mov.w r0, #0x100` then reboots —
    and `0x100` is exactly the reboot type the boot log reports for
    `dbg reboot adfu` (`system reboot, type 0x100!`).

Sequence (as implemented by `actions_flash`'s `adfu_reboot`), sent as ordinary
Bulk-Only Mass Storage CBWs to the normal-mode device `10d6:b00b`:

    1. CDB[0]=0xCC, CDB[7]=11, data_len=11, flags=0x80 (IN)
       -> expect the 11 bytes "ACTIONSUSBD", then a CSW
    2. CDB[0]=0xCB, CDB[1]=0x21, CDB[7]=2, data_len=2, flags=0x80 (IN)
       -> expect 0xff 0x00, then a CSW; the device then reboots into ADFU

After this the device re-enumerates as `10d6:10d6` and the normal ADFU flow
applies (`lark_cd.py handover adfus_u_go.bin`, then `lark_adfu_u.py`).

Note this is the *normal-mode* switch. It is unrelated to the boot-ROM `0xCD`
framing or to the running payload's raw 16-byte protocol.

Usage:
    python3 adfu_enter_usb.py            # switch, then wait for 10d6:10d6
    python3 adfu_enter_usb.py --probe    # only do step 1, do not reboot
"""

import argparse
import struct
import sys
import time

import usb.core
import usb.util

VID = 0x10D6
PID_NORMAL = 0xB00B
PID_ADFU = 0x10D6
USBC, USBS = 0x43425355, 0x53425355


def find_eps(dev):
    """Locate the bulk IN/OUT endpoints of the mass-storage interface."""
    cfg = dev.get_active_configuration()
    for intf in cfg:
        ep_in = ep_out = None
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                ep_in = ep.bEndpointAddress
            else:
                ep_out = ep.bEndpointAddress
        if ep_in and ep_out:
            return intf.bInterfaceNumber, ep_out, ep_in
    raise SystemExit("no bulk endpoint pair found")


class Msc:
    def __init__(self, dev, ep_out, ep_in, timeout=4000):
        self.d, self.o, self.i, self.t = dev, ep_out, ep_in, timeout
        self.tag = 0

    def cmd(self, cdb, dlen, flags=0x80):
        self.tag += 1
        cbw = struct.pack("<IIIBBB", USBC, self.tag, dlen, flags, 0, 16) + bytes(cdb)
        self.d.write(self.o, cbw, self.t)
        data = b""
        if dlen:
            try:
                data = bytes(self.d.read(self.i, dlen, self.t))
            except Exception as e:
                return None, f"no-data ({type(e).__name__})"
        try:
            csw = bytes(self.d.read(self.i, 13, self.t))
            st = csw[12] if len(csw) >= 13 and struct.unpack("<I", csw[:4])[0] == USBS else None
        except Exception:
            st = None
        return data, st


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--probe", action="store_true",
                   help="only run the ACTIONSUSBD handshake, do not reboot")
    p.add_argument("--wait", type=float, default=15.0)
    args = p.parse_args()

    if usb.core.find(idVendor=VID, idProduct=PID_ADFU):
        print("device is already in ADFU mode (10d6:10d6)")
        return 0

    dev = usb.core.find(idVendor=VID, idProduct=PID_NORMAL)
    if dev is None:
        raise SystemExit("device not found as 10d6:b00b — is it in normal mode?")

    try:
        dev.set_configuration()
    except Exception:
        pass
    intf, ep_out, ep_in = find_eps(dev)
    print(f"mass storage: interface {intf}, "
          f"EP OUT 0x{ep_out:02x} / EP IN 0x{ep_in:02x}")

    try:
        if dev.is_kernel_driver_active(intf):
            dev.detach_kernel_driver(intf)
            print("  detached kernel driver")
    except Exception:
        pass

    m = Msc(dev, ep_out, ep_in)

    cdb = bytearray(16); cdb[0] = 0xCC; cdb[7] = 11
    data, st = m.cmd(cdb, 11)
    print(f"1. 0xCC handshake -> {data!r} csw={st}")
    if data != b"ACTIONSUSBD":
        print("   unexpected response; the device may not support the USB switch")
        return 1
    print("   ACTIONSUSBD confirmed")

    if args.probe:
        print("\n--probe given: stopping before the reboot step")
        return 0

    cdb = bytearray(16); cdb[0] = 0xCB; cdb[1] = 0x21; cdb[7] = 2
    data, st = m.cmd(cdb, 2)
    print(f"2. 0xCB/0x21 reboot -> {data.hex(' ') if data else data} csw={st}")

    print("\nwaiting for re-enumeration as 10d6:10d6 ...")
    t0 = time.time()
    while time.time() - t0 < args.wait:
        time.sleep(0.5)
        if usb.core.find(idVendor=VID, idProduct=PID_ADFU):
            print(f"  ADFU mode reached after {time.time()-t0:.1f}s")
            return 0
    print("  timed out; device did not enter ADFU")
    return 1


if __name__ == "__main__":
    sys.exit(main())
