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

# Free flash at the tail of fw0_sys (0x1e7000..0x1f4000, 52 KB, erased) used to
# hold the division stubs.  XIP = 0x10000000 + (flash - FW0_SYS).
STUB_FLASH = 0x1E7000
STUB_XIP = XIP_BASE + (STUB_FLASH - FW0_SYS)      # 0x101d3000
STUB_STRIDE = 0x20   # stubs are up to 18 bytes

# `page = line / 8`, hardcoded as `it lt / addlt rX,#7 / asr rX,rX,#3`.
# Address is the `it`; the idiom is exactly 6 bytes, and bl+nop is exactly 6.
DIV_SITES = [
    (0x10049446, 1), (0x100494CA, 0), (0x1004955C, 1), (0x10049670, 1),
    (0x10049816, 2), (0x10049E34, 1), (0x10049EAC, 1), (0x1004A30E, 3),
]
DIV_REGS = [0, 1, 2, 3]     # one stub per destination register

# The INVERSE conversion, page -> line, at 0x10049266:
#     rsb r1, r1, r3, lsl #3      ; r1 = (page-1)*8 - reading_position
# The x8 is a shift buried inside an RSB, which is why a byte scan for a bare
# `lsls #3` never found it.  Replaced by a bl to a fifth stub computing
# r1 = r3*lines - r1.  4 bytes in, 4 bytes out.
MUL_SITE = 0x10049266
MUL_STUB_INDEX = 4

# UNSIGNED `pages = count / 8 + 1`, emitted as a bare 2-byte `lsrs rX,rX,#3`
# followed by `adds rX,#1` -- no rounding idiom at all, which is why every scan
# for the signed `it lt / addlt #7 / asr #3` sequence missed these. Four bytes
# in total, exactly the size of a bl.  0x1004ba96/baa8/bb90 are
# ebook_calculate_pages turning a line count into a page count.
DIVP1_SITES = [(0x1004A572, 6), (0x1004BA96, 3), (0x1004BAA8, 3),
               (0x1004BB90, 3)]
DIVP1_REGS = [3, 6]
DIVP1_BASE_INDEX = 5

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


def mod_imm(v):
    """Encode v as a Thumb-2 modified immediate, or None.

    Only the two forms we need: a bare 8-bit value, and an 8-bit value with
    bit 7 set, rotated.  imm12[11:7] is the rotation, imm12[6:0] the low seven
    bits of the byte (bit 7 is implicit).
    """
    if 0 <= v <= 0xFF:
        return v
    for k in range(1, 25):
        if v & ((1 << k) - 1):
            continue
        x = v >> k
        if 0x80 <= x <= 0xFF:
            return ((32 - k) << 7) | (x & 0x7F)
    return None


def add_w(rd, rn, v):
    """ADD.W Rd, Rn, #<modified immediate> (T3)."""
    imm12 = mod_imm(v)
    if imm12 is None:
        raise ValueError(f"{v:#x} is not a Thumb-2 modified immediate")
    hw1 = 0xF000 | ((imm12 >> 11) & 1) << 10 | 0x100 | (rn & 0xF)
    return struct.pack("<HH", hw1, _t2_hw2(imm12 & 0x7FF, rd))


def add_const(rd, rn, v):
    """Widest-range 32-bit add: prefer ADDW, fall back to ADD.W."""
    if v <= 0xFFF:
        return addw(rd, rn, v)
    return add_w(rd, rn, v)


def pick_context_size(lines):
    """Smallest word-aligned context size for `lines` whose array end offset
    (3 * size) can be encoded in the single instruction at 0x1004c360."""
    minimum = HDR + lines * REC
    size = (minimum + 3) & ~3
    while size < minimum + 0x40:
        if 3 * size <= 0xFFF or mod_imm(3 * size) is not None:
            return size
        size += 4
    raise ValueError(f"no encodable context size for {lines} lines")


