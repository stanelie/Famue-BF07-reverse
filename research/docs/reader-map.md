# Ebook app map

Recovered with `tools/extract_symbols.py`, which reads the firmware's **own log
calls**: nearly every function passes its name as the third argument to the
logger at `0x100ee68a`, so the image documents itself. **1267 functions named.**

Full list: [symbols.txt](symbols.txt).

This is the answer to guessing at signatures from single call sites. Three
inferences made that way were wrong (`fw_wrap_line`, the mode argument of
`0x100eb4b8`, and the supposed lock pair `0x100fd8b8`/`0x100fd8c2`), and each
cost a flash-and-test cycle.

## App lifecycle

| addr | name |
|---|---|
| `0x10047ef8` | `ebook_view_init` |
| `0x10047860` | `_ebook_app_loop` / `_ebook_event_handle` |
| `0x10047aac` | `_ebook_view_layout` |
| `0x10047e0c` | `_ebook_view_paint` |
| `0x10047d34` | `_ebook_view_delete` |

## Scenes

| addr | name |
|---|---|
| `0x100482f8` / `0x10048464` | `ebook_scene_file_list_enter` / `_exit` |
| `0x10048720` / `0x1004868c` | `ebook_scene_file_tile_enter` / `_exit` |
| `0x10049ec0` | `ebook_scene_reading_enter` = `_reading_create_content` = `_reading_load_resource` |
| `0x1004a418` | `_reading_unload_resource` |
| `0x1004b078` / `0x1004b20c` | `ebook_scene_bmk_enter` / `_exit` |

## Reading and pagination

| addr | name |
|---|---|
| `0x1004b29c` | `ebook_file_init` |
| `0x1004b4ec` | `_get_page_offset` |
| `0x1004b7c4` | `ebook_read_page_data` |
| `0x1004bbbc` | `ebook_decode_page` |
| `0x1004bd6c` | `ebook_calculate_pages` -- the background paginator; the thread that asserts |
| `0x1004be64` | `ebook_decode_get_line` |
| `0x10049074` | `txt_analy_one_line` |
| `0x10049298` | `_decode_one_page` |
| `0x1004bf98` | `ebook_reading` / `_ebook_reading_event_handle` |

## Input

| addr | name |
|---|---|
| `0x10048d64` | `_reading_btn_event_cb` |
| `0x10049684` | `_reading_scroll_event_cb` |
| `0x100494dc` | `_ebook_return_btn_event_cb` |
| `0x10048bf4` | `_ebook_flag_btn_event_cb` |

## Bookmarks

| addr | name |
|---|---|
| `0x1004c784` | `ebook_bmk_init` |
| `0x1004c488` | `ebook_bmk_add` |
| `0x1004c5b0` | `ebook_bmk_del` |
| `0x1004c658` | `ebook_bmk_update` |
| `0x10028d9c` | `_fnavi_del_ebookbmk` |

## Fonts -- the real text metrics

This is what the wrap-width work needed and never found by inference:

| addr | name |
|---|---|
| `0x100ddd98` | `bitmap_font_open` |
| `0x100de638` | `bitmap_font_get_bitmap` |
| `0x100decbc` | `bitmap_font_get_glyph_dsc` |
| `0x100dec3a` | `_font_get_glyph_dsc` |
| `0x100dedec` | `bitmap_font_load_high_freq_chars` |

`bitmap_font_get_glyph_dsc` yields per-glyph metrics, so line width can be
computed exactly instead of estimated from a character-width table.

## Coverage

Named functions per 64K region: `0x1003` 199, `0x100b` 175, `0x1006` 175,
`0x1004` 159 (the ebook app), `0x100d` 147, `0x1005` 134, `0x100a` 118,
`0x100c` 116, `0x1002` 98, `0x1007` 40, `0x100e` 23, `0x1008` 13.

## Method

`tools/extract_symbols.py <fw_code_full.bin>` -- finds every `bl 0x100ee68a`,
walks back for the `ldr r2, [pc, ...]` feeding its third argument, resolves the
string, then attributes it to the nearest preceding `push` (the function
prologue). Several addresses carry more than one name where a function is
inlined into or shares a prologue with another.
