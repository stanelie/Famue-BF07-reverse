#!/usr/bin/env python3
"""Build the end-user install bundle: tools/bf07.py plus everything it needs
to install the reader over ADFU alone, zipped up and ready to hand to someone
with no AI, no git, and no build toolchain.

    make_release.py               builds dist/bf07-bundle-<version>/ and .zip

Pulls exactly bf07.py's --patch dependency graph (bf07.py, lark_cd.py,
patchset.py, patch_lines.py, mkpatch.py -- traced from its imports, not
guessed), the vendored ADFU payload, our own reader-patch.bin, and fonts/.
Nothing else in tools/ -- the other ~30 scripts here are reverse-engineering
and development tools, not part of the installer.

The bundle's README.md is the repo's own top-level README.md, copied
verbatim: one file, so the GitHub landing page and what ships in the zip can
never say different things.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)              # research/
ROOT = os.path.dirname(RESEARCH)              # repo root

TOOLS = ["bf07.py", "lark_cd.py", "patchset.py", "patch_lines.py", "mkpatch.py"]

sys.path.insert(0, HERE)


def version():
    try:
        return subprocess.check_output(
            ["git", "describe", "--tags", "--always"], cwd=ROOT,
            stderr=subprocess.DEVNULL, text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sha = "unknown"
    import datetime
    return f"{datetime.date.today().isoformat()}-{sha}"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SINGLE_STUB = '''#!/usr/bin/env python3
"""BF07 reader installer %(ver)s -- everything in one file.

Run it:      python3 %(name)s

No unpacking, no folders, nothing to install. Everything the installer needs is
embedded below and unpacked into a temporary directory each run, which is
removed on exit; nothing is left behind on your machine.

Full instructions: https://github.com/stanelie/Famue-BF07-reverse
"""
import atexit, base64, os, shutil, sys, tempfile, zlib

_BLOBS = {
%(blobs)s}


def _unpack():
    d = tempfile.mkdtemp(prefix="bf07-")
    atexit.register(shutil.rmtree, d, True)
    for name, b64 in _BLOBS.items():
        path = os.path.join(d, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(zlib.decompress(base64.b64decode(b64)))
    return d


if __name__ == "__main__":
    root = _unpack()
    sys.path.insert(0, os.path.join(root, "tools"))
    import bf07
    sys.exit(bf07.main())
'''


def build_single_file(staging, dist, ver):
    """Pack the whole bundle into one runnable .py.

    A zip asks the user to find it, unpack it, and know which file inside to
    run. A single script is `python3 <file>` and done -- which matters when the
    audience is a device owner, not a developer.

    Layout is preserved on unpack (tools/ beside reference/ and fonts/) because
    the installer locates the ADFU payload and the font relative to itself.
    Unpacking to a temp dir that is deleted on exit keeps the "one file" promise
    literal: nothing is scattered into the working directory.
    """
    import base64, zlib
    name = f"bf07-installer-{ver}.py"
    out = os.path.join(dist, name)
    lines = []
    for dirpath, _, filenames in os.walk(staging):
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, staging).replace(os.sep, "/")
            blob = base64.b64encode(zlib.compress(open(full, "rb").read(), 9)).decode()
            lines.append(f'    "{rel}": (')
            for i in range(0, len(blob), 76):
                lines.append(f'        "{blob[i:i + 76]}"')
            lines.append("    ),")
    src = SINGLE_STUB % {"ver": ver, "name": name, "blobs": "\n".join(lines) + "\n"}
    with open(out, "w") as f:
        f.write(src)
    os.chmod(out, 0o755)
    return out


def main():
    # Pre-flight: fail fast if a module is broken, before anything is copied.
    import patchset          # noqa: F401  (imports patch_lines transitively)
    import mkpatch

    ver = version()
    print(f"version: {ver}")

    import glob as _glob
    patches = sorted(_glob.glob(os.path.join(RESEARCH, "reference", "reader-patch*.bin")))
    if not patches:
        raise SystemExit("no reader-patch*.bin in reference/ -- build one with mkpatch.py")
    patch_src = patches[0]
    payload_src = os.path.join(RESEARCH, "reference", "adfus_u_go.bin")
    for label, path in [("reader-patch.bin", patch_src),
                         ("adfus_u_go.bin", payload_src)]:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise SystemExit(
                f"missing or empty {label} at {path}\n"
                f"  reader-patch.bin: build it with mkpatch.py from a decrypted dump\n"
                f"  adfus_u_go.bin: from Actions' public LARK SDK, see reference/README.md")

    # One patch per firmware build: the installer reads the device and picks.
    # Shipping only one would refuse every user on the other build.
    for p in patches:
        reader, blocks, ref_sha, verify, installed = mkpatch.load_patch(open(p, "rb").read())
        print(f"{os.path.basename(p)}: {len(reader)} reader sector(s), {len(blocks)} "
              f"vendor block(s), firmware {ref_sha.hex()[:16]}..., "
              f"{len(verify)} verify, {len(installed)} installed")
        if not verify:
            raise SystemExit(f"{os.path.basename(p)} has no firmware-check table; "
                             f"rebuild it with mkpatch.py --ref-cipher")

    dist = os.path.join(ROOT, "dist")
    staging = os.path.join(dist, f"bf07-bundle-{ver}")
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(os.path.join(staging, "tools"))
    os.makedirs(os.path.join(staging, "reference"))

    for name in TOOLS:
        shutil.copy2(os.path.join(HERE, name), os.path.join(staging, "tools", name))
    for p in patches:
        shutil.copy2(p, os.path.join(staging, "reference", os.path.basename(p)))
    shutil.copy2(payload_src, os.path.join(staging, "reference", "adfus_u_go.bin"))
    shutil.copytree(os.path.join(ROOT, "fonts"), os.path.join(staging, "fonts"))

    shutil.copy2(os.path.join(ROOT, "README.md"), os.path.join(staging, "README.md"))
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(staging, "LICENSE"))
    shutil.copy2(os.path.join(ROOT, "THIRD_PARTY_NOTICE.md"),
                 os.path.join(staging, "THIRD_PARTY_NOTICE.md"))

    zip_path = f"{staging}.zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _, filenames in os.walk(staging):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.join(f"bf07-bundle-{ver}",
                                            os.path.relpath(full, staging)))

    single = build_single_file(staging, dist, ver)

    print(f"\nwrote {staging}/")
    print(f"wrote {zip_path}")
    print(f"     sha256 {sha256_file(zip_path)}")
    print(f"wrote {single}")
    print(f"     sha256 {sha256_file(single)}")


if __name__ == "__main__":
    main()
