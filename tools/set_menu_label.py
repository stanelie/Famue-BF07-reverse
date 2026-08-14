#!/usr/bin/env python3
"""Rewrite one string in the NOR UI resource, so a menu row can be relabelled.

The font menu's labels are not in the firmware: each menu row carries a 32-bit
id, that id selects a 36-byte record in `common.sty`, and the record's +0x08
field is an index into the per-language string file (`common.eng` and friends).
Six font labels live at STR157..STR162, and one of them -- "Fangsong Small
Font", id 0xf40f37ea -- is referenced by NO menu row, exactly like the fang16
font index that has no row. Pointing our row at that id (a word patch in
fw0_sys) and rewriting its string here gives a properly labelled entry without
inventing an id or reversing the id hash.

Only the 32-byte blocks that actually change are re-encrypted; every other block
in the sector is written back as its own ciphertext, so nothing else in the
sector has to be understood -- the same idiom `bf07.py install --patch` uses.

    set_menu_label.py --dry-run
    set_menu_label.py --write
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bf07                                                     # noqa: E402

SECTOR_SIZE = 0x1000
NOR_SDFS = 0x299000            # NOR resource container, mapped at 0x12400000
COMMON_ENG = 0x11eae0          # within the container
STR160_OFF = 0x1df5            # entry #159, name STR160, len 20
STR160_LEN = 20

TARGET = NOR_SDFS + COMMON_ENG + STR160_OFF
SECTOR = TARGET & ~(SECTOR_SIZE - 1)
OLD = b"Fangsong Small Font\x00"
NEW = b"Custom\x00"

BACKUPS = os.environ.get(
    "BF07_BACKUPS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 os.pardir, "bf07-backups"))


def newest_backup():
    import glob
    c = sorted(glob.glob(os.path.join(BACKUPS, "bf07_flash_full_*.bin")))
    if not c:
        raise SystemExit(f"no full backup in {BACKUPS} -- take one first")
    return c[-1]


def to_adfu():
    """macOS cannot send the ADFU switch itself -- the kernel owns the device's
       only interface -- so go in over the UART, the way the flasher does."""
    import glob
    import time
    import serial
    import usb.core
    if usb.core.find(idVendor=0x10D6, idProduct=0x10D6):
        return
    ports = glob.glob("/dev/cu.usbserial-*")
    if not ports:
        raise SystemExit("no USB serial adapter, and macOS cannot switch to "
                         "ADFU over USB -- connect the UART")
    s = serial.Serial(ports[0], 2000000, timeout=0.2)
    s.write(b"dbg reboot adfu\r\n")
    s.flush()
    time.sleep(1.2)
    s.close()
    for _ in range(15):
        time.sleep(1)
        if usb.core.find(idVendor=0x10D6, idProduct=0x10D6):
            return
    raise SystemExit("device never reached ADFU")


def main():
    write = "--write" in sys.argv
    ref = open(newest_backup(), "rb").read()
    if len(ref) != bf07.FLASH_SIZE:
        raise SystemExit(f"backup is {len(ref)} bytes")

    print(f"string at flash 0x{TARGET:06x}, sector 0x{SECTOR:06x}, "
          f"offset 0x{TARGET - SECTOR:03x}")

    to_adfu()
    d = bf07.connect()
    cur = d.read(SECTOR, SECTOR_SIZE)

    # The sector must match the backup everywhere EXCEPT the two blocks this
    # tool owns, so a re-run after a bad program is allowed but a sector that
    # drifted anywhere else still stops us.
    stock = ref[SECTOR:SECTOR + SECTOR_SIZE]
    pos0 = (TARGET - SECTOR) & 31
    base0 = (TARGET - SECTOR) - pos0
    drift = [o for o in range(0, SECTOR_SIZE, 32)
             if cur[o:o + 32] != stock[o:o + 32] and not base0 <= o < base0 + 64]
    if drift and "--force" not in sys.argv:
        raise SystemExit(f"ABORT: sector differs from the backup outside our "
                         f"two blocks, at {[hex(o) for o in drift[:8]]} "
                         f"-- pass --force to rewrite the whole sector from "
                         f"the backup (safe: every byte is sourced from it)")
    if drift:
        print(f"  --force: {len(drift)} drifted block(s) will be restored "
              f"from the backup")
    ours = [o for o in (base0, base0 + 32) if cur[o:o + 32] != stock[o:o + 32]]
    print(f"  sector matches the backup outside our blocks"
          + (f" ({len(ours)} of ours already written)" if ours else ""))

    # Plaintext of the two blocks the string spans, read from the mapped view
    # while the device was running (peek at 0x125208c0).
    plain = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              os.pardir, "reference", "str160_blocks.bin"),
                 "rb").read()
    if len(plain) != 64:
        raise SystemExit("expected 64 bytes of plaintext for the two blocks")

    pos = (TARGET - SECTOR) & 31                      # offset inside block A
    base = (TARGET - SECTOR) - pos                    # block A's sector offset
    if plain[pos:pos + STR160_LEN] != OLD:
        raise SystemExit(f"ABORT: plaintext does not hold {OLD!r} at +0x{pos:x}")

    edited = bytearray(plain)
    edited[pos:pos + STR160_LEN] = NEW + b"\x00" * (STR160_LEN - len(NEW))
    old_txt = OLD.rstrip(b"\x00").decode()
    new_txt = NEW.rstrip(b"\x00").decode()
    print(f"  '{old_txt}' -> '{new_txt}'")

    if not write:
        print("dry run -- pass --write to commit")
        return

    # Untouched blocks come from the BACKUP, not from the device: re-running
    # after a bad program must not fossilise whatever the bad program left.
    #
    # Encrypted blocks go FIRST, and every block is read back. The first
    # write_plain issued after a run of write_raw is ACKed but never programmed
    # -- that block stayed 0xFF, and the OTFD decrypting erased bytes is what
    # put garbage in the menu. "Differs from stock" hid it, because unwritten
    # 0xFF differs from stock too. Verify what a block IS, not that it changed.
    d.erase(SECTOR)
    for attempt in range(4):
        todo = [o for o in (base, base + 32)
                if d.read(SECTOR + o, 32) == b"\xff" * 32]
        if not todo:
            break
        for o in todo:
            d.write_plain(SECTOR + o, bytes(edited[o - base:o - base + 32]))
    else:
        raise SystemExit("encrypted blocks would not program")

    for off in range(0, SECTOR_SIZE, 32):
        if not base <= off < base + 64:
            d.write_raw(SECTOR + off, stock[off:off + 32])

    back = d.read(SECTOR, SECTOR_SIZE)
    bad = [hex(o) for o in range(0, SECTOR_SIZE, 32)
           if back[o:o + 32] != stock[o:o + 32] and not base <= o < base + 64]
    blank = [hex(o) for o in (base, base + 32)
             if back[o:o + 32] == b"\xff" * 32]
    if bad:
        raise SystemExit(f"blocks outside ours came back wrong: {bad[:8]}")
    if blank:
        raise SystemExit(f"our blocks are still unprogrammed: {blank}")
    print("  every other block restored byte-exact; both of ours programmed")
    print("written -- reboot and check the menu")


if __name__ == "__main__":
    main()
