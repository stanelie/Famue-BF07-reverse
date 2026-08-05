#!/usr/bin/env python3
"""LARK ADFU over USB — the protocol the running `adfus_u` payload actually speaks.

This supersedes every earlier assumption in this repo. There is **no CBW/CSW
wrapper**: the payload reads a raw **16-byte command packet** straight off the
bulk OUT endpoint.

    [0..1]   opcode: two ASCII chars, read as u16 LE
    [2..3]   reserved (0)
    [4..7]   length, u32 LE
    [8..11]  address, u32 LE   (BYTE address, not sector)
    [12..15] reserved (0)

Dispatcher at `0x01014a88` in `adfus_u.bin`:

    01014a90  movs r2, #0x10        ; read exactly 16 bytes
    01014a9c  ldrh.w r1, [sp]       ; opcode = first u16
    01014aa0  ldr r2, =0x01010e06   ; 16 command IDs   (u16 each)
    01014aac  ldr r2, =0x01010e28   ; 16 handlers      (u32 each)
    01014ab4  blx r3                ; handler(packet)

Command set (from those tables):

    gf  get flash info     rm  read memory      wm  write memory
    ic  ic version         rs  read sector      ws  write sector
    is  init storage       es  erase sector     cf  config
    si  storage info       rr  rx  sf  af  cr   misc

Handlers transfer in chunks of at most 0x4000 and stream the result on EP 0x81.

## Getting here

1. `dbg reboot adfu` on the UART shell
2. Upload a **patched `adfus_u.bin`** to 0x01010000 with `cd 13`, start with
   `cd 20` (see `lark_cd.py` for that boot-ROM stage)

Two patches are required, both RAM-only:

  * `0x274c`: `01 20` -> `00 20`  — first storage attempt uses type 0 (SPI NOR);
    the stock payload tries only types 1 and 2 (NAND).
  * `0x2752`: `60 b9` -> `0c e0`  — `cbnz r0` becomes an unconditional branch,
    so a failed storage init no longer loops forever without ever reaching the
    command dispatcher.

## Verified on hardware

`rm` reads back the uploaded payload byte-exactly, and `rs` matches the
independently captured, byte-verified 4 MB serial dump. Bulk reads run at
~676 KB/s with 256 KB requests — a full 4 MB image in seconds rather than the
~40 minutes the UART path takes.

**A stale status packet may be queued on EP 0x81**; drain the endpoint before
each command or every reply arrives shifted by one.

Usage:
    python3 lark_adfu_u.py info
    python3 lark_adfu_u.py dump out.bin [--size 0x400000]
    python3 lark_adfu_u.py rm 0x01010000 256
"""

import argparse
import struct
import sys
import time

import usb.core

VID = PID = 0x10D6
EP_OUT, EP_IN = 0x02, 0x81
MAX_CHUNK = 0x40000          # host-side request size; payload caps at 0x4000


class AdfuU:
    def __init__(self, timeout=6000):
        self.t = timeout
        d = usb.core.find(idVendor=VID, idProduct=PID)
        if d is None:
            raise SystemExit("no 10d6:10d6 — is the payload running?")
        try:
            d.set_configuration()
        except Exception:
            pass
        self.d = d

    def drain(self, n=12):
        """Discard any queued status packets so replies line up."""
        for _ in range(n):
            try:
                self.d.read(EP_IN, 512, 200)
            except Exception:
                return

    def _pkt(self, op, length, addr):
        p = bytearray(16)
        p[0:2] = op
        struct.pack_into("<I", p, 4, length)
        struct.pack_into("<I", p, 8, addr)
        return bytes(p)

    def cmd(self, op, length=0, addr=0, expect=None):
        self.drain()
        self.d.write(EP_OUT, self._pkt(op, length, addr), self.t)
        want = length if expect is None else expect
        buf = b""
        while len(buf) < want:
            try:
                buf += bytes(self.d.read(EP_IN, min(want - len(buf), 16384),
                                         self.t))
            except Exception:
                break
        return buf

    def read_flash(self, addr, length):
        out = bytearray()
        while len(out) < length:
            n = min(MAX_CHUNK, length - len(out))
            part = self.cmd(b"rs", n, addr + len(out))
            if not part:
                raise RuntimeError(f"rs failed at 0x{addr + len(out):x}")
            out.extend(part)
        return bytes(out)

    def read_mem(self, addr, length):
        return self.cmd(b"rm", length, addr)


def do_info(a, args):
    for op, lbl, n in ((b"ic", "ic  chip version", 64),
                       (b"gf", "gf  flash info", 16),
                       (b"is", "is  init storage", 16),
                       (b"si", "si  storage info", 16)):
        r = a.cmd(op, n, 0, expect=n)
        print(f"  {lbl:<22} {len(r):>4}B  {r[:32].hex(' ')}")
    return 0


def do_dump(a, args):
    t0 = time.time()
    data = a.read_flash(args.start, args.size)
    el = time.time() - t0
    open(args.outfile, "wb").write(data)
    print(f"read {len(data)} bytes in {el:.1f}s "
          f"({len(data)/el/1024:.0f} KB/s) -> {args.outfile}")
    if args.compare:
        ref = open(args.compare, "rb").read()
        n = min(len(ref), len(data))
        bad = [i for i in range(0, n, 512) if data[i:i+512] != ref[i:i+512]]
        print(f"compared {n} bytes against {args.compare}: "
              f"{'IDENTICAL' if not bad else f'{len(bad)} differing blocks'}")
        for b in bad[:8]:
            print(f"    first differing block at 0x{b:x}")
    return 0


def do_rm(a, args):
    r = a.read_mem(args.addr, args.length)
    print(f"{len(r)} bytes @ 0x{args.addr:08x}")
    for i in range(0, min(len(r), 256), 16):
        print(f"  {args.addr+i:08x}  {r[i:i+16].hex(' ')}")
    if args.out:
        open(args.out, "wb").write(r)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info"); s.set_defaults(fn=do_info)

    s = sub.add_parser("dump")
    s.add_argument("outfile")
    s.add_argument("--start", type=lambda x: int(x, 0), default=0)
    s.add_argument("--size", type=lambda x: int(x, 0), default=0x400000)
    s.add_argument("--compare", default=None)
    s.set_defaults(fn=do_dump)

    s = sub.add_parser("rm")
    s.add_argument("addr", type=lambda x: int(x, 0))
    s.add_argument("length", type=lambda x: int(x, 0))
    s.add_argument("--out", default=None)
    s.set_defaults(fn=do_rm)

    args = p.parse_args()
    return args.fn(AdfuU(), args)


if __name__ == "__main__":
    sys.exit(main())
