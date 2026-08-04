#!/usr/bin/env python3
"""Build and verify Famue BF07 `ota.bin` images.

Implements the exact container format and validation order recovered from the
firmware (see OTA_FORMAT.md). `verify()` mirrors what the device's
ota_image_check / ota_image_check_data / ota_image_check_file do, so an image
that passes here should pass on-device.
"""
import struct
import sys
import zlib

MAGIC = 0x41544F41            # "AOTA"
HEADER_SIZE = 0x400
DIR_OFFSET = 0x200
DIR_ENTRY_SIZE = 0x20
MAX_FILES = (HEADER_SIZE - DIR_OFFSET) // DIR_ENTRY_SIZE   # 16
MAX_TOTAL = 0x3000000
NAME_LEN = 12


def crc32(data, init=0):
    """Matches the firmware routine at 0x1007f864 (plain zlib CRC-32)."""
    return zlib.crc32(data, init) & 0xFFFFFFFF


class OtaError(Exception):
    pass


def build(files, data_offset=HEADER_SIZE):
    """files: list of (name, bytes). Returns the complete ota.bin image."""
    if len(files) > MAX_FILES:
        raise OtaError(f"too many files: {len(files)} > {MAX_FILES}")

    payload = bytearray()
    entries = []
    for name, data in files:
        nb = name.encode()
        if len(nb) >= NAME_LEN:
            raise OtaError(f"name too long (max {NAME_LEN-1}): {name}")
        # keep each file 4-byte aligned within the payload
        while len(payload) % 4:
            payload.append(0)
        off = data_offset + len(payload)
        payload += data
        entries.append((nb, off, len(data), crc32(data)))

    total_size = data_offset + len(payload)
    if total_size > MAX_TOTAL:
        raise OtaError(f"image too large: 0x{total_size:x} > 0x{MAX_TOTAL:x}")

    img = bytearray(data_offset + len(payload))

    # directory
    for i, (nb, off, length, ck) in enumerate(entries):
        base = DIR_OFFSET + i * DIR_ENTRY_SIZE
        img[base:base + len(nb)] = nb
        struct.pack_into("<I", img, base + 0x10, off)
        struct.pack_into("<I", img, base + 0x14, length)
        struct.pack_into("<I", img, base + 0x18, 0)
        struct.pack_into("<I", img, base + 0x1C, ck)

    img[data_offset:] = payload

    # header fields
    struct.pack_into("<I", img, 0x00, MAGIC)
    struct.pack_into("<H", img, 0x0A, HEADER_SIZE)
    struct.pack_into("<H", img, 0x0C, len(entries))
    struct.pack_into("<H", img, 0x12, data_offset)
    struct.pack_into("<I", img, 0x14, total_size)
    struct.pack_into("<I", img, 0x18, crc32(bytes(img[data_offset:total_size])))

    # header CRC last: covers [0x08, 0x400)
    struct.pack_into("<I", img, 0x04, crc32(bytes(img[0x08:HEADER_SIZE])))
    return bytes(img)


def parse_header(img):
    if len(img) < HEADER_SIZE:
        raise OtaError("image shorter than header")
    magic, hcrc = struct.unpack_from("<II", img, 0)
    hsize, fcount = struct.unpack_from("<HH", img, 0x0A)
    doff, = struct.unpack_from("<H", img, 0x12)
    total, dcrc = struct.unpack_from("<II", img, 0x14)
    return dict(magic=magic, header_crc=hcrc, header_size=hsize,
                file_count=fcount, data_offset=doff,
                total_size=total, data_checksum=dcrc)


def parse_dir(img, count):
    out = []
    for i in range(count):
        b = DIR_OFFSET + i * DIR_ENTRY_SIZE
        raw = img[b:b + NAME_LEN]
        name = raw.split(b"\0")[0].decode(errors="replace")
        off, length, unk, ck = struct.unpack_from("<IIII", img, b + 0x10)
        out.append(dict(name=name, offset=off, length=length,
                        unknown=unk, checksum=ck))
    return out


