/* Firmware entry points recovered by reverse engineering.
 *
 * CONFIDENCE LEVELS — respect these, a wrong signature crashes rather than
 * misbehaves:
 *   [OBSERVED] call site read in the disassembly, arguments confirmed by use
 *   [INFERRED] plausible from context, NOT yet confirmed on hardware
 *
 * All call addresses carry the Thumb bit (+1).
 */
#ifndef FW_H
#define FW_H
#include <stdint.h>

/* --- libc ------------------------------------------------------------- */
/* [OBSERVED] _decode_one_page: memset(ctx+i*rec+0x2c, 0, rec) */
#define fw_memset  ((void *(*)(void *, int, unsigned))0x100f1f4b)
/* [OBSERVED] _decode_one_page: memcpy(sp+0xc, filebuf, len) */
#define fw_memcpy  ((void *(*)(void *, const void *, unsigned))0x100f1f43)

/* --- logging ---------------------------------------------------------- */
/* [OBSERVED] printf-like: (fmt, s1, s2, value); reaches the UART console */
#define fw_log     ((void (*)(const char *, const char *, const char *, int))0x100ee68b)

/* --- LVGL wrappers ---------------------------------------------------- */
/* [OBSERVED] _reading_create_content label loop */
#define lv_obj_set_size   ((void (*)(void *, int16_t, int16_t))0x100f8083)
#define lv_obj_set_pos    ((void (*)(void *, int16_t, int16_t))0x100f800d)
/* [OBSERVED] confirmed on hardware: child0.y1 == 24 as measured */
#define lv_obj_get_child  ((void *(*)(void *, uint32_t))0x100f9903)
/* [OBSERVED] confirmed on hardware: returned 18 as measured */
#define lv_obj_child_cnt  ((uint32_t (*)(void *))0x100f9921)
#define lv_label_set_text ((void (*)(void *, const char *, int))0x100ec577)
/* [INFERRED] style property getter: (obj, part, prop) -> value; 0x1004 = height */
#define lv_get_style_prop ((int32_t (*)(void *, uint32_t, uint32_t))0x100f9015)
#define LV_PROP_WIDTH   0x1001
#define LV_PROP_HEIGHT  0x1004

/* --- filesystem (Zephyr FS, dispatchers confirmed by their error strings) --
 * struct fs_file_t { void *filep; struct fs_mount_t *mp; uint8_t flags; }
 * fs_open stores flags at [file+8], matching that layout. Zero it first. */
/* WRONG at 12 bytes. ebook_file_init memsets its handle with r2 = 0x14, so
   fs_file_t is 20 BYTES here. A 12-byte stack copy let fs_open write past it
   and corrupt the stack, which surfaced as a k_mutex_unlock wait-queue
   assertion inside ebook_decode_page -- nowhere near the damage. */
typedef struct { uint8_t opaque[20]; } fs_file_t;

/* The vendor's OWN open handle for the book, from
   ebook_file_init(r0=0x1801a084, ...) -> fs_open_cluster. Reuse it: opening the
   same file a second time corrupts the FS layer's mutex bookkeeping. */
#define FW_BOOK_FILE ((fs_file_t *)0x1801a084)
#define FS_O_READ 0x01
/* Zephyr fs flags: mode in the low bits, FS_O_CREATE separate. Used for our own
   bookmark file -- the vendor's .bmk cannot serve, because its reading_line is
   frozen at 0 by design (its turn path is dead), so it saves 0 on exit. */
#define FS_O_WRITE 0x02
#define FS_O_RDWR  0x03
#define FS_O_CREATE 0x10
#define FS_SEEK_SET 0
/* [OBSERVED] "%sfile open error (%d)" */
#define fs_open  ((int (*)(fs_file_t *, const char *, uint8_t))0x1007fba9)
/* [OBSERVED] "%sfile close error (%d)" */
#define fs_close ((int (*)(fs_file_t *))0x1007fd01)
/* [OBSERVED] "%sfile read error (%d)" */
#define fs_read  ((int (*)(fs_file_t *, void *, uint32_t))0x1007fd3d)
/* [OBSERVED] "%sfile seek error (%d)" */
#define fs_seek  ((int (*)(fs_file_t *, int32_t, int))0x1007fde1)