def mov_w(rd, v):
    """MOV.W Rd, #<modified immediate> (T2) -- does NOT set flags."""
    imm12 = mod_imm(v)
    if imm12 is None:
        raise ValueError(f"{v:#x} is not a Thumb-2 modified immediate")
    hw1 = 0xF04F | (((imm12 >> 11) & 1) << 10)
    return struct.pack("<HH", hw1, _t2_hw2(imm12 & 0x7FF, rd))


def sdiv(rd, rn, rm):
    return struct.pack("<HH", 0xFB90 | (rn & 0xF),
                       0xF0F0 | ((rd & 0xF) << 8) | (rm & 0xF))


def mul_rr(rd, rn, rm):
    return struct.pack("<HH", 0xFB00 | (rn & 0xF),
                       0xF000 | ((rd & 0xF) << 8) | (rm & 0xF))


def sub_w(rd, rn, rm):
    """SUB.W Rd, Rn, Rm (register, no shift) -- does not set flags."""
    return struct.pack("<HH", 0xEBA0 | (rn & 0xF), ((rd & 0xF) << 8) | (rm & 0xF))


def push_pop(reg, pop=False):
    return struct.pack("<H", (0xBC00 if pop else 0xB400) | (1 << reg))


BX_LR = struct.pack("<H", 0x4770)
NOP = struct.pack("<H", 0xBF00)


def bl(addr, target):
    """BL (T1).  addr and target are even Thumb addresses."""
    off = target - (addr + 4)
    if not -(1 << 24) <= off < (1 << 24) or off & 1:
        raise ValueError(f"bl out of range: {off:#x}")
    S = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    j1 = (i1 ^ 1) ^ S
    j2 = (i2 ^ 1) ^ S
    hw1 = 0xF000 | (S << 10) | ((off >> 12) & 0x3FF)
    hw2 = 0xD000 | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def stub_for(reg, lines):
    """rX = rX / lines, signed, preserving every other register AND the flags."""
    scratch = 1 if reg == 0 else 0
    return (push_pop(scratch) + mov_w(scratch, lines) + sdiv(reg, reg, scratch)
            + push_pop(scratch, pop=True) + BX_LR)


def udiv(rd, rn, rm):
    return struct.pack("<HH", 0xFBB0 | (rn & 0xF),
                       0xF0F0 | ((rd & 0xF) << 8) | (rm & 0xF))


def divp1_stub(reg, lines):
    """rX = rX / lines + 1, unsigned, preserving other registers and flags."""
    scratch = 1 if reg == 0 else 0
    return (push_pop(scratch) + mov_w(scratch, lines) + udiv(reg, reg, scratch)
            + add_w(reg, reg, 1) + push_pop(scratch, pop=True) + BX_LR)


def mul_stub(lines):
    """r1 = r3 * lines - r1, preserving every other register and the flags."""
    return (push_pop(0) + mov_w(0, lines) + mul_rr(0, 3, 0)
            + sub_w(1, 0, 1) + push_pop(0, pop=True) + BX_LR)


def build_stub_sector(lines):
    """4 KB image for STUB_FLASH: erased 0xFF with one stub per register."""
    sec = bytearray(b"\xff" * 0x1000)
    for i, reg in enumerate(DIV_REGS):
        code = stub_for(reg, lines)
        sec[i * STUB_STRIDE:i * STUB_STRIDE + len(code)] = code
    code = mul_stub(lines)
    off = MUL_STUB_INDEX * STUB_STRIDE
    sec[off:off + len(code)] = code
    for j, reg in enumerate(DIVP1_REGS):
        code = divp1_stub(reg, lines)
        off = (DIVP1_BASE_INDEX + j) * STUB_STRIDE
        sec[off:off + len(code)] = code
    return bytes(sec)


def cmp_imm8(rd, imm8):
    return struct.pack("<H", 0x2800 | (rd & 7) << 8 | (imm8 & 0xFF))


def movs_imm8(rd, imm8):
    return struct.pack("<H", 0x2000 | (rd & 7) << 8 | (imm8 & 0xFF))


