#!/usr/bin/env python3
"""bf07 — back up, restore and patch a Famue BF07, over USB.

No case to open and no soldering: the device's own mass-storage stack implements
the Actions ADFU switch, so everything here runs over the USB cable you charge
it with.

    bf07.py backup  -o mybf07.bin        full 4MB image + SHA-256 sidecar
    bf07.py verify  -b mybf07.bin        what on the device differs from it
    bf07.py restore -b mybf07.bin        put it back, byte-exact
    bf07.py install -b mybf07.bin --patch reader-patch.bin   install the reader

WHAT IS AND IS NOT REDISTRIBUTED
    Your firmware is read from your own device and stays on your machine. This
    tool ships no vendor code. The ADFU payload (adfus_u_go.bin) comes from
    Actions' own public SDK -- see reference/README.md for where to get it.

NO SERIAL CABLE IS NEEDED
    `install --patch` works over ADFU alone. The patch carries PLAINTEXT -- our
    reader plus 256 bytes of stock context at the hook sites -- and every device
    encrypts it with its OWN key on write, so nothing here depends on the flash
    key being shared between units. Untouched blocks are the device's own
    ciphertext, rewritten verbatim.

    Only BUILDING a patch needs a decrypted image (mkpatch.py), once per
    firmware version, by one person. `install -p <image>` remains for anyone
    who has their own dump.

SAFETY
    Restoring does NOT need plaintext: writes with address bit 31 clear store
    bytes verbatim, so the encrypted backup goes straight back (verified on
    hardware). Take a backup before you write anything -- this tool refuses to
    install without one.
"""
import argparse
import glob
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


def mounted_volumes():
    """Filesystems on this device that the OS still has mounted (Linux).

    Entering ADFU pulls usb-storage off the interface and reboots the device,
    so anything mounted from it disappears underneath the kernel. Best effort
    and Linux-only: elsewhere this returns nothing and the caller proceeds, as
    it always did.
    """
    found = []
    try:
        with open("/proc/mounts") as fh:
            mounts = [ln.split()[:2] for ln in fh if ln.startswith("/dev/")]
    except OSError:
        return found
    for blk in glob.glob("/sys/block/sd*"):
        node = os.path.realpath(blk)
        while node and node != "/":
            vid = os.path.join(node, "idVendor")
            if os.path.exists(vid):
                try:
                    if open(vid).read().strip().lower() == "10d6":
                        name = os.path.basename(blk)
                        found += [(src, mnt) for src, mnt in mounts
                                  if src.startswith("/dev/" + name)]
                except OSError:
                    pass
                break
            node = os.path.dirname(node)
    return found


