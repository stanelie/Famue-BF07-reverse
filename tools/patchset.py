#!/usr/bin/env python3
"""The replacement reader's patch table: plaintext image in, patched sectors out.

Kept separate from the flashing so both the developer flasher (mkflash.py) and
the user-facing installer (bf07.py) describe the patch exactly once.

What gets written:

  * the reader itself, into the 53 KB of unused 0xFF padding at 0x1e7000, which
    is inside the XIP partition and so executes like any other firmware code;
  * a handful of words of vendor code -- the hooks, two layout constants, and
    one data word in the font menu's row table.

Every one is documented in docs/reader-architecture.md:

  0x1004a1fc  container top      -> 24
  0x1004a222  container height   -> 24 subtracted
  0x1004a288  line height        -> our pitch (drawing position only)
  0x1004c002  message receive    -> our page preparation (ebook thread)
  0x100493b2  timer TAIL         -> our render pass (reached unconditionally)
  0x100d92e8  gesture handler    -> input capture
  0x100e07b4  _lvgl_pointer_put  -> touch input capture
  0x100e1348  glyph dsc callback -> font capture, for real glyph widths
  0x100e1440  lvgl_bitmap_font_open -> install the user font over the vendor's
  0x1005934c  app_menulist_load_res_id -> menu label follows the file
  0x10128e98  font menu row      -> label id for the "Custom" row (DATA)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import patch_lines as P                                          # noqa: E402

FW0, XIP = 0x14000, 0x10000000
CODE_BASE, CODE_LIMIT = 0x1e7000, 0x1f4000
CONT_TOP, CONT_SUB = 24, 24

# Symbol name -> the site it is branched from, and how.
#
# Keep this in step with tools/mkflash.py, which the development loop uses.
# These two drifted apart once already: the font-open and menu hooks and the
# label word patch existed only in the dev flasher, so `install --patch` would
# have produced a reader with no user-font backend and no "Custom" label.
BL_HOOKS = {"hook": 0x1004A288, "prepare_hook": 0x1004C002}
BW_HOOKS = {"tail_hook": 0x100493B2, "pointer_hook": 0x100E07B4,
            "font_hook": 0x100E1348, "gesture_hook": 0x100D92E8,
            "fontopen_hook": 0x100E1440, "menulist_hook": 0x1005934C}

# Vendor DATA patches {xip_addr: u32}. The font menu row for fang18 is
# repointed at the label id 0xf40f37ea ("Fangsong Small Font"), which no row
# referenced; tools/set_menu_label.py rewrites that string to "Custom" in the
# NOR resource. Without the NOR step the row simply reads its old name -- the
# reader itself works either way.
WORD_PATCHES = {0x10128E98: 0xf40f37ea}


def symbols(elf):
    """Read our own symbol addresses out of the built ELF."""
    import subprocess
    out = subprocess.run(["arm-none-eabi-nm", "-n", elf],
                         capture_output=True, text=True).stdout
    syms = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "T":
            syms[parts[2]] = int(parts[0], 16)
    return syms


def build(plain, blob=None, elf=None):
    """plain: the DECRYPTED fw0_sys image (index 0 == XIP 0x10000000).

    Returns {flash_sector_address: 4096 bytes of plaintext}, ready to erase and
    write with address bit 31 set.
    """
    root = os.path.dirname(_HERE)
    blob = blob or open(os.path.join(root, "reader", "reader.bin"), "rb").read()
    elf = elf or os.path.join(root, "reader", "reader.elf")
    if CODE_BASE + len(blob) > CODE_LIMIT:
        raise SystemExit(f"reader is {len(blob)} bytes; free space is "
                         f"{CODE_LIMIT - CODE_BASE}")
    syms = symbols(elf)

    data = bytearray(plain)
    data[0x1004A1FC - XIP:0x1004A1FC - XIP + 2] = P.movs_imm8(2, CONT_TOP)
    data[0x1004A222 - XIP:0x1004A222 - XIP + 2] = P.sub_imm8(0, CONT_SUB)
    for name, site in BL_HOOKS.items():
        if name in syms:
            data[site - XIP:site - XIP + 4] = P.bl(site, syms[name])
    for name, site in BW_HOOKS.items():
        if name in syms:
            data[site - XIP:site - XIP + 4] = P.bw(site, syms[name])
    for addr, value in WORD_PATCHES.items():
        data[addr - XIP:addr - XIP + 4] = value.to_bytes(4, "little")

    sectors = {}
    # the reader's own sectors, padded with 0xFF like the free space they sit in
    for n in range((len(blob) + 0xfff) // 0x1000):
        sec = bytearray(b"\xff" * 0x1000)
        chunk = blob[n * 0x1000:(n + 1) * 0x1000]
        sec[0:len(chunk)] = chunk
        sectors[CODE_BASE + n * 0x1000] = bytes(sec)
    # every vendor sector a hook lands in
    for site in (list(BL_HOOKS.values()) + list(BW_HOOKS.values())
                 + list(WORD_PATCHES) + [0x1004A1FC]):
        flash = (FW0 + (site - XIP)) & ~0xfff
        off = flash - FW0
        sectors[flash] = bytes(data[off:off + 0x1000])
    return sectors


if __name__ == "__main__":
    img = open(sys.argv[1], "rb").read()
    for a, s in sorted(build(img).items()):
        print(f"0x{a:06x}  {len(s)} bytes")
