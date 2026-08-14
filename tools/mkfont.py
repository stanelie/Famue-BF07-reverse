#!/usr/bin/env python3
"""Render a TTF into the LVGL binary font format the BF07 loads.

The device's own fonts ARE lv_font_conv output -- `head`/`cmap`/`loca`/`glyf`
chunks, verified by reading you18.font off the SD card. So this writes the same
format rather than inventing one. Parameters are copied from that file so the
vendor's parser sees exactly what it expects:

    bpp 1, xy_bits 5, wh_bits 5, advance_bits 6, compression 0 (raw),
    indexToLocFormat 1 (Offset32), glyphIdFormat 1 (2 bytes)

Coordinates follow the spec: origin at the baseline-left, y up, and the BBox
(x, y) is its BOTTOM-left corner, while the bitmap itself is written from the
top-left going right and down.

    mkfont.py Literata-Regular.ttf 16 out.font
    mkfont.py Literata-VariableFont_opsz,wght.ttf 13 out.font --axes 7,400

bpp is 1 -- that is what the vendor's own fonts use, so there is no antialiasing
to soften anything. Two separate things then make strokes come out uneven, and
both have to be handled or the text looks blotchy:

1. Stroke contrast in the design. A face drawn for large text has stems much
   heavier than its hairlines; at 13px the grid can only round those to 2px or
   1px, so the contrast reappears as weight. Literata's 18pt cut at 13px gives
   `b` a 2px stem against 1px hairlines. Use a face drawn for small sizes --
   for a variable font, the smallest `opsz` (Literata: 7).

2. Grid fitting. Unhinted rasterisation rounds each stem independently, so caps
   land on 2px while lowercase lands on 1px (`H` vs `b`), and a single bowl can
   come out 1px on the left and 2px on the right (`e`, `o`). FreeType's
   autohinter normalises stem widths and snaps them to the grid, which is why
   this renders through freetype-py with FORCE_AUTOHINT rather than through
   Pillow -- variable-font instances carry no usable TrueType hints of their own.
"""
import struct
import sys

import freetype
from freetype import (FT_LOAD_FORCE_AUTOHINT, FT_LOAD_MONOCHROME,
                      FT_LOAD_RENDER, FT_LOAD_TARGET_MONO)

LOAD = FT_LOAD_RENDER | FT_LOAD_TARGET_MONO | FT_LOAD_MONOCHROME | FT_LOAD_FORCE_AUTOHINT


class Bits:
    def __init__(self):
        self.acc = 0
        self.n = 0
        self.buf = bytearray()

    def put(self, value, width):
        for i in range(width - 1, -1, -1):
            self.acc = (self.acc << 1) | ((value >> i) & 1)
            self.n += 1
            if self.n == 8:
                self.buf.append(self.acc)
                self.acc = 0
                self.n = 0

    def align(self):
        while self.n:
            self.put(0, 1)
        return bytes(self.buf)


def chunk(tag, payload):
    return struct.pack("<I4s", len(payload) + 8, tag) + payload


def build(ttf, px, codepoints, axes=None):
    face = freetype.Face(ttf)
    face.set_char_size(px * 64)
    if axes:
        face.set_var_design_coords(list(axes))
    ascent = face.size.ascender >> 6
    descent = -(face.size.descender >> 6)        # positive, like Pillow reported

    glyphs = [None]                              # id 0 = "undefined", empty
    for cp in codepoints:
        face.load_char(chr(cp), LOAD)
        g = face.glyph
        adv = (g.advance.x + 32) >> 6
        bm = g.bitmap
        w, h = bm.width, bm.rows
        if w == 0 or h == 0:                     # space and friends
            glyphs.append((adv, 0, 0, 0, 0, b""))
            continue
        bx = g.bitmap_left
        by = g.bitmap_top - h                    # baseline -> bbox bottom, y up
        rows = bytearray()
        for row in range(h):                     # bit-packed, pitch >= ceil(w/8)
            src = bm.buffer[row * bm.pitch:(row + 1) * bm.pitch]
            for col in range(w):
                rows.append((src[col >> 3] >> (7 - (col & 7))) & 1)
        glyphs.append((adv, bx, by, w, h, bytes(rows)))

    # glyf: each glyph starts on a byte boundary; loca holds those offsets
    glyf = bytearray()
    loca = [8]                                   # first glyph sits after the header
    for g in glyphs:
        if g is None:
            loca.append(8 + len(glyf))
            continue
        adv, bx, by, w, h, rows = g
        b = Bits()
        b.put(adv, 6)
        b.put(bx & 0x1F, 5)                      # signed, 5 bits, two's complement
        b.put(by & 0x1F, 5)
        b.put(w, 5)
        b.put(h, 5)
        for bit in rows:
            b.put(bit, 1)
        glyf += b.align()
        loca.append(8 + len(glyf))
    loca = loca[:-1] if len(loca) > len(glyphs) else loca
    while len(loca) < len(glyphs):
        loca.append(8 + len(glyf))

    # cmap: one "format 0 tiny" subtable per contiguous run -- glyph ids are
    # assigned in code point order, so no data segment is needed at all
    runs = []
    start = prev = codepoints[0]
    gid = 1
    gid_start = 1
    for cp in codepoints[1:]:
        if cp != prev + 1:
            runs.append((start, prev - start + 1, gid_start))
            start = cp
            gid_start = gid + 1
        prev = cp
        gid += 1
    runs.append((start, prev - start + 1, gid_start))

    sub = b""
    for rstart, rlen, gstart in runs:
        sub += struct.pack("<IIHHHBB", 0, rstart, rlen, gstart, 0, 2, 0)
    cmap = struct.pack("<I", len(runs)) + sub

    min_y = min((g[2] for g in glyphs if g), default=0)
    max_y = max((g[2] + g[4] for g in glyphs if g), default=px)

    head = struct.pack("<IHHHhHhHhh", 1, 3, px, ascent, -descent,
                       ascent, -descent, 0, min_y, max_y)
    head += struct.pack("<HH", 0, 16)            # defaultAdvanceWidth, kerningScale
    head += bytes([1, 1, 0, 1, 5, 5, 6, 0, 0, 0])
    head += struct.pack("<hH", 1, 1)             # underline position, thickness

    loca_blob = struct.pack("<I", len(loca)) + b"".join(struct.pack("<I", o) for o in loca)
    return (chunk(b"head", head) + chunk(b"cmap", cmap)
            + chunk(b"loca", loca_blob) + chunk(b"glyf", bytes(glyf)))


def default_codepoints():
    cps = list(range(0x20, 0x7F))                        # ASCII
    cps += [0xA0] + list(range(0xC0, 0x100))             # Latin-1 letters, nbsp
    cps += [0x152, 0x153, 0x178]                         # OE oe Y-diaeresis
    cps += [0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026]
    return sorted(set(cps))


if __name__ == "__main__":
    ttf, px, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    axes = None
    if "--axes" in sys.argv:
        axes = [float(v) for v in sys.argv[sys.argv.index("--axes") + 1].split(",")]
    blob = build(ttf, px, default_codepoints(), axes)
    open(out, "wb").write(blob)
    tag = f" axes={axes}" if axes else ""
    print(f"{out}: {len(blob)} bytes, {len(default_codepoints())} glyphs at {px}px{tag}")
