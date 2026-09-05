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
IN_MENU = False        # set by menu(): changes how errors tell you to recover
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


def unmount_volumes(busy, attempts=4):
    """Unmount the device's own volumes before switching it into ADFU.

    -> list of (source, error) for whatever is still mounted at the end.

    Three things this has to cope with, all seen on real hardware:

    * The tool is usually run under sudo, but the mount belongs to the desktop
      session, so `udisksctl` is run AS THE INVOKING USER via sudo -u. Run as
      root it talks to udisks with the wrong identity for a session mount.
    * The device has often just rebooted and re-enumerated, so udisks may
      auto-mount it again a moment after we unmount. Hence retrying rather than
      trying once and believing the answer.
    * Failures were previously discarded, so a user got "could not be unmounted"
      with no hint why. The actual message is now kept and shown.

    sync() first, because entering ADFU reboots the device: anything still in
    the page cache for that filesystem is lost, and someone who just copied a
    book across has no reason to expect that.
    """
    import subprocess
    print("unmounting the device's storage first:")
    for src, mnt in busy:
        print(f"  {src} on {mnt}")
    try:
        subprocess.run(["sync"], timeout=30)
    except Exception:
        pass

    # sudo sets SUDO_USER; without it we are already the right user.
    who = os.environ.get("SUDO_USER")
    errs = {}
    for attempt in range(attempts):
        for src, _ in list(busy):
            cmds = []
            if who:
                cmds.append(["sudo", "-u", who, "udisksctl", "unmount", "-b", src])
            cmds.append(["udisksctl", "unmount", "-b", src])
            cmds.append(["umount", src])
            for cmd in cmds:
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        errs.pop(src, None)
                        break
                    msg = (r.stderr or r.stdout or "").strip().splitlines()
                    if msg:
                        errs[src] = msg[-1]
                except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                    errs[src] = f"{cmd[0]}: {e.__class__.__name__}"
        busy = mounted_volumes()
        if not busy:
            return []
        if attempt < attempts - 1:
            time.sleep(1.0)          # give udisks time to settle after a remount
    return [(src, errs.get(src, "still mounted")) for src, _ in busy]


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
        stuck = unmount_volumes(busy)
        if stuck:
            raise SystemExit(
                "The device's storage is still mounted and could not be\n"
                "unmounted automatically:\n"
                + "".join(f"    {src}: {why}\n" for src, why in stuck) +
                "\nEntering ADFU detaches usb-storage and reboots the device,\n"
                "which would pull that filesystem out from under the kernel.\n"
                "Close anything using the drive -- a file manager window, or a\n"
                "terminal sitting in it -- then unmount it by hand:\n"
                + "".join(f"    udisksctl unmount -b {src}\n" for src, _ in stuck))

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
            # Appearing on the bus is not the same as being ready for a bulk
            # transfer: the device has just re-enumerated and the host is still
            # configuring it. Returning the instant it appears makes the very
            # next thing -- uploading the payload -- fail intermittently. That
            # race was invisible while commands were typed by hand, and started
            # firing once the tool began rebooting the device itself and running
            # the next operation immediately.
            time.sleep(1.0)
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

    def reboot(self):
        """Leave ADFU and boot the firmware, without a button press.

        ADFU has no working software reset (its SCSI reset opcode does
        nothing), so this goes the long way round: clear the reboot type in
        RTC_REMAIN3 to magic|NORMAL -- otherwise the boot ROM re-reads
        GOTO_ADFU and lands straight back here -- then let the watchdog fire.

        The second write resets the chip mid-command, so no ACK comes back and
        none is waited for. Returns True if the device left ADFU.
        """
        try:
            self.cmd(b"wm", 4, 0x4000C03C, expect=4,
                     data=struct.pack("<I", 0x42520000))
            self.cmd(b"wm", 4, 0x4000C020, expect=4, data=struct.pack("<I", 0x5F))
        except Exception:
            pass
        for _ in range(20):
            time.sleep(0.25)
            if find(PID_ADFU) is None:
                return True
        return False

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
    # A device that has only just enumerated can refuse the first transfer, so
    # a single failure is not proof it is wedged -- retry before giving up and
    # sending the user to the reset button.
    last = None
    for attempt in range(3):
        try:
            a = Adfu(timeout=8000)
            a.write(OP_WRITE, 0x01010000, blob)
            a.cmd(OP_EXEC1, 0x01010000)
            usb.util.dispose_resources(a.d)
            del a
            break
        except usb.core.USBError as e:
            last = e
            if attempt == 2:
                raise SystemExit(
                    f"ADFU is not responding ({e}).\n"
                    "Two things cause this. Either the device had not finished\n"
                    "coming up -- in which case simply running this again works --\n"
                    "or ADFU is wedged, which needs a power-cycle (reset button)\n"
                    "and only a power-cycle clears.\n"
                    "Nothing has been written either way.")
            time.sleep(1.5)
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
    reboot_after(d, args)


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
    reboot_after(d, args)