/* lvgl_bitmap_font_open / _close. Both take the caller's lv_font_t; open takes
   the path as well and returns 0 on success, -1 on failure (three -1 exits, all
   reached by `pop {r4,r5,r6,pc}`). close takes only the font -- there is no path
   involved, so nothing keys the cache on the string we substitute. */
#define fw_font_close ((int (*)(void *))0x100e150d)

/* --- allocation --------------------------------------------------------
 * [OBSERVED] 0x100a0644 names itself in its own error string:
 *   "couldn't allocate memory (%lu bytes)" ... lv_mem.c ... 'lv_mem_alloc'
 * A general allocator over LVGL's heap. */
#define lv_mem_alloc ((void *(*)(uint32_t))0x100a0645)

/* Anchor for our heap pointer: 0x18018e98..0x18019098 (512 bytes) survived a
 * full workload canary -- reading, paging, AUDIO PLAYBACK and scene changes --
 * with 0 of 128 words touched. Only 8 bytes are used; everything else lives in
 * allocated memory, so no further region has to stay free. */
#define INJ_ANCHOR 0x18018e98

/* --- text layout ------------------------------------------------------ */
/* WRONG: marked [OBSERVED] from one call site, but a single call consumed a
   whole 512-byte buffer. Do not use.
   #define fw_wrap_line ((int (*)(const char *, int, int, int))0x10049075) */

/* [OBSERVED] the real measurer, from txt_analy_one_line at 0x100490d6..e6:
 *   r0 buf, r1 len, r2 encoding, r3 info (0xbc scratch, zeroed),
 *   [sp] max width in PIXELS, [sp+4] -> bytes consumed
 * The vendor passes width 0xc0 and clamps len to 0x60. */
#define fw_measure ((void (*)(const char *, int, int, void *, int, int *))0x100eb4b9)
#define FW_MEASURE_INFO 0xbc
#define FW_MEASURE_MAXLEN 0x60

/* --- reader object ---------------------------------------------------- */
/* [OBSERVED] global -> app object -> [+0x3c] is the reader */
#define FW_APP_GLOBAL   0x18018978
#define RD_OFF_LIST     0x14    /* lv_obj * label container            */
#define RD_OFF_CTXS     0x190   /* page-context array base             */
#define RD_OFF_LINE     0x194   /* reading position, in lines          */
#define RD_OFF_TOTALPG  0x19c   /* total pages                         */

/* Static page contexts. Replacing the vendor reader frees these:
 * 0x18018a4c (0x3cc) + 0x18019098..0x18019bfc (0xb64) = 0xf30 bytes of
 * known-good SRAM at fixed addresses. */
#define FW_CTX_STANDALONE 0x18018a4c
/* A page context STARTS WITH A KERNEL MUTEX; its header (0x00..0x2b) is live.
   Only the line records behind it may be reused. */
#define FW_CTX_HEADER     0x2c
#define FW_CTX_ARRAY      0x18019098
#define FW_CTX_TOTAL_BYTES 0xf30

/* --- geometry (measured on hardware) ---------------------------------- */
#define SCREEN_H        264
#define LIST_X          4
#define LINE_H_BASE     1      /* the +1 in `fp = [r4+0x1de] + literal` */

#endif /* FW_H */
#define lv_event_get_code ((uint32_t (*)(void *))0x100f6871)   /* [OBSERVED] reading scroll cb tests its result against 0x0b = LV_EVENT_SCROLL */

/* Book file size, kept by ebook_file_init ("open ebook ok, size: 495465!").
   [OBSERVED] live at 0x1801a090 and mirrored at 0x18019e24. Exact, so the
   binary-search probe is only a fallback now. */
