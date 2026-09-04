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


def main():
    # Pre-flight: fail fast if a module is broken, before anything is copied.
    import patchset          # noqa: F401  (imports patch_lines transitively)
    import mkpatch

    ver = version()
    print(f"version: {ver}")

    patch_src = os.path.join(RESEARCH, "reference", "reader-patch.bin")
    payload_src = os.path.join(RESEARCH, "reference", "adfus_u_go.bin")
    for label, path in [("reader-patch.bin", patch_src),
                         ("adfus_u_go.bin", payload_src)]:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise SystemExit(
                f"missing or empty {label} at {path}\n"
                f"  reader-patch.bin: build it with mkpatch.py from a decrypted dump\n"
                f"  adfus_u_go.bin: from Actions' public LARK SDK, see reference/README.md")

    reader, blocks, ref_sha, verify, installed = mkpatch.load_patch(
        open(patch_src, "rb").read())
    print(f"reader-patch.bin: {len(reader)} reader sector(s), {len(blocks)} "
          f"vendor block(s), built from plaintext sha256 {ref_sha.hex()[:16]}...")

    dist = os.path.join(ROOT, "dist")
    staging = os.path.join(dist, f"bf07-bundle-{ver}")
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(os.path.join(staging, "tools"))
    os.makedirs(os.path.join(staging, "reference"))

    for name in TOOLS:
        shutil.copy2(os.path.join(HERE, name), os.path.join(staging, "tools", name))
    shutil.copy2(patch_src, os.path.join(staging, "reference", "reader-patch.bin"))
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

    print(f"\nwrote {staging}/")
    print(f"wrote {zip_path}")
    print(f"sha256 {sha256_file(zip_path)}")


if __name__ == "__main__":
    main()