# Why losing power here is the dangerous case, for anyone reading the source:
# the firmware is incomplete between the first erase and the verify, and the
# flag that would let the tool back in lives in RTC_REMAIN3, which does NOT
# survive a power cycle (measured: set it, power cycle, reads back 0). The
# device would boot the half-written firmware, and mbrec does not check it.
# The user does not need any of that -- they need to know not to unplug it.
DANGER_OPEN = """
!!  DO NOT DISCONNECT POWER, and do not unplug the USB cable, until this
!!  command prints that it is finished.
"""

DANGER_CLOSED = "verified -- safe to disconnect now."
POWER_CYCLE = "Power-cycle the device to boot it."


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
    reboot_after(d, args)


def reboot_after(d, args):
    """Bring the device back to normal after an operation that ends a session.

    Entering ADFU is a reboot into a mode with no UI, so a device left there
    looks dead: blank screen, no drive, nothing but a bare USB id. Anything
    that finishes a job should hand it back running.

    Done after EVERY operation, including the read-only ones. An earlier
    version skipped backup and verify, reasoning that leaving the device in
    ADFU saved the user re-selecting disk-drive mode before the next command.
    That premise was simply wrong -- the device comes back into disk mode by
    itself when it is on USB -- and the cost of the mistake was real: leaving a
    payload running means the NEXT operation has to decide whether it is still
    alive, and a false negative there uploads a second payload and wedges ADFU
    until a power-cycle. Rebooting makes every operation start from the same
    clean state instead of inheriting one.
    """
    if getattr(args, "no_reboot", False):
        print(POWER_CYCLE)
        return
    print("rebooting the device...")
    if not d.reboot():
        print(f"  could not reboot it from here -- {POWER_CYCLE.lower()}")
        return
    # Leaving ADFU is not the same as being usable again: the device boots for
    # a good few seconds before it re-appears as a disk. Returning as soon as
    # it leaves ADFU makes the NEXT operation fail with "No BF07 found" -- which
    # is what rebooting after every operation caused, and reads to the user as
    # the tool being broken rather than merely early.
    if wait_for_normal():
        print("  it is running again.")
    else:
        print("  it rebooted, but has not re-appeared over USB yet.")
        print("  Give it a moment, or pick disk drive mode on the device.")


def wait_for_normal(timeout=40):
    """Wait for the device to finish booting and offer its drive again."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if find(PID_NORMAL):
            time.sleep(1.0)          # let the host finish configuring it
            return True
        time.sleep(0.5)
    return False


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
        reboot_after(d, args)
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
    reboot_after(d, args)


def cmd_install(args):
    if not os.path.exists(args.backup):
        raise SystemExit("refusing to install without a backup: run `backup` first")
    backup = open(args.backup, "rb").read()
    sys.path.insert(0, HERE)

    # No --patch means "use the ones shipped alongside me", which is what a
    # release bundle always wants; patch_files() resolves that and fails with
    # a useful message if there are none. Demanding the flag here made
    # `install -b backup.bin` fail for the very users the default exists for.
    if not args.plain:
        return install_patch(args, backup)

    from patchset import build          # legacy path only: needs a full image

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
    print(DANGER_CLOSED + " " + POWER_CYCLE)
    if IN_MENU:
        print("If anything is wrong, restore from your backup (option 5).")
    else:
        print(f"If anything is wrong, restore from your backup:\n"
              f"  bf07.py restore -b {args.backup}")


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


def patch_files(spec):
    """--patch may name one file or a directory of them. More than one BF07
    firmware build ships, so a release carries a patch per build and the right
    one is chosen by reading the device, not by asking the user to know which
    firmware they have -- they cannot tell. The on-device version string is not
    even reliable: a unit running May-27 code reported the Jun-30 version,
    because the version metadata lives outside fw0_sys."""
    if spec is None:
        here = os.path.join(HERE, os.pardir, "reference")
        if not os.path.isdir(here):
            raise SystemExit("give --patch <file or directory>")
        spec = here
    if os.path.isdir(spec):
        found = sorted(glob.glob(os.path.join(spec, "reader-patch*.bin")))
        if not found:
            raise SystemExit(f"no reader-patch*.bin in {spec}")
        return found
    return [spec]


def load_candidate(path):
    from mkpatch import load_patch
    reader, blocks, ref_sha, verify, installed = load_patch(open(path, "rb").read())
    return {"path": path, "reader": reader, "blocks": blocks,
            "ref_sha": ref_sha, "verify": verify, "installed": installed}


UNKNOWN_FIRMWARE = """
None of the available patches match the firmware on this device.

