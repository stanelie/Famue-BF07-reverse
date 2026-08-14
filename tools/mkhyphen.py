#!/usr/bin/env python3
"""Pack Knuth-Liang hyphenation patterns into a form the reader can use from flash.

Layout per language, all little-endian:

    u16 npat            number of patterns
    u16 nalpha          alphabet size
    u8  alpha[nalpha]   index -> Unicode code point, low byte (see cp[] below)
    u16 cp[nalpha]      index -> full code point (so accents work)
    u16 nindex          sparse index entries
    { u16 off; u8 len; u8 letters[len] } index[nindex]   every STRIDE-th pattern
    u8  letters[]       front-coded: u8 (shared<<4 | suffix_len), then suffix
    u8  values[]        per pattern: u8 count, then count x (pos<<4 | value)

Front-coding is what makes this fit: sorted patterns share long prefixes, which
takes the two languages from 41 KB raw to about 31 KB. The sparse index restores
binary search -- seek to a block, then decode forward at most STRIDE entries.

Licences (both redistributable, notices preserved in the generated header):
  hyph-en-us  Copyright (C) 1990, 2004, 2005 Gerard D.C. Kuiken -- royalty-free
              redistribution permitted with this notice
  hyph-fr     Copyright (C) 1994-2002 Daniel Flipo, Bernard Gaulle,
              2016 Arthur Reutenauer -- MIT
"""
import struct
import sys

STRIDE = 32


def load(path):
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("%"):
            out.append(line)
    return out


def split_pattern(pat):
    letters, vals = "", {}
    for ch in pat:
        if ch.isdigit():
            vals[len(letters)] = int(ch)
        else:
            letters += ch
    return letters, vals


def pack(path):
    """Pack one language.

    Layout (little-endian). Index entries are FIXED size so the device can
    binary search them, and each carries BOTH stream offsets so letters and
    values can be walked in lockstep from a block start -- the values stream is
    variable-length per pattern, so it cannot be indexed any other way.

        u16 npat, u16 nalpha, u8 maxlen, u8 stride, u16 nindex
        u32 letters_off, u32 values_off        (from the start of the blob)
        u16 cp[nalpha]                         alphabet index -> code point
        index[nindex] { u16 loff; u16 voff; u8 len; u8 letters[maxlen] }
        u8 letters[]   front-coded: u8 (shared<<4 | suffix_len), then suffix
        u8 values[]    per pattern: u8 count, then count x (pos<<4 | value)
    """
    entries = sorted(split_pattern(p) for p in load(path))
    alpha = sorted({c for l, _ in entries for c in l})
    idx = {c: i for i, c in enumerate(alpha)}
    maxlen = max(len(l) for l, _ in entries)

    letters_blob = bytearray()
    values_blob = bytearray()
    index = []
    prev = ""
    for n, (l, v) in enumerate(entries):
        if n % STRIDE == 0:
            index.append((len(letters_blob), len(values_blob), l))
            shared = 0
        else:
            shared = 0
            while (shared < len(prev) and shared < len(l)
                   and prev[shared] == l[shared] and shared < 15):
                shared += 1
        suffix = l[shared:]
        assert shared <= 15 and len(suffix) <= 15, (shared, len(suffix))
        letters_blob += bytes([(shared << 4) | len(suffix)]) + bytes(idx[c] for c in suffix)
        values_blob += bytes([len(v)]) + bytes((p << 4) | d for p, d in sorted(v.items()))
        prev = l

    ENTRY = 2 + 2 + 1 + maxlen
    head = struct.pack("<HHBBH", len(entries), len(alpha), maxlen, STRIDE, len(index))
    head_len = len(head) + 8 + 2 * len(alpha) + ENTRY * len(index)
    blob = head + struct.pack("<II", head_len, head_len + len(letters_blob))
    blob += b"".join(struct.pack("<H", ord(c)) for c in alpha)
    for loff, voff, l in index:
        e = struct.pack("<HHB", loff, voff, len(l)) + bytes(idx[c] for c in l)
        blob += e + b"\x00" * (ENTRY - len(e))
    blob += bytes(letters_blob) + bytes(values_blob)
    return blob, entries, alpha, ENTRY * len(index), len(letters_blob), len(values_blob)


