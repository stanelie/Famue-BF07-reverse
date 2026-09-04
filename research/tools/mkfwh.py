#!/usr/bin/env python3
"""Rewrite reader/include/fw.h for a different firmware build.

    mkfwh.py -r <reference plain> -t <target plain> -i fw.h -o fw_target.h

The reader reaches ~20 vendor functions by absolute address, and the compiler
materialises each as a movw/movt immediate pair baked into reader.bin. Those
addresses move between builds, so supporting a second build means REBUILDING
the reader against relocated addresses -- retargeting the patch's hook sites is
not enough, and a reader whose calls are 0x24 bytes off lands mid-instruction
on every one of them.

Only the numeric literal inside a `#define` is rewritten. Addresses that appear
in COMMENTS are left exactly as they are: they document where something was
observed in the reference build, and silently rewriting them would destroy the
provenance that makes the header auditable.

Function pointers carry the Thumb bit (odd addresses); it is stripped before
resolving and restored afterwards. Data addresses (LVGL class pointers) are
even and are resolved by content with pointer-shaped words masked out, because
a class struct is full of pointers that themselves moved.
"""
import argparse
import re
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import retarget

XIP, LIMIT = 0x10000000, 0x101D3000


def resolve_data(ref, tgt, addr, n=32):
    """Locate a data structure by content, ignoring words that look like
    pointers -- those moved with everything else, so comparing them finds
    nothing even when the struct is plainly the same one."""
    def profile(b):
        ws = [struct.unpack_from("<I", b, i)[0] for i in range(0, len(b) - 3, 4)]
        return tuple("P" if XIP <= w < LIMIT else w for w in ws)
    o = addr - XIP
    want = profile(ref[o:o + n])
    if sum(1 for x in want if x != "P") < 3:
        return None, "too few non-pointer words to identify"
    hits = []
    for i in range(0, len(tgt) - n, 4):
        if profile(tgt[i:i + n]) == want:
            hits.append(i)
            if len(hits) > 4:
                break
    if len(hits) == 1:
        return hits[0] + XIP, "content match (pointers masked)"
    return None, (f"{len(hits)} matches -- ambiguous" if hits else "no content match")


MOVW_RE = re.compile(r'(movw\s+(r\d+|ip|lr),\s*#0x)([0-9a-fA-F]{4})')
MOVT_RE = re.compile(r'(movt\s+(r\d+|ip|lr),\s*#0x)([0-9a-fA-F]{4})')