This is not a fault and nothing has been written -- your device is untouched.
It means your BF07 runs a build we have not seen. More than one exists: two are
known so far, and a unit bought LATER shipped the OLDER one, so age tells you
nothing about which you have.

Two ways forward:

 1. HELP US SUPPORT YOUR BUILD -- preferred.
    You already have a backup from `bf07.py backup`. Open an issue at
    https://github.com/stanelie/Famue-BF07-reverse/issues and attach it, or a
    link to it -- it is 4 MB. That image is exactly what is needed to build a
    patch for your build, and it can be flashed onto a development unit to
    test one safely. Mention roughly when you bought the device.

 2. FLASH A FIRMWARE WE DO SUPPORT, then patch that.
    The repository archives stock images for the known builds, under
    research/firmware/. Writing one replaces fw0_sys only -- your settings,
    calibration and resources are left alone -- after which the matching patch
    installs normally:
        bf07.py restore --plain <a supported stock fw0_sys image>
    KEEP YOUR OWN BACKUP FIRST. It is the only copy of YOUR build in
    existence, and the thing that makes option 1 possible for everyone else.
"""


def select_patch(d, cands, args):
    """Pick the patch built for the firmware actually on this device.

    Decided by reading the device, never by trusting a version string, and an
    unrecognised build is refused outright: installing a patch whose hooks were
    computed against different code hangs the device before USB comes up, and
    the only way back is opening the case to short TX/RX.
    """
    for c in cands:
        if c["installed"] and already_installed(d, c["installed"]):
            print(f"\n{os.path.basename(c['path'])} is already installed on this "
                  f"device -- nothing to do.")
            print("Not erasing or rewriting anything.")
            raise SystemExit(0)

    matched = []
    for c in cands:
        if not c["verify"]:
            continue                      # BF07PAT1: carries no way to check
        edited = {}
        for a, _ in c["blocks"]:
            edited.setdefault(a & ~0xfff, set()).add(a & 0xfff)
        if all(mkpatch_context_digest(d.read(a, SECTOR), edited.get(a, set())) == w
               for a, w in c["verify"]):
            matched.append(c)

    if len(matched) == 1:
        print(f"\nfirmware recognised -> {os.path.basename(matched[0]['path'])}")
        return matched[0]
    if len(matched) > 1:
        raise SystemExit("more than one patch claims this firmware -- refusing "
                         "to guess:\n" +
                         "".join(f"  {os.path.basename(c['path'])}\n" for c in matched))

    unchecked = [c for c in cands if not c["verify"]]
    if unchecked and getattr(args, "force", False):
        print("!! --force with an uncheckable (BF07PAT1) patch. If the reader")
        print("!! does not start, restore immediately.\n")
        return unchecked[0]
    raise SystemExit(UNKNOWN_FIRMWARE)


INSTALL_RISK = """
About to modify the firmware on your BF07.
--------------------------------------------------------------------
This is the only step that writes to the device. Read this first.

WHAT NORMALLY HAPPENS
  21 sectors are written and each one read back and checked. If a check
  fails the tool stops and tells you to restore, and
      bf07.py restore -b {backup}
  puts the device back byte-for-byte. That path has been used many times
  and always worked.

THE RISK THAT IS NOT COVERED BY THAT
  Restoring needs the device to still appear over USB. If a write leaves
  it unable to boot far enough to bring USB up, it will not appear at
  all -- and then NO software recovery is possible, including the
  command above.

  Getting back from that state means OPENING THE CASE, shorting the two
  debug UART pads (TX and RX) together, and pressing reset. That forces
  the device into firmware-update mode before it tries to boot, after
  which restoring works normally. It needs a jumper wire or tweezers,
  not soldering -- but it does need the case open.

  This happened twice while developing this tool, and recovery worked
  both times. It is a real possibility, not a theoretical one.

  If you are not willing or able to open the case should it come to
  that, stop here. Your device is completely untouched so far.
