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
    tool ships no vendor code of its own. The ADFU payload (adfus_u_go.bin)
    comes from Actions' own public SDK; if a release bundle didn't include it,
    see reference/README.md for where to get it.

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
FW0_SYS_END = 0x1F4000          # fw0_sdfs begins here
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

    def write_plain(self, addr, data, skip_blank=True):
        """Bit 31 SET: the SoC encrypts on the way in.

        skip_blank=False writes 0xFF blocks too. Needed only by a faithful
        full-image restore, where a 0xFF block may be genuine DATA rather than
        padding -- see restore_plain().
        """
        self._write(addr, data, encrypt=True, skip_blank=skip_blank)

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

    def _write(self, addr, data, encrypt, skip_blank=True):
        blank = b"\xff" * 32
        for off in range(0, len(data), 32):          # 32-byte transactions only
            block = data[off:off + 32]
            # An erased sector is already 0xFF. Writing 0xFF as PLAINTEXT would
            # encrypt it into ciphertext garbage -- padding after the reader
            # showed up as 2 extra changed blocks, which is how this was caught.
            if block == blank and skip_blank:
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


DANGER_OPEN = """
!!  DO NOT DISCONNECT POWER, and do not unplug the USB cable, until this
!!  command prints that it is finished.
!!
!!  From the first erase until the verify passes, fw0_sys is incomplete. A
!!  failed verify is harmless -- run the command again. Losing power in that
!!  window is not: the GOTO_ADFU flag that would let you back in lives in
!!  RTC_REMAIN3, and it does NOT survive a power cycle (measured: set, power
!!  cycled, read back 0x00000000). The device would boot the half-written
!!  firmware instead, and mbrec does not check it -- there is no automatic
!!  recovery on this board.
"""

DANGER_CLOSED = ("verified -- safe to disconnect now. "
                 "Power-cycle the device to boot it.")


def erased_sentinel(img):
    """The plaintext value that ERASED flash decrypts to, found in the image.

    A plaintext dump is read through the XIP decryptor, which happily decrypts
    erased flash into a fixed 32-byte block of garbage. So a dump cannot tell
    "erased" from "data" by inspection, and writing it back verbatim would fill
    every hole with encrypted rubbish -- including the 53 KB the reader is
    installed into, which `install --patch` then refuses because blocks that
    should be erased are not.

    Measured on the reference dump: one distinct block, 1736 occurrences, and it
    never appears where flash is genuinely programmed. It is the device's own
    decryption of 0xFF, so a donor image carries the DONOR's value -- hence
    deriving it from the image rather than hardcoding it. Identification is by
    dominance: 1736 against a runner-up of 181, a 9.6x margin.
    """
    import collections
    c = collections.Counter(img[o:o + 32] for o in range(0, len(img), 32))
    top = c.most_common(2)
    if len(top) < 2:
        return None
    (blk, n1), (_, n2) = top
    if blk == b"\xff" * 32 or n1 < 5 * max(n2, 1):
        return None
    return blk