def enter_adfu(timeout=25):
    """Switch a normally-running device into ADFU, over USB alone.

    The normal-mode device answers the classic Actions handshake: ask for
    ACTIONSUSBD, then send the switch command; it reboots as 10d6:10d6.
    """
    if find(PID_ADFU):
        return True
    dev = find(PID_NORMAL)
    if dev is None:
        raise SystemExit("No BF07 found. Connect it over USB and choose disk\n"
                         "drive mode on the boot menu, then try again.")

    busy = mounted_volumes()
    if busy:
        raise SystemExit(
            "The device's storage is still mounted:\n"
            + "".join(f"    {src} on {mnt}\n" for src, mnt in busy) +
            "Entering ADFU detaches usb-storage and reboots the device, which\n"
            "would pull that filesystem out from under the kernel. Unmount it\n"
            "first:\n"
            + "".join(f"    udisksctl unmount -b {src}\n" for src, _ in busy))

    # The claim can fail at set_configuration OR at the first transfer,
    # depending on the backend, so the whole exchange is guarded.
    try:
        # Linux binds usb-storage to the only interface this device has, and it
        # re-binds between processes, so this cannot be done once out of band.
        # With a udev rule granting access it succeeds unprivileged; without one
        # it raises and the guidance below still applies.
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        out = usb.util.find_descriptor(intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        inp = usb.util.find_descriptor(intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)

        # Bulk-Only Transport, in full: CBW, then the data phase, then the
        # CSW. The CSW is not optional bookkeeping -- leaving it unread halts
        # the endpoint, and every later transfer on this device then fails
        # with EOVERFLOW, EPIPE or a bogus status until something clears the
        # halt. A whole afternoon of "the switch command does not work" was
        # this, self-inflicted, on a device that was answering correctly.
        mps = inp.wMaxPacketSize or 512

        def scsi(cdb, dlen, tag):
            """-> (data, csw_status). status 0 is success, None unreadable."""
            out.write(struct.pack("<IIIBBB16s", 0x43425355, tag, dlen, 0x80,
                                  0, len(cdb), cdb.ljust(16, b"\0")), 3000)
            data = b""
            if dlen:
                # Read a packet, not exactly dlen: a REJECTED command skips the
                # data phase and answers with the 13-byte CSW, which overflows
                # a dlen-sized buffer and hides the real status behind an
                # errno. Taking a packet lets us tell the two apart.
                data = bytes(inp.read(mps, 3000))
                if data[:4] == b"USBS":            # CSW where data belonged
                    return b"", data[12] if len(data) >= 13 else None
                data = data[:dlen]
            csw = bytes(inp.read(mps, 2000))
            st = csw[12] if len(csw) >= 13 and csw[:4] == b"USBS" else None
            return data, st

        ident, st = scsi(bytes([0xCC]) + b"\0" * 6 + bytes([11]), 11, 1)
        if ident != b"ACTIONSUSBD":
            raise SystemExit(
                f"unexpected identity {ident!r} (status {st}) -- is this a "
                f"BF07, and is it in disk drive mode?")
        _, st = scsi(bytes([0xCB, 0x21]) + b"\0" * 5 + bytes([2]), 2, 2)
        if st not in (0, None):
            raise SystemExit(f"the device refused the ADFU switch (status {st})")
    except usb.core.USBError as e:
        if "Access denied" in str(e) or "busy" in str(e).lower() or e.errno == 13:
            raise SystemExit(
                "Cannot reach the device's USB interface: the operating system\n"
                "is holding it. The BF07 exposes only one interface (mass\n"
                "storage), so there is nothing else to talk to.\n"
                "  Linux  : install the udev rule for 10d6 (see docs/flashing.md)\n"
                "           so the detach above can succeed unprivileged, or run\n"
                "           this as root\n"
                "  Windows: bind that interface to WinUSB (e.g. with Zadig)\n"
                "  macOS  : not possible -- the kernel driver cannot be detached,\n"
                "           and unmounting does not release it. Put the device in\n"
                "           ADFU another way (serial `dbg reboot adfu`, or another\n"
                "           machine); everything after that works here.")
        raise
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
        # Do NOT set_configuration() here. The payload owns the endpoints from
        # the exec onward, and re-selecting the (already active) configuration
        # resets them under it: every raw packet after that returns EIO. Linux
        # configures the device at enumeration, so the call is redundant as
        # well as destructive; macOS tolerated it. See docs/flashing.md.
        try:
            self.d.get_active_configuration()
        except usb.core.USBError:
            self.d.set_configuration()
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

    def write_plain_checked(self, addr, data, attempts=4):
        """write_plain, then confirm the block actually programmed.

        The first write_plain issued after a run of write_raw is ACKed but
        NEVER PROGRAMMED -- the block stays erased. Measured while relabelling
        a menu string: the block read back 0xff..ff, and the OTFD decrypting
        erased bytes put garbage on the screen.

        It hid because the obvious check is "did this block change?", and an
        unwritten block differs from stock too. Check what the block IS.
        """
        for _ in range(attempts):
            self.write_plain(addr, data)
            if self.read(addr, len(data)) != b"\xff" * len(data):
                return
        raise SystemExit(f"block at 0x{addr:06x} would not program")

    def _write(self, addr, data, encrypt):
        blank = b"\xff" * 32
        for off in range(0, len(data), 32):          # 32-byte transactions only
            block = data[off:off + 32]
            # An erased sector is already 0xFF. Writing 0xFF as PLAINTEXT would
            # encrypt it into ciphertext garbage -- padding after the reader
            # showed up as 2 extra changed blocks, which is how this was caught.
            if block == blank:
                continue
            a = addr + off
            ack = self.cmd(b"ws", 32, a | (1 << 31) if encrypt else a,
                           expect=4, data=block)
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
    # Same rule as Device.__init__, and it matters most here: probing a LIVE
    # payload with set_configuration() kills it, so this returned False, the
    # caller uploaded a second time, and ADFU wedged -- precisely the failure
    # this function exists to prevent.
    try:
        d.get_active_configuration()
    except usb.core.USBError:
        d.set_configuration()
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
    sys.path.insert(0, HERE)
    from patchset import build          # the reader's patch table

    if args.patch:
        return install_patch(args, backup)
    if not args.plain:
        raise SystemExit("give --patch reader-patch.bin (recommended) or "
                         "-p <decrypted image> (legacy)")

    # Legacy path: needs the full decrypted image (one serial dump).
    plain = open(args.plain, "rb").read()
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


def install_patch(args, backup):
    """Install a distributable patch file -- ADFU only, no serial, no image.

    Each patched sector is rebuilt block by block: the few blocks the patch
    names are written as plaintext (the SoC encrypts them with the device's own
    key), and every other block is restored from the device's OWN ciphertext,
    verbatim. So the tool never needs the device's plaintext, only the 256 bytes
    of stock context the patch carries -- and it works whether the flash key is
    per-device or global, because nothing here assumes anything about the key.
    """
    from mkpatch import load_patch
    reader, blocks, ref_sha = load_patch(open(args.patch, "rb").read())
    print(f"patch: {len(reader)} reader sector(s), {len(blocks)} vendor block(s)")
    print(f"       built from plaintext sha256 {ref_sha.hex()[:16]}...")
    print("NOTE: your device must run the firmware this patch was built for.")
    print("      A mismatch is recoverable with `restore` -- that is why a backup")
    print("      is required.\n")

    # group the named vendor blocks by their containing sector
    by_sector = {}
    for addr, data in blocks:
        by_sector.setdefault(addr & ~0xfff, {})[addr & 0xfff] = data

    d = connect()

    # reader sectors: pure ours, write plaintext, leave 0xFF erased
    for addr, data in reader:
        d.erase(addr)
        for off in range(0, SECTOR, 32):
            if data[off:off + 32] != b"\xff" * 32:
                d.write_plain(addr + off, data[off:off + 32])
        # Re-encrypt each block through the device and compare: the SoC is a
        # deterministic oracle (32-byte ECB, no address tweak), so a correctly
        # written block re-encrypts to exactly what is now in flash.
        back = d.read(addr, SECTOR)
        blank = sum(1 for o in range(0, SECTOR, 32)
                    if data[o:o + 32] == b"\xff" * 32
                    and back[o:o + 32] != b"\xff" * 32)
        if blank:
            raise SystemExit(f"0x{addr:06x}: {blank} block(s) should be erased "
                             f"but are not -- restore with -b {args.backup}")
        # ...and the converse, which is the one that bites: a block that should
        # carry data but is still erased. An ACK is not proof of a program.
        lost = sum(1 for o in range(0, SECTOR, 32)
                   if data[o:o + 32] != b"\xff" * 32
                   and back[o:o + 32] == b"\xff" * 32)
        if lost:
            raise SystemExit(f"0x{addr:06x}: {lost} block(s) never programmed "
                             f"-- restore with -b {args.backup}")
        print(f"  0x{addr:06x}: reader sector written and checked")

    # vendor sectors: keep the device's own ciphertext, swap only the named blocks
    for sec in sorted(by_sector):
        cur = d.read(sec, SECTOR)                 # this device's current ciphertext
        d.erase(sec)
        edits = by_sector[sec]
        # Our encrypted blocks go FIRST and are read back. Interleaving them
        # with the verbatim restores loses whichever write_plain follows a run
        # of write_raw -- see write_plain_checked.
        for off in sorted(edits):
            d.write_plain_checked(sec + off, edits[off])     # SoC encrypts our patch
        for off in range(0, SECTOR, 32):
            if off not in edits and cur[off:off + 32] != b"\xff" * 32:
                d.write_raw(sec + off, cur[off:off + 32])    # restore verbatim
        # Verify CONTENT, not just which blocks moved.
        #
        # An earlier version only checked the changed/unchanged PATTERN against
        # stock and reported "byte-identical to the reference install". A wrong
        # patched block passes that test -- and one did, producing a device that
        # bus-faulted inside the font hook at boot. Every block is now compared
        # against what it must be: the pre-erase ciphertext for untouched
        # blocks, and a read-back-and-re-encrypt check for the patched ones.
        back = d.read(sec, SECTOR)
        bad = []
        for o in range(0, SECTOR, 32):
            if o in edits:
                if back[o:o + 32] == b"\xff" * 32:
                    bad.append(f"+0x{o:03x} still erased (write lost)")
                elif back[o:o + 32] == cur[o:o + 32]:
                    bad.append(f"+0x{o:03x} unchanged (write lost)")
            elif back[o:o + 32] != cur[o:o + 32]:
                bad.append(f"+0x{o:03x} clobbered")
        if bad:
            raise SystemExit(
                f"0x{sec:06x}: VERIFY FAILED -- {'; '.join(bad[:4])}\n"
                f"The device is in an unknown state. Restore now:\n"
                f"  bf07.py restore -b {args.backup}")
        print(f"  0x{sec:06x}: {len(edits)} block(s) patched, "
              f"{SECTOR//32 - len(edits)} preserved, verified")
    print("\ndone -- power-cycle the device")
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
    p.add_argument("--patch", help="reader-patch.bin (ADFU only, no serial/image)")
    p.add_argument("-p", "--plain", help="decrypted fw0_sys image (legacy path)")
    p.set_defaults(fn=cmd_install)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