--------------------------------------------------------------------
"""


def confirm_install(args):
    """Informed consent before the only step that writes.

    The firmware check makes a wrong-firmware install very unlikely, but
    "unlikely" is not the same as "recoverable over USB". The failure that
    matters is a device that will not enumerate, because every software
    recovery path -- including restore -- needs USB. That one needs the case
    open, and the user deserves to know before the first erase, not after.
    """
    if getattr(args, "yes", False):
        return
    print(INSTALL_RISK.format(backup=args.backup))
    if not sys.stdin.isatty():
        raise SystemExit(
            "Not running interactively, so this cannot be confirmed. Re-run "
            "with --yes if you have read the above and accept it.")
    try:
        ans = input("Type YES to install, anything else to stop: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\naborted -- nothing was written.")
    if ans != "YES":
        raise SystemExit("aborted -- nothing was written, your device is "
                         "untouched.")
    print()


def install_patch(args, backup):
    """Install a distributable patch file -- ADFU only, no serial, no image.

    Each patched sector is rebuilt block by block: the few blocks the patch
    names are written as plaintext (the SoC encrypts them with the device's own
    key), and every other block is restored from the device's OWN ciphertext,
    verbatim. So the tool never needs the device's plaintext, only the 256 bytes
    of stock context the patch carries -- and it works whether the flash key is
    per-device or global, because nothing here assumes anything about the key.
    """
    paths = patch_files(args.patch)
    print(f"{len(paths)} patch file(s) available:")
    cands = []
    for p in paths:
        c = load_candidate(p)
        cands.append(c)
        print(f"  {os.path.basename(p)}: {len(c['reader'])} reader sector(s), "
              f"for firmware {c['ref_sha'].hex()[:12]}...")

    d = connect()
    chosen = select_patch(d, cands, args)
    reader, blocks = chosen["reader"], chosen["blocks"]
    verify, installed = chosen["verify"], chosen["installed"]
    print(f"\nusing {os.path.basename(chosen['path'])}")

    # group the named vendor blocks by their containing sector
    by_sector = {}
    for addr, data in blocks:
        by_sector.setdefault(addr & ~0xfff, {})[addr & 0xfff] = data

    confirm_install(args)
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
                f"The device is in an unknown state. Do not power it off.\n" +
                ("Restore now, from the menu: option 5." if IN_MENU
                 else f"Restore now:\n  bf07.py restore -b {args.backup}"))
        print(f"  0x{sec:06x}: {len(edits)} block(s) patched, "
              f"{SECTOR//32 - len(edits)} preserved, verified")
    print("\n" + DANGER_CLOSED)
    if IN_MENU:
        print("If anything is wrong, restore from your backup (option 5).")
    else:
        print(f"If anything is wrong, restore from your backup:\n"
              f"  bf07.py restore -b {args.backup}")
    # Only here, i.e. only once every sector has been written AND read back and
    # compared. A reboot on any other path would boot a half-written fw0_sys.
    if not getattr(args, "no_reboot", False):
        print("\nrebooting the device...")
        if d.reboot():
            print("  it is booting the new reader now.")
        else:
            print("  could not reboot it from here -- press reset on the device.")


def cmd_font(args):
    """Copy the user font onto the drive the device already exposes.

    Note this is the ONE operation that needs the volume MOUNTED -- everything
    else here unmounts it to enter ADFU. Nothing is flashed: the reader opens
    this file itself at runtime, so a bad or missing font is a cosmetic problem,
    not a recoverable-only-with-a-jumper one.
    """
    src = args.font
    if not src:
        # fonts/ sits beside tools/ in a release bundle, but one level higher
        # in the source tree (research/tools -> repo root). Try both rather
        # than working only where it happened to be developed.
        for cand in (os.path.join(HERE, os.pardir, "fonts", "custom.font"),
                     os.path.join(HERE, os.pardir, os.pardir, "fonts", "custom.font")):
            if os.path.isfile(cand):
                src = cand
                break
    if not src or not os.path.isfile(src):
        raise SystemExit("no bundled font found -- pass one with --font <file.font>")
    vols = mounted_volumes()
    if not vols:
        raise SystemExit(
            "The device's drive is not mounted, so there is nowhere to copy to.\n"
            "Connect the BF07 over USB and choose disk drive mode on its own\n"
            "boot menu, wait for the drive to appear, then try again.\n"
            "(Every other option here needs the drive UNMOUNTED -- this one is\n"
            "the exception, because it copies a file rather than flashing.)")
    if len(vols) > 1:
        raise SystemExit("more than one volume from this device is mounted:\n"
                         + "".join(f"    {s} on {m}\n" for s, m in vols))
    _, mnt = vols[0]
    dst = os.path.join(mnt, "custom.font")
    import shutil
    shutil.copy2(src, dst)
    try:
        import subprocess
        subprocess.run(["sync"], timeout=30)
    except Exception:
        pass
    print(f"copied {os.path.basename(src)} -> {dst} ({os.path.getsize(dst)} bytes)")
    print("Now pick the custom font in the reader's font menu on the device.")
    print("(The row is named \"Fangsong Small Font\" unless the optional")
    print(" serial-only relabel step has been run -- it is the right row.)")


MENU_HEADER = """
==============================================================================
  BF07 reader installer
