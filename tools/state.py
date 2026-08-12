#!/usr/bin/env python3
"""Dump the injected reader's state by NAME, with offsets read from the ELF.

Hardcoded offsets have now produced two rounds of nonsense readings (nlines=4998,
file_ready=101) because adding fields to struct inj_state shifts everything after
them. The offsets come from DWARF in the build under test, so they cannot drift.

    state.py                 dump every field once
    state.py calls want gen  watch these fields for 3s and show what moved
"""
import glob, re, subprocess, sys, time, serial

ELF = "/Users/selie/Documents/bf07-research/reader/reader.elf"
STATE_PTR = 0x18018E9C

def offsets():
    d = subprocess.run(["arm-none-eabi-objdump", "--dwarf=info", ELF],
                       capture_output=True, text=True).stdout
    i = d.index("inj_state")
    out, name = {}, None
    for ln in d[i:].splitlines()[1:]:
        if "DW_TAG_structure_type" in ln and out: break
        m = re.search(r"DW_AT_name\b.*?:\s*(\w+)\s*$", ln)
        if m and "DW_TAG" not in ln: name = m.group(1)
        m = re.search(r"DW_AT_data_member_location:\s*(\d+)", ln)
        if m and name: out[name] = int(m.group(1)); name = None
    # Field SIZE matters as much as offset: sp/need_prep/file_ready/io_fail are
    # single bytes, and reading them as words splices four fields together
    # (file_ready came back as 0xb2000000). Sizes come from the gaps between
    # consecutive offsets, which is exact for the packed scalars at the head.
    ks = sorted(out, key=lambda k: out[k])
    return {k: (out[k], min(4, (out[ks[i+1]] - out[k]) if i+1 < len(ks) else 4))
            for i, k in enumerate(ks)}

class Dev:
    def __init__(self):
        self.s = serial.Serial(glob.glob("/dev/cu.usbserial-*")[0], 2000000, timeout=0.5)
        time.sleep(0.3)
    def rd(self, a, n=1):
        for _ in range(4):
            self.s.reset_input_buffer()
            self.s.write(f"dbg mdw 0x{a:08x} {n:x}\r\n".encode()); self.s.flush()
            t, b = time.time(), b""
            while time.time() - t < 0.9:
                d = self.s.read(32768)
                if d: b += d
            o = {}
            for m in re.finditer(r"^([0-9a-f]{8}): ((?:[0-9a-f]{8} ?){1,4})",
                                 b.decode("utf8", "replace"), re.M):
                base = int(m.group(1), 16)
                for i, w in enumerate(m.group(2).split()): o[base + i*4] = int(w, 16)
            if o: return o
        return {}
    def w(self, a): return self.rd(a).get(a, None)
    def field(self, base, off, size):
        """Read a field of 1/2/4 bytes; mdw is word-aligned, so mask and shift."""
        w = self.w((base + off) & ~3)
        if w is None: return None
        if size >= 4: return w
        sh = ((base + off) & 3) * 8
        return (w >> sh) & ((1 << (size * 8)) - 1)

def main():
    off = offsets()
    d = Dev()
    st = d.w(STATE_PTR)
    print(f"state = 0x{st:08x}   ({len(off)} fields from DWARF)")
    if not st or st < 0x01000000: raise SystemExit("no state pointer")
    watch = [a for a in sys.argv[1:] if a in off]
    if watch:
        a = {k: d.field(st, *off[k]) for k in watch}
        time.sleep(3)
        b = {k: d.field(st, *off[k]) for k in watch}
        for k in watch:
            mark = "  MOVED" if a[k] != b[k] else "  frozen"
            print(f"  {k:12s} {a[k]} -> {b[k]}{mark}")
        return
    for k in sorted(off, key=lambda x: off[x][0]):
        o, sz = off[k]
        if o > 0x300: continue
        v = d.field(st, o, sz)
        if v is None: continue
        print(f"  +0x{o:03x} {k:14s} ({sz}B) = {v:11d}  0x{v:08x}")

main()
