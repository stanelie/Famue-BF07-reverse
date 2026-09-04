#!/usr/bin/env python3
"""Retarget the official LARK adfus.bin from SPI NAND to SPI NOR.

Why
---
The SDK's prebuilt LARK `adfus.bin` (`Ver1.1-adfu`, build Apr 24 2023) is
compiled for the **SD-NAND** reference boards — every LARK board shipping an
`adfus.bin` in this SDK is an `*_dev_watch_sdnand` variant. The BF07 has 4 MB
SPI **NOR** inside the SoC package.

Both storage backends are present in the binary. `main()` just hardcodes the
NAND one:

    01012540  push {r3, lr}
    01012542  bl   0x10124b0
    01012546  ldr  r0, ='[D] '        \\ printf
    01012548  bl   0x1013054          /
    0101254c  ldr  r0, ='adfus run\\n' \\ printf
    0101254e  bl   0x1013054          /
    01012552  movs r1, #0
    01012554  movs r0, #2             <-- storage type 2 = SPI NAND
    01012556  bl   0x1012610          storage_bind()
    0101255a  b    0x101255a          <-- infinite loop if it returns

On a NOR device the NAND probe fails ("Can't get spinand id, Please check!")
and control falls into that `b .` self-loop at `0x101255a`. Which is exactly
the observed hardware behaviour: `cd 20` accepted with CSW 0, then the device
stays enumerated as `10d6:10d6` while servicing neither USB nor UART.

Storage-type dispatch, from the switch at `0x1012564`:

    type 0 -> 0x01013e2c   prints 'spinor0_binding'   <-- SPI NOR
    type 1 -> 0x01013bc0
    type 2 -> 0x01012dbc   the spinand path           <-- currently selected

`0x01013e2c` was confirmed by disassembly: it loads `0x01010a16`
(`'spinor0_binding'`), prints it, and installs a three-entry function table.

The patch
---------
One instruction, two bytes, at file offset 0x2554:

    02 20   movs r0, #2      ->   00 20   movs r0, #0

Safety
------
This only ever produces a *file*. The payload is uploaded to RAM at
0x01010000 with `cd 13` and run with `cd 20` — nothing here touches flash.

Usage
-----
    python3 patch_adfus.py <in.bin> <out.bin> [--type 0]
    python3 patch_adfus.py --verify <in.bin>
"""

import argparse
import struct
import sys

OFF_TYPE = 0x2554          # file offset of `movs r0, #imm`
EXPECT   = b"\x02\x20"     # movs r0, #2
BASE     = 0x01010000

TYPES = {0: "SPI NOR  (handler 0x01013e2c, 'spinor0_binding')",
         1: "unknown  (handler 0x01013bc0)",
         2: "SPI NAND (handler 0x01012dbc, 'spinand')"}


def check(d):
    """Verify this really is the payload we analysed."""
    problems = []
    if len(d) != 47608:
        problems.append(f"size {len(d)}, expected 47608")
    sp, rv = struct.unpack_from("<II", d, 0x100)
    if (sp, rv) != (0x2000F000, 0x01010000):
        problems.append(f"vector table {sp:#x}/{rv:#x}, expected "
                        f"0x2000f000/0x01010000")
    if d[0x255A:0x255C] != b"\xfe\xe7":
        problems.append("no `b .` self-loop at 0x255a")
    if b"adfus run" not in d:
        problems.append("missing 'adfus run' string")
    if b"spinor0_binding" not in d:
        problems.append("missing 'spinor0_binding' — no NOR backend to switch to")
    return problems


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("infile")
    p.add_argument("outfile", nargs="?")
    p.add_argument("--type", type=int, default=0, choices=(0, 1, 2))
    p.add_argument("--verify", action="store_true",
                   help="report the current storage type and exit")
    args = p.parse_args()

    d = bytearray(open(args.infile, "rb").read())

    cur = d[OFF_TYPE + 1] == 0x20 and d[OFF_TYPE]
    print(f"{args.infile}: {len(d)} bytes")
    print(f"  bytes at 0x{OFF_TYPE:x}: {bytes(d[OFF_TYPE:OFF_TYPE+2]).hex(' ')}"
          f"  -> movs r0, #{cur}")
    print(f"  current storage type: {cur} = {TYPES.get(cur, '?')}")

    problems = check(d)
    if problems:
        print("\n  sanity checks FAILED:")
        for x in problems:
            print(f"    - {x}")
        if not args.verify:
            return 1
    else:
        print("  sanity checks passed (size, vector table, self-loop, strings)")

    if args.verify:
        return 0
    if not args.outfile:
        print("\n  no outfile given; nothing written")
        return 1
    if bytes(d[OFF_TYPE:OFF_TYPE + 2]) != EXPECT:
        print(f"\n  refusing: expected {EXPECT.hex(' ')} at 0x{OFF_TYPE:x}")
        return 1

    d[OFF_TYPE] = args.type
    open(args.outfile, "wb").write(bytes(d))
    print(f"\n  patched -> storage type {args.type} = {TYPES[args.type]}")
    print(f"  wrote {args.outfile}")
    print(f"\n  upload with:  lark_cd.py handover {args.outfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
