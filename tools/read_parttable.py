#!/usr/bin/env python3
"""Read the live partition table off the BF07 via the UART shell (`dbg mdw`).

Why this matters
----------------
The application's OTA is an A/B **mirror** scheme. Its log line
`'storage %d is xip, skip erase'` does not mean XIP is unwritable — it means the
*currently executing* partition is skipped, because the update is written to
that partition's **mirror** and the bootloader swaps on reboot.

So: if this device's table defines mirror partitions for the system image,
there is a firmware write path that needs no ADFU at all. If it does not, ADFU
stays on the critical path. This tool answers that question.

Where the table lives (from the printer at 0x10078fb0 in fw0_sys):

    10078fc6  ldr r4, =0x1801d684    ; RAM pointer to the loaded table
    10078fd0  ldr r3, =0x54504341    ; magic 'ACPT'
    10078fea  mov.w r2, #0x2e4       ; CRC covers the first 740 bytes
    10078fa2  cmp r7, #0x1e          ; 30 entries

Layout (SDK zephyr/subsys/partition/partition.c):

    struct partition_table {          struct partition_entry {   // 24 bytes
        u32 magic;      // 'ACPT'         u8  name[8];
        u16 version;                      u8  type;
        u16 table_size;                   u8  file_id;
        u16 part_cnt;                     u8  mirror_id:4, storage_id:4;
        u16 part_entry_size;              u8  flag;
        u8  reserved1[4];                 u32 offset;
        struct partition_entry parts[30]; u32 size;
        u8  Reserved2[4];                 u32 file_offset;
        u32 table_crc;                }
    };                                // total 744 bytes

Usage:
    python3 read_parttable.py [--port /dev/cu.usbserial-...]
"""

import argparse
import re
import struct
import os
import sys
import time

import serial

PORT = os.environ.get("BF07_PORT", "/dev/cu.usbserial-XXXX")
BAUD = 2000000
PTR_ADDR = 0x1801D684
MAGIC = 0x54504341          # 'ACPT'
TABLE_BYTES = 744

TYPES = {0: "INVALID", 1: "BOOT", 2: "SYSTEM", 3: "RECOVERY",
         4: "DATA", 5: "TEMP", 6: "PARAM"}
STORAGE = {0: "NOR", 1: "SD", 2: "NAND"}
FLAGS = [(1 << 0, "CRC"), (1 << 1, "ENCRYPT"), (1 << 2, "BOOTCHK")]

WORD_LINE = re.compile(rb"([0-9a-fA-F]{6,8})\s*:\s*((?:[0-9a-fA-F]{1,8}\s+)+)")


def shell(s, cmd, wait=2.5):
    s.reset_input_buffer()
    s.write((cmd + "\r\n").encode())
    s.flush()
    buf = bytearray()
    deadline = time.time() + wait
    while time.time() < deadline:
        d = s.read(4096)
        if d:
            buf.extend(d)
    return bytes(buf)


def parse_words(out, want_addr=None):
    """Return {address: word} from `mdw` output."""
    words = {}
    for m in WORD_LINE.finditer(out):
        try:
            base = int(m.group(1), 16)
        except ValueError:
            continue
        for k, tok in enumerate(m.group(2).split()):
            try:
                words[base + 4 * k] = int(tok, 16)
            except ValueError:
                pass
    return words


