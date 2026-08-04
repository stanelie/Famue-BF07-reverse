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
