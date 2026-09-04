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

Format (reader-patch.bin):
    magic  'BF07PAT1'
    u32    reader_nsec, then reader_nsec x (u32 flash_addr, 4096 bytes plaintext)
    u32    nblocks,     then nblocks x (u32 flash_addr, 32 bytes patched plaintext)
    32     sha256 of the reference plaintext image (informational / version tag)
"""
import os, sys, struct, hashlib
_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("BF07_ROOT", os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
import patchset

FW0 = 0x14000
MAGIC = b"BF07PAT1"

def is_reader(addr):
    """Sectors holding OUR code, derived from patchset -- never hardcoded.

    This was a fixed {0x1e7000, 0x1e8000} and silently broke when the reader
    grew to a third sector: 0x1e9000 was then treated as VENDOR code, so the
    patch tried to carry 136 blocks (4352 bytes) of "stock context" that is
    really our own reader sitting in erased padding.
    """
    return patchset.CODE_BASE <= addr < patchset.CODE_LIMIT


def build_patch(plain):
    sectors = patchset.build(plain)
    reader, blocks = [], []
    for addr in sorted(sectors):
        if is_reader(addr):
            reader.append((addr, sectors[addr]))
        else:
            stock = plain[addr - FW0:addr - FW0 + 0x1000]
            for off in range(0, 0x1000, 32):
                if sectors[addr][off:off + 32] != stock[off:off + 32]:
                    blocks.append((addr + off, sectors[addr][off:off + 32]))
    out = bytearray(MAGIC)
    out += struct.pack("<I", len(reader))
    for addr, data in reader:
        out += struct.pack("<I", addr) + data
    out += struct.pack("<I", len(blocks))
    for addr, data in blocks:
        out += struct.pack("<I", addr) + data
    out += hashlib.sha256(plain).digest()
    return bytes(out), len(reader), len(blocks)


def load_patch(blob):
    assert blob[:8] == MAGIC, "not a BF07 patch file"
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
    ref_sha = blob[p:p + 32]
    return reader, blocks, ref_sha


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--plain", required=True, help="decrypted fw0_sys image")
    ap.add_argument("-o", "--out", default="reader-patch.bin")
    a = ap.parse_args()
    plain = open(a.plain, "rb").read()
    blob, nsec, nblk = build_patch(plain)
    open(a.out, "wb").write(blob)
    print(f"{a.out}: {len(blob)} bytes")
    print(f"  reader: {nsec} sector(s) (ours)")
    print(f"  vendor: {nblk} block(s) = {nblk*32} bytes (stock context at hook sites)")
    print(f"  ref sha256: {hashlib.sha256(plain).hexdigest()[:16]}...")
