"""Flash the 11-lines-per-page patch: three sectors of fw0_sys.

Same proven recipe as patch2.py -- plaintext in, address bit 31 set so the SoC
encrypts on write, 128 separate 32-byte writes per sector, verify every block
against the encrypted backup.
"""
import struct
import sys
import time

sys.path.insert(0, '$BF07_ROOT/tools')
import serial
import usb.core
import usb.util
from lark_cd import Adfu, OP_EXEC1, OP_WRITE

SPD = "$BF07_WORK/"
OUT = SPD + "outip2/"
dump = open("$BF07_BACKUPS/bf07_flash_full_2026-08-05.bin", "rb").read()

JOBS = [
    (0x5D000, OUT + "sector_05d000.bin", [0x260, 0x280, 0x2C0, 0x2E0, 0x300, 0x320, 0x340, 0x440, 0x4C0, 0x540, 0x560, 0x660, 0x800, 0xE20, 0xEA0], True),
    (0x5E000, OUT + "sector_05e000.bin", [0x280, 0x300, 0x560], True),
    (0x5F000, OUT + "sector_05f000.bin", [0xA80, 0xAA0, 0xB80], True),
    (0x1E7000, OUT + "sector_1e7000.bin", [0x0, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0], False),
]


def payload_alive():
    dv = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
    if dv is None:
        return False
    try:
        dv.set_configuration()
    except Exception:
        pass
    try:
        p = bytearray(16)
        p[0:2] = b'ic'
        struct.pack_into("<I", p, 4, 64)
        struct.pack_into("<I", p, 8, 0)
        dv.write(0x02, bytes(p), 1500)
        return len(bytes(dv.read(0x81, 64, 1500))) > 0
    except Exception:
        return False
    finally:
        usb.util.dispose_resources(dv)


ALREADY = payload_alive()
if not ALREADY and not usb.core.find(idVendor=0x10D6, idProduct=0x10D6):
    s = serial.Serial("/dev/cu.usbserial-XXXX", 2000000, timeout=0.2)
    s.write(b"dbg reboot adfu\r\n")
    s.flush()
    time.sleep(1.2)
    s.close()
    for _ in range(15):
        time.sleep(1)
        if usb.core.find(idVendor=0x10D6, idProduct=0x10D6):
            break

if not usb.core.find(idVendor=0x10D6, idProduct=0x10D6):
    print("RESULT: device never reached ADFU - nothing written", flush=True)
    sys.exit(1)
print("in ADFU (payload already running)" if ALREADY else "in ADFU", flush=True)

if not ALREADY:
    blob = open(SPD + "adfus_u_go.bin", "rb").read()
    if len(blob) % 256:
        blob += b"\x00" * (256 - len(blob) % 256)
    a = Adfu(timeout=8000)
    print("cd13:", a.write(OP_WRITE, 0x01010000, blob),
          "cd20:", a.cmd(OP_EXEC1, 0x01010000), flush=True)
    usb.util.dispose_resources(a.d)
    del a
    time.sleep(2.5)

d = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
try:
    d.set_configuration()
except Exception:
    pass


def drain(n=15):
    for _ in range(n):
        try:
            d.read(0x81, 512, 150)
        except Exception:
            return


def cmd(op, l, ad, expect=None, data=None):
    p = bytearray(16)
    p[0:2] = op
    struct.pack_into("<I", p, 4, l)
    struct.pack_into("<I", p, 8, ad)
    d.write(0x02, bytes(p), 6000)
    if data is not None:
        d.write(0x02, data, 10000)
    want = expect if expect is not None else l
    buf = b''
    while len(buf) < want:
        try:
            c = bytes(d.read(0x81, min(want - len(buf), 512), 3000))
        except Exception:
            break
        if not c:
            break
        buf += c
        if want <= 16 and len(buf) >= 4:
            break
    return buf


drain()
assert cmd(b'is', 16, 0, expect=4)[:1] == b'\xaa', "is failed"

ok = True
for FLASH, path, expect_blocks, full in JOBS:
    data = open(path, "rb").read()
    assert len(data) == 0x1000, f"{path} is not 4096 bytes"
    # which 32-byte blocks actually need writing
    todo = (range(0, 0x1000, 32) if full
            else [b for b in range(0, 0x1000, 32)
                  if data[b:b + 32] != b"\xff" * 32])

    cur = cmd(b'rs', 0x1000, FLASH)
    if len(cur) != 0x1000:
        print(f"0x{FLASH:x}: short read {len(cur)} - SKIPPING", flush=True)
        ok = False
        continue
    pre = [i for i in range(0, 0x1000, 32)
           if cur[i:i + 32] != dump[FLASH + i:FLASH + i + 32]]
    print(f"0x{FLASH:x}: pre-state differs from backup in {len(pre)} "
          f"block(s) {[hex(x) for x in pre]}", flush=True)

    cmd(b'es', 0x1000, FLASH, expect=4)
    time.sleep(0.6)
    assert cmd(b'rs', 64, FLASH) == b'\xff' * 64, "erase failed"

    for off in todo:
        ack = cmd(b'ws', 32, (FLASH + off) | (1 << 31), expect=4,
                  data=data[off:off + 32])
        assert ack and ack[0] == 0xAA, f"write fail at +0x{off:x}"
    time.sleep(0.5)

    back = cmd(b'rs', 0x1000, FLASH)
    diff = [i for i in range(0, 0x1000, 32)
            if back[i:i + 32] != dump[FLASH + i:FLASH + i + 32]]
    good = diff == expect_blocks
    ok = ok and good
    print(f"0x{FLASH:x}: differing blocks {[hex(x) for x in diff]}  "
          f"expected {[hex(x) for x in expect_blocks]}  "
          f"{'OK' if good else 'MISMATCH'}", flush=True)

print("RESULT:", "11-LINE BUILD v2 FLASHED" if ok else "PROBLEM",
      flush=True)
