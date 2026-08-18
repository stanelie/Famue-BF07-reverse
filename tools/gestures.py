#!/usr/bin/env python3
"""Read the gesture ring our hook records at 0x100d92e8.

The vendor dispatches input through a gesture/view layer above LVGL, so this is
the first place a press is visible as data we control. Offsets come from DWARF.
"""
import glob, re, subprocess, time, serial

import os as _os
import serialport
_HERE = _os.path.dirname(_os.path.abspath(__file__))
# Override with BF07_ELF if the build lives elsewhere.
ELF = _os.environ.get("BF07_ELF",
                      _os.path.join(_HERE, _os.pardir, "reader", "reader.elf"))

def offsets():
    d = subprocess.run(["arm-none-eabi-objdump", "--dwarf=info", ELF],
                       capture_output=True, text=True).stdout
    i = d.index("inj_state"); out, name = {}, None
    for ln in d[i:].splitlines()[1:]:
        if "DW_TAG_structure_type" in ln and out: break
        m = re.search(r"DW_AT_name\b.*?:\s*(\w+)\s*$", ln)
        if m and "DW_TAG" not in ln: name = m.group(1)
        m = re.search(r"DW_AT_data_member_location:\s*(\d+)", ln)
        if m and name: out[name] = int(m.group(1)); name = None
    return out

s = serialport.open(timeout=0.5)
time.sleep(0.2)
def blk(a, n):
    for _ in range(4):
        s.reset_input_buffer()
        s.write(f"dbg mdw 0x{a:08x} {n:x}\r\n".encode()); s.flush()
        t, b = time.time(), b""
        while time.time() - t < 1.0:
            d = s.read(65536)
            if d: b += d
            elif b: break
        o = {}
        for m in re.finditer(r"^([0-9a-f]{8}): ((?:[0-9a-f]{8} ?){1,4})",
                             b.decode("utf8", "replace"), re.M):
            base = int(m.group(1), 16)
            for i, w in enumerate(m.group(2).split()): o[base + i*4] = int(w, 16)
        if len(o) >= n * 0.8: return o
    return {}

off = offsets()
st = blk(0x18018E9C, 1).get(0x18018E9C)
if not st or st < 0x01000000: raise SystemExit("no state pointer -- open a book first")
tn = blk(st + off["touch_n"], 1).get(st + off["touch_n"])
nz = blk(st + off["touch_nz"], 1).get(st + off["touch_nz"])
tring = blk(st + off["touch"], 12)
tbase = st + off["touch"]
print(f"touch calls: {tn}   non-idle: {nz}")
for i in range(4):
    w = [tring.get(tbase + (i*3 + j)*4, 0) for j in range(3)]
    if not any(w): continue
    mark = "  <- newest" if nz and i == (nz - 1) % 4 else ""
    # w0 is most likely a packed point: two int16s
    x, y = w[0] & 0xffff, (w[0] >> 16) & 0xffff
    x -= 0x10000 if x > 0x7fff else 0
    y -= 0x10000 if y > 0x7fff else 0
    print(f"  [{i}] w0=0x{w[0]:08x} w1=0x{w[1]:08x} w2=0x{w[2]:08x}"
          f"   -> as point ({x},{y}), w2 low byte = {w[2] & 0xff}{mark}")
print()
n = blk(st + off["gest_n"], 1).get(st + off["gest_n"])
ring = blk(st + off["gest"], 8)
print(f"state 0x{st:08x}   gestures seen: {n}")
if not n:
    print("no gestures (that dispatcher serves the multi-view UI, not the reader)")
    raise SystemExit(0)
base = st + off["gest"]
for i in range(8):
    v = ring.get(base + i*4, 0)
    if not v: continue
    gid = (v >> 24) & 0xff
    if gid >= 0x80: gid -= 0x100
    x, y = (v >> 12) & 0xfff, v & 0xfff
    mark = "  <- newest" if i == (n - 1) % 8 else ""
    print(f"  slot {i}: id={gid:4d}  start=({x:4d},{y:4d}){mark}")
s.close()
