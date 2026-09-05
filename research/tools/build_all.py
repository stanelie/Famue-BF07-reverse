#!/usr/bin/env python3
"""Rebuild the reader and its patch for EVERY supported firmware build.

    build_all.py [--installed-jun30 <backup>] [--installed-may27 <backup>]

Run this after any change to the reader. It regenerates the per-build sources,
compiles each, builds each patch, and runs every check that does not need
hardware. What it cannot do is prove the reader RUNS -- see "what this does not
cover" at the bottom.

WHY THIS EXISTS
    The May 27 build is not a fork. Its header and its C source are GENERATED
    from the Jun 30 ones with tools/mkfwh.py, so a change to the reader
    propagates automatically. The danger is that it propagates SILENTLY and
    wrongly: the reader reaches vendor code by absolute address, and an address
    that is not relocated points into unrelated code on the other build. That
    is not a subtle failure -- it boot-loops the device and needs the case
    opened to recover.

    So the point of this script is not convenience. It is that "I changed the
    reader and only tested it on my own device" must not be able to ship.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
READER = os.path.join(RESEARCH, "reader")
FW = os.path.join(RESEARCH, "firmware")
sys.path.insert(0, HERE)

CFLAGS = ("--target=thumbv7em-none-eabi -mthumb -mcpu=cortex-m4 -O2 -ffreestanding "
          "-fno-builtin -fomit-frame-pointer -ffunction-sections -g -Wall").split()

# The reference build, whose sources are the ones a human edits.
REF = {"name": "jun30", "plain": f"{FW}/stock-fw0_sys-plain-2025-06-30.bin",
       "cipher": f"{FW}/stock-full-flash-2025-06-30.bin",
       "inc": "include", "src": "src/main.c",
       "bin": "reader.bin", "elf": "reader.elf", "patch": "reader-patch.bin"}

# Every OTHER build, generated from the reference.
TARGETS = [
    {"name": "may27", "plain": f"{FW}/stock-fw0_sys-plain-2025-05-27.bin",
     "cipher": f"{FW}/stock-full-flash-2025-05-27.bin",
     "inc": "include-may27", "src": "src-may27/main.c",
     "bin": "reader-may27.bin", "elf": "reader-may27.elf",
     "patch": "reader-patch-may27.bin"},
]


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        raise SystemExit(f"FAILED: {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r.stdout


def compile_reader(inc, src, elf, binout):
    obj = os.path.join(READER, src.replace(".c", ".o"))
    os.makedirs(os.path.dirname(obj), exist_ok=True)
    run(["clang-15"] + CFLAGS + ["-I" + inc, "-Iinclude", "-c", src, "-o", obj],
        cwd=READER)
    run(["arm-none-eabi-ld", "-T", "link.ld", obj, "-o", elf], cwd=READER)
    run(["arm-none-eabi-objcopy", "-O", "binary", elf, binout], cwd=READER)
    return os.path.getsize(os.path.join(READER, binout))


MOVW = re.compile(r"movw\s+(\w+),\s*#(\d+)")
MOVT = re.compile(r"movt\s+(\w+),\s*#(\d+)")


def materialised_addresses(elf):
    """Every vendor address the binary actually builds into a register.

    Reads the DISASSEMBLY rather than the source, because that is the only
    place all of them are visible at once: some come from fw.h through the
    compiler, others are written by hand inside inline asm. An audit of the
    header alone missed seven of the second kind and boot-looped a device.
    """
    out = run(["arm-none-eabi-objdump", "-d", os.path.join(READER, elf)])
    pend, found = {}, set()
    for line in out.splitlines():
        m = MOVW.search(line)
        if m:
            pend[m.group(1)] = int(m.group(2))
            continue
        m = MOVT.search(line)
        if m and m.group(1) in pend:
            a = (int(m.group(2)) << 16) | pend.pop(m.group(1))
            if 0x10000000 <= a < 0x101D3000:
                found.add(a & ~1)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for t in [REF] + TARGETS:
        ap.add_argument(f"--installed-{t['name']}",
                        help=f"backup of a device with this patch installed "
                             f"({t['name']}); without it the patch cannot skip "
                             f"an already-up-to-date device")
    args = ap.parse_args()
    import mkfwh, retarget, mkpatch

    ref_plain = open(REF["plain"], "rb").read()

    print("== reference build ==")
    size = compile_reader(REF["inc"], REF["src"], REF["elf"], REF["bin"])
    print(f"  {REF['bin']}: {size} bytes")
    ref_addrs = materialised_addresses(REF["elf"])
    print(f"  vendor addresses materialised: {len(ref_addrs)}")

    for t in TARGETS:
        print(f"\n== {t['name']} ==")
        tgt_plain = open(t["plain"], "rb").read()

        # 1. regenerate header + source from the reference
        run([sys.executable, os.path.join(HERE, "mkfwh.py"),
             "-r", REF["plain"], "-t", t["plain"],
             "-i", os.path.join(READER, "include", "fw.h"),
             "-o", os.path.join(READER, t["inc"], "fw.h"),
             "--source", os.path.join(READER, REF["src"]),
             "--source-out", os.path.join(READER, t["src"])])
        print(f"  regenerated {t['inc']}/fw.h and {t['src']}")

        # 2. compile
        size = compile_reader(t["inc"], t["src"], t["elf"], t["bin"])
        print(f"  {t['bin']}: {size} bytes")

        # 3. THE check that matters: every address the reference materialises
        #    must appear here RELOCATED, and nothing may still hold a reference
        #    address. This is what catches an address the generator did not know
        #    how to rewrite -- the failure mode that boot-loops a device.
        want = set()
        for addr in ref_addrs:
            new, why = retarget.resolve(ref_plain, tgt_plain, addr)
            if new is None:
                new, why = mkfwh.resolve_data(ref_plain, tgt_plain, addr)
            if new is None:
                raise SystemExit(
                    f"  CANNOT RELOCATE 0x{addr:08x} ({why}).\n"
                    f"  The reader uses this address and there is no way to "
                    f"place it on {t['name']}. Do not ship this build.")
            want.add(new)
        got = materialised_addresses(t["elf"])
        stale = got & (ref_addrs - want)
        missing = want - got
        if stale:
            raise SystemExit(
                "  STALE ADDRESSES -- these are the reference build's and were "
                "never relocated:\n"
                + "".join(f"    0x{a:08x}\n" for a in sorted(stale))
                + "  Installing this would boot-loop the device. Most likely a "
                  "new hardcoded\n  address in inline asm that mkfwh.py does "
                  "not recognise.")
        if missing:
            raise SystemExit(
                "  MISSING ADDRESSES -- expected after relocation but absent:\n"
                + "".join(f"    0x{a:08x}\n" for a in sorted(missing)))
        print(f"  address check: {len(got)} materialised, 0 stale, 0 missing")

    # 4. build every patch and prove each one only accepts its own firmware
    print("\n== patches ==")
    built = []
    for t in [REF] + TARGETS:
        cmd = [sys.executable, os.path.join(HERE, "mkpatch.py"),
               "-p", t["plain"], "--ref-cipher", t["cipher"],
               "-o", os.path.join(RESEARCH, "reference", t["patch"])]
        inst = getattr(args, f"installed_{t['name']}")
        if inst:
            cmd += ["--ref-installed", inst]
        out = run(cmd)
        nver = "8 verify" if "verify:" in out else "?"
        print(f"  {t['patch']}: built"
              + ("" if inst else "   (no --installed-* : cannot skip an "
                                 "up-to-date device)"))
        built.append(t)

    print("\n== cross-check: each patch must accept ONLY its own firmware ==")
    ok = True
    for p in built:
        _, blocks, _, ver, _ = mkpatch.load_patch(
            open(os.path.join(RESEARCH, "reference", p["patch"]), "rb").read())
        edited = {}
        for addr, _ in blocks:
            edited.setdefault(addr & ~0xfff, set()).add(addr & 0xfff)
        for f in built:
            img = open(f["cipher"], "rb").read()
            match = all(mkpatch.context_digest(img[x:x + 0x1000],
                                               edited.get(x, set())) == w
                        for x, w in ver)
            expect = (p["name"] == f["name"])
            mark = "ok" if match == expect else "FAIL"
            ok &= match == expect
            print(f"  {p['patch']:26s} vs {f['name']:6s} firmware: "
                  f"{'accepts' if match else 'refuses':8s} {mark}")
    if not ok:
        raise SystemExit("a patch accepts the wrong firmware -- do not ship")

    print("""
== what this does NOT cover ==
  Everything above is static. It proves each build compiles, fits, carries no
  address belonging to another build, and that each patch accepts only its own
  firmware. It does NOT prove the reader works.

  A reader whose addresses are all correctly relocated can still fault on a
  structure layout or a vendor function that behaves differently. Install on a
  device of EACH build and open a book before releasing.
""")


if __name__ == "__main__":
    main()
