#!/usr/bin/env python3
"""Dump the DECRYPTED fw0_sys over USB alone -- no serial cable.

The flash cipher IS live in ADFU with its key loaded. Earlier notes in this
project claimed the opposite; that was wrong, and every "keyless" result was a
stale cache line. Proven without cache ambiguity by writing plaintext that had
never executed, letting the SoC encrypt it, and reading it back byte-exact
through the XIP window.

TWO TRAPS, both paid for:

  1. `RMU_MRCR0` bit 8 must be cleared out of reset before `SPICACHE_CTL` will
     accept a write at all -- it silently ignores writes until then.
  2. The cache mapping address must be **4 KB aligned** (the SDK rejects
     anything else: `if (nor_phy_addr % 0x1000) return -EINVAL`). A 2 KB chunk
     size produced a dump where every odd chunk repeated the previous one --
     49% correct, which looks like data corruption but is a rejected mapping.

The window is re-pointed and invalidated per chunk: reading many addresses after
a single invalidate returns one stale line over and over.

Erased flash (0xFF) read through the decrypting path comes back as noise, not
0xFF. That is inherent, not a dump error.

Usage:  usb_plaindump.py [out.bin]        (device already in ADFU + payload)
"""
import os, sys, struct, time
sys.path.insert(0, os.environ.get(
    "BF07_TOOLS", os.path.dirname(os.path.abspath(__file__))))
import usb.core

FW0, SIZE, CHUNK = 0x14000, 0x1e0000, 0x1000      # 4 KB: mapping requires it

CMU_DEVCLKEN0, RMU_MRCR0 = 0x40001004, 0x40000000
SPICACHE_CTL, SPICACHE_INVALIDATE = 0x40014000, 0x40014004
MAP_ADDR, MAP_ENTRY = 0x40010300, 0x40010304
SPI0_CTL, SPI0_04 = 0x40028000, 0x40028004
SPI0_CTL_DECRYPT = 0x203b1c38                     # 0x38 alone reads CIPHERTEXT


class Dev:
    def __init__(self):
        self.d = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
        if self.d is None:
            raise SystemExit("not in ADFU (enter via serial or the USB switch, "
                             "then start the payload)")
        try:
            self.d.set_configuration()
        except usb.core.USBError:
            pass

    def _drain(self):
        for _ in range(15):
            try:
                self.d.read(0x81, 512, 60)
            except usb.core.USBError:
                return

    def cmd(self, op, ln, addr, expect=None, data=None):
        self._drain()
        p = bytearray(16)
        p[0:2] = op
        struct.pack_into("<I", p, 4, ln)
        struct.pack_into("<I", p, 8, addr)
        self.d.write(0x02, bytes(p), 5000)
        if data is not None:
            self.d.write(0x02, data, 8000)
        want = expect or ln
        b = b""
        while len(b) < want:
            try:
                c = bytes(self.d.read(0x81, min(want - len(b), 512), 3000))
            except usb.core.USBError:
                break
            if not c:
                break
            b += c
            if want <= 16 and len(b) >= 4:
                break
        return b

    def w32(self, a, v):
        return self.cmd(b"wm", 4, a, expect=4, data=struct.pack("<I", v))

    def r32(self, a):
        r = self.cmd(b"rm", 4, a)
        return struct.unpack("<I", r)[0] if len(r) == 4 else None

    def invalidate(self):
        self.w32(SPICACHE_INVALIDATE, 1)
        for _ in range(8):
            v = self.r32(SPICACHE_INVALIDATE)
            if v is None or not (v & 1):
                return


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "fw_plain_usb.bin"
    d = Dev()
    if d.cmd(b"is", 16, 0, expect=4)[:1] != b"\xaa":
        raise SystemExit("payload not responding -- re-upload it")

    for bit in (3, 4, 8):                # OTFD, SPI0, SPI0CACHE
        c, m = d.r32(CMU_DEVCLKEN0), d.r32(RMU_MRCR0)
        if c is None or m is None:
            raise SystemExit("register read failed")
        d.w32(CMU_DEVCLKEN0, c | (1 << bit))
        d.w32(RMU_MRCR0, m | (1 << bit))     # without this the next write is a no-op
    d.w32(SPICACHE_CTL, 0x21)
    d.w32(SPI0_CTL, SPI0_CTL_DECRYPT)
    d.w32(SPI0_04, 0x14)
    d.w32(MAP_ADDR, 0x10000001)
    if d.r32(SPICACHE_CTL) != 0x21:
        raise SystemExit("SPICACHE_CTL did not take -- is RMU_MRCR0 bit 8 clear?")

    out = bytearray()
    t0 = time.time()
    for off in range(0, SIZE, CHUNK):
        d.w32(MAP_ENTRY, FW0 + off)          # 4 KB aligned by construction
        d.invalidate()
        part = d.cmd(b"rm", CHUNK, 0x10000000)
        if len(part) != CHUNK:
            raise SystemExit(f"short read at 0x{off:06x} ({len(part)} bytes)")
        out += part
        if (off // CHUNK) % 96 == 0:
            print(f"  {100 * off // SIZE:3d}%  {time.time() - t0:.0f}s", flush=True)
    open(out_path, "wb").write(bytes(out))
    print(f"wrote {out_path}: {len(out)} bytes in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
