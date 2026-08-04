#!/usr/bin/env python3
"""Disassemble a window of the extracted BF07 firmware (ARM Thumb-2, XIP base 0x10000000).

Usage: dis.py <vaddr> [count_bytes] [--back N]
Resolves PC-relative literal loads to their target values and annotates
any that point into the string region so log calls are readable.
"""
import sys, struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN

SCRATCH = "/private/tmp/claude-504/-Users-user/5d7d024b-ca45-4829-b929-aa9b9dba425d/scratchpad"
BASE = 0x10000000
DATA = open(f"{SCRATCH}/fw_code_full.bin", "rb").read()

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
md.detail = True


def rd32(va):
    off = va - BASE
    if 0 <= off <= len(DATA) - 4:
        return struct.unpack_from("<I", DATA, off)[0]
    return None


def cstr(va, maxlen=90):
    off = va - BASE
    if not (0 <= off < len(DATA)):
        return None
    end = DATA.find(b"\0", off)
    if end < 0 or end - off > maxlen:
        return None
    s = DATA[off:end]
    try:
        t = s.decode("ascii")
    except UnicodeDecodeError:
        return None
    if len(t) >= 3 and all(32 <= ord(c) < 127 for c in t):
        return t
    return None


def annotate(insn):
    """Return a comment for PC-relative literal loads and immediate values."""
    txt = insn.op_str
    # ldr rX, [pc, #imm]  -> capstone renders as [pc, #0x..]
    if insn.mnemonic.startswith("ldr") and "[pc" in txt:
        try:
            imm = int(txt.split("#")[-1].rstrip("]"), 0)
        except ValueError:
            return ""
        lit_va = ((insn.address + 4) & ~3) + imm
        val = rd32(lit_va)
        if val is None:
            return ""
        s = cstr(val)
        if s is not None:
            return f'  ; =0x{val:08x} "{s}"'
        return f"  ; =0x{val:08x}"
    return ""


def disasm(start_va, nbytes):
    off = start_va - BASE
    code = DATA[off:off + nbytes]
    for insn in md.disasm(code, start_va):
        raw = " ".join(f"{b:02x}" for b in insn.bytes)
        print(f"{insn.address:08x}  {raw:<12s} {insn.mnemonic:<8s} {insn.op_str}{annotate(insn)}")


if __name__ == "__main__":
    va = int(sys.argv[1], 0)
    n = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x100
    disasm(va, n)