def restore_plain(args):
    """Rewrite fw0_sys from a PLAINTEXT image -- for a device with no backup.

    Why this works without knowing the flash key: writes with bit 31 set hand
    plaintext to the SoC, which encrypts it with *this* device's own key. So a
    decrypted image captured from a second, working unit installs correctly
    here, whatever the key situation is. The device's own ciphertext backup is
    the better source when it exists (`restore -b`), because it needs no
    re-encryption at all; this is the path for the unit you broke while
    developing, restored from the one you did not.

    Only `fw0_sys` is touched. mbrec, the recovery partition and the nvram
    partitions are outside the range and are never written, which is also why
    the TX/RX-short rescue keeps working no matter how this goes.
    """
    img = open(args.plain, "rb").read()
    if len(img) % SECTOR:
        raise SystemExit(f"{args.plain} is {len(img)} bytes -- not a whole "
                         f"number of {SECTOR}-byte sectors")
    end = FW0_START + len(img)
    if end > FW0_SYS_END:
        raise SystemExit(f"image runs to 0x{end:06x}, past fw0_sys "
                         f"(0x{FW0_SYS_END:06x}); refusing")
    n = len(img) // SECTOR
    print(f"restoring {n} sector(s) of fw0_sys from plaintext "
          f"0x{FW0_START:06x}-0x{end:06x}")
    blank = b"\xff" * 32
    sentinel = None if args.no_erase_detect else erased_sentinel(img)
    if sentinel:
        holes = sum(1 for i in range(0, len(img), 32)
                    if img[i:i + 32] == sentinel)
        print(f"erased-flash marker detected: {sentinel[:8].hex(' ')}... "
              f"({holes} block(s) will be LEFT ERASED)")
    else:
        print("no erased-flash marker detected -- writing every block. If this "
              "image came from an XIP dump, holes will be filled with garbage.")
    d = connect()
    print(DANGER_OPEN)
    for i in range(0, len(img), SECTOR):
        addr = FW0_START + i
        sec = img[i:i + SECTOR]
        # A block is erased flash IFF it equals the sentinel. Everything else is
        # real data and must be written -- INCLUDING 0xFF blocks, which are
        # genuine content here, not padding. Five such blocks exist in the
        # reference image, and skipping them would leave them decrypting to
        # sentinel garbage instead of 0xFF.
        want = [o for o in range(0, SECTOR, 32)
                if not (sentinel and sec[o:o + 32] == sentinel)]
        for attempt in range(4):
            d.erase(addr)
            for o in want:
                d.write_plain(addr + o, sec[o:o + 32], skip_blank=False)
            back = d.read(addr, SECTOR)
            # An ACK is not proof of a program: a block we wrote that reads back
            # erased was never programmed. Check what it IS.
            lost = [o for o in want if back[o:o + 32] == blank]
            if not lost:
                break
        else:
            raise SystemExit(
                f"0x{addr:06x}: {len(lost)} block(s) never programmed after 4 "
                f"attempts. STAY IN ADFU, do not power off.")
        if (i // SECTOR) % 32 == 0 or i + SECTOR >= len(img):
            print(f"  0x{addr:06x}  {i // SECTOR + 1}/{n}", flush=True)
    print(DANGER_CLOSED)


def cmd_restore(args):
    if args.plain:
        return restore_plain(args)
    if not args.backup:
        raise SystemExit("give -b <backup> (preferred) or --plain <image>")
    backup = open(args.backup, "rb").read()
    if len(backup) < FW0_END:
        raise SystemExit("backup is too small to be a full image")
    d = connect()
    bad = differing(d, backup)
    if not bad:
        print("device already matches the backup")
        return
    print(f"restoring {len(bad)} sector(s)")
    print(DANGER_OPEN)
    failed = []
    for s, _ in bad:
        d.erase(s)
        d.write_raw(s, backup[s:s + SECTOR])     # ciphertext, verbatim
        ok = d.read(s, SECTOR) == backup[s:s + SECTOR]
        if not ok:
            failed.append(s)
        print(f"  0x{s:06x}: {'restored' if ok else 'STILL DIFFERS'}")
    if failed:
        raise SystemExit(
            f"{len(failed)} sector(s) did not verify: "
            + ", ".join(f"0x{s:06x}" for s in failed)
            + "\nSTAY IN ADFU and run this command again -- do not power off.")
    print(DANGER_CLOSED)


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
    print(DANGER_OPEN)
    for addr in sorted(sectors):
        d.erase(addr)
        d.write_plain(addr, sectors[addr])       # SoC encrypts on write
        back = d.read(addr, SECTOR)
        if back != sectors[addr]:
            raise SystemExit(
                f"0x{addr:06x} did not read back as written.\n"
                f"STAY IN ADFU -- do not power off. Either run this again, or\n"
                f"put the device back with: bf07.py restore -b {args.backup}")
        changed = [i for i in range(0, SECTOR, 32)
                   if back[i:i + 32] != backup[addr + i:addr + i + 32]]
        print(f"  0x{addr:06x}: {len(changed)} block(s) changed")
    print(DANGER_CLOSED)
    print(f"If anything is wrong: bf07.py restore -b {args.backup}")


def mkpatch_context_digest(sector_bytes, edited_offsets):
    from mkpatch import context_digest       # one definition, both sides
    return context_digest(sector_bytes, edited_offsets)


def already_installed(d, installed):
    """True if every patched sector already holds exactly what we would write.

    Worth checking before an erase, not after: reinstalling identical content
    buys nothing and costs a full erase/write of every patched sector, and the
    erase window is precisely when losing power leaves the device unbootable.
    """
    if not installed:
        return False
    for addr, want in installed:
        sec = d.read(addr, SECTOR)
        if len(sec) != SECTOR or hashlib.sha256(sec).digest() != want:
            return False
    return True


def check_firmware(d, verify, blocks, args):
    """Refuse to install a patch built for a different firmware build.

    At least two BF07 builds ship in the wild (Jun 30 2025 and May 27 2025).
    They have an identical string set and 465 of 480 differing sectors -- the
    same code recompiled and relinked. Installing the wrong one puts our hooks
    at addresses holding unrelated code: the device hangs BEFORE USB comes up,
    so no software recovery exists and it takes shorting TX/RX with the case
    open to get back in. That happened once; this function is why it should not
    happen again.

    The check compares CIPHERTEXT, read with the ordinary `rs` path, because
    the obvious alternative is a trap: reading plaintext means reconfiguring
    SPI0 for decryption mid-session, and that wedges the running payload --
    every later transfer times out and only a physical power-cycle clears it.
    Measured, on hardware, while building this. usb_plaindump.py may do that
    because it exits straight afterwards; a tool that then has to WRITE must
    not touch those registers at all.

    Comparing ciphertext works because the flash key is not per-device: across
    two units and two firmware builds, 17,848 of 17,849 shared plaintext blocks
    encrypt identically. If some unit did have its own key the hashes simply
    would not match and this refuses to install -- the safe direction. It never
    green-lights the wrong firmware.

    Only the hook sectors are read (a few 4 KB reads, seconds), not the whole
    1.9 MB partition -- enough to prove the code we are about to hook is the
    code the patch was built against.
    """
    if not verify:
        print("!! This patch carries no firmware check (old BF07PAT1 format).")
        print("!! It CANNOT be confirmed to match your device. If the reader")
        print("!! does not start after installing, restore immediately:")
        print(f"!!   bf07.py restore -b {args.backup}\n")
        return
    if getattr(args, "force", False):
        print("!! --force: skipping the firmware check. A mismatch here hangs")
        print("!! the device before USB and needs the TX/RX short to recover.\n")
        return

    print(f"checking this device runs the firmware the patch was built for "
          f"({len(verify)} sector(s))...")
    # Which blocks this patch overwrites, per sector -- excluded from the hash
    # so an already-patched device still matches on the surrounding context.
    edited = {}
    for addr, _ in blocks:
        edited.setdefault(addr & ~0xfff, set()).add(addr & 0xfff)
    bad = []
    for addr, want in verify:
        sec = d.read(addr, SECTOR)
        if len(sec) != SECTOR:
            raise SystemExit(f"short read at 0x{addr:06x} -- cannot verify the "
                             f"firmware, refusing to write")
        if mkpatch_context_digest(sec, edited.get(addr, set())) != want:
            bad.append(addr)

    if bad:
        raise SystemExit(
            "This patch was NOT built for the firmware on this device.\n"
            f"  {len(bad)} of {len(verify)} hook sector(s) differ: "
            + ", ".join(f"0x{a:06x}" for a in bad) + "\n"
            "Installing it would hang the device at boot, recoverable only by\n"
            "opening the case and shorting the debug UART's TX and RX pads.\n"
            "\nNothing has been written -- your device is untouched.\n"
            "There is more than one BF07 firmware build; you need the patch\n"
            "built for yours.")
    print(f"  ok: all {len(verify)} hook sector(s) match\n")


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
    reader, blocks, ref_sha, verify, installed = load_patch(
        open(args.patch, "rb").read())
    print(f"patch: {len(reader)} reader sector(s), {len(blocks)} vendor block(s)")
    print(f"       built from plaintext sha256 {ref_sha.hex()[:16]}...")

    # group the named vendor blocks by their containing sector
    by_sector = {}
    for addr, data in blocks:
        by_sector.setdefault(addr & ~0xfff, {})[addr & 0xfff] = data

    d = connect()
    if already_installed(d, installed):
        print("This device already has exactly this patch installed -- nothing")
        print("to do. Not erasing or rewriting anything.")
        return
    check_firmware(d, verify, blocks, args)
    print(DANGER_OPEN)

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
                             f"but are not. STAY IN ADFU, do not power off.\n"
                             f"  bf07.py restore -b {args.backup}")
        # ...and the converse, which is the one that bites: a block that should
        # carry data but is still erased. An ACK is not proof of a program.
        lost = sum(1 for o in range(0, SECTOR, 32)
                   if data[o:o + 32] != b"\xff" * 32
                   and back[o:o + 32] == b"\xff" * 32)
        if lost:
            raise SystemExit(f"0x{addr:06x}: {lost} block(s) never programmed. "
                             f"STAY IN ADFU, do not power off.\n"
                             f"  bf07.py restore -b {args.backup}")
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
        # "Unchanged" is only evidence of a lost write if this sector was NOT
        # already carrying our patch. Re-installing writes the same plaintext,
        # which encrypts to the same ciphertext, so every edited block reads
        # back equal to its pre-erase value -- and flagging that aborts a
        # perfectly good install. (It did, on a device that already had this
        # exact reader.) A genuinely lost write leaves the block ERASED, which
        # is checked unconditionally below; the ambiguous "unchanged" case is
        # only an error when some edited blocks changed and others did not,
        # which no single consistent outcome explains.
        unchanged = [o for o in edits if back[o:o + 32] == cur[o:o + 32]]
        partial = 0 < len(unchanged) < len(edits)
        for o in range(0, SECTOR, 32):
            if o in edits:
                if back[o:o + 32] == b"\xff" * 32:
                    bad.append(f"+0x{o:03x} still erased (write lost)")
                elif back[o:o + 32] == cur[o:o + 32] and partial:
                    bad.append(f"+0x{o:03x} unchanged while others changed")
            elif back[o:o + 32] != cur[o:o + 32]:
                bad.append(f"+0x{o:03x} clobbered")
        if unchanged and not partial:
            print(f"  0x{sec:06x}: already carried this patch, re-written identically")
        if bad:
            raise SystemExit(
                f"0x{sec:06x}: VERIFY FAILED -- {'; '.join(bad[:4])}\n"
                f"The device is in an unknown state. STAY IN ADFU and do not\n"
                f"power off -- the way back in does not survive a power cycle.\n"
                f"Restore now:\n"
                f"  bf07.py restore -b {args.backup}")
        print(f"  0x{sec:06x}: {len(edits)} block(s) patched, "
              f"{SECTOR//32 - len(edits)} preserved, verified")
    print("\n" + DANGER_CLOSED)
    print(f"If anything is wrong: bf07.py restore -b {args.backup}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("backup"); p.add_argument("-o", "--out", default="bf07-backup.bin"); p.set_defaults(fn=cmd_backup)
    p = sub.add_parser("verify"); p.add_argument("-b", "--backup", required=True); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("restore")
    p.add_argument("-b", "--backup", help="this device's own backup (preferred)")
    p.add_argument("--plain", help="plaintext fw0_sys image, e.g. captured from "
                                   "a second working unit (no backup needed)")
    p.add_argument("--no-erase-detect", action="store_true",
                   help="write every block, even ones that look like erased "
                        "flash decrypted through XIP (rarely correct)")
    p.set_defaults(fn=cmd_restore)
    p = sub.add_parser("install")
    p.add_argument("-b", "--backup", required=True)
    p.add_argument("--patch", help="reader-patch.bin (ADFU only, no serial/image)")
    p.add_argument("-p", "--plain", help="decrypted fw0_sys image (legacy path)")
    p.add_argument("--force", action="store_true",
                   help="install even if the patch was built for a different "
                        "firmware build (hangs the device; needs the TX/RX "
                        "short to recover)")
    p.set_defaults(fn=cmd_install)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