#define FW_BOOK_SIZE ((volatile uint32_t *)0x1801a090)
/* File picker's list: 0x100-byte entries, filename at +3, NUL-terminated.
   Located by dumping 0x18006000-0x18008000 with a book open -- the two book
   names appear verbatim at +0x003 and +0x103. NOT 0x18007800: that address is
   inside the buffer the vendor reuses for book text once reading begins. */
#define FW_FILE_LIST 0x18007000u

/* Background paginator guard. ebook_calculate_pages (0x1004bd6c) is called from
   the ebook thread's message loop only when this byte is non-zero:
       1004c0ac  ldr  r3, [pc, #388]   -> this address
       1004c0b0  cbz  r3, 0x1004c0c8   ; skip the call
       1004c0b8  bl   0x1004bd6c       ; ebook_calculate_pages
   Clearing it at runtime stopped the scan dead -- 11,449 bytes per 20 s to
   exactly 0 -- with the reader still working. */
#define FW_REPAGINATE ((volatile unsigned char *)0x1806517b)

/* bitmap_font_get_glyph_dsc_cb: (font, dsc_out, letter, letter_next) -> bool.
   Writes the advance at dsc+0, then box_w +2, box_h +4, ofs_x +6, ofs_y +8,
   bpp +10 -- read straight off its stores.

   The advance is in WHOLE PIXELS here, not the 8.4 fixed point upstream LVGL
   documents: measured on device 'i' 4, 'e' 8, 'm' 12, 'W' 14, space 4. */
#define fw_glyph_dsc ((int (*)(void *, void *, uint32_t, uint32_t))0x100e1349)

/* Ebook context in static RAM (so, unlike the heap reader object, code that
   touches it is findable by literal search).
   [OBSERVED] confirmed live and from both sides of ebook_bmk_init /
   ebook_calculate_pages, which seek to these header offsets and fs_write them. */
#define FW_TOTAL_LINES  ((volatile uint32_t *)0x1801a030)  /* lines in the book */
#define FW_CUR_LINE     ((volatile uint32_t *)0x1801a080)  /* saved position    */
#define FW_LINES_PER_PG ((volatile uint8_t  *)0x1801a098)  /* divisor, not a shift */
#define FW_BMK_FILE     ((fs_file_t *)0x1801a0ac)          /* the .bmk handle   */
#define fs_write ((int (*)(fs_file_t *, const void *, uint32_t))0x1007fd75)

/* Real lv_label_set_text(obj, text): strlen + realloc + COPY into obj+0x24.
   [OBSERVED] the vendor uses this for status widgets (it formats "%d/%d" and
   calls it). Distinct from 0x100ec577 above, which stores the POINTER and
   takes a flag -- the static-text variant, fine for our own constant buffers
   but not for these widgets. */
#define lv_label_set_text_copy ((void (*)(void *, const char *))0x100fe945)

/* Widget classes, read live from the running scene. */
#define LV_CLASS_OUR_LINES   0x10129b54u   /* the 18 reading labels        */
#define LV_CLASS_COUNTER_IN  0x1012c7f0u   /* leaf inside the page counter */

/* Recompute a textarea's cursor pixel fields (+0x3c/+0x44/+0x48) after its
   text or cursor index has changed. [OBSERVED] the delete path calls this on
   the textarea immediately after re-setting the label, and it reads +0x24 and
   works with a 0xffff sentinel. Setting the label WITHOUT this (and without
   +0x40) is what desynced the widget and made the next keypress edit from the
   wrong offset. */
#define fw_ta_refresh ((void (*)(void *))0x100fee85)

/* Cursor position, in characters, at textarea+0x40; a second counter tracks it
   at +0x4c. Measured: adding one character moved 0 -> 1 in both. */
#define TA_CURSOR   0x40
#define TA_CURSOR2  0x4c
