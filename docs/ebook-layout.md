# Ebook text layout — internals and patch points

This is the original goal: better line spacing, better reflow, and hyphenation.
All addresses are XIP virtual (base `0x10000000`); subtract the base for a file offset
into the extracted `fw0_sys` image.

## Finding the code

The firmware is built with Zephyr logging that passes `__func__`, so every function's
name appears as a string and is referenced from that function's literal pool. There is
also a **name pointer table at `0x101aa4a0`–`0x101aa55c`** listing the whole ebook module:

```
_ebook_view_dialog   _ebook_view_layout    _ebook_view_paint    _ebook_view_delete
ebook_view_init      ebook_view_deinit     book_filelist_cb
_get_line_len_utf8   txt_analy_one_line    _decode_one_page
_reading_scroll_event_cb   _reading_btn_event_cb   _reading_create_content
ebook_scene_reading_enter/exit             ebook_file_init
_get_page_offset     _read_file_line       ebook_read_page_data
_check_chapter       ebook_decode_page     ebook_calculate_pages
ebook_decode_get_line     _cur_chapter_find    ebook_reading
ebook_bmk_add/del/update/init
```

To locate a function: find its name string, find the 4-byte little-endian references to
that address (its literal pool entry), then scan backwards for the nearest Thumb
prologue (`push {...,lr}` = `xx b5`, or `push.w` = `2d e9`).

## Patch point 1 — line wrap width

`_get_line_len_utf8` at **`0x10048f60`** binary-searches for the longest text prefix
that fits, comparing rendered width against a hardcoded constant:

```
1004903a  bl      #0x100fdafc      ; measure rendered width of candidate prefix
1004903e  cmp     r0, #0xa8        ; <-- 0xa8 = 168 px, the wrap width
10001040  itt     le
10001042  movle   sb, sl           ; accept, search longer
```

**Patch: byte at file offset `0x4903e` (value `0xa8`).**

The panel is 176 px wide, so 168 leaves 8 px total margin. Raising it reduces the margin;
lowering it narrows the text column.

## Patch point 2 — lines per page

`_decode_one_page` at **`0x10049298`**:

```
10049348  ldr     r3, [r4, #0x28]
1004934a  cmp     r3, #7           ; <-- loop while line_count <= 7  => 8 lines/page
1004934c  ble     #0x100492d2
```

**Patch: byte at file offset `0x4934a` (value `0x07`).**

Each line is stored in a **0x74-byte record**: text at `+0x34` (96 bytes max), length
byte at `+0x94`, file offset at `+0x2c`. Three page buffers of `0x3cc` each
(previous/current/next). *Increasing the line count requires the record array and the
`0x3cc` buffer size to grow too — this is not a safe single-byte change.*

## Patch point 3 — word-break characters

An 18-byte table at **`0x10164a32`**, tested by the helper at `0x10048f3c`:

```
00 20 21 2c 2e 2f 3a 3b 3c 3d 3e 3f 40 5b 5d 7b 7d 2d
NUL SP  !  ,  .  /  :  ;  <  =  >  ?  @  [  ]  {  }  -
```

**Patch: 18 bytes at file offset `0x164a32`.**

Note the hyphen `0x2d` is present, so the reader *breaks at existing hyphens*.

## Line spacing — the surprise

The GUI is stock **LVGL v8**. Decoding `lv_obj_init_draw_label_dsc` at `0x100f7c32`
gives the style property IDs used:

| `movw r2, #...` | prop | stored at |
|---|---|---|
| `0x4457` | `0x57` `LV_STYLE_TEXT_COLOR` | dsc+0x0c |
| `0x1459` | `0x59` `LV_STYLE_TEXT_FONT` | dsc+0x00 |
| `0x145a` | `0x5a` `LV_STYLE_TEXT_LETTER_SPACE` | dsc+0x14 |
| `0x145b` | `0x5b` `LV_STYLE_TEXT_LINE_SPACE` | **dsc+0x12** |
| `0x458`  | `0x58` `LV_STYLE_TEXT_OPA` | dsc+0x1a |

`text_canvas_refr_text` (~`0x100505c4`) then calls `lv_txt_get_size(...)` passing
`line_space` from `dsc+0x12`.

**No code anywhere in the firmware sets property `0x5b`.** Line spacing therefore falls
through to the LVGL default. A search for the setter pattern (`movs r1,#0x5b` etc.)
found only a Bluetooth command table — a false positive.

So increasing line spacing means *adding* a style call, not changing a constant. That is
a larger change than the width patch.

## Hyphenation — not present at all

There is **no hyphenation dictionary or algorithm anywhere in the image**. The reader
only breaks at the 18 delimiter characters above. Real hyphenation (Liang patterns +
dictionary) would be new code plus data, which must fit in the existing partition. This
is by far the largest of the three requested changes.