==============================================================================

  This installs a replacement ebook reader on a Famue BF07, over USB.

  BACK UP FIRST. Option 1 saves your device's firmware to a file. That file
  is the only way back, so keep a copy somewhere other than this computer.

  BEFORE YOU INSTALL, understand the one risk a backup does not cover:

    Restoring needs the device to still show up over USB. If a write leaves
    it unable to boot far enough to do that, NO software recovery is
    possible -- including the restore option below.

    Getting back from that means OPENING THE CASE, shorting the two debug
    UART pads (TX and RX) together, and pressing reset. A jumper wire or
    tweezers will do; no soldering.
"""


def _ask_path(prompt, default=None, must_exist=False):
    while True:
        d = f" [{default}]" if default else ""
        try:
            v = input(f"{prompt}{d}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\ncancelled.")
        v = v or (default or "")
        if not v:
            print("  give a filename.")
            continue
        v = os.path.expanduser(v)
        if must_exist and not os.path.isfile(v):
            print(f"  no such file: {v}")
            continue
        return v


def _backups_here():
    """Existing backups, newest first -- so restore/verify can offer the one
    they almost certainly mean instead of asking them to remember a path."""
    found = [f for f in glob.glob("*.bin") if os.path.getsize(f) == FLASH_SIZE]
    return sorted(found, key=os.path.getmtime, reverse=True)


def leave_device_running():
    """Never end a session with the device parked in ADFU.

    backup and verify deliberately leave it there, because the next menu choice
    would otherwise need the user to pick disk-drive mode on the device again.
    That is fine mid-session and not fine at the end: ADFU has no UI, so a
    device abandoned in it looks broken -- blank screen, no drive -- and the
    way out is a button press the user has no reason to know about.
    """
    if find(PID_ADFU) is None:
        return
    print("the device is still in update mode -- rebooting it...")
    try:
        d = Device()
        if d.reboot():
            print("  it is running again.")
            return
    except Exception:
        pass
    print(f"  could not reboot it from here -- {POWER_CYCLE.lower()}")


def install_all_in_one(known):
    """Back up, check the backup, then install -- as one choice.

    These three were separate menu entries, but they are never useful apart:
    the installer refuses to run without a backup, and a backup nobody checked
    is not yet a way back. Splitting them asked the user to know the order and
    to not stop halfway, which is exactly the sort of thing a person does once
    and regrets.

    Stops at the first failure. A backup that does not verify is a reason NOT
    to write to the device, so the install never starts.
    """
    out = _ask_path("  save your backup as", default="mybf07.bin")
    if os.path.exists(out):
        try:
            ans = input(f"  {out} exists. Overwrite it? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\ncancelled.")
        if ans not in ("y", "yes"):
            raise SystemExit("kept the existing file -- nothing was done.")

    print("\n--- 1/3  backing up ---")
    cmd_backup(argparse.Namespace(out=out, no_reboot=False))

    print("\n--- 2/3  checking that backup ---")
    cmd_verify(argparse.Namespace(backup=out, no_reboot=False))
    # cmd_verify prints its own verdict; re-read it here so a mismatch STOPS us.
    d = connect()
    bad = differing(d, open(out, "rb").read())
    reboot_after(d, argparse.Namespace(no_reboot=False))
    if bad:
        raise SystemExit(
            f"\nThe backup does not match the device ({len(bad)} sector(s) "
            f"differ).\nNot installing: a backup you cannot trust is not a way "
            f"back.\nTry again, and if it keeps happening please open an issue.")

    print("\n--- 3/3  installing ---")
    cmd_install(argparse.Namespace(backup=out, patch=None, plain=None,
                                   yes=True, force=False, no_reboot=False))


def menu():
    """Interactive front end, so the whole job is doable without composing a
    single command line. The risk warning lives here, once, above the choices:
    the user reads it before picking anything rather than being interrupted by
    it after they have already decided."""
    global IN_MENU
    IN_MENU = True
    print(MENU_HEADER)
    while True:
        known = _backups_here()
        if known:
            print(f"\n  backups found here: {', '.join(known[:3])}"
                  + (" ..." if len(known) > 3 else ""))
        print("""
  1) INSTALL THE READER  -- backs up, checks the backup, then installs
  2) Back up only                       (safe, reads only)
  3) Check a backup against the device  (safe, reads only)
  4) Copy the custom font to the drive  (safe, just a file copy)
  5) Restore from a backup / go stock   (WRITES to the device)
  6) Quit