def relocate_source(ref, tgt, text):
    """Rewrite movw/movt immediate PAIRS that encode a vendor address.

    These are the trampolines that jump back into the vendor function after a
    hook runs, and they are the reason a first May-27 build boot-looped: seven
    of ten still pointed at Jun-30 addresses, so every hook returned into
    unrelated code. They live inside inline-asm string literals, split across
    two halves, which is why rewriting the HEADER missed them entirely -- the
    dependency audit enumerated what the reader DECLARES, not what it ENCODES.

    Only a movw immediately followed by a movt on the SAME register counts:
    that pair is how a 32-bit constant is materialised, and anything else is
    not an address being built.
    """
    lines = text.splitlines()
    out, i, changed, kept, failed = [], 0, 0, 0, []
    while i < len(lines):
        m1 = MOVW_RE.search(lines[i])
        m2 = MOVT_RE.search(lines[i + 1]) if i + 1 < len(lines) else None
        if not (m1 and m2 and m1.group(2) == m2.group(2)):
            out.append(lines[i]); i += 1
            continue
        old = (int(m2.group(3), 16) << 16) | int(m1.group(3), 16)
        if not (XIP <= (old & ~1) < LIMIT):
            out.append(lines[i]); i += 1
            continue
        thumb = old & 1
        new, how = retarget.resolve(ref, tgt, old & ~1)
        if new is None:
            failed.append((f"0x{old:08x}", how))
            out.append(lines[i]); i += 1
            continue
        new |= thumb
        if new == old:
            kept += 1
            out.append(lines[i]); out.append(lines[i + 1]); i += 2
            continue
        changed += 1
        l1 = MOVW_RE.sub(lambda m: f"{m.group(1)}{new & 0xffff:04x}", lines[i], count=1)
        l2 = MOVT_RE.sub(lambda m: f"{m.group(1)}{new >> 16:04x}", lines[i + 1], count=1)
        # A trailing comment documenting THIS immediate would otherwise be left
        # stating the old address, which is worse than no comment at all. The
        # comments write the address WITHOUT the Thumb bit, so both forms have
        # to be tried -- matching only the odd form silently left every one of
        # them stale.
        l1 = l1.replace(f"0x{old:08x}", f"0x{new:08x}")
        l1 = l1.replace(f"0x{old & ~1:08x}", f"0x{new & ~1:08x}")
        out.append(l1); out.append(l2); i += 2
    return "\n".join(out) + "\n", changed, kept, failed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-r", "--reference", required=True)
    ap.add_argument("-t", "--target", required=True)
    ap.add_argument("-i", "--header", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--source", help="C source with inline-asm movw/movt vendor "
                                      "addresses (main.c)")
    ap.add_argument("--source-out", help="where to write the relocated source")
    a = ap.parse_args()

    ref = open(a.reference, "rb").read()
    tgt = open(a.target, "rb").read()
    src = open(a.header).read()

    out_lines, failed, changed, kept = [], [], 0, 0
    for line in src.splitlines():
        stripped = line.lstrip()
        # Only #define lines, and only the part before any trailing comment.
        if not stripped.startswith("#define"):
            out_lines.append(line)
            continue
        code, sep, comment = line.partition("/*")

        def sub(m):
            nonlocal changed, kept
            old = int(m.group(0), 16)
            if not (XIP <= old < LIMIT):
                return m.group(0)
            thumb = old & 1
            new, how = retarget.resolve(ref, tgt, old & ~1)
            if new is None:
                new, how = resolve_data(ref, tgt, old & ~1)
            if new is None:
                failed.append((m.group(0), how))
                return m.group(0)
            new |= thumb
            if new == old:
                kept += 1
            else:
                changed += 1
            return f"0x{new:08x}"

        code = re.sub(r"0x1[0-9a-fA-F]{7}", sub, code)
        out_lines.append(code + sep + comment)

    text = "\n".join(out_lines) + "\n"
    banner = (f"/* GENERATED by mkfwh.py -- do not edit.\n"
              f" * reference: {os.path.basename(a.reference)}\n"
              f" * target:    {os.path.basename(a.target)}\n"
              f" * {changed} address(es) relocated, {kept} unchanged.\n"
              f" * Addresses inside comments are the REFERENCE build's and are\n"
              f" * deliberately left alone. */\n")
    open(a.out, "w").write(banner + text)

    print(f"  wrote {a.out}: {changed} relocated, {kept} unchanged")

    if a.source:
        if not a.source_out:
            raise SystemExit("--source needs --source-out")
        stext, schanged, skept, sfailed = relocate_source(ref, tgt, open(a.source).read())
        open(a.source_out, "w").write(
            f"/* GENERATED by mkfwh.py from {os.path.basename(a.source)} -- do not edit.\n"
            f" * {schanged} inline-asm vendor address(es) relocated, {skept} unchanged. */\n"
            + stext)
        print(f"  wrote {a.source_out}: {schanged} relocated, {skept} unchanged")
        failed += sfailed
    if failed:
        for lit, why in failed:
            print(f"    UNRESOLVED {lit}: {why}")
        raise SystemExit(f"{len(failed)} address(es) could not be relocated -- "
                         f"do NOT build from this header. Each one is a vendor "
                         f"call that would land in the wrong place.")


if __name__ == "__main__":
    main()