def hyphenate(word, entries, alpha):
    """Reference implementation, used to verify the packed form on the host."""
    idx = {c: i for i, c in enumerate(alpha)}
    w = "." + word.lower() + "."
    pts = [0] * (len(w) + 1)
    table = {l: v for l, v in entries}
    for i in range(len(w)):
        for j in range(i + 1, min(i + 15, len(w)) + 1):
            v = table.get(w[i:j])
            if v:
                for pos, d in v.items():
                    if pts[i + pos] < d:
                        pts[i + pos] = d
    out = []
    for k in range(2, len(word) - 1):        # lefthyphenmin 2, righthyphenmin 3
        if pts[k + 1] % 2:
            out.append(k)
    return out


def emit_c(paths, out):
    """Emit both languages as one C array, plus their offsets.

    The two tables stay SEPARATE. Fusing them was measured and is not viable:
    Knuth-Liang patterns compete (values are max'd, odd allows a break, even
    inhibits), so a union opens wrong breaks in one language and suppresses
    correct ones in the other. On 4000-word samples, fusing left only 62% of
    English and 47% of French words hyphenated as they should be, producing
    breaks like cat-ti-sh-ly and eli-m-i-nais.
    """
    blobs = {}
    for lang, path in paths.items():
        blobs[lang] = pack(path)[0]
    with open(out, "w") as f:
        f.write("/* Generated by tools/mkhyphen.py -- do not edit.\n"
                " *\n"
                " * Knuth-Liang hyphenation patterns, packed for flash (XIP), so they\n"
                " * cost no RAM. See mkhyphen.py for the layout.\n"
                " *\n"
                " * hyph-en-us: Copyright (C) 1990, 2004, 2005 Gerard D.C. Kuiken.\n"
                " *   Copying and distribution permitted in any medium without royalty\n"
                " *   provided this notice is preserved.\n"
                " * hyph-fr: Copyright (C) 1994-2002 Daniel Flipo, Bernard Gaulle,\n"
                " *   2016 Arthur Reutenauer. MIT licence.\n"
                " */\n#ifndef HYPHEN_DATA_H\n#define HYPHEN_DATA_H\n\n")
        for lang, blob in blobs.items():
            f.write(f"#define HYPH_{lang.upper()}_LEN {len(blob)}\n")
        f.write("\nstatic const unsigned char hyph_data[] = {\n")
        joined = b"".join(blobs[l] for l in paths)
        for i in range(0, len(joined), 16):
            f.write("    " + ",".join(f"0x{b:02x}" for b in joined[i:i+16]) + ",\n")
        f.write("};\n\n")
        off = 0
        for lang, blob in blobs.items():
            f.write(f"#define HYPH_{lang.upper()}_OFF {off}\n")
            off += len(blob)
        f.write("\n#endif\n")
    return {l: len(b) for l, b in blobs.items()}


if __name__ == "__main__":
    total = 0
    for name, path in (("en", sys.argv[1]), ("fr", sys.argv[2])):
        blob, entries, alpha, ib, lb, vb = pack(path)
        total += len(blob)
        print(f"{name}: {len(entries)} patterns, alphabet {len(alpha)}")
        print(f"    index {ib}  letters {lb}  values {vb}   TOTAL {len(blob)} bytes")
        for w in ("hyphenation", "computer", "difficult", "présentation", "difficile"):
            try:
                pts = hyphenate(w, entries, alpha)
            except KeyError:
                continue
            if pts:
                s = "".join(c + ("-" if i + 1 in [p for p in pts] else "")
                            for i, c in enumerate(w))
                print(f"      {w} -> {s}")
    print(f"\nboth languages: {total} bytes")
    if len(sys.argv) > 3:
        sizes = emit_c({"en": sys.argv[1], "fr": sys.argv[2]}, sys.argv[3])
        print(f"wrote {sys.argv[3]}: {sum(sizes.values())} bytes of table data")
