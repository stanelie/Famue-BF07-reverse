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


MAPPED = 0x125208c0     # the two blocks, in the NOR resource's mapped view
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                     "reference", "str160_blocks.bin")


def read_plain_blocks():
    """Plaintext of the two 32-byte blocks the string spans.

    Read from YOUR device, not shipped: these 64 bytes are vendor strings. The
    NOR resource is encrypted in flash but plaintext through the mapping the
    firmware sets up at 0x12400000, so the debug shell can read it while the
    device is running -- before it is put into ADFU to be written.
    """
    if os.path.exists(CACHE):
        return open(CACHE, "rb").read()

    import glob
    import re
    import time
    import serial
    ports = glob.glob("/dev/cu.usbserial-*")
    if not ports:
        raise SystemExit("need the UART to read the current strings, or place "
                         f"64 bytes at {CACHE}")
    s = serial.Serial(ports[0], 2000000, timeout=0.5)
    time.sleep(0.3)
    words = {}
    for _ in range(4):
        s.reset_input_buffer()
        s.write(f"dbg mdw 0x{MAPPED:08x} 10\r\n".encode())
        s.flush()
        t, b = time.time(), b""
        while time.time() - t < 1.2:
            d = s.read(65536)
            if d:
                b += d
        for m in re.finditer(r"^([0-9a-f]{8}): ((?:[0-9a-f]{8} ?){1,4})",
                             b.decode("utf8", "replace"), re.M):
            base = int(m.group(1), 16)
            for i, w in enumerate(m.group(2).split()):
                words[base + i * 4] = int(w, 16)
        if len(words) >= 16:
            break
    s.close()
    out = b"".join(words.get(MAPPED + i * 4, 0).to_bytes(4, "little")
                   for i in range(16))
    if len(words) < 16:
        raise SystemExit("could not read the strings over the debug shell "
                         "(is the device running, not in ADFU?)")
    return out          # cached by the caller, and only when still pristine


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

    # BEFORE ADFU: the strings are only readable through the mapping the
    # running firmware sets up, and entering ADFU takes that away.
    plain = read_plain_blocks()

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

    if len(plain) != 64:
        raise SystemExit("expected 64 bytes of plaintext for the two blocks")

    pos = (TARGET - SECTOR) & 31                      # offset inside block A
    base = (TARGET - SECTOR) - pos                    # block A's sector offset
    if plain[pos:pos + len(NEW)] == NEW:
        print(f"  already reads '{NEW.rstrip(chr(0).encode()).decode()}' "
              f"-- nothing to do")
        return
    if plain[pos:pos + STR160_LEN] != OLD:
        raise SystemExit(f"ABORT: plaintext does not hold {OLD!r} at +0x{pos:x}")
    if not os.path.exists(CACHE):                     # pristine: worth keeping
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        open(CACHE, "wb").write(plain)

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
