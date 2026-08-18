#!/usr/bin/env python3
"""Diff the device's LIVE code against the stock image, in plaintext.

`dbg mdw` reads through the XIP mapping, which is decrypted -- so patches can be
identified without ADFU and without decrypting flash. A sector-level count says
only THAT something differs; this says what.
"""
import glob, re, sys, time, serial

import os as _os
import serialport
# The stock image comes from YOUR OWN device (see docs/firmware-extraction.md);
# it is never redistributed here. Point BF07_STOCK at your dump.
STOCK = _os.environ.get("BF07_STOCK", "fw_code_full.bin")
img = open(STOCK, "rb").read()          # img[0] == XIP 0x10000000

s = serialport.open(timeout=0.5)
time.sleep(0.2)

def block(a, words):
    for _ in range(4):
        s.reset_input_buffer()
        s.write(f"dbg mdw 0x{a:08x} {words:x}\r\n".encode()); s.flush()
        t, b = time.time(), b""
        while time.time() - t < 1.5:
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

for lo, hi, tag in [(0x10049000, 0x1004a000, "sector 0x5d000"),
                    (0x1004a000, 0x1004b000, "sector 0x5e000"),
                    (0x1004c000, 0x1004c400, "sector 0x60000 (head)")]:
    print(f"--- {tag}  {lo:#x}-{hi:#x}")
    got = {}
    for a in range(lo, hi, 0x400):
        got.update(block(a, 0x100))
    # A silent UART must never read as "identical to stock". Reading nothing and
    # concluding nothing-differs is a mistake this project has made three times;
    # the run aborts instead.
    want_words = (hi - lo) // 4
    if len(got) < want_words * 0.5:
        raise SystemExit(f"ABORT: only {len(got)}/{want_words} words read from "
                         f"{tag} -- is the device booted with the shell up?")
    runs, cur = [], None
    for a in range(lo, hi, 4):
        live = got.get(a)
        if live is None: continue
        want = int.from_bytes(img[a-0x10000000:a-0x10000000+4], "little")
        if live != want:
            if cur and a == cur[-1] + 4: cur.append(a)
            else:
                if cur: runs.append(cur)
                cur = [a]
    if cur: runs.append(cur)
    if not runs: print("   identical to stock")
    for r in runs:
        print(f"   PATCHED 0x{r[0]:08x}..0x{r[-1]+3:08x}  ({len(r)} word(s))")
        for a in r:
            want = int.from_bytes(img[a-0x10000000:a-0x10000000+4], "little")
            print(f"     0x{a:08x}: stock 0x{want:08x}  ->  live 0x{got[a]:08x}")
s.close()
