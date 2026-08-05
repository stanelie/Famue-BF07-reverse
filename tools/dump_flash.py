#!/usr/bin/env python3
"""Dump the entire 4 MB SPI NOR over the UART shell, via `dbg fread spi_flash`.

No ADFU required. This reads **raw** flash (ciphertext for encrypted
partitions), which is exactly what you want for a backup — unlike `dbg mdw`,
which reads the decrypted XIP view and only covers the code partition.

`dbg dumpbuf` silently writes zeros for this region; do not use it.

Output format being parsed (note `%2x`, so single digits are space-padded):

    fread 512b: offset=0x0, buf=0x1806ee80
           0: 7a 37 b8  1 1e de 32 57 8a 97 e9 56  7 b2 11 a0
          10: ad 78 31 62 f9 e7 aa f5 3a 7b  d bc f9 7c 11 ba
          ...
         1f0: ...

Resumable: re-running skips blocks already present in the output file.

Usage:
    python3 dump_flash.py out.bin [--size 0x400000] [--start 0x0]
"""

import argparse
import os
import re
import sys
import time

import serial

PORT = "/dev/cu.usbserial-XXXX"
BAUD = 2000000
BLOCK = 512

LINE = re.compile(rb"^\s*([0-9a-f]+):\s+((?:[0-9a-f ]{2}\s*){1,16})$", re.I)


def read_block(s, off, tries=2):
    """One 512-byte block. Returns bytes or None."""
    # The shell emits unrelated background log lines, so never extend the
    # deadline on activity - use a hard cutoff and parse incrementally.
    for attempt in range(tries):
        s.reset_input_buffer()
        s.write(f"dbg fread spi_flash 0x{off:x}\r\n".encode())
        s.flush()

        got = {}
        pending = bytearray()
        deadline = time.time() + 8.0
        while time.time() < deadline and len(got) < BLOCK // 16:
            chunk = s.read(4096)
            if not chunk:
                continue
            pending.extend(chunk)
            *lines, rest = bytes(pending).split(b"\n")
            pending = bytearray(rest)
            for raw in lines:
                m = LINE.match(raw.strip())
                if not m:
                    continue
                try:
                    o = int(m.group(1), 16)
                except ValueError:
                    continue
                if o % 16 or o >= BLOCK:
                    continue
                vals = [int(x, 16) for x in m.group(2).split()]
                if len(vals) == 16:
                    got[o] = bytes(vals)

        if len(got) == BLOCK // 16:
            return b"".join(got[o] for o in sorted(got))
        # Sustained hammering degrades the device's print path; it recovers
        # when given idle time. Back off before retrying.
        time.sleep(1.5 * (attempt + 1))
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("outfile")
    p.add_argument("--size", type=lambda x: int(x, 0), default=0x400000)
    p.add_argument("--start", type=lambda x: int(x, 0), default=0x0)
    p.add_argument("--port", default=PORT)
    p.add_argument("--settle", type=float, default=0.06,
                   help="pause between blocks so the device drains")
    p.add_argument("--rest-every", type=int, default=256,
                   help="take a longer rest every N blocks")
    args = p.parse_args()

    # resume: keep whatever is already complete
    data = bytearray(b"\xff" * args.size)
    done = set()
    if os.path.exists(args.outfile):
        old = open(args.outfile, "rb").read()
        n = min(len(old), args.size)
        data[:n] = old[:n]
        st = os.path.getsize(args.outfile + ".state") if os.path.exists(
            args.outfile + ".state") else 0
        if st:
            for line in open(args.outfile + ".state"):
                line = line.strip()
                if line:
                    done.add(int(line, 16))
        print(f"resuming: {len(done)} blocks already captured")

    s = serial.Serial(args.port, BAUD, timeout=0.15)
    time.sleep(0.3)

    state = open(args.outfile + ".state", "a", buffering=1)
    total = (args.size - args.start) // BLOCK
    ok = fail = 0
    t_start = time.time()

    # Blocks whose bytes are in `data` but not yet written to disk. The journal
    # must never claim a block the output file does not actually contain, or a
    # resume silently keeps stale bytes -- so always flush data BEFORE
    # journalling. (Earlier versions journalled per block while flushing data
    # every 64, which corrupts on any hard kill.)
    pending = []

    def flush():
        if not pending:
            return
        with open(args.outfile, "wb") as f:
            f.write(bytes(data))
            f.flush()
            os.fsync(f.fileno())
        for o in pending:
            state.write(f"{o:x}\n")
        pending.clear()

    try:
        for i in range(total):
            off = args.start + i * BLOCK
            if off in done:
                continue
            blk = read_block(s, off)
            if blk is None:
                fail += 1
                print(f"  0x{off:06x}  FAILED", flush=True)
                time.sleep(4.0)          # let the device recover
            else:
                data[off:off + BLOCK] = blk
                pending.append(off)
                ok += 1
                time.sleep(args.settle)
                if args.rest_every and ok % args.rest_every == 0:
                    flush()
                    time.sleep(3.0)      # periodic drain
            if (ok + fail) % 64 == 0 or i == total - 1:
                flush()
                el = time.time() - t_start
                rate = ok / el if el else 0
                eta = (total - i - 1) / rate / 60 if rate else 0
                pct = 100.0 * (i + 1) / total
                print(f"  0x{off:06x}  {pct:5.1f}%  ok={ok} fail={fail}  "
                      f"{rate:.1f} blk/s  eta {eta:.0f} min", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        flush()
        s.close()
        state.close()

    print(f"\nwrote {args.outfile}: {ok} blocks ok, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