## Things that are NOT the answer

- `APP_EBOOK_DATA_INFO` in NVRAM (`09 05 01 00 …`) looks like layout config but isn't —
  byte 0 is the **auto-page-turn interval** (`byte0 × 2000 ms` = 18 s), used at `0x10048cd2`.
- `/NOR:K/ebook.sty` is a UI layout/colour resource (widget rects, `0xC8C8C8` greys), not
  text metrics.

## Related

Fonts live on the `SD1:C` partition: `/SD1:C/{fang,sans,you}{16,18}.font`.
Max 96 chars per line (`cmp r7, #0x60` in `txt_analy_one_line` at `0x10049074`).

---

# Interline spacing — measured and patched (2026-08-06)

## How the reader lays out a page

Two **independent** constants, both hardcoded — changing one does not affect
the other:

| what | where | original |
|---|---|---|
| lines per page | `_decode_one_page`, file `0x4934a` (`cmp r3, #7`) | 8 (indices 0..7) |
| line height | `_reading_create_content`, file `0x4a288` | `content_height / 8` |

```
1004a276  bl   0x100f9014           ; content height
1004a27e  ldrb.w fp, [r4, #0x1de]   ; base
1004a288  add.w fp, fp, r0, asr #3  ; line_height = base + height/8
1004a28e  ldr  r0, ='%s%s: line_height:%d'
```

## Measured on hardware

The firmware logs both values when a book opens:

```
_reading_create_content: list height *************236
_reading_create_content: line_height:29
```

So `236 >> 3 = 29` and the base at `[r4+0x1de]` is **0** — line height is purely
`content_height / 8`. With 8 lines that is `8 x 29 = 232` of 236 px, i.e. the
page is already full.

**Consequence:** raising the line count alone does *not* tighten spacing — the
pitch stays 29 px and the extra lines overflow. Lowering it alone (the 8->7 test)
leaves the same gaps plus blank space at the bottom. Both constants must move
together.

The font box is ~21 px (`asc 17, desc -4`), which is the hard floor for pitch.

| lines | pitch needed | gap over font |
|---|---|---|
| 8 (stock) | 29 | 8 px |
| 9 | 26 | 5 px |
| 10 | 23 | 2 px |
| 16 (`asr #4`) | 14 | **overlaps — do not** |

`asr` granularity is too coarse (/4=59, /8=29, /16=14), so instead **replace the
computation with a constant** — same 4 bytes:

```
0x4a288:  0b eb e0 0b   add.w fp, fp, r0, asr #3
       -> 4f f0 17 0b   mov.w fp, #23
0x4934a:  07 2b  cmp r3,#7   ->   09 2b  cmp r3,#9    (10 lines)
```

Applied and verified: sector `0x5d000` differed only at block `0x340`, sector
`0x5e000` only at block `0x280`, everything else byte-identical to the backup.

**Caveat:** line height is now a constant rather than derived from the content
area. Harmless while the reader area is fixed at 236 px, but it would no longer
adapt if the firmware ever renders the reader at another size.

---

# IMPORTANT: lines-per-page is an ARRAY BOUND, not a display limit

Raising it to 12 made **page turns crash and reboot the device**. Raising it to
10 appeared to work but was silently corrupting memory.

```
100492ce  mov.w sb, #0x74      ; 0x74 bytes per line record
10049312  mul   r8, sb, r3     ; record = base + 0x74 * line_index
10049316  add.w r0, r8, #0x2c  ; array begins at +0x2c
```

Eight records of `0x74` bytes occupy `0x2c`..`0x3cc`. Writing a 9th or later
record runs past the end of the array into adjacent memory. At 12 lines that is
four records — 464 bytes — of overflow, which corrupts whatever follows and
faults on the next page render.

**`cmp r3, #7` bounds an 8-entry array. Do not raise it without enlarging the
array.** Lowering it is safe (fewer entries used).

Both sectors were reverted to stock and verified byte-identical (zero differing
blocks).

## Consequence for layout work

More text per screen cannot be obtained by raising the line count alone. What
remains available:

* **Wrap width** (`0x4903e`, `cmp r0, #0xa8` = 168 px) — touches no array, safe
  to change; gives more characters per line.
* **Line height** (`0x4a288`) — safe on its own, but with 8 lines fixed it only
  moves the blank space to the bottom.
* **Font metrics** — the white space between lines plus the clipped descenders
  show the glyph is positioned low in its box: `asc 17` exceeds the real ink
  height. That is a property of `/SD1:C/sans16.font`, not of the firmware, and
  the SD card can be changed with zero flash risk.
* **New code in free space** — see below.

## Lesson

A constant that looks like a display parameter may be an allocation bound.
Trace what indexes with it *before* increasing it. The 10-line version passing a
visual check was misleading: the corruption only surfaced on a code path
(page turn) that was not exercised.
