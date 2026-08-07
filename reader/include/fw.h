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
typedef struct { void *filep; void *mp; uint8_t flags; } fs_file_t;
#define FS_O_READ 0x01
#define FS_SEEK_SET 0
/* [OBSERVED] "%sfile open error (%d)" */
#define fs_open  ((int (*)(fs_file_t *, const char *, uint8_t))0x1007fba9)
/* [OBSERVED] "%sfile close error (%d)" */
#define fs_close ((int (*)(fs_file_t *))0x1007fd01)
/* [OBSERVED] "%sfile read error (%d)" */
#define fs_read  ((int (*)(fs_file_t *, void *, uint32_t))0x1007fd3d)
/* [OBSERVED] "%sfile seek error (%d)" */
#define fs_seek  ((int (*)(fs_file_t *, int32_t, int))0x1007fde1)

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
#define FW_CTX_ARRAY      0x18019098
#define FW_CTX_TOTAL_BYTES 0xf30

/* --- geometry (measured on hardware) ---------------------------------- */
#define SCREEN_H        264
#define LIST_X          4
#define LINE_H_BASE     1      /* the +1 in `fp = [r4+0x1de] + literal` */

#endif /* FW_H */
