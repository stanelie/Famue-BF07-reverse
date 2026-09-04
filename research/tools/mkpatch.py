#!/usr/bin/env python3
"""Build a distributable reader patch from ONE plaintext firmware dump.

The point: only the person who makes the patch needs a decrypted image (one
serial dump, ever). Everyone else installs it over ADFU alone -- no serial, no
firmware file -- because the patch carries exactly what the device cannot
provide, and nothing it can.

What the device CAN provide over ADFU (no key needed): the ciphertext of every
sector. Unchanged blocks in the patched sectors are restored from that
ciphertext, verbatim (raw write, bit 31 clear -- proven to preserve ciphertext).

What the device CANNOT provide: the plaintext of the few 32-byte blocks we edit,
because we change bytes inside them and need the surrounding stock instructions
to rebuild the block. That is the ONLY vendor content in the patch -- measured
at 8 blocks, 256 bytes -- and it is firmware-version-specific, identical on every
unit running the same firmware.

Format (reader-patch.bin), 'BF07PAT2':
    magic  'BF07PAT2'
    u32    reader_nsec, then reader_nsec x (u32 flash_addr, 4096 bytes plaintext)
    u32    nblocks,     then nblocks x (u32 flash_addr, 32 bytes patched plaintext)
    32     sha256 of the reference plaintext image (informational / version tag)
    u32    nverify,     then nverify x (u32 sector_addr, 32 bytes sha256)

The trailing verify table is what stops this patch being installed onto the
wrong firmware. Each entry is the sha256 of one hook sector's STOCK
**ciphertext**, so the installer can read those few sectors off the target with
the ordinary `rs` path and refuse if the code it is about to hook is not the
code the patch was built against.

Ciphertext, not plaintext, for a hard practical reason: reading plaintext means
reconfiguring SPI0 for decryption mid-session, which wedges the ADFU payload
until a physical power-cycle -- fine for usb_plaindump.py, which then exits,
fatal for a tool that has to write afterwards. Building the table therefore
needs a stock ciphertext backup (`--ref-cipher`) as well as the plaintext.

That check exists because its absence cost a bricked device: at least two BF07
firmware builds ship in the wild (Jun 30 2025 and May 27 2025), they have an
identical string set but 465 of 480 differing sectors, and installing the wrong
one hangs the device before USB comes up -- recoverable only by shorting TX/RX
with the case open. `ref_sha` alone could not prevent that: it describes the
whole image, which the installer cannot cheaply obtain.

'BF07PAT1' files still load (with an empty verify table) so older patches keep
working, but they install without the protection and say so.
"""
import os, sys, struct, hashlib
_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("BF07_ROOT", os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
import patchset

FW0 = 0x14000
MAGIC = b"BF07PAT2"
MAGIC_V1 = b"BF07PAT1"

def is_reader(addr):
    """Sectors holding OUR code, derived from patchset -- never hardcoded.

    This was a fixed {0x1e7000, 0x1e8000} and silently broke when the reader
    grew to a third sector: 0x1e9000 was then treated as VENDOR code, so the
    patch tried to carry 136 blocks (4352 bytes) of "stock context" that is
    really our own reader sitting in erased padding.
    """
    return patchset.CODE_BASE <= addr < patchset.CODE_LIMIT


def context_digest(sector_bytes, edited_offsets):
    """Hash a hook sector EXCLUDING the blocks this patch overwrites.

    Hashing the whole sector looks right and is wrong: a device that already
    has the reader installed carries our edits there, so a full-sector hash
    rejects it and no one could ever reinstall or update. Only the surrounding
    stock context is common to a fresh device and a patched one -- and the
    context is also the thing we actually need to be sure of, since it is the
    code the hooks branch into.
    """
    keep = b"".join(sector_bytes[o:o + 32] for o in range(0, 0x1000, 32)
                    if o not in edited_offsets)
    return hashlib.sha256(keep).digest()


def build_patch(plain, ref_cipher=None, ref_installed=None):
    """ref_cipher: a full 4 MB STOCK ciphertext backup of a device running this
    firmware. Without it no verify table is emitted and the patch installs
    unchecked, which is how a device got bricked once -- so pass it."""
    sectors = patchset.build(plain)
    reader, blocks = [], []
    edited = {}                       # sector addr -> set of edited offsets
    for addr in sorted(sectors):
        if is_reader(addr):
            reader.append((addr, sectors[addr]))
        else:
            stock = plain[addr - FW0:addr - FW0 + 0x1000]
            for off in range(0, 0x1000, 32):
                if sectors[addr][off:off + 32] != stock[off:off + 32]:
                    blocks.append((addr + off, sectors[addr][off:off + 32]))
                    edited.setdefault(addr, set()).add(off)

    # The stock CIPHERTEXT context of every hook sector, so the installer can
    # confirm the target runs this firmware before it writes anything, using
    # only the ordinary read path.
    verify = []
    if ref_cipher:
        for addr in sorted(edited):
            verify.append((addr, context_digest(
                ref_cipher[addr:addr + 0x1000], edited[addr])))

    # What every patched sector's ciphertext looks like once this patch IS
    # installed, so the installer can recognise a device that already has it
    # and skip the write entirely. Reinstalling identical content is an
    # erase/write cycle of pure flash wear -- and an erase is the one window
    # where losing power leaves the device unbootable.
    installed = []
    if ref_installed:
        for addr in sorted(list(edited) + [a for a, _ in reader]):
            installed.append((addr, hashlib.sha256(
                ref_installed[addr:addr + 0x1000]).digest()))
    out = bytearray(MAGIC)
    out += struct.pack("<I", len(reader))
    for addr, data in reader:
        out += struct.pack("<I", addr) + data
    out += struct.pack("<I", len(blocks))
    for addr, data in blocks:
        out += struct.pack("<I", addr) + data
    out += hashlib.sha256(plain).digest()
    out += struct.pack("<I", len(verify))
    for addr, sha in verify:
        out += struct.pack("<I", addr) + sha
    out += struct.pack("<I", len(installed))
    for addr, sha in installed:
        out += struct.pack("<I", addr) + sha
    return bytes(out), len(reader), len(blocks), len(verify), len(installed)


def load_patch(blob):
    """-> (reader, blocks, ref_sha, verify).

    `verify` is [(sector_addr, digest_of_stock_ciphertext_context)] and
    `installed` is [(sector_addr, digest_of_full_ciphertext_when_installed)];
    both empty for the older 'BF07PAT1' format -- callers must treat empty as
    "cannot check", not as "the target checked out".
    """
    v2 = blob[:8] == MAGIC
    assert v2 or blob[:8] == MAGIC_V1, "not a BF07 patch file"
    p = 8
    nsec, = struct.unpack_from("<I", blob, p); p += 4
    reader = []
    for _ in range(nsec):
        addr, = struct.unpack_from("<I", blob, p); p += 4
        reader.append((addr, blob[p:p + 0x1000])); p += 0x1000
    nblk, = struct.unpack_from("<I", blob, p); p += 4
    blocks = []
    for _ in range(nblk):
        addr, = struct.unpack_from("<I", blob, p); p += 4
        blocks.append((addr, blob[p:p + 32])); p += 32
    ref_sha = blob[p:p + 32]; p += 32
    verify, installed = [], []
    if v2:
        nver, = struct.unpack_from("<I", blob, p); p += 4
        for _ in range(nver):
            addr, = struct.unpack_from("<I", blob, p); p += 4
            verify.append((addr, blob[p:p + 32])); p += 32
        ninst, = struct.unpack_from("<I", blob, p); p += 4
        for _ in range(ninst):
            addr, = struct.unpack_from("<I", blob, p); p += 4
            installed.append((addr, blob[p:p + 32])); p += 32
    return reader, blocks, ref_sha, verify, installed


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--plain", required=True, help="decrypted fw0_sys image")
    ap.add_argument("-o", "--out", default="reader-patch.bin")
    ap.add_argument("--ref-cipher", help="full 4MB STOCK ciphertext backup from a "
                                          "device running this same firmware; without "
                                          "it the patch installs with NO firmware check")
    ap.add_argument("--ref-installed", help="full 4MB backup of a device with THIS patch "
                                             "already installed; lets the installer skip a "
                                             "device that is already up to date")
    a = ap.parse_args()
    plain = open(a.plain, "rb").read()
    ref_cipher = open(a.ref_cipher, "rb").read() if a.ref_cipher else None
    ref_installed = open(a.ref_installed, "rb").read() if a.ref_installed else None
    for label, img in (("--ref-cipher", ref_cipher), ("--ref-installed", ref_installed)):
        if img and len(img) < FW0 + len(plain):
            raise SystemExit(f"{label} is {len(img)} bytes -- expected a "
                             f"full {FW0 + len(plain)}-byte flash image")
    blob, nsec, nblk, nver, ninst = build_patch(plain, ref_cipher, ref_installed)
    open(a.out, "wb").write(blob)
    print(f"{a.out}: {len(blob)} bytes")
    print(f"  reader: {nsec} sector(s) (ours)")
    print(f"  vendor: {nblk} block(s) = {nblk*32} bytes (stock context at hook sites)")
    if nver:
        print(f"  verify: {nver} hook sector hash(es) -- installer refuses on the wrong firmware")
    else:
        print("  verify: NONE -- pass --ref-cipher so the installer can refuse")
        print("          a device running different firmware. Without it a")
        print("          mismatch hangs the device and needs the case opened.")
    if ninst:
        print(f"  installed: {ninst} sector hash(es) -- installer skips an up-to-date device")
    else:
        print("  installed: NONE -- pass --ref-installed so the installer can")
        print("             skip a device that already has this patch, instead")
        print("             of erasing and rewriting it for nothing.")
    print(f"  ref sha256: {hashlib.sha256(plain).hexdigest()[:16]}...")
