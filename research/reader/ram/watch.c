/* RAM payload: pure observer. Logs state and calls nothing.
 *
 * The wrapping version had a race: it cleared code_magic around the inner call
 * to avoid recursing, and since the ebook thread sits in that window almost
 * permanently, the display thread kept seeing the trampoline disabled -- so
 * only what=1 was ever traced. Observing without wrapping removes the problem.
 *
 * The reader stops drawing and preparing while this is active. That is
 * acceptable for diagnosing a stall (nothing is progressing anyway) and it is
 * reverted with `ramload.py off`.
 */
#include "fw.h"

#define U32(S, off) (*(volatile uint32_t *)((uint32_t)(S) + (off)))
#define I32(S, off) (*(volatile int32_t *)((uint32_t)(S) + (off)))
#define U8(S, off)  (*(volatile unsigned char *)((uint32_t)(S) + (off)))

#define OFF_CALLS      0x00c
#define OFF_LAST_LINE  0x010
#define OFF_WANT       0x014
#define OFF_NEED_PREP  0x01a
#define OFF_FILE_READY 0x251
#define OFF_IO_FAIL    0x252
#define OFF_CUR_START  0x25c
#define OFF_CUR_END    0x260

/* the vendor's live reading line, for comparison with our last_line */
static int32_t vendor_line(void)
{
    uint32_t app = *(volatile uint32_t *)FW_APP_GLOBAL;
    if (app < 0x18000000 || app >= 0x18200000) return -1;
    uint32_t rd = *(volatile uint32_t *)(app + 0x3c);
    if (rd < 0x18000000 || rd >= 0x18200000) return -2;
    return *(volatile int32_t *)(rd + 0x194);
}

void inj_entry(int what, void *S)
{
    static int32_t p_line = -999999;
    static int32_t p_last = -999999;
    static int32_t p_want = -999999;
    static int32_t p_cur = -999999;
    static uint32_t p_flags = 0xffffffff;

    int32_t line = vendor_line();
    int32_t last = I32(S, OFF_LAST_LINE);
    int32_t want = I32(S, OFF_WANT);
    int32_t cur = I32(S, OFF_CUR_START);
    uint32_t flags = (uint32_t)U8(S, OFF_NEED_PREP) * 10000u +
                     (uint32_t)U8(S, OFF_FILE_READY) * 100u +
                     (uint32_t)U8(S, OFF_IO_FAIL);

    if (line == p_line && last == p_last && want == p_want &&
        cur == p_cur && flags == p_flags) {
        return;                       /* nothing moved: stay quiet */
    }
    p_line = line; p_last = last; p_want = want; p_cur = cur; p_flags = flags;

    static const char a[] = "%s%s: W vline=%d\n";
    fw_log(a, "", "inj", line);
    static const char b[] = "%s%s: W last=%d\n";
    fw_log(b, "", "inj", last);
    static const char c[] = "%s%s: W want=%d\n";
    fw_log(c, "", "inj", want);
    static const char d[] = "%s%s: W cur=%d\n";
    fw_log(d, "", "inj", cur);
    static const char e[] = "%s%s: W prep*10000+ready*100+fail=%d\n";
    fw_log(e, "", "inj", (int)flags);
    static const char f[] = "%s%s: W what=%d\n";
    fw_log(f, "", "inj", what);
}
