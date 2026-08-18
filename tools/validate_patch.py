#!/usr/bin/env python3
"""Validate `install --patch` end to end at the CURRENT reader size (3 sectors).

Sequence, all over ADFU:
  1. capture the WORKING flash (installed via build.sh) as the reference
  2. restore those sectors to stock
  3. install from the patch file, exactly as bf07.py install --patch does
  4. compare every sector byte for byte against the reference

The device ends where it started -- with the working reader -- because step 3
should reproduce step 1's bytes exactly. If it does not, the mismatch is printed
and the reference is written back.
"""
import os, sys, glob, time, struct, pickle
sys.path.insert(0, os.environ.get(
    "BF07_TOOLS", os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BF07_ROOT", os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
import serial, usb.core, usb.util
from lark_cd import Adfu, OP_EXEC1, OP_WRITE
from mkpatch import load_patch
import patchset
import serialport

SEC = 0x1000
STOCK = os.environ["BF07_BACKUP"]           # your own encrypted backup
PATCH = "/tmp/reader-patch.bin"

def in_adfu(): return usb.core.find(idVendor=0x10D6, idProduct=0x10D6) is not None

def enter():
    if in_adfu(): return
    s = serialport.open(timeout=0.1)
    for _ in range(120):
        s.write(b"dbg reboot adfu\r\n"); s.flush(); time.sleep(0.05)
        if in_adfu(): break
    s.close()
    for _ in range(40):
        if in_adfu(): return
        time.sleep(0.25)

def payload():
    blob = open(os.environ.get("BF07_PAYLOAD", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "reference", "adfus_u_go.bin")), "rb").read()
    if len(blob) % 256: blob += b"\0" * (256 - len(blob) % 256)
    a = Adfu(timeout=8000)
    a.write(OP_WRITE, 0x01010000, blob); a.cmd(OP_EXEC1, 0x01010000)
    usb.util.dispose_resources(a.d); del a; time.sleep(2.5)

enter()
if not in_adfu(): raise SystemExit("no ADFU")
payload()
d = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
try: d.set_configuration()
except usb.core.USBError: pass
def drain():
    for _ in range(15):
        try: d.read(0x81, 512, 60)
        except usb.core.USBError: return
def cmd(op, ln, addr, expect=None, data=None, tries=3):
    for _ in range(tries):
        try:
            drain(); p = bytearray(16); p[0:2] = op
            struct.pack_into("<I", p, 4, ln); struct.pack_into("<I", p, 8, addr)
            d.write(0x02, bytes(p), 6000)
            if data is not None: d.write(0x02, data, 10000)
            want = expect or ln; b = b""
            while len(b) < want:
                c = bytes(d.read(0x81, min(want - len(b), 512), 3000))
                if not c: break
                b += c
                if want <= 16 and len(b) >= 4: break
            return b
        except usb.core.USBError:
            time.sleep(0.4)
    return b""
def rs(a, n=SEC):
    out = b""
    while len(out) < n:
        c = min(SEC, n - len(out)); part = cmd(b"rs", c, a + len(out))
        if len(part) != c: break
        out += part
    return out
assert cmd(b"is", 16, 0, expect=4)[:1] == b"\xaa", "storage bind failed"

plain = open(os.environ["BF07_PLAIN"], "rb").read()   # decrypted image
targets = sorted(patchset.build(plain))
print(f"sectors involved: {[hex(a) for a in targets]}", flush=True)

print("1. capturing the working flash as reference", flush=True)
ref = {a: rs(a) for a in targets}
pickle.dump(ref, open("/tmp/ref_working.pkl", "wb"))
for a in targets:
    if len(ref[a]) != SEC: raise SystemExit(f"short read at 0x{a:06x}")

stock = open(STOCK, "rb").read()
print("2. restoring those sectors to stock", flush=True)
for a in targets:
    cmd(b"es", SEC, a, expect=4); time.sleep(0.6)
    for off in range(0, SEC, 32):
        blk = stock[a+off:a+off+32]
        if blk != b"\xff"*32: cmd(b"ws", 32, a+off, expect=4, data=blk)

print("3. installing from the patch file", flush=True)
reader, blocks, _ = load_patch(open(PATCH, "rb").read())
by = {}
for addr, data in blocks: by.setdefault(addr & ~0xfff, {})[addr & 0xfff] = data
for addr, data in reader:
    cmd(b"es", SEC, addr, expect=4); time.sleep(0.6)
    for off in range(0, SEC, 32):
        if data[off:off+32] != b"\xff"*32:
            cmd(b"ws", 32, (addr+off) | (1 << 31), expect=4, data=data[off:off+32])
for sec in sorted(by):
    cur = rs(sec)
    cmd(b"es", SEC, sec, expect=4); time.sleep(0.6)
    ed = by[sec]
    for off in range(0, SEC, 32):
        if off in ed: cmd(b"ws", 32, (sec+off) | (1 << 31), expect=4, data=ed[off])
        elif cur[off:off+32] != b"\xff"*32: cmd(b"ws", 32, sec+off, expect=4, data=cur[off:off+32])

print("4. byte-for-byte comparison against the working reference", flush=True)
allok = True
for a in targets:
    now = rs(a)
    diff = [off for off in range(0, SEC, 32) if now[off:off+32] != ref[a][off:off+32]]
    print(f"   0x{a:06x}: {'identical' if not diff else str(len(diff)) + ' block(s) DIFFER ' + str([hex(x) for x in diff[:5]])}", flush=True)
    if diff: allok = False

if allok:
    print("\n*** PATCH INSTALL == WORKING INSTALL (3 sectors) ***", flush=True)
else:
    print("\nMISMATCH -- writing the working reference back", flush=True)
    for a in targets:
        cmd(b"es", SEC, a, expect=4); time.sleep(0.6)
        for off in range(0, SEC, 32):
            blk = ref[a][off:off+32]
            if blk != b"\xff"*32: cmd(b"ws", 32, a+off, expect=4, data=blk)
        back = rs(a)
        print(f"   0x{a:06x}: {'restored' if back == ref[a] else 'RESTORE FAILED'}", flush=True)
print("\nrebooting the device", flush=True)
def wm(a, v):
    drain(); p = bytearray(16); p[0:2] = b"wm"
    struct.pack_into("<I", p, 4, 4); struct.pack_into("<I", p, 8, a)
    try:
        d.write(0x02, bytes(p), 3000); d.write(0x02, struct.pack("<I", v), 3000)
        d.read(0x81, 4, 2000)
    except usb.core.USBError: pass
wm(0x4000c03c, 0x42520000); wm(0x4000c020, 0x5f)
print("done", flush=True)