def adds_imm8(rd, imm8):
    return struct.pack("<H", 0x3000 | (rd & 7) << 8 | (imm8 & 0xFF))


def ldrb_w(rt, rn, imm12):
    return struct.pack("<HH", 0xF890 | (rn & 0xF), (rt & 0xF) << 12 | imm12)


def strb_w(rt, rn, imm12):
    return struct.pack("<HH", 0xF880 | (rn & 0xF), (rt & 0xF) << 12 | imm12)


def record_layout(lines):
    """Shrink the per-line record so `lines` records fit the STOCK context.

    rec+0x00 index, +0x04 file offset, +0x08 text, +8+text length byte.
    Returns (record_size, text_size).
    """
    room = STOCK_SIZE - HDR                       # 0x3a0
    rec = (room // lines) & ~3
    if rec > REC:
        rec = REC
    text = rec - 9                                # 4 + 4 + text + 1 byte
    text &= ~3
    if HDR + lines * rec > STOCK_SIZE:
        raise ValueError(f"{lines} lines do not fit the stock context")
    return rec, text


def build_patches_inplace(lines, line_height):
    """Patch set that keeps the contexts at their stock addresses.

    No relocation: the records are made smaller instead of the context bigger,
    so every literal, memset size and stride stays stock.
    """
    rec, text = record_layout(lines)
    # Two different bases index the same byte:
    #   decode: fp = ctx + i*rec        -> length byte at 0x2c + 8 + text
    #   render: r5 = ctx + 0x34 + i*rec -> already at the text, so offset = text
    lb_decode = HDR + 8 + text
    lb_render = text

    return sorted([
        (0x1004934A, cmp_imm8(3, STOCK_LINES - 1), cmp_imm8(3, lines - 1),
         f"array bound cmp r3,#{STOCK_LINES-1} -> #{lines-1}"),

        # --- decode side -------------------------------------------------
        (0x100492CE, bytes.fromhex("4ff07409"), mov_w(9, rec),
         f"record stride {REC:#x} -> {rec:#x}"),
        (0x100492DA, cmp_imm8(6, 0x60), cmp_imm8(6, text),
         f"input clamp {0x60:#x} -> {text:#x}"),
        (0x100492E0, movs_imm8(6, 0x60), movs_imm8(6, text),
         f"input clamp {0x60:#x} -> {text:#x}"),
        (0x100492F0, movs_imm8(2, 0x60), movs_imm8(2, text),
         f"wrap max {0x60:#x} -> {text:#x}"),
        (0x1004931E, movs_imm8(2, REC), movs_imm8(2, rec),
         f"memset len {REC:#x} -> {rec:#x}"),
        (0x10049330, movs_imm8(2, 0x60), movs_imm8(2, text),
         f"memcpy len {0x60:#x} -> {text:#x}"),
        (0x1004933A, strb_w(6, 11, 0x94), strb_w(6, 11, lb_decode),
         f"length byte {0x94:#x} -> {lb_decode:#x}"),

        # --- render side -------------------------------------------------
        (0x1004927A, ldrb_w(3, 5, 0x60), ldrb_w(3, 5, lb_render),
         f"read length byte {0x60:#x} -> {lb_render:#x}"),
        (0x10049288, strb_w(8, 5, 0x60), strb_w(8, 5, lb_render),
         f"write length byte {0x60:#x} -> {lb_render:#x}"),
        (0x1004928E, adds_imm8(5, REC), adds_imm8(5, rec),
         f"record stride {REC:#x} -> {rec:#x}"),

        # --- line height -------------------------------------------------
        (0x1004A288, bytes.fromhex("0bebe00b"), add_w(11, 11, line_height),
         f"line height: content/8 -> literal {line_height}px"),

        # --- page <-> line arithmetic (stubs in free flash) --------------
        *[(addr,
           bytes.fromhex("b8bf07") + bytes([0x30 | reg]) +
           struct.pack("<H", 0x10C0 | (reg << 3) | reg),
           bl(addr, STUB_XIP + DIV_REGS.index(reg) * STUB_STRIDE) + NOP,
           f"page=line/8 -> /{lines} via stub (r{reg})")
          for addr, reg in DIV_SITES],
        (MUL_SITE, bytes.fromhex("c1ebc301"),
         bl(MUL_SITE, STUB_XIP + MUL_STUB_INDEX * STUB_STRIDE),
         f"page*8 -> page*{lines} via stub"),

        # unsigned `pages = count/8 + 1` -> `count/lines + 1`
        *[(addr,
           struct.pack("<HH", 0x08C0 | (reg << 3) | reg, 0x3001 | (reg << 8)),
           bl(addr, STUB_XIP + (DIVP1_BASE_INDEX + DIVP1_REGS.index(reg))
              * STUB_STRIDE),
           f"pages=count/8+1 -> /{lines}+1 via stub (r{reg})")
          for addr, reg in DIVP1_SITES],
    ])


def le32(v):
    return struct.pack("<I", v)


# --- patch table ------------------------------------------------------------


def build_patches(lines, new_base, line_height):
    """Return [(xip_addr, old_bytes, new_bytes, description)]."""
    size = pick_context_size(lines)
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
        (0x10049FBA, bytes.fromhex("07f57377"), add_const(7, 7, size),
         f"stride r7 {STOCK_SIZE:#x} -> {size:#x}"),
        (0x1004A318, bytes.fromhex("4ff4737a"), movw(10, size),
         f"stride sl {STOCK_SIZE:#x} -> {size:#x}"),
        (0x1004A440, bytes.fromhex("07f57377"), add_const(7, 7, size),
         f"stride r7 {STOCK_SIZE:#x} -> {size:#x}"),
        (0x1004C39A, bytes.fromhex("05f57375"), add_const(5, 5, size),
         f"stride r5 {STOCK_SIZE:#x} -> {size:#x}"),

        # the array end pointer: end = base + 3 * sizeof(context)
        (0x1004C360, addw(3, 5, STOCK_ARRAY), add_const(3, 5, array),
         f"array end {STOCK_ARRAY:#x} -> {array:#x}"),

        # line height: the stock code computes content_height / 8 with the
        # divisor HARDCODED, independent of the line count.  Replace the shift
        # with a literal height, or 11 records would still be spaced for 8.
        (0x1004A288, bytes.fromhex("0bebe00b"), add_w(11, 11, line_height),
         f"line height: content/8 -> literal {line_height}px"),

        # `page = line / 8` -> real division, via a stub in free flash.  A
        # shift cannot divide by 11, and the idiom is only 6 bytes -- exactly
        # the size of bl + nop.
        *[(addr,
           bytes.fromhex("b8bf07") + bytes([0x30 | reg]) +
           struct.pack("<H", 0x10C0 | (reg << 3) | reg),
           bl(addr, STUB_XIP + DIV_REGS.index(reg) * STUB_STRIDE) + NOP,
           f"page=line/8 -> /{lines} via stub (r{reg})")
          for addr, reg in DIV_SITES],

        # page -> line: `rsb r1, r1, r3, lsl #3` -> bl to the multiply stub
        (MUL_SITE, bytes.fromhex("c1ebc301"),
         bl(MUL_SITE, STUB_XIP + MUL_STUB_INDEX * STUB_STRIDE),
         f"page*8 -> page*{lines} via stub"),

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
    ap.add_argument("--ram", type=lambda s: int(s, 0),
                    help="base of a relocated context block; omit to keep the "
                         "contexts in place and shrink the line records")
    ap.add_argument("--content-height", type=int, default=236,
                    help="reader content area height in px (measured: 236)")
    ap.add_argument("--line-height", type=int,
                    help="override px per line (default content_height/lines)")
    ap.add_argument("--outdir", help="write patched 4 KB sectors here")
    args = ap.parse_args()

    data = bytearray(open(args.image, "rb").read())
    lh = args.line_height or args.content_height // args.lines
    if lh * args.lines > args.content_height:
        print(f"ABORT: {args.lines} x {lh}px = {lh * args.lines}px exceeds the "
              f"{args.content_height}px content area")
        return 1
    if args.ram:
        patches, size, array, ctx = build_patches(args.lines, args.ram, lh)
        total = 4 * size
    else:
        rec, text = record_layout(args.lines)
        patches = build_patches_inplace(args.lines, lh)
        print(f"mode             : IN PLACE (contexts stay at their stock "
              f"addresses; no relocation)")
        print(f"lines per page   : {STOCK_LINES} -> {args.lines}")
        print(f"record size      : {REC:#x} -> {rec:#x}")
        print(f"line text buffer : {0x60:#x} -> {text:#x} "
              f"({0x60} -> {text} bytes)")
        print(f"context usage    : {HDR + args.lines * rec:#x} of "
              f"{STOCK_SIZE:#x}")
        print(f"line height      : {lh} px  ({args.lines} x {lh} = "
              f"{args.lines * lh} of {args.content_height} px)\n")
        bad = 0
        data2 = data
        for xip, old, new, desc in patches:
            off = xip - XIP_BASE
            cur = bytes(data2[off:off + len(old)])
            if cur != old or len(new) != len(old):
                print(f"  MISMATCH 0x{xip:08x}: found {cur.hex()}, "
                      f"expected {old.hex()}  ({desc})")
                bad += 1
                continue
            data2[off:off + len(new)] = new
            print(f"  0x{xip:08x} flash 0x{FW0_SYS + off:06x}  {old.hex():<8} "
                  f"-> {new.hex():<8}  {desc}")
        if bad:
            print(f"\nABORT: {bad} site(s) did not match")
            return 1
        sectors = sorted({(FW0_SYS + (x - XIP_BASE)) & ~0xFFF
                          for x, *_ in patches})
        print(f"\n{len(patches)} sites across {len(sectors)} sectors: "
              + ", ".join(hex(x) for x in sectors))
        print(f"plus the stub sector 0x{STUB_FLASH:06x}")
        if args.outdir:
            for x in sectors:
                off = x - FW0_SYS
                open(f"{args.outdir}/sector_{x:06x}.bin", "wb").write(
                    bytes(data2[off:off + 0x1000]))
                print(f"  wrote sector_{x:06x}.bin")
            open(f"{args.outdir}/sector_{STUB_FLASH:06x}.bin", "wb").write(
                build_stub_sector(args.lines))
            print(f"  wrote sector_{STUB_FLASH:06x}.bin  (stubs)")
        return 0

    print(f"lines per page   : {STOCK_LINES} -> {args.lines}")
    slack = size - (HDR + args.lines * REC)
    print(f"context size     : {STOCK_SIZE:#x} -> {size:#x}"
          + (f"  (+{slack} pad, to keep 3*size encodable)" if slack else ""))
    print(f"array (3 ctx)    : {STOCK_ARRAY:#x} -> {array:#x}")
    print(f"relocated block  : {args.ram:#x} .. {args.ram + total:#x} "
          f"({total} bytes)")
    print(f"line height      : {lh} px  ({args.lines} x {lh} = "
          f"{args.lines * lh} of {args.content_height} px)\n")

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
    print(f"plus the stub sector 0x{STUB_FLASH:06x} "
          f"({len(DIV_REGS)} division stubs at 0x{STUB_XIP:08x})")

    if args.outdir:
        for s in sectors:
            off = s - FW0_SYS
            path = f"{args.outdir}/sector_{s:06x}.bin"
            open(path, "wb").write(bytes(data[off:off + 0x1000]))
            print(f"  wrote {path}")
        path = f"{args.outdir}/sector_{STUB_FLASH:06x}.bin"
        open(path, "wb").write(build_stub_sector(args.lines))
        print(f"  wrote {path}  (stubs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
