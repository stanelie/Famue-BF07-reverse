#!/usr/bin/env python3
"""Recover a symbol map from the firmware's own log calls.

Nearly every function in this build logs its own name:

    ldr r0, =<format>
    ldr r2, ='_reading_create_content'
    bl  0x100ee68a

So: find every call to the logger, walk back for the `ldr r2, [pc, ...]` that
feeds it, resolve the string, then find the enclosing function's prologue.
That yields address -> name for a large part of the image, which is what turns
"guess the signature from one call site" into "read the map".
"""
import argparse
import re
import struct
from collections import defaultdict

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

XIP = 0x10000000
LOGGER = 0x100EE68A


def load(path):
    return open(path, "rb").read()


def cstr(d, a, n=64):
    o = a - XIP
    if not (0 <= o < len(d)):
        return None
    e = d.find(b"\0", o, o + n)
    if e < 0:
        return None
    s = d[o:e]
    if len(s) < 3 or not all(32 <= c < 127 for c in s):
        return None
    return s.decode("ascii")


def bl_target(d, a):
    """Decode a Thumb BL at `a`, or None."""
    o = a - XIP
    if o + 4 > len(d):
        return None
    hw1, hw2 = struct.unpack_from("<HH", d, o)
    if (hw1 & 0xF800) != 0xF000 or (hw2 & 0xD000) != 0xD000:
        return None
    s = (hw1 >> 10) & 1
    j1, j2 = (hw2 >> 13) & 1, (hw2 >> 11) & 1
    i1, i2 = (~(j1 ^ s)) & 1, (~(j2 ^ s)) & 1
    off = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw1 & 0x3FF) << 12) \
        | ((hw2 & 0x7FF) << 1)
    if s:
        off -= 1 << 25
    return a + 4 + off


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--start", type=lambda s: int(s, 0), default=0x10000000)
    ap.add_argument("--end", type=lambda s: int(s, 0), default=0x101F0000)
    args = ap.parse_args()

    d = load(args.image)
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    names = defaultdict(set)

    for i in range(args.start - XIP, min(args.end - XIP, len(d) - 4), 2):
        a = XIP + i
        if bl_target(d, a) != LOGGER:
            continue
        # walk back for `ldr r2, [pc, #imm]` feeding the 3rd argument
        for b in range(a - 2, a - 0x40, -2):
            ins = next(md.disasm(d[b - XIP:b - XIP + 4], b), None)
            if not ins or not ins.mnemonic.startswith("ldr"):
                continue
            if not ins.op_str.startswith("r2, [pc"):
                continue
            try:
                imm = int(ins.op_str.rsplit("#", 1)[1].rstrip("]"), 0)
            except ValueError:
                break
            slot = ((b + 4) & ~3) + imm
            if not (0 <= slot - XIP < len(d) - 3):
                break
            name = cstr(d, struct.unpack_from("<I", d, slot - XIP)[0])
            if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                break
            # enclosing function: nearest preceding push
            fn = None
            for c in range(a, a - 0x400, -2):
                x = next(md.disasm(d[c - XIP:c - XIP + 4], c), None)
                if x and x.mnemonic.startswith("push"):
                    fn = c
                    break
            names[fn if fn else a].add(name)
            break

    print(f"# {len(names)} functions named by the firmware's own log calls")
    for addr in sorted(names):
        for n in sorted(names[addr]):
            print(f"0x{addr:08x}  {n}")


if __name__ == "__main__":
    main()
