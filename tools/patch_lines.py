#!/usr/bin/env python3
"""Rebuild the BF07 ebook reader for a different number of lines per page.

Operates entirely offline on the decrypted XIP image (fw_code_full.bin) and
emits the 4 KB flash sectors to be written back.  See docs/ebook-more-lines.md
for how every site here was derived.

The reader keeps four page contexts, all of them STATIC:

    0x18018a4c            standalone, stored at [ctx+0x18c]
    0x18019098/464/830    3-entry array, base stored at [ctx+0x190]

each sized 0x2c + lines * 0x74.  Raising the line count therefore means
relocating all four to a larger block of free RAM and rewriting every constant
that encodes the old size.
"""

import argparse
import struct
import sys

XIP_BASE = 0x10000000
FW0_SYS = 0x14000          # flash offset of the fw0_sys partition
HDR = 0x2C                 # page-context header, before the line records
REC = 0x74                 # bytes per line record
STOCK_LINES = 8
STOCK_SIZE = HDR + STOCK_LINES * REC        # 0x3cc
STOCK_ARRAY = 3 * STOCK_SIZE                # 0xb64

# --- Thumb-2 encoders -------------------------------------------------------


def _t2_hw2(imm, rd):
    return ((imm >> 8) & 7) << 12 | (rd & 0xF) << 8 | (imm & 0xFF)


def movw(rd, imm16):
    if not 0 <= imm16 <= 0xFFFF:
        raise ValueError(f"movw immediate out of range: {imm16:#x}")
    hw1 = 0xF000 | ((imm16 >> 11) & 1) << 10 | 0x240 | ((imm16 >> 12) & 0xF)
    return struct.pack("<HH", hw1, _t2_hw2(imm16, rd))


def addw(rd, rn, imm12):
    if not 0 <= imm12 <= 0xFFF:
        raise ValueError(
            f"addw immediate {imm12:#x} exceeds the 12-bit field; this line "
            f"count is not reachable without lengthening the instruction")
    hw1 = 0xF000 | ((imm12 >> 11) & 1) << 10 | 0x200 | (rn & 0xF)
    return struct.pack("<HH", hw1, _t2_hw2(imm12, rd))


def cmp_imm8(rd, imm8):
    return struct.pack("<H", 0x2800 | (rd & 7) << 8 | (imm8 & 0xFF))


def le32(v):
    return struct.pack("<I", v)


# --- patch table ------------------------------------------------------------


def build_patches(lines, new_base):
    """Return [(xip_addr, old_bytes, new_bytes, description)]."""
    size = HDR + lines * REC
    array = 3 * size
    ctx = [new_base + i * size for i in range(4)]   # standalone, a0, a1, a2

    p = [
        # _decode_one_page: the line-record array bound
        (0x1004934A, cmp_imm8(3, STOCK_LINES - 1), cmp_imm8(3, lines - 1),
         f"array bound cmp r3,#{STOCK_LINES-1} -> #{lines-1}"),

        # literal pool feeding _decode_one_page's four callers
        (0x100493C0, le32(0x18018A4C), le32(ctx[0]), "literal standalone"),
        (0x100493C4, le32(0x18019098), le32(ctx[1]), "literal array[0]"),
        (0x100493C8, le32(0x18019464), le32(ctx[2]), "literal array[1]"),
        (0x100493CC, le32(0x18019830), le32(ctx[3]), "literal array[2]"),

        # _reading_create_content: the two memsets that clear the contexts
        (0x10049F1E, bytes.fromhex("4ff47372"), movw(2, size),
         f"memset size {STOCK_SIZE:#x} -> {size:#x}"),
        (0x10049F30, movw(2, STOCK_ARRAY), movw(2, array),
         f"memset size {STOCK_ARRAY:#x} -> {array:#x}"),

        # array walks: ctx += sizeof(context)
        (0x10049FBA, bytes.fromhex("07f57377"), addw(7, 7, size),
         f"stride r7 {STOCK_SIZE:#x} -> {size:#x}"),
        (0x1004A318, bytes.fromhex("4ff4737a"), movw(10, size),
         f"stride sl {STOCK_SIZE:#x} -> {size:#x}"),
        (0x1004A440, bytes.fromhex("07f57377"), addw(7, 7, size),
         f"stride r7 {STOCK_SIZE:#x} -> {size:#x}"),
        (0x1004C39A, bytes.fromhex("05f57375"), addw(5, 5, size),
         f"stride r5 {STOCK_SIZE:#x} -> {size:#x}"),

        # the array end pointer: end = base + 3 * sizeof(context)
        (0x1004C360, addw(3, 5, STOCK_ARRAY), addw(3, 5, array),
         f"array end {STOCK_ARRAY:#x} -> {array:#x}"),

        # the other literal pool, in _reading_create_content
        (0x1004A110, le32(0x18018A4C), le32(ctx[0]), "literal standalone"),
        (0x1004A114, le32(0x18019098), le32(ctx[1]), "literal array[0]"),
        (0x1004A4F0, le32(0x18019098), le32(ctx[1]), "literal array[0]"),
    ]
    return sorted(p), size, array, ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="decrypted XIP image (fw_code_full.bin)")
    ap.add_argument("--lines", type=int, default=11)
    ap.add_argument("--ram", type=lambda s: int(s, 0), required=True,
                    help="base of the relocated context block")
    ap.add_argument("--outdir", help="write patched 4 KB sectors here")
    args = ap.parse_args()

    data = bytearray(open(args.image, "rb").read())
    patches, size, array, ctx = build_patches(args.lines, args.ram)
    total = 4 * size

    print(f"lines per page   : {STOCK_LINES} -> {args.lines}")
    print(f"context size     : {STOCK_SIZE:#x} -> {size:#x}")
    print(f"array (3 ctx)    : {STOCK_ARRAY:#x} -> {array:#x}")
    print(f"relocated block  : {args.ram:#x} .. {args.ram + total:#x} "
          f"({total} bytes)")
    print(f"line height      : 236/{args.lines} = {236 // args.lines} px\n")

    bad = 0
    for xip, old, new, desc in patches:
        off = xip - XIP_BASE
        cur = bytes(data[off:off + len(old)])
        if cur != old:
            print(f"  MISMATCH 0x{xip:08x}: found {cur.hex()}, "
                  f"expected {old.hex()}  ({desc})")
            bad += 1
            continue
        if len(new) != len(old):
            print(f"  LENGTH CHANGE 0x{xip:08x} ({desc})")
            bad += 1
            continue
        data[off:off + len(new)] = new
        flash = FW0_SYS + off
        print(f"  0x{xip:08x} flash 0x{flash:06x}  {old.hex():<8} -> "
              f"{new.hex():<8}  {desc}")

    if bad:
        print(f"\nABORT: {bad} site(s) did not match the expected bytes")
        return 1

    sectors = sorted({(FW0_SYS + (x - XIP_BASE)) & ~0xFFF for x, *_ in patches})
    print(f"\n{len(patches)} sites patched across {len(sectors)} sectors: "
          + ", ".join(hex(s) for s in sectors))

    if args.outdir:
        for s in sectors:
            off = s - FW0_SYS
            path = f"{args.outdir}/sector_{s:06x}.bin"
            open(path, "wb").write(bytes(data[off:off + 0x1000]))
            print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
