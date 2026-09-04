#!/usr/bin/env python3
"""Annotated disassembler: resolves bl targets against the recovered symbol
map and inlines string literals. Usage: dis.py <addr> [len]"""
import struct
import sys

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

import os
import serialport
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.environ.get("BF07_ROOT", os.path.dirname(_HERE))
_BACKUPS = os.environ.get("BF07_BACKUPS", os.path.join(os.path.dirname(_ROOT), "bf07-backups"))
PORT = serialport.resolve()

XIP = 0x10000000
IMG = os.path.join(_BACKUPS, "fw_code_full.bin")
SYMS = os.path.join(_ROOT, "docs", "symbols.txt")

d = open(IMG, "rb").read()
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)

syms = {}
try:
    for ln in open(SYMS):
        if ln.startswith("0x"):
            a, n = ln.split()
            syms.setdefault(int(a, 16), n.strip())
except FileNotFoundError:
    pass


def word(a):
    o = a - XIP
    return struct.unpack_from("<I", d, o)[0] if 0 <= o < len(d) - 3 else None


def cstr(a, n=72):
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


def main():
    start = int(sys.argv[1], 0)
    length = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x100
    for ins in md.disasm(d[start - XIP:start - XIP + length], start):
        note = ""
        if ins.mnemonic in ("bl", "blx", "b.w") and ins.op_str.startswith("#"):
            try:
                t = int(ins.op_str.lstrip("#"), 0)
                nm = syms.get(t) or syms.get(t & ~1)
                if nm:
                    note = f"   -> {nm}"
            except ValueError:
                pass
        if ins.mnemonic.startswith("ldr") and "[pc" in ins.op_str:
            try:
                imm = int(ins.op_str.rsplit("#", 1)[1].rstrip("]"), 0)
                slot = ((ins.address + 4) & ~3) + imm
                v = word(slot)
                if v is not None:
                    s = cstr(v)
                    nm = syms.get(v) or syms.get(v & ~1)
                    note = f"   ; =0x{v:08x}"
                    if s:
                        note += f" {s!r}"
                    elif nm:
                        note += f" <{nm}>"
            except (ValueError, IndexError):
                pass
        here = syms.get(ins.address)
        if here:
            print(f"\n{here}:")
        print(f"  0x{ins.address:08x}  {ins.mnemonic:<8} {ins.op_str}{note}")


if __name__ == "__main__":
    main()
