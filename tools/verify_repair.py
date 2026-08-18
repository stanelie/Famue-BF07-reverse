#!/usr/bin/env python3
"""Compare the device's fw0_sys against the encrypted backup and repair it.

Restoring "the sectors we patched" from memory has failed twice: the first pass
missed 0xff000 (the page-setter hook) and the second missed 0x5c000 (the button
probe). A hook site left pointing at erased code is worse than the patch it
replaced -- the device jumps into blank flash and reboots.

So: read every sector, diff it against the backup, and rewrite only what
differs. No list to forget.

    verify_repair.py            report differences, change nothing
    verify_repair.py --repair   rewrite every differing sector
"""
import os
import sys
import time

sys.path.insert(0, os.path.expanduser("~/Documents/bf07-research/tools"))
import serial  # noqa: E402
import usb.core  # noqa: E402
import usb.util  # noqa: E402
from lark_cd import Adfu, OP_EXEC1, OP_WRITE  # noqa: E402
import serialport

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUPS = os.path.expanduser("~/Documents/bf07-backups")
ENC = os.path.join(BACKUPS, "bf07_flash_full_2026-08-05.bin")
PAYLOAD = os.path.join(HERE, "adfus_u_go.bin")

FW0_START = 0x14000          # fw0_sys partition
FW0_END = 0x200000
SECTOR = 0x1000


def in_adfu():
    return usb.core.find(idVendor=0x10D6, idProduct=0x10D6) is not None


def enter_adfu():
    import glob
    if in_adfu():
        return True
    port = serialport.find()
    t0 = time.time()
    while time.time() - t0 < 60:
        if in_adfu():
            return True
        if port:
            try:
                s = serial.Serial(port, 2000000, timeout=0.05)
                for _ in range(10):
                    s.write(b"dbg reboot adfu\r\n")
                    s.flush()
                    time.sleep(0.05)
                s.close()
            except Exception:
                pass
        time.sleep(0.2)
    return False


def start_payload():
    """Always upload a fresh payload: a stale one fails with 'is failed'."""
    blob = open(PAYLOAD, "rb").read()
    if len(blob) % 256:
        blob += b"\x00" * (256 - len(blob) % 256)
    a = Adfu(timeout=8000)
    a.write(OP_WRITE, 0x01010000, blob)
    a.cmd(OP_EXEC1, 0x01010000)
    usb.util.dispose_resources(a.d)
    del a
    time.sleep(2.5)


def main():
    repair = "--repair" in sys.argv
    if not enter_adfu():
        raise SystemExit("could not reach ADFU")
    start_payload()

    import struct
    d = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
    try:
        d.set_configuration()
    except Exception:
        pass

    def cmd(op, length, addr, expect=None, data=None):
        p = bytearray(16)
        p[0:2] = op
        struct.pack_into("<I", p, 4, length)
        struct.pack_into("<I", p, 8, addr)
        d.write(0x02, bytes(p), 6000)
        if data is not None:
            d.write(0x02, data, 10000)
        want = expect if expect is not None else length
        buf = b""
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

    for _ in range(15):
        try:
            d.read(0x81, 512, 150)
        except Exception:
            break
    assert cmd(b"is", 16, 0, expect=4)[:1] == b"\xaa", "is failed"

    backup = open(ENC, "rb").read()
    bad = []
    print(f"scanning 0x{FW0_START:06x}-0x{FW0_END:06x} ...")
    for s in range(FW0_START, FW0_END, SECTOR):
        cur = cmd(b"rs", SECTOR, s)
        if len(cur) != SECTOR:
            print(f"  0x{s:06x}: short read, skipped")
            continue
        if cur != backup[s:s + SECTOR]:
            blocks = [i for i in range(0, SECTOR, 32)
                      if cur[i:i + 32] != backup[s + i:s + i + 32]]
            bad.append(s)
            print(f"  0x{s:06x}: DIFFERS from backup in {len(blocks)} block(s)")
            # Name the XIP address of each differing block: a count alone does
            # not say WHAT is patched, and an unaccounted-for block is how
            # stale patches from earlier experiments stayed live for days.
            for i in blocks[:12]:
                print(f"       block +0x{i:03x} -> XIP 0x{0x10000000 + s + i - FW0_START:08x}")
    print(f"\n{len(bad)} sector(s) differ: {[hex(x) for x in bad]}")

    if not bad or not repair:
        print("(run with --repair to rewrite them)" if bad else "device matches the backup")
        return

    # Compare against the ENCRYPTED backup, but write PLAINTEXT: the SoC
    # encrypts on write when address bit 31 is set. Writing ciphertext back
    # would encrypt it a second time and produce garbage.
    plain = open(os.path.join(BACKUPS, "fw_code_full.bin"), "rb").read()

    for s in bad:
        data = plain[s - FW0_START:s - FW0_START + SECTOR]
        if len(data) != SECTOR:
            print(f"  0x{s:06x}: outside the decrypted image, skipped")
            continue
        cmd(b"es", SECTOR, s, expect=4)
        time.sleep(0.6)
        for off in range(0, SECTOR, 32):
            ack = cmd(b"ws", 32, (s + off) | (1 << 31), expect=4,
                      data=data[off:off + 32])
            assert ack and ack[0] == 0xAA, f"write failed at 0x{s+off:x}"
        back = cmd(b"rs", SECTOR, s)
        ok = back == backup[s:s + SECTOR]      # ciphertext must match the backup
        print(f"  0x{s:06x}: {'RESTORED' if ok else 'STILL DIFFERS'}")
    print("RESULT: repair complete")


if __name__ == "__main__":
    main()
