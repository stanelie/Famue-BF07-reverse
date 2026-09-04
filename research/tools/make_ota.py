#!/usr/bin/env python3
"""Build a BF07 `ota.bin` — including the `ota.xml` manifest the device requires.

Why this exists
---------------
`ota_tool.py` builds the AOTA container and its CRCs, but an image also needs a
file named **`ota.xml`** inside it. The device parses that manifest *before*
writing anything and rejects the image without it. The required tags were
recovered from the parser's own error strings in `fw0_sys` (0x1018e900-0x1018ed00):

    <firmware_version> <version_code> <version_res> <version_name>
    <board_name>          -> "unmatched board name, skip ota" on mismatch
    <partitionsNum>
    <partitions><partition>
        <type> <file_id> <file_name> <file_size> <checksum>

The container itself is packed by the SDK's own `build_ota_image.py`, invoked as
a subprocess, so the byte layout is the vendor's rather than a reimplementation.
That tool additionally requires the manifest root tag to be `ota_firmware` and a
`board_name` inside `firmware_version`.

CRITICAL: app.bin must be CIPHERTEXT
------------------------------------
`fw0_sys` is stored **encrypted** on this device, and the application contains
no flash-encryption routine (every encrypt/AES/crypt string in `fw0_sys` belongs
to the Bluetooth stack). ADFU `ws` was verified to write raw. So the OTA writes
raw too, and the vendor's production tool encrypts at build time.

Therefore the `app.bin` placed in an image must be the **exact encrypted bytes**
as they appear in flash — i.e. sliced straight out of a raw flash dump. Shipping
plaintext here would write plaintext over an encrypted partition and brick the
device. `--from-dump` does the correct thing; do not hand-assemble app.bin.

Usage
-----
    # no-op recovery image: reflash the current firmware, byte-identical
    python3 make_ota.py --from-dump ~/Documents/bf07-backups/bf07_flash_full_2026-08-05.bin \\
                        -o /Volumes/16GB/ota.bin

    # inspect an image
    python3 make_ota.py --inspect /Volumes/16GB/ota.bin
"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile
import zlib

SDK = os.path.expanduser(
    "~/Documents/bf07-actions-lark-sdk/action_technology_sdk")
BUILDER = os.path.join(SDK, "bootloader/tools/build_ota_image.py")

BOARD_NAME = "xlx_58120_bf07"

# fw0_sys on this device (confirmed by the live partition table)
SYS_OFFSET = 0x14000
SYS_SIZE = 0x1E0000
SYS_FILE_ID = 4
SYS_TYPE = "SYSTEM"

MANIFEST = """<?xml version='1.0' encoding='UTF-8'?>
<ota_firmware>
\t<firmware_version>
\t\t<version_name>{version_name}</version_name>
\t\t<version_code>{version_code}</version_code>
\t\t<version_res>{version_res}</version_res>
\t\t<board_name>{board_name}</board_name>
\t</firmware_version>

\t<partitionsNum>{n}</partitionsNum>
\t<partitions>
{parts}\t</partitions>
</ota_firmware>
"""

PART = """\t\t<partition>
\t\t\t<type>{type}</type>
\t\t\t<name>{name}</name>
\t\t\t<file_id>{file_id}</file_id>
\t\t\t<file_name>{file_name}</file_name>
\t\t\t<file_size>{file_size}</file_size>
\t\t\t<checksum>{checksum}</checksum>
\t\t</partition>
"""


def crc32(b):
    return zlib.crc32(b, 0) & 0xFFFFFFFF


def make_manifest(parts, version_name, version_code, version_res,
                  board_name=BOARD_NAME):
    body = "".join(
        PART.format(type=p["type"], name=p["name"], file_id=p["file_id"],
                    file_name=p["file_name"],
                    file_size="0x%x" % p["size"],
                    checksum="0x%08x" % p["checksum"])
        for p in parts)
    return MANIFEST.format(version_name=version_name,
                           version_code="0x%x" % version_code,
                           version_res="0x%x" % version_res,
                           board_name=board_name, n=len(parts), parts=body)


def build(files, out):
    """files: list of (name, bytes). Packs via the SDK's own builder."""
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for name, data in files:
            p = os.path.join(td, name)
            with open(p, "wb") as f:
                f.write(data)
            paths.append(p)
        cmd = [sys.executable, BUILDER, "-o", out] + paths
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout); print(r.stderr, file=sys.stderr)
            raise SystemExit("SDK builder failed")
        return r.stdout


