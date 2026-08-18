#!/usr/bin/env python3
"""Compare flash-controller registers: ADFU (SPI command mode) vs running (XIP).

The difference is what the boot ROM configures to make the memory-mapped,
decrypting read path work -- which is what the ADFU payload would have to set up
for `rm 0x10000000` to return the firmware instead of stale cache lines.
"""
import glob, re, time, serial
import serialport

BLOCKS = [(0x40028000, "SPI-NOR controller (JEDEC 0x85 seen here)"),
          (0x40038000, "?"), (0x40054000, "?"), (0x40068000, "?")]

s = serialport.open(timeout=0.5)
time.sleep(0.2)

def blk(a, words):
    for _ in range(4):
        s.reset_input_buffer()
        s.write(f"dbg mdw 0x{a:08x} {words:x}\r\n".encode()); s.flush()
        t, b = time.time(), b""
        while time.time() - t < 1.2:
            d = s.read(65536)
            if d: b += d
            elif b: break
        o = {}
        for m in re.finditer(r"^([0-9a-f]{8}): ((?:[0-9a-f]{8} ?){1,4})",
                             b.decode("utf8", "replace"), re.M):
            base = int(m.group(1), 16)
            for i, w in enumerate(m.group(2).split()): o[base + i*4] = int(w, 16)
        if len(o) >= words * 0.8: return o
    return {}

adfu = {}
for line in open("/tmp/adfu_regs.txt"):
    a, v = line.split()
    adfu[int(a, 16)] = int(v, 16)

for base, note in BLOCKS:
    live = blk(base, 16)
    if not live:
        print(f"--- 0x{base:08x}: no read (shell up?)"); continue
    print(f"--- 0x{base:08x}  {note}")
    for i in range(16):
        a = base + i*4
        l, r = live.get(a), adfu.get(a)
        if l is None or r is None: continue
        mark = "   <-- DIFFERS" if l != r else ""
        print(f"   +0x{i*4:02x}  running {l:08x}   adfu {r:08x}{mark}")
s.close()
