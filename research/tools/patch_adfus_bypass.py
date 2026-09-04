#!/usr/bin/env python3
"""Patch the LARK adfus.bin so it enters its USB service loop even when the
storage init fails.

The actual failure mode
-----------------------
`0x01012610` is not `storage_bind` — it is the ADFU **service loop**, and USB is
**polled**, not interrupt-driven (the `Adfus_Irq` vector at IRQ 11 is only a
stub that logs and acknowledges):

    01012648  bl   0x10147a8      ; init with 3 callbacks
    0101264c  mov  r1, r5         ; retry count 0x32
    0101264e  mov  r0, r4         ; storage type
    01012650  bl   0x1014d38      ; storage init -> returns 0 on FAILURE
    01012654  cbz  r0, 0x1012668  ; on failure, skip the service loop   <-- (A)
    01012656  bl   0x1014758      ; poll for a USB event
    0101265a  cmp  r0, #6         ; 6 = exit
    0101265e  cmp  r0, #0
    01012660  bne  0x1012656
    01012662  bl   0x1012dd4      ; dispatch the CBW
    01012666  b    0x1012656
    01012668  cmp  r6, #0
    0101266a  bne  0x101264c      ; retry storage init FOREVER           <-- (B)

With `r1 = 0` at the call site, `r6 = 1`, so a failing storage init spins
between (A) and (B) forever, printing its error each time — which is exactly the
repeating output observed after `adfus run`. The USB poller at `0x1014758` is
never reached, so the device enumerates but never answers a CBW.

The patch
---------
Two 2-byte NOPs, both RAM-only:

    0x2654  40 b1  cbz r0, 0x1012668  ->  00 bf  nop   (A) ignore init failure
    0x266a  ef d1  bne 0x101264c      ->  00 bf  nop   (B) never retry-loop

(A) alone is enough to reach the poller; (B) is belt-and-braces so any later
path cannot fall back into the spin.

Expected outcome: the payload answers USB even though flash access may not work.
That would give us a working ADFU transport to debug storage against — a much
better position than total silence.

This does NOT fix storage. Combine with `patch_adfus.py --type 0` (SPI NOR)
since the SDK payload is built for SPI NAND and the BF07 is NOR.

Nothing here touches flash: the payload is uploaded to RAM at 0x01010000 with
`cd 13` and started with `cd 20`.

Usage:
    python3 patch_adfus_bypass.py <in.bin> <out.bin>
"""

import argparse
import struct
import sys

PATCHES = [
    (0x2654, b"\x40\xb1", b"\x00\xbf", "cbz r0,0x1012668 -> nop  (ignore storage init failure)"),
    (0x266A, b"\xef\xd1", b"\x00\xbf", "bne 0x101264c    -> nop  (kill the retry loop)"),
]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("infile")
    p.add_argument("outfile")
    p.add_argument("--only-a", action="store_true",
                   help="apply only the cbz patch, leaving the retry branch")
    args = p.parse_args()

    d = bytearray(open(args.infile, "rb").read())
    print(f"{args.infile}: {len(d)} bytes")

    sp, rv = struct.unpack_from("<II", d, 0x100)
    if (sp, rv) != (0x2000F000, 0x01010000):
        print(f"  refusing: vector table {sp:#x}/{rv:#x} is not the LARK adfus")
        return 1

    todo = PATCHES[:1] if args.only_a else PATCHES
    for off, want, new, desc in todo:
        cur = bytes(d[off:off + 2])
        if cur == new:
            print(f"  0x{off:04x}: already patched")
            continue
        if cur != want:
            print(f"  refusing: 0x{off:04x} is {cur.hex(' ')}, expected {want.hex(' ')}")
            return 1
        d[off:off + 2] = new
        print(f"  0x{off:04x}: {want.hex(' ')} -> {new.hex(' ')}   {desc}")

    stype = d[0x2554]
    print(f"\n  storage type currently {stype} "
          f"({'NOR' if stype == 0 else 'NAND' if stype == 2 else '?'})")
    if stype != 0:
        print("  NOTE: run patch_adfus.py --type 0 as well (BF07 is SPI NOR)")

    open(args.outfile, "wb").write(bytes(d))
    print(f"\n  wrote {args.outfile}")
    print(f"  test with: lark_cd.py handover {args.outfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
