#!/usr/bin/env python3
"""bf07 — back up, restore and patch a Famue BF07, over USB.

No case to open and no soldering: the device's own mass-storage stack implements
the Actions ADFU switch, so everything here runs over the USB cable you charge
it with.

    bf07.py backup  -o mybf07.bin        full 4MB image + SHA-256 sidecar
    bf07.py verify  -b mybf07.bin        what on the device differs from it
    bf07.py restore -b mybf07.bin        put it back, byte-exact
    bf07.py install -b mybf07.bin -p plain.bin   install the replacement reader

WHAT IS AND IS NOT REDISTRIBUTED
    Your firmware is read from your own device and stays on your machine. This
    tool ships no vendor code. The ADFU payload (adfus_u_go.bin) comes from
    Actions' own public SDK -- see reference/README.md for where to get it.

THE ONE THING THAT STILL NEEDS A SERIAL CABLE
    `install` needs the DECRYPTED image (`-p`), because patches are built by
    editing plaintext. ADFU reads ciphertext, and it cannot see the decrypted
    XIP window (measured: `rm 0x10000000` returns neither plaintext nor
    ciphertext). Only the running firmware can read it, via `dbg mdw` on the
    UART. `backup`, `verify` and `restore` need none of that.

SAFETY
    Restoring does NOT need plaintext: writes with address bit 31 clear store
    bytes verbatim, so the encrypted backup goes straight back (verified on
    hardware). Take a backup before you write anything -- this tool refuses to
    install without one.
"""
import argparse
import hashlib
import os
import struct
import sys
import time

import usb.core
import usb.util

VID, PID_NORMAL, PID_ADFU = 0x10D6, 0xB00B, 0x10D6
FLASH_SIZE = 0x400000
FW0_START, FW0_END, SECTOR = 0x14000, 0x200000, 0x1000
HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.environ.get("BF07_PAYLOAD", os.path.join(HERE, os.pardir, "reference", "adfus_u_go.bin"))


# ---------------------------------------------------------------- ADFU entry

def find(pid):
    return usb.core.find(idVendor=VID, idProduct=pid)


