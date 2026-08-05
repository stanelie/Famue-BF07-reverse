#!/usr/bin/env python3
"""LARK ADFU — the *real* boot-ROM protocol, recovered from a live capture of
the Actions Multimedia Product Tool talking to a mock USB device.

The mistake that blocked this project for a whole session
--------------------------------------------------------
Every command we synthesised from HardwareEx.dll put the opcode in `CDB[0]`.
The boot ROM does not work that way. `CDB[0]` is a fixed vendor escape byte
`0xCD`; the opcode lives in `CDB[1]`. So every probe we ever sent looked like
an unknown opcode and came back CSW status 2 — correctly.

CDB layout (5/5 commands in the capture agree, lengths match the CBW dlen
exactly and the addresses are the known ATJ load address 0x118000):

    CDB[0]     = 0xCD          vendor escape, constant
    CDB[1]     = opcode
    CDB[2..4]  = 0             reserved
    CDB[5..8]  = length, LE32  (mirrors CBW dlen)
    CDB[9..12] = address, LE32
    CDB[13..15]= 0

Opcodes seen:

    0x13  write memory      (host->device, data phase)
    0x20  execute at addr   (no data phase)   <-- the handover
    0x21  execute at addr, second variant
    0x23  read info block   (device->host)

Captured sequence (a *classic ATJ* firmware, not LARK — the tool was fed a
non-BF07 image on purpose):

    cd 13  5120 B -> 0x118000      probe stage 1 (ADFUS.BIN, 4980 B padded)
    cd 20         -> 0x118000      run it
    cd 13  1536 B -> 0x11e000      probe stage 2
    cd 21         -> 0x11e000      run it
    cd 23   156 B <- 0            read chip identity

The probe blobs contain the strings "ic_version:", "BDG_CTL:", "jtag_ctl:", so
`cd 23` is the tool's chip-identification read. Our mock answered 156 zero
bytes and the tool stopped there.

For LARK the addresses differ: the official adfus.bin is 47,608 bytes and loads
at 0x01010000 (confirmed five ways — see docs/adfu-protocol.md).

Safety
------
Nothing here writes flash. `cd 13` writes RAM; `cd 20`/`cd 21` branch; `cd 23`
reads. Recovery from any wedge is the physical reset button, so keep the device
in reach. Enter ADFU first with `dbg reboot adfu` on the UART shell.

Usage
-----
    python3 lark_cd.py info                    # cd 23 against the boot ROM
    python3 lark_cd.py handover adfus.bin      # upload + cd 20 + probe
"""

import argparse
import struct
import sys
import time

import usb.core

VID = PID = 0x10D6
EP_OUT, EP_IN = 0x02, 0x81
USBC, USBS = 0x43425355, 0x53425355

ESC = 0xCD
OP_WRITE = 0x13
OP_EXEC1 = 0x20
OP_EXEC2 = 0x21
OP_INFO  = 0x23

LARK_LOAD = 0x01010000