""")
        try:
            c = input("  choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            leave_device_running()
            return
        print()
        try:
            if c == "1":
                install_all_in_one(known)
            elif c == "2":
                out = _ask_path("  save backup as", default="mybf07.bin")
                cmd_backup(argparse.Namespace(out=out, no_reboot=False))
            elif c == "3":
                b = _ask_path("  backup file", default=(known[0] if known else None),
                              must_exist=True)
                cmd_verify(argparse.Namespace(backup=b, no_reboot=False))
            elif c == "4":
                cmd_font(argparse.Namespace(font=None))
            elif c == "5":
                b = _ask_path("  backup to restore from",
                              default=(known[0] if known else None), must_exist=True)
                cmd_restore(argparse.Namespace(
                    backup=b, plain=None, no_erase_detect=False))
            elif c == "6":
                leave_device_running()
                return
            else:
                print("  pick 1-6.")
                continue
        except SystemExit as e:
            # A refusal is information, not a reason to close the program --
            # "your firmware is not recognised" should leave the user at the
            # menu able to take a backup and send it in, not staring at a shell.
            if str(e) and str(e) != "0":
                print(f"\n{e}")
        print("\n" + "-" * 78)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("backup")
    p.add_argument("-o", "--out", default="bf07-backup.bin")
    p.add_argument("--no-reboot", action="store_true",
                   help="leave the device in update mode afterwards")
    p.set_defaults(fn=cmd_backup)
    p = sub.add_parser("verify")
    p.add_argument("-b", "--backup", required=True)
    p.add_argument("--no-reboot", action="store_true",
                   help="leave the device in update mode afterwards")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("restore")
    p.add_argument("-b", "--backup", help="this device's own backup (preferred)")
    p.add_argument("--plain", help="plaintext fw0_sys image, e.g. captured from "
                                   "a second working unit (no backup needed)")
    p.add_argument("--no-reboot", action="store_true",
                   help="leave the device in update mode instead of rebooting it")
    p.add_argument("--no-erase-detect", action="store_true",
                   help="write every block, even ones that look like erased "
                        "flash decrypted through XIP (rarely correct)")
    p.set_defaults(fn=cmd_restore)
    p = sub.add_parser("font")
    p.add_argument("-f", "--font", help="a .font file (default: the bundled one)")
    p.set_defaults(fn=cmd_font)
    p = sub.add_parser("install")
    p.add_argument("-b", "--backup", required=True)
    p.add_argument("--patch", help="reader-patch*.bin, or a DIRECTORY of them; the one matching your firmware is chosen automatically "
                                "(default: the bundle's reference/ directory)")
    p.add_argument("-p", "--plain", help="decrypted fw0_sys image (legacy path)")
    p.add_argument("--no-reboot", action="store_true",
                   help="leave the device in ADFU after installing instead of "
                        "rebooting it")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt (you have read the "
                        "recovery warning and accept it)")
    p.add_argument("--force", action="store_true",
                   help="install even if the patch was built for a different "
                        "firmware build (hangs the device; needs the TX/RX "
                        "short to recover)")
    p.set_defaults(fn=cmd_install)
    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        return menu()            # no arguments at all -> interactive
    args.fn(args)


if __name__ == "__main__":
    main()