def enter_adfu(timeout=25):
    """Switch a normally-running device into ADFU, over USB alone.

    The normal-mode device answers the classic Actions handshake: ask for
    ACTIONSUSBD, then send the switch command; it reboots as 10d6:10d6.
    """
    if find(PID_ADFU):
        return True
    dev = find(PID_NORMAL)
    if dev is None:
        raise SystemExit("No BF07 found. Plug it in over USB and try again.")
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        if "Access denied" in str(e) or "busy" in str(e).lower():
            raise SystemExit(
                "The operating system is holding the device's mass-storage\n"
                "interface, so the ADFU switch cannot be sent.\n"
                "  Linux : run as root, or detach usb-storage for 10d6:b00b\n"
                "  Windows: bind that interface to WinUSB (e.g. with Zadig)\n"
                "  macOS : not possible -- the kernel driver cannot be detached.\n"
                "          Enter ADFU another way, then re-run this command.")
        raise
    out = usb.util.find_descriptor(dev.get_active_configuration()[(0, 0)],
                                   custom_match=lambda e: usb.util.endpoint_direction(
                                       e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    inp = usb.util.find_descriptor(dev.get_active_configuration()[(0, 0)],
                                   custom_match=lambda e: usb.util.endpoint_direction(
                                       e.bEndpointAddress) == usb.util.ENDPOINT_IN)

    def cbw(cdb, dlen, tag):
        return struct.pack("<IIIBBB16s", 0x43425355, tag, dlen, 0x80, 0, len(cdb),
                           cdb.ljust(16, b"\0"))

    out.write(cbw(bytes([0xCC]) + b"\0" * 6 + bytes([11]), 11, 1), 3000)
    ident = bytes(inp.read(11, 3000))
    try:
        inp.read(13, 1000)
    except usb.core.USBError:
        pass
    if ident != b"ACTIONSUSBD":
        raise SystemExit(f"unexpected identity {ident!r} -- is this a BF07?")
    out.write(cbw(bytes([0xCB, 0x21]) + b"\0" * 5 + bytes([2]), 2, 2), 3000)
    try:
        inp.read(2, 2000)
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)

    t0 = time.time()
    while time.time() - t0 < timeout:
        if find(PID_ADFU):
            return True
        time.sleep(0.25)
    raise SystemExit("device did not come back as ADFU")


# ------------------------------------------------------------ ADFU transport

class Device:
    """The running ADFU payload: raw 16-byte packets, not CBW framing."""

    def __init__(self):
        self.d = find(PID_ADFU)
        if self.d is None:
            raise SystemExit("not in ADFU")
        try:
            self.d.set_configuration()
        except usb.core.USBError:
            pass
        for _ in range(15):
            try:
                self.d.read(0x81, 512, 150)
            except usb.core.USBError:
                break

    def cmd(self, op, length, addr, expect=None, data=None):
        p = bytearray(16)
        p[0:2] = op
        struct.pack_into("<I", p, 4, length)
        struct.pack_into("<I", p, 8, addr)
        self.d.write(0x02, bytes(p), 6000)
        if data is not None:
            self.d.write(0x02, data, 10000)
        want = expect if expect is not None else length
        buf = b""
        while len(buf) < want:
            try:
                c = bytes(self.d.read(0x81, min(want - len(buf), 512), 3000))
            except usb.core.USBError:
                break
            if not c:
                break
            buf += c
            if want <= 16 and len(buf) >= 4:
                break
        return buf

    def open_storage(self):
        if self.cmd(b"is", 16, 0, expect=4)[:1] != b"\xaa":
            raise SystemExit("could not bind the flash -- re-upload the payload")

    def read(self, addr, length):
        out = b""
        while len(out) < length:
            n = min(0x1000, length - len(out))
            part = self.cmd(b"rs", n, addr + len(out))
            if not part:
                raise SystemExit(f"read failed at 0x{addr + len(out):06x}")
            out += part
        return out

    def erase(self, addr):
        self.cmd(b"es", SECTOR, addr, expect=4)
        time.sleep(0.6)

    def write_raw(self, addr, data):
        """Bit 31 CLEAR: bytes are stored verbatim, no encryption.

        This is what lets an encrypted backup go straight back without ever
        knowing the plaintext. Verified on hardware: write ciphertext, read the
        same ciphertext.
        """
        self._write(addr, data, encrypt=False)

    def write_plain(self, addr, data):
        """Bit 31 SET: the SoC encrypts on the way in."""
        self._write(addr, data, encrypt=True)

    def _write(self, addr, data, encrypt):
        for off in range(0, len(data), 32):          # 32-byte transactions only
            a = addr + off
            ack = self.cmd(b"ws", 32, a | (1 << 31) if encrypt else a,
                           expect=4, data=data[off:off + 32])
            if not ack or ack[0] != 0xAA:
                raise SystemExit(f"write failed at 0x{a:06x}")


def payload_alive():
    """Is the flash payload already running?

    Uploading it a second time is what wedges ADFU -- every USB transfer then
    times out and only a power-cycle clears it. That is worth avoiding: a user
    running `backup` and then `verify` would hit it every time.

    The running payload answers a raw 16-byte `is` with 0xAA. The boot ROM does
    not, so a timeout means "nothing loaded yet".
    """
    d = find(PID_ADFU)
    if d is None:
        return False
    try:
        d.set_configuration()
    except usb.core.USBError:
        pass
    try:
        p = bytearray(16)
        p[0:2] = b"is"
        struct.pack_into("<I", p, 4, 16)
        struct.pack_into("<I", p, 8, 0)
        d.write(0x02, bytes(p), 1500)
        return bytes(d.read(0x81, 4, 1500))[:1] == b"\xaa"
    except usb.core.USBError:
        return False
    finally:
        usb.util.dispose_resources(d)


def start_payload():
    """Upload and start the flash payload. Always fresh: a stale one fails."""
    path = os.path.abspath(PAYLOAD)
    if not os.path.exists(path):
        raise SystemExit(
            f"ADFU payload not found at {path}\n"
            "It comes from Actions' public LARK SDK and is not shipped here.\n"
            "See reference/README.md, or set BF07_PAYLOAD.")
    sys.path.insert(0, HERE)
    from lark_cd import Adfu, OP_EXEC1, OP_WRITE
    blob = open(path, "rb").read()
    if len(blob) % 256:
        blob += b"\0" * (256 - len(blob) % 256)
    try:
        a = Adfu(timeout=8000)
        a.write(OP_WRITE, 0x01010000, blob)
        a.cmd(OP_EXEC1, 0x01010000)
        usb.util.dispose_resources(a.d)
        del a
    except usb.core.USBError:
        raise SystemExit(
            "ADFU is not responding. It wedges if the payload is uploaded while\n"
            "one is already running. Power-cycle the device (reset button) and\n"
            "run this again -- nothing has been written.")
    time.sleep(2.5)


def connect():
    enter_adfu()
    if not payload_alive():
        start_payload()
    d = Device()
    d.open_storage()
    return d


# ------------------------------------------------------------------ commands

def cmd_backup(args):
    d = connect()
    print(f"reading {FLASH_SIZE // 1024} KB ...")
    t0 = time.time()
    img = d.read(0, FLASH_SIZE)
    if len(img) != FLASH_SIZE:
        raise SystemExit(f"short read: {len(img)} of {FLASH_SIZE}")
    open(args.out, "wb").write(img)
    h = hashlib.sha256(img).hexdigest()
    open(args.out + ".sha256", "w").write(f"{h}  {os.path.basename(args.out)}\n")
    print(f"wrote {args.out} in {time.time() - t0:.1f}s")
    print(f"sha256 {h}")
    print("Keep this file. It is the only way back.")


def differing(d, backup):
    bad = []
    for s in range(FW0_START, FW0_END, SECTOR):
        cur = d.read(s, SECTOR)
        if len(cur) == SECTOR and cur != backup[s:s + SECTOR]:
            n = sum(1 for i in range(0, SECTOR, 32)
                    if cur[i:i + 32] != backup[s + i:s + i + 32])
            bad.append((s, n))
    return bad


def cmd_verify(args):
    backup = open(args.backup, "rb").read()
    d = connect()
    print(f"comparing 0x{FW0_START:06x}-0x{FW0_END:06x} against {args.backup} ...")
    bad = differing(d, backup)
    for s, n in bad:
        print(f"  0x{s:06x}: differs in {n} block(s)")
    print(f"{len(bad)} sector(s) differ" if bad else "device matches the backup")


def cmd_restore(args):
    backup = open(args.backup, "rb").read()
    if len(backup) < FW0_END:
        raise SystemExit("backup is too small to be a full image")
    d = connect()
    bad = differing(d, backup)
    if not bad:
        print("device already matches the backup")
        return
    print(f"restoring {len(bad)} sector(s)")
    for s, _ in bad:
        d.erase(s)
        d.write_raw(s, backup[s:s + SECTOR])     # ciphertext, verbatim
        ok = d.read(s, SECTOR) == backup[s:s + SECTOR]
        print(f"  0x{s:06x}: {'restored' if ok else 'STILL DIFFERS'}")
    print("done -- power-cycle the device")


def cmd_install(args):
    if not os.path.exists(args.backup):
        raise SystemExit("refusing to install without a backup: run `backup` first")
    backup = open(args.backup, "rb").read()
    plain = open(args.plain, "rb").read()
    sys.path.insert(0, HERE)
    from patchset import build          # the reader's patch table
    sectors = build(plain)              # {flash_addr: 4096 bytes of PLAINTEXT}
    d = connect()
    print(f"installing into {len(sectors)} sector(s)")
    for addr in sorted(sectors):
        d.erase(addr)
        d.write_plain(addr, sectors[addr])       # SoC encrypts on write
        back = d.read(addr, SECTOR)
        changed = [i for i in range(0, SECTOR, 32)
                   if back[i:i + 32] != backup[addr + i:addr + i + 32]]
        print(f"  0x{addr:06x}: {len(changed)} block(s) changed")
    print("done -- power-cycle the device")
    print(f"If anything is wrong: bf07.py restore -b {args.backup}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("backup"); p.add_argument("-o", "--out", default="bf07-backup.bin"); p.set_defaults(fn=cmd_backup)
    p = sub.add_parser("verify"); p.add_argument("-b", "--backup", required=True); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("restore"); p.add_argument("-b", "--backup", required=True); p.set_defaults(fn=cmd_restore)
    p = sub.add_parser("install")
    p.add_argument("-b", "--backup", required=True)
    p.add_argument("-p", "--plain", required=True, help="decrypted fw0_sys image")
    p.set_defaults(fn=cmd_install)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
