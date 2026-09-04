#!/usr/bin/env python3
"""Recursive-descent disassembler for the extracted firmware.

A linear sweep is not usable on this image: ARM literal pools sit inside the
code and disassemble as plausible-looking nonsense, which is how
_reading_unload_resource's constants once appeared as `ldrh r5, [r6, #0x30]`.
That desync also silently empties the call graph, because everything after the
first pool is garbage.

This follows control flow instead: from each entry it walks instructions until
a terminator, queues branch targets, and records every `ldr rX, [pc, #imm]`
target as DATA so pool bytes are never decoded as instructions.

Outputs, for a chosen address range:
  - function inventory with sizes and how each was discovered
  - call graph (callers and callees), including tail calls
  - per-function references to RAM statics and to string literals

Usage:
    rdisasm.py <start> <end> [--json out.json]
    rdisasm.py 0x10047000 0x1004d000
"""
import bisect
import json
import os
import struct
import sys

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs

XIP = 0x10000000
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.environ.get("BF07_ROOT", os.path.dirname(_HERE))
_BACKUPS = os.environ.get("BF07_BACKUPS",
                          os.path.join(os.path.dirname(_ROOT), "bf07-backups"))
IMG = os.path.join(_BACKUPS, "fw_code_full.bin")
SYMS = os.path.join(_ROOT, "docs", "symbols.txt")

img = open(IMG, "rb").read()
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
md.detail = True

syms = {}
for line in open(SYMS):
    parts = line.split()
    if len(parts) >= 2 and parts[0].startswith("0x"):
        syms[int(parts[0], 16)] = parts[1]
sym_keys = sorted(syms)


def name_of(addr):
    """Nearest preceding symbol, as name+offset."""
    i = bisect.bisect_right(sym_keys, addr) - 1
    if i < 0:
        return f"0x{addr:08x}"
    base = sym_keys[i]
    return syms[base] if base == addr else f"{syms[base]}+0x{addr - base:x}"


def in_image(addr):
    return XIP <= addr < XIP + len(img)


def read32(addr):
    return struct.unpack_from("<I", img, addr - XIP)[0]


def cstring(addr, limit=96):
    """A printable C string at addr, or None."""
    if not in_image(addr):
        return None
    off = addr - XIP
    end = img.find(b"\x00", off, off + limit)
    if end <= off:
        return None
    raw = img[off:end]
    if not all(32 <= b < 127 or b in (9, 10, 13) for b in raw):
        return None
    return raw.decode("ascii", "replace")


TERMINATORS = ("b", "bx", "bxj")


def walk(entry, stop_at):
    """Disassemble one function by following its control flow.

    Returns (instructions, data_addrs, calls, tail_calls, end_addr).
    Literal-pool targets are collected so the caller can avoid decoding them.
    """
    seen = set()
    queue = [entry]
    ins_by_addr = {}
    data = set()
    calls = set()
    tails = set()

    while queue:
        pc = queue.pop()
        while True:
            if pc in seen or not in_image(pc) or pc >= stop_at:
                break
            chunk = img[pc - XIP: pc - XIP + 24]
            decoded = list(md.disasm(chunk, pc, count=1))
            if not decoded:
                break
            ins = decoded[0]
            seen.add(pc)
            ins_by_addr[pc] = ins

            # literal pool target -> data, never code
            if ins.mnemonic.startswith("ldr") and "[pc," in ins.op_str:
                try:
                    off = int(ins.op_str.split("#")[-1].rstrip("]"), 16)
                    data.add(((pc + 4) & ~3) + off)
                except ValueError:
                    pass

            op = ins.op_str
            if ins.mnemonic in ("bl", "blx") and op.startswith("#"):
                calls.add(int(op[1:], 16))
            elif ins.mnemonic in ("b", "b.w") and op.startswith("#"):
                target = int(op[1:], 16)
                if entry <= target < stop_at:
                    queue.append(target)          # local jump
                else:
                    tails.add(target)             # tail call out of the function
                break                             # unconditional: stop this run
            elif ins.mnemonic.startswith("b") and op.startswith("#"):
                # conditional branch: take both paths
                target = int(op[1:], 16)
                if entry <= target < stop_at:
                    queue.append(target)
            elif ins.mnemonic in ("cbz", "cbnz") and "#" in op:
                try:
                    queue.append(int(op.split("#")[-1], 16))
                except ValueError:
                    pass

            # returns end a run
            if ins.mnemonic in ("bx",) and "lr" in op:
                break
            if ins.mnemonic.startswith("pop") and "pc" in op:
                break
            if ins.mnemonic == "udf":
                break

            pc += ins.size

    end = max(ins_by_addr) + ins_by_addr[max(ins_by_addr)].size if ins_by_addr else entry
    return ins_by_addr, data, calls, tails, end


def main():
    lo = int(sys.argv[1], 16)
    hi = int(sys.argv[2], 16)
    out_json = None
    if "--json" in sys.argv:
        out_json = sys.argv[sys.argv.index("--json") + 1]

    entries = sorted(a for a in sym_keys if lo <= a < hi)
    print(f"# recursive-descent map of 0x{lo:08x}-0x{hi:08x}")
    print(f"# {len(entries)} named entry points\n")

    funcs = {}
    for i, entry in enumerate(entries):
        stop = entries[i + 1] if i + 1 < len(entries) else hi
        # a function may legitimately run past the next symbol (aliases exist),
        # so let it walk to the end of the range and record where it stopped
        ins, data, calls, tails, end = walk(entry, hi)
        funcs[entry] = dict(name=syms[entry], size=end - entry, ins=len(ins),
                            calls=sorted(calls), tails=sorted(tails),
                            data=sorted(data), next_sym=stop)

    # invert to callers
    callers = {a: set() for a in funcs}
    for a, f in funcs.items():
        for t in f["calls"] + f["tails"]:
            if t in callers:
                callers[t].add(a)

    for a in entries:
        f = funcs[a]
        print(f"## {f['name']}  0x{a:08x}  ({f['ins']} insns)")
        if callers[a]:
            print("   called by: " + ", ".join(sorted(syms[c] for c in callers[a])))
        outs = [name_of(t) for t in f["calls"]]
        if outs:
            print("   calls    : " + ", ".join(sorted(set(outs))))
        if f["tails"]:
            print("   tails to : " + ", ".join(sorted(set(name_of(t) for t in f["tails"]))))
        statics = [d for d in f["data"] if in_image(d)]
        vals = []
        for d in statics:
            v = read32(d)
            s = cstring(v)
            if s:
                vals.append(f'"{s}"')
            elif 0x18000000 <= v < 0x18200000:
                vals.append(f"ram:0x{v:08x}")
        if vals:
            uniq = sorted(set(vals))
            print("   refs     : " + ", ".join(uniq[:14]) +
                  (f" (+{len(uniq) - 14} more)" if len(uniq) > 14 else ""))
        print()

    if out_json:
        json.dump({hex(a): f for a, f in funcs.items()}, open(out_json, "w"), indent=1)
        print(f"# json written to {out_json}")


if __name__ == "__main__":
    main()
