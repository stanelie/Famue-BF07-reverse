"""Flash the 11-lines-per-page patch: three sectors of fw0_sys.

Same proven recipe as patch2.py -- plaintext in, address bit 31 set so the SoC
encrypts on write, 128 separate 32-byte writes per sector, verify every block
against the encrypted backup.
"""
import struct
import sys
import time

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("BF07_TOOLS", _HERE))
import serial
import usb.core
import usb.util
from lark_cd import Adfu, OP_EXEC1, OP_WRITE
import serialport

# Where mkflash.py wrote the sector images (BF07_WORK, else alongside this file).
SPD = os.environ.get("BF07_WORK", _HERE) + "/"
OUT = SPD + "outbase/"
# The ENCRYPTED backup of your own device, used to verify every written block.
# Never redistributed -- see docs/flashing.md.
dump = open(os.environ["BF07_BACKUP"], "rb").read()

JOBS = [
    (0x5D000, OUT + "sector_05d000.bin", [0x3a0], True),
    (0x5E000, OUT + "sector_05e000.bin", [0x1e0, 0x220, 0x280], True),
    (0x60000, OUT + "sector_060000.bin", [0x0], True),
    (0x1E7000, OUT + "sector_1e7000.bin", [0x0, 0x20, 0x40, 0x60, 0x80, 0xa0, 0xc0, 0xe0, 0x100, 0x120, 0x140, 0x160, 0x180, 0x1a0, 0x1c0, 0x1e0, 0x200, 0x220, 0x240, 0x260, 0x280, 0x2a0, 0x2c0, 0x2e0, 0x300, 0x320, 0x340, 0x360, 0x380, 0x3a0, 0x3c0, 0x3e0, 0x400, 0x420, 0x440, 0x460, 0x480, 0x4a0, 0x4c0, 0x4e0, 0x500, 0x520, 0x540, 0x560, 0x580, 0x5a0, 0x5c0, 0x5e0, 0x600, 0x620, 0x640, 0x660, 0x680, 0x6a0, 0x6c0, 0x6e0, 0x700, 0x720, 0x740, 0x760, 0x780, 0x7a0, 0x7c0, 0x7e0, 0x800, 0x820, 0x840, 0x860, 0x880, 0x8a0, 0x8c0, 0x8e0, 0x900, 0x920, 0x940, 0x960, 0x980, 0x9a0, 0x9c0, 0x9e0, 0xa00, 0xa20, 0xa40, 0xa60, 0xa80, 0xaa0, 0xac0, 0xae0, 0xb00, 0xb20, 0xb40, 0xb60, 0xb80, 0xba0, 0xbc0, 0xbe0, 0xc00, 0xc20, 0xc40, 0xc60, 0xc80, 0xca0, 0xcc0, 0xce0, 0xd00, 0xd20, 0xd40, 0xd60, 0xd80, 0xda0, 0xdc0, 0xde0, 0xe00, 0xe20, 0xe40, 0xe60, 0xe80, 0xea0, 0xec0, 0xee0, 0xf00, 0xf20, 0xf40, 0xf60, 0xf80, 0xfa0, 0xfc0, 0xfe0], False),
    (0x1E8000, OUT + "sector_1e8000.bin", [0x0, 0x20, 0x40, 0x60, 0x80, 0xa0, 0xc0, 0xe0, 0x100, 0x120, 0x140, 0x160, 0x180, 0x1a0, 0x1c0, 0x1e0, 0x200, 0x220, 0x240, 0x260, 0x280, 0x2a0, 0x2c0, 0x2e0, 0x300, 0x320, 0x340, 0x360, 0x380, 0x3a0, 0x3c0, 0x3e0, 0x400, 0x420, 0x440, 0x460, 0x480, 0x4a0, 0x4c0], False),
    (0x5F000, SPD + "stock/sector_05f000.bin", [], True),
    (0xFF000, SPD + "stock/sector_0ff000.bin", [], True),
]


def payload_alive():
    dv = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
    if dv is None:
        return False
    # No set_configuration() -- see the note at the main device open. Probing a
    # LIVE payload with it is what made this detector destroy the thing it was
    # detecting, so it always reported dead and the caller always re-uploaded.
    try:
        dv.get_active_configuration()
    except usb.core.USBError:
        dv.set_configuration()
    try:
        # Ask `is` and require the payload's 0xAA. The old test sent `ic` and
        # accepted ANY reply, but the BOOT ROM also replies to a malformed
        # 16-byte packet -- it speaks 31-byte CBWs -- so a bare ROM read as
        # "payload already running", the upload was skipped, and the `is`
        # assert below then failed. Latent until build.sh stopped forcing a
        # re-upload; check WHAT came back, not that something did.
        p = bytearray(16)
        p[0:2] = b'is'
        struct.pack_into("<I", p, 4, 16)
        struct.pack_into("<I", p, 8, 0)
        dv.write(0x02, bytes(p), 1500)
        return bytes(dv.read(0x81, 4, 1500))[:1] == b'\xaa'
    except Exception:
        return False
    finally:
        usb.util.dispose_resources(dv)


ALREADY = payload_alive()
if not ALREADY and not usb.core.find(idVendor=0x10D6, idProduct=0x10D6):
    s = serialport.open(timeout=0.2)
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
# Do NOT set_configuration() here. The payload has just taken over USB, and on
# Linux the kernel already configured the device at enumeration -- so this call
# is redundant AND destructive: the redundant SET_CONFIGURATION resets the
# endpoint state the payload owns, and every raw packet after it fails with
# EIO ("is failed"). macOS tolerated it, which is why this only appeared on the
# move to Linux. Configure only if nothing has configured it yet.
try:
    d.get_active_configuration()
except usb.core.USBError:
    d.set_configuration()


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

print("""
!!  DO NOT DISCONNECT POWER until this prints RESULT. From the first erase
!!  until the verify passes, fw0_sys is incomplete. A failed verify is
!!  harmless -- run it again. Losing power is not: the GOTO_ADFU flag that
!!  lets you back in does NOT survive a power cycle (measured), and mbrec
!!  boots fw0_sys without checking it. There is no automatic recovery.
""", flush=True)
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

print("\n!!  STAY IN ADFU -- do not power off; re-run to retry.\n"
      if not ok else "verified -- safe to disconnect.", flush=True)
print("RESULT:", "outbase FLASHED (5344 bytes of C)" if ok else "PROBLEM",
      flush=True)
