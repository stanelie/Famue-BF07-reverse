#!/usr/bin/env python3
"""Relocate the reader's hook sites onto a different firmware build.

    retarget.py -r <reference fw0_sys plain> -t <target fw0_sys plain>

Prints a ready-to-paste patchset.BUILDS entry, or explains which site it could
not place. Nothing is written and no device is touched.

WHY THIS IS NEEDED
    At least two BF07 builds ship (Jun 30 2025 and May 27 2025). They have an
    identical string set and the same features, but are separately compiled and
    linked, so code sits at different addresses -- and a patch whose hooks point
    at the old addresses lands in unrelated code and hangs the device at boot.

WHAT IT EXPLOITS
    The shift is not one constant. Measured between those two builds: the ebook
    module does not move at all, the driver/library area moves by -0x24, and the
    font-menu data table moves by -0x20. So every site is located
    independently, by signature, rather than by assuming an offset.

THE TRAP THIS AVOIDS
    A naive signature is the bytes AT the site, and that fails on exactly the
    sites that matter most: several hooks replace a `bl`/`b.w`, and a branch
    encodes its own target, so its bytes differ between builds even when the
    site has not moved at all. Those look "missing" and would be silently
    mis-located. The signature here is therefore the surrounding context with
    the 4-byte site itself treated as a hole.
"""
import argparse
import hashlib
import sys

XIP = 0x10000000
CTX = 16          # bytes of context each side; enough to be unique in ~2 MB


def unstable_bytes(win):
    """Byte indices inside `win` that belong to a Thumb-2 32-bit branch.

    A branch encodes its own target, so its bytes differ between two builds
    even where nothing moved. Comparing them is what made a first version of
    this script report "the surrounding code changed" for six sites that had
    not moved an inch -- the context simply contained calls.

    First halfword 0xF000-0xF7FF with a second of 0x8000-0xFFFF is BL/B.W
    (and friends). Deliberately over-inclusive: wrongly ignoring a few bytes
    costs a little uniqueness, wrongly comparing them costs a false negative.
    """
    bad = set()
    for i in range(0, len(win) - 3, 2):
        hw1 = win[i] | (win[i + 1] << 8)
        hw2 = win[i + 2] | (win[i + 3] << 8)
        if 0xF000 <= hw1 <= 0xF7FF and hw2 >= 0x8000:
            bad.update((i, i + 1, i + 2, i + 3))
    return bad


def resolve(ref, tgt, addr):
    """-> (new_addr, how) or (None, why). Never guesses: an ambiguous match is
    a failure, because writing a hook to the wrong address bricks the device.

    Tries symmetric context first, then a body-only window. Several hooks sit
    on a function's first instruction, and the bytes BEFORE it are the tail of
    whatever function the linker happened to place previously -- unrelated code
    that moves independently. Anchoring those on preceding context reports a
    false "the code changed" for a site that simply starts a function.
    """
    for pre_ctx, post_ctx, label in ((CTX, CTX, "context"),
                                     (0, 40, "body")):
        r = _try(ref, tgt, addr, pre_ctx, post_ctx)
        if r[0] is not None:
            return r[0], f"{label} match" + r[1]
        last = r[1]
    return None, last


def _try(ref, tgt, addr, pre_ctx, post_ctx):
    ctx = pre_ctx
    o = addr - XIP
    if not (pre_ctx <= o < len(ref) - post_ctx):
        return None, "address outside the image"
    win = ref[o - pre_ctx:o + post_ctx]        # the site sits at index pre_ctx
    skip = unstable_bytes(win) | {ctx, ctx + 1, ctx + 2, ctx + 3}
    stable = [i for i in range(len(win)) if i not in skip]
    if len(stable) < 8:
        return None, "not enough stable context to identify this site"

    def matches(base):
        if base < 0 or base + len(win) > len(tgt):
            return False
        return all(tgt[base + i] == win[i] for i in stable)

    if matches(o - pre_ctx):
        same = tgt[o:o + 4] == ref[o:o + 4]
        return addr, "" if same else ", site is a branch (offset differs)"

    # Anchor the search on the longest contiguous stable run, then verify the
    # rest -- scanning every offset in a 1.9 MB image byte-by-byte is far too
    # slow in Python.
    runs, cur = [], []
    for i in stable:
        if cur and i == cur[-1] + 1:
            cur.append(i)
        else:
            if cur:
                runs.append(cur)
            cur = [i]
    if cur:
        runs.append(cur)
    anchor = max(runs, key=len)
    if len(anchor) < 4:
        return None, "no usable anchor (context is almost all branches)"
    sig = win[anchor[0]:anchor[-1] + 1]

    hits, s = [], 0
    while True:
        i = tgt.find(sig, s)
        if i < 0:
            break
        base = i - anchor[0]
        if matches(base):
            hits.append(base + pre_ctx)
        s = i + 1
    if len(hits) == 1:
        new = hits[0] + XIP
        same = tgt[hits[0]:hits[0] + 4] == ref[o:o + 4]
        return new, "" if same else ", site is a branch (offset differs)"
    if not hits:
        return None, "no match -- the surrounding code really changed"
    return None, f"{len(hits)} matches -- ambiguous"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-r", "--reference", required=True,
                    help="plaintext fw0_sys the current hook addresses belong to")
    ap.add_argument("-t", "--target", required=True,
                    help="plaintext fw0_sys of the build to retarget onto")
    a = ap.parse_args()

    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    import patchset as PS

    ref = open(a.reference, "rb").read()
    tgt = open(a.target, "rb").read()
    if len(ref) != len(tgt):
        print(f"note: images differ in length ({len(ref)} vs {len(tgt)})", file=sys.stderr)

    sites = ([(n, v, "BL") for n, v in PS.BL_HOOKS.items()]
             + [(n, v, "BW") for n, v in PS.BW_HOOKS.items()]
             + [(f"WORD:{hex(k)}", k, "WORD") for k in PS.WORD_PATCHES]
             + [("CONT_Y", 0x1004A1FC, "IMM"), ("CONT_SUB", 0x1004A222, "IMM")])

    out, failed = {}, []
    for name, addr, kind in sites:
        new, how = resolve(ref, tgt, addr)
        if new is None:
            failed.append((name, addr, how))
            print(f"  {name:24s} 0x{addr:08x} -> FAILED: {how}")
        else:
            out[name] = new
            d = new - addr
            print(f"  {name:24s} 0x{addr:08x} -> 0x{new:08x}  "
                  f"delta {d:+#x}  ({how})")

    # The reader needs its free space to be genuinely free on the target too.
    print(f"\n  target plaintext sha256: {hashlib.sha256(tgt).hexdigest()}")
    if failed:
        raise SystemExit(f"\n{len(failed)} site(s) could not be placed -- do NOT "
                         f"build a patch from this. Each one is a hook that would "
                         f"land in unrelated code.")
    print("\n  all sites placed. Paste into patchset.BUILDS:\n")
    print(f'    "{hashlib.sha256(tgt).hexdigest()}": {{')
    for n, v in out.items():
        print(f'        "{n}": 0x{v:08X},')
    print("    },")


if __name__ == "__main__":
    main()
