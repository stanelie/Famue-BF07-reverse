#!/usr/bin/env python3
"""Upload reader code into the device's RAM over UART -- no flash, no ADFU.

Why this exists
---------------
Every flash leaves the device in ADFU needing a physical button press: there is
no working soft reboot (SYSRESETREQ re-enters ADFU, the reboot-type register is
consumed by the boot ROM, and ADFU opcode 0x22 reaches only a shell-less USB
mode). So each experiment cost a human round trip, which is precisely what made
guessing expensive instead of measuring.

The flashed reader allocates a code buffer and calls into it when
`code_magic` is set, so new code can be delivered over the debug shell:

    read  S->code_buf        (the address to link at)
    build with -Ttext=<addr>
    write the bytes with `dbg mww`
    set   S->code_magic      (activates it)

Clearing code_magic reverts to the flashed implementation, so a bad upload
costs a poke, not a reflash.

Usage:
    ramload.py info                 show the state block and code buffer
    ramload.py load <file.bin>      upload and activate
    ramload.py off                  deactivate (back to flashed code)
    ramload.py poke <addr> <val>    raw word write
"""
import glob
import re
import sys
import time

import serial

ANCHOR = 0x18018E98
INJ_MAGIC = 0x52444252
CODE_MAGIC = 0x434F4445

# offsets within struct inj_state -- keep in step with reader/src/main.c
OFF_LAYOUT = 0x04
OFF_GEN = 0x08
OFF_CALLS = 0x0C


def port():
    p = glob.glob("/dev/cu.usbserial-*")
    if not p:
        raise SystemExit("no UART adapter found")
    return p[0]


class Shell:
    def __init__(self):
        self.s = serial.Serial(port(), 2000000, timeout=0.3)
        time.sleep(0.3)

    def cmd(self, text, wait=0.25):
        self.s.reset_input_buffer()
        self.s.write((text + "\r\n").encode())
        self.s.flush()
        t0 = time.time()
        buf = b""
        while time.time() - t0 < wait:
            d = self.s.read(8192)
            if d:
                buf += d
        return buf.decode("utf8", "replace")

    def rd(self, addr, words=1):
        txt = self.cmd(f"dbg mdw 0x{addr:08x} {words:x}", 0.4 + words * 0.002)
        out = {}
        for m in re.finditer(r"^([0-9a-f]{8}): ((?:[0-9a-f]{8} ?){1,4})", txt, re.M):
            base = int(m.group(1), 16)
            for i, w in enumerate(m.group(2).split()):
                out[base + i * 4] = int(w, 16)
        if not out:
            raise RuntimeError("no reply from the device")
        return out

    def wr(self, addr, val, wait=0.02):
        self.s.reset_input_buffer()
        self.s.write(f"dbg mww 0x{addr:08x} 0x{val:08x}\r\n".encode())
        self.s.flush()
        time.sleep(wait)

    def state(self):
        a = self.rd(ANCHOR, 2)
        if a.get(ANCHOR) != INJ_MAGIC:
            raise RuntimeError("reader state not present -- open a book first")
        return a.get(ANCHOR + 4)


def find_code_buf(sh, st):
    """code_magic and code_buf sit just before `cur`; locate them by scanning
    the head of the struct for our magic or a plausible heap pointer."""
    # scan the WHOLE struct: the code fields sit near the end, after the
    # 512-byte page buffer and the file handle. An earlier version scanned
    # only the first 0x180 bytes and reported "not allocated".
    layout = sh.rd(st + 4)[st + 4]
    words = min(max(layout // 4, 0x80), 0x200)
    w = sh.rd(st, words)
    for off in range(0x20, words * 4, 4):
        v = w.get(st + off)
        nxt = w.get(st + off + 4)
        if v in (0, CODE_MAGIC) and nxt and 0x01000000 <= nxt < 0x01100000:
            return st + off, nxt
    return None, None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    sh = Shell()
    cmd = sys.argv[1]

    if cmd == "poke":
        sh.wr(int(sys.argv[2], 0), int(sys.argv[3], 0))
        print("poked")
        return

    st = sh.state()
    w = sh.rd(st, 4)
    print(f"state    0x{st:08x}  layout={w.get(st+OFF_LAYOUT)} "
          f"gen={w.get(st+OFF_GEN)} calls={w.get(st+OFF_CALLS)}")
    moff, buf = find_code_buf(sh, st)
    if not buf:
        raise SystemExit("code buffer not allocated yet -- open a book and retry")
    print(f"code_magic at 0x{moff:08x}   code_buf = 0x{buf:08x}")

    if cmd == "info":
        return
    if cmd == "off":
        sh.wr(moff, 0)
        print("trampoline disabled -- flashed code is live again")
        return
    if cmd == "load":
        blob = open(sys.argv[2], "rb").read()
        if len(blob) % 4:
            blob += b"\x00" * (4 - len(blob) % 4)
        print(f"uploading {len(blob)} bytes to 0x{buf:08x} "
              f"({len(blob)//4} words)...")
        sh.wr(moff, 0)                       # deactivate while writing
        t0 = time.time()
        for i in range(0, len(blob), 4):
            word = int.from_bytes(blob[i:i + 4], "little")
            sh.wr(buf + i, word)
            if (i // 4) % 128 == 0:
                print(f"   {i}/{len(blob)}", flush=True)
        # verify
        bad = 0
        for i in range(0, len(blob), 4 * 4):
            got = sh.rd(buf + i, 4)
            for j in range(4):
                if i + j * 4 >= len(blob):
                    break
                want = int.from_bytes(blob[i + j * 4:i + j * 4 + 4], "little")
                if got.get(buf + i + j * 4) != want:
                    bad += 1
        print(f"uploaded in {time.time()-t0:.0f}s, {bad} mismatched words")
        if bad:
            raise SystemExit("verify failed -- not activating")
        sh.wr(moff, CODE_MAGIC)
        print("activated")
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