def read_mem(s, addr, nwords):
    """Read nwords 32-bit words starting at addr, in chunks."""
    out = {}
    per = 32
    for off in range(0, nwords, per):
        n = min(per, nwords - off)
        a = addr + off * 4
        raw = shell(s, f"dbg mdw 0x{a:x} {n}", 2.0)
        got = parse_words(raw)
        if not got:
            raw = shell(s, f"dbg mdw 0x{a:x},{n}", 2.0)
            got = parse_words(raw)
        out.update(got)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", default=PORT)
    p.add_argument("--ptr", type=lambda x: int(x, 0), default=PTR_ADDR)
    p.add_argument("--save", default=None, help="write raw table bytes here")
    args = p.parse_args()

    s = serial.Serial(args.port, BAUD, timeout=0.15)
    time.sleep(0.3)

    raw = shell(s, f"dbg mdw 0x{args.ptr:x} 1", 2.0)
    w = parse_words(raw)
    ptr = w.get(args.ptr)
    if not ptr:
        print("could not read the table pointer. raw output:")
        print(raw.decode("utf-8", "replace")[:600])
        return 1
    print(f"g_part_table pointer @ 0x{args.ptr:08x} -> 0x{ptr:08x}")
    if not (0x18000000 <= ptr < 0x18200000 or 0x10000000 <= ptr < 0x101E0000):
        print(f"  warning: 0x{ptr:08x} is outside expected RAM/XIP ranges")

    words = read_mem(s, ptr, TABLE_BYTES // 4)
    s.close()

    missing = [ptr + 4 * i for i in range(TABLE_BYTES // 4)
               if (ptr + 4 * i) not in words]
    if missing:
        print(f"  incomplete read: {len(missing)} of {TABLE_BYTES//4} words missing")
        if len(missing) > TABLE_BYTES // 8:
            return 1

    blob = b"".join(struct.pack("<I", words.get(ptr + 4 * i, 0))
                    for i in range(TABLE_BYTES // 4))
    if args.save:
        open(args.save, "wb").write(blob)
        print(f"  raw table saved to {args.save}")

    magic, version, tsize, cnt, esize = struct.unpack_from("<IHHHH", blob, 0)
    print(f"\nmagic=0x{magic:08x} {'OK (ACPT)' if magic == MAGIC else 'MISMATCH!'}"
          f"  version=0x{version:04x} table_size={tsize} "
          f"part_cnt={cnt} entry_size={esize}")
    if magic != MAGIC:
        print("  table not valid at this address; aborting")
        return 1

    print(f"\n{'id':<4}{'name':<10}{'type':<10}{'fid':<5}{'mir':<5}"
          f"{'stor':<6}{'offset':<12}{'size':<12}{'file_off':<12}flags")
    print("-" * 88)
    mirrors = {}
    types_by_fid = {}
    for i in range(min(cnt if cnt else 30, 30)):
        e = 16 + i * 24
        name = blob[e:e + 8].rstrip(b"\x00")
        typ, fid, packed, flag = blob[e + 8:e + 12]
        mir, stor = packed & 0xF, (packed >> 4) & 0xF
        off, size, foff = struct.unpack_from("<III", blob, e + 12)
        if typ == 0 and not name:
            continue
        fl = "|".join(n for b, n in FLAGS if flag & b) or "-"
        try:
            nm = name.decode()
        except UnicodeDecodeError:
            nm = name.hex()
        print(f"{i:<4}{nm:<10}{TYPES.get(typ, typ):<10}{fid:<5}"
              f"{'-' if mir == 0xF else mir:<5}{STORAGE.get(stor, stor):<6}"
              f"0x{off:<10x}0x{size:<10x}0x{foff:<10x}{fl}")
        mirrors.setdefault(fid, []).append((i, nm, mir, off, size))
        types_by_fid[fid] = TYPES.get(typ, typ)

    print("\n=== mirror analysis ===")
    for fid, lst in sorted(mirrors.items()):
        if len(lst) > 1:
            print(f"  file_id {fid}: mirrored -> "
                  + ", ".join(f"{n}(mirror {m}) @0x{o:x} size 0x{sz:x}"
                              for _, n, m, o, sz in lst))

    # The only question that matters: is the SYSTEM partition mirrored?
    # A mirrored bootloader does not help replace the code image.
    sys_fids = {fid for fid in mirrors if types_by_fid.get(fid) == "SYSTEM"}
    print()
    verdict_ok = False
    for fid in sorted(sys_fids):
        lst = mirrors[fid]
        biggest = max(sz for *_x, sz in lst)
        if len(lst) > 1:
            verdict_ok = True
            print(f"  SYSTEM file_id {fid} IS mirrored ({len(lst)} copies) — an OTA")
            print("  written to the inactive mirror could replace it without ADFU.")
        else:
            n = lst[0][1]
            print(f"  SYSTEM file_id {fid} ({n}, 0x{biggest:x} bytes) has NO mirror.")
    if not verdict_ok:
        temp = [(n, sz) for lst in mirrors.values() for _i, n, _m, _o, sz in lst
                if n.startswith("fw0_temp") or n.startswith("temp")]
        for n, sz in temp:
            print(f"  Staging partition {n} is only 0x{sz:x} bytes — far too small")
            print("  to hold a system image.")
        print()
        print("  => The A/B mirror scheme covers only the bootloader/param")
        print("     partitions. It CANNOT replace the system image.")
        print("     ADFU remains the only way to rewrite fw0_sys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