def verify(img, verbose=True):
    """Replicate the device-side checks. Returns list of problems (empty == OK)."""
    problems = []

    def say(m):
        if verbose:
            print(m)

    h = parse_header(img)
    say(f"magic          : 0x{h['magic']:08x} "
        f"({'OK' if h['magic'] == MAGIC else 'BAD, expected AOTA'})")
    if h["magic"] != MAGIC:
        problems.append("wrong magic")

    say(f"header_size    : 0x{h['header_size']:x} "
        f"({'OK' if h['header_size'] == HEADER_SIZE else 'BAD, must be 0x400'})")
    if h["header_size"] != HEADER_SIZE:
        problems.append("invalid header size")

    say(f"total_size     : 0x{h['total_size']:x} (actual file 0x{len(img):x})")
    if h["total_size"] > MAX_TOTAL:
        problems.append("total_size exceeds 0x3000000")
    if h["total_size"] > len(img):
        problems.append("total_size larger than actual image")

    calc = crc32(img[0x08:HEADER_SIZE])
    say(f"header_crc     : stored 0x{h['header_crc']:08x} calc 0x{calc:08x} "
        f"({'OK' if calc == h['header_crc'] else 'MISMATCH'})")
    if calc != h["header_crc"]:
        problems.append("bad head crc")
    if h["header_crc"] == 0xFFFFFFFF:
        problems.append("header_crc is 0xFFFFFFFF (rejected)")

    dcalc = crc32(img[h["data_offset"]:h["total_size"]])
    say(f"data_checksum  : stored 0x{h['data_checksum']:08x} calc 0x{dcalc:08x} "
        f"({'OK' if dcalc == h['data_checksum'] else 'MISMATCH'})")
    if dcalc != h["data_checksum"]:
        problems.append("bad data crc")

    say(f"file_count     : {h['file_count']}")
    if h["file_count"] > MAX_FILES:
        problems.append(f"file_count > {MAX_FILES}")

    entries = parse_dir(img, min(h["file_count"], MAX_FILES))
    say("\nfiles:")
    names = set()
    for e in entries:
        data = img[e["offset"]:e["offset"] + e["length"]]
        ok = len(data) == e["length"] and crc32(data) == e["checksum"]
        say(f"  {e['name']:<14s} off=0x{e['offset']:06x} len={e['length']:<8d} "
            f"crc=0x{e['checksum']:08x} {'OK' if ok else 'BAD'}")
        if not ok:
            problems.append(f"file {e['name']} checksum error")
        names.add(e["name"])

    if "ota.xml" not in names:
        problems.append("missing ota.xml manifest")
        say("\n  !! no ota.xml -> device logs 'cannot get manifest file in image'")

    return problems


def _selftest():
    manifest = b"""<?xml version="1.0" encoding="utf-8"?>
<ota>
  <firmware_version>
    <version_code>2</version_code>
    <version_res>1</version_res>
    <version_name>1.0.1</version_name>
    <board_name>BF07</board_name>
  </firmware_version>
  <partitionsNum>1</partitionsNum>
  <partitions>
    <partition>
      <type>SYSTEM</type>
      <file_id>4</file_id>
      <file_name>sys.bin</file_name>
      <file_size>%d</file_size>
      <checksum>0x%08x</checksum>
    </partition>
  </partitions>
</ota>
"""
    sysbin = bytes(range(256)) * 40          # stand-in payload
    manifest = manifest % (len(sysbin), crc32(sysbin))

    img = build([("ota.xml", manifest), ("sys.bin", sysbin)])
    print(f"built image: {len(img)} bytes\n")
    problems = verify(img)
    print("\nSELF-TEST:", "PASS (image satisfies all device-side checks)"
          if not problems else f"FAIL {problems}")

    # negative control: corrupt one payload byte, data CRC must fail
    bad = bytearray(img)
    bad[0x500] ^= 0xFF
    p2 = verify(bytes(bad), verbose=False)
    print("corruption detected:", "yes" if p2 else "NO (bug!)", p2)
    return 0 if not problems and p2 else 1


if __name__ == "__main__":
    if len(sys.argv) == 1 or sys.argv[1] == "selftest":
        sys.exit(_selftest())
    elif sys.argv[1] == "verify":
        sys.exit(1 if verify(open(sys.argv[2], "rb").read()) else 0)
    else:
        print(__doc__)
        sys.exit(2)