class Adfu:
    def __init__(self, timeout=5000):
        self.t = timeout
        self.tag = 0
        d = usb.core.find(idVendor=VID, idProduct=PID)
        if d is None:
            raise SystemExit("not in ADFU mode (no 10d6:10d6) — "
                             "run `dbg reboot adfu` on the UART shell")
        try:
            d.set_configuration()
        except Exception:
            pass
        self.d = d

    # -- framing ---------------------------------------------------------

    def _cdb(self, op, length, addr):
        c = bytearray(16)
        c[0] = ESC
        c[1] = op
        struct.pack_into("<I", c, 5, length)
        struct.pack_into("<I", c, 9, addr)
        return bytes(c)

    def _cbw(self, dlen, flags, cdb):
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        return struct.pack("<IIIBBB", USBC, self.tag, dlen, flags,
                           0, len(cdb)) + cdb

    def _csw(self):
        try:
            r = bytes(self.d.read(EP_IN, 13, self.t))
        except Exception as e:
            return f"no-csw ({e})"
        if len(r) >= 13 and struct.unpack("<I", r[:4])[0] == USBS:
            return r[12]
        return f"raw:{r.hex()}"

    # -- commands --------------------------------------------------------

    def cmd(self, op, addr=0):
        """No data phase (cd 20 / cd 21)."""
        self.d.write(EP_OUT, self._cbw(0, 0x00, self._cdb(op, 0, addr)), self.t)
        return self._csw()

    def read(self, op, addr, n):
        """Device -> host (cd 23)."""
        self.d.write(EP_OUT, self._cbw(n, 0x80, self._cdb(op, n, addr)), self.t)
        try:
            data = bytes(self.d.read(EP_IN, n, self.t))
        except Exception as e:
            return None, f"no-data ({e})"
        if len(data) == 13 and struct.unpack("<I", data[:4])[0] == USBS:
            return None, f"csw-instead:{data[12]}"
        return data, self._csw()

    def write(self, op, addr, payload):
        """Host -> device (cd 13)."""
        n = len(payload)
        self.d.write(EP_OUT, self._cbw(n, 0x00, self._cdb(op, n, addr)), self.t)
        self.d.write(EP_OUT, payload, max(self.t, 10000))
        return self._csw()


# ---------------------------------------------------------------- actions

def do_info(a, args):
    print("cd 23 — chip identity read, straight at the boot ROM\n")
    for n in (156, 64, 512):
        data, st = a.read(OP_INFO, 0, n)
        if data:
            print(f"  len={n:<4} csw={st}")
            print(f"    {data[:64].hex(' ')}")
            printable = "".join(chr(c) if 32 <= c < 127 else "." for c in data)
            print(f"    {printable[:64]}")
            if any(data):
                print("\n  Non-zero — the ROM answers cd 23 directly.")
                return 0
        else:
            print(f"  len={n:<4} {st}")
    print("\n  All zero or no reply — cd 23 is probably served by the probe "
          "payload, not the ROM.")
    return 1


def do_handover(a, args):
    blob = open(args.payload, "rb").read()
    addr = args.addr
    print(f"payload {args.payload}: {len(blob)} bytes -> 0x{addr:08x}\n")

    print(f"1. cd 13 upload ({args.chunk} B chunks)")
    off = 0
    while off < len(blob):
        part = blob[off:off + args.chunk]
        st = a.write(OP_WRITE, addr + off, part)
        if st != 0:
            print(f"   offset 0x{off:x}: csw={st}  -- aborting")
            return 1
        off += len(part)
    print(f"   uploaded {off} bytes, all CSW 0")

    for op, name in ((OP_EXEC1, "cd 20"), (OP_EXEC2, "cd 21")):
        print(f"\n2. {name} @ 0x{addr:08x}")
        st = a.cmd(op, addr)
        print(f"   csw={st}")
        time.sleep(args.settle)

        print(f"3. probe cd 23 after {name}")
        data, st2 = a.read(OP_INFO, 0, 156)
        if data and any(data):
            print(f"   {data[:64].hex(' ')}")
            print(f"\n   *** payload is alive and answering after {name} ***")
            return 0
        print(f"   {st2 if data is None else 'all zero'}")

    print("\nneither exec variant produced a responsive payload")
    return 1


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="cd 23 against the boot ROM (read-only)")
    s.set_defaults(fn=do_info)

    s = sub.add_parser("handover", help="upload a payload and try cd 20 / cd 21")
    s.add_argument("payload")
    s.add_argument("--addr", type=lambda x: int(x, 0), default=LARK_LOAD)
    s.add_argument("--chunk", type=lambda x: int(x, 0), default=4096)
    s.add_argument("--settle", type=float, default=1.0)
    s.set_defaults(fn=do_handover)

    args = p.parse_args()
    return args.fn(Adfu(), args)


if __name__ == "__main__":
    sys.exit(main())