def inspect(path):
    d = open(path, "rb").read()
    magic, hcrc, hver, hsize, cnt, hflag, diroff, dataoff, dlen, dcrc = \
        struct.unpack("<4sIHHHHHHII", d[:28])
    print(f"file           : {path} ({len(d)} bytes)")
    print(f"magic          : {magic!r} {'OK' if magic == b'AOTA' else 'BAD'}")
    print(f"header_size    : 0x{hsize:x}   dir_offset 0x{diroff:x}   "
          f"data_offset 0x{dataoff:x}")
    print(f"file_count     : {cnt}")
    print(f"data_len       : 0x{dlen:x}")
    calc_d = crc32(d[dataoff:dlen])
    print(f"data_crc       : stored 0x{dcrc:08x} calc 0x{calc_d:08x} "
          f"{'OK' if calc_d == dcrc else 'MISMATCH'}")
    calc_h = crc32(d[8:dataoff])
    print(f"header_crc     : stored 0x{hcrc:08x} calc 0x{calc_h:08x} "
          f"{'OK' if calc_h == hcrc else 'MISMATCH'}")
    # version blocks live at 0x40 (new) and 0xa0 (old)
    vn, bn = struct.unpack_from("<32s24s", d, 0x40)
    NUL = b"\x00"
    vname = vn.split(NUL)[0].decode(errors="replace")
    bname = bn.split(NUL)[0].decode(errors="replace")
    print(f"version_name   : {vname}")
    print(f"board_name     : {bname} "
          f"{'OK' if bname == BOARD_NAME else 'MISMATCH -> device will skip ota'}")
    print("\nfiles:")
    for i in range(cnt):
        e = diroff + i * 0x20
        name, off, length, ck = struct.unpack("<12s4xII4xI", d[e:e + 0x20])
        nm = name.split(b"\0")[0].decode(errors="replace")
        actual = crc32(d[off:off + length])
        print(f"  {nm:<12} off 0x{off:<8x} len 0x{length:<8x} "
              f"crc 0x{ck:08x} {'OK' if actual == ck else 'MISMATCH'}")
        if nm == "ota.xml":
            print("  --- manifest ---")
            for line in d[off:off + length].decode(errors="replace").splitlines():
                if line.strip():
                    print("   ", line)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--from-dump", help="raw 4MB flash image to slice app.bin from")
    p.add_argument("-o", "--output")
    p.add_argument("--inspect")
    p.add_argument("--version-name", default="1.00_recovery")
    p.add_argument("--version-code", type=lambda x: int(x, 0), default=0x10000)
    p.add_argument("--version-res", type=lambda x: int(x, 0), default=0x10000)
    p.add_argument("--board", default=BOARD_NAME)
    args = p.parse_args()

    if args.inspect:
        inspect(args.inspect); return 0

    if not args.from_dump or not args.output:
        p.error("need --from-dump and -o (or --inspect)")

    dump = open(args.from_dump, "rb").read()
    if len(dump) < SYS_OFFSET + SYS_SIZE:
        raise SystemExit("dump too small")
    app = dump[SYS_OFFSET:SYS_OFFSET + SYS_SIZE]
    print(f"app.bin: {len(app)} bytes from {args.from_dump} "
          f"@0x{SYS_OFFSET:x} (ENCRYPTED, as stored in flash)")
    print(f"  first16: {app[:16].hex(' ')}")
    print(f"  crc32  : 0x{crc32(app):08x}")

    parts = [dict(type=SYS_TYPE, name="fw0_sys", file_id=SYS_FILE_ID,
                  file_name="app.bin", size=len(app), checksum=crc32(app))]
    xml = make_manifest(parts, args.version_name, args.version_code,
                        args.version_res, args.board)
    print("\n--- ota.xml ---")
    print(xml)

    out = build([("app.bin", app), ("ota.xml", xml.encode())], args.output)
    print(out)
    print(f"wrote {args.output} ({os.path.getsize(args.output)} bytes)\n")
    inspect(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
