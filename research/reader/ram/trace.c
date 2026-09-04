/* RAM payload: instrument the stall without reflashing.
 *
 * Wraps the flashed implementations rather than replacing them: log a snapshot,
 * then call the real function. To avoid recursing back into ourselves (the
 * flashed bodies check the trampoline at their top), code_magic is cleared for
 * the duration of the inner call and restored afterwards.
 *
 * Field offsets are taken from struct inj_state in ../src/main.c and confirmed
 * against the live block (code_magic sits at +0x254, matching the layout).
 */
#include "fw.h"

#define FLASH_PREPARE  ((void (*)(void))(0x101d3010u | 1u))
#define FLASH_RENDER   ((void (*)(void))(0x101d3f04u | 1u))

#define U32(S, off) (*(volatile uint32_t *)((uint32_t)(S) + (off)))
#define I32(S, off) (*(volatile int32_t *)((uint32_t)(S) + (off)))
#define U8(S, off)  (*(volatile unsigned char *)((uint32_t)(S) + (off)))

#define OFF_CALLS      0x00c
#define OFF_LAST_LINE  0x010
#define OFF_WANT       0x014
#define OFF_NEED_PREP  0x01a
#define OFF_CODE_MAGIC 0x254
#define OFF_FILE_READY 0x251
#define OFF_IO_FAIL    0x252
#define OFF_CUR_START  0x25c
#define OFF_CUR_END    0x260

void inj_entry(int what, void *S)
{
    static int32_t seen_want = -12345;
    static int32_t seen_cur = -12345;
    static unsigned char seen_ready = 0xff;
    static unsigned char seen_fail = 0xff;
    static uint32_t ticks = 1;   /* non-zero: keeps it in .data, which is uploaded */

    int32_t want = I32(S, OFF_WANT);
    int32_t cur = I32(S, OFF_CUR_START);
    unsigned char ready = U8(S, OFF_FILE_READY);
    unsigned char fail = U8(S, OFF_IO_FAIL);

    if (want != seen_want || cur != seen_cur ||
        ready != seen_ready || fail != seen_fail) {
        seen_want = want; seen_cur = cur;
        seen_ready = ready; seen_fail = fail;
        static const char a[] = "%s%s: T cur=%d\n";
        fw_log(a, "", "inj", cur);
        static const char b[] = "%s%s: T want=%d\n";
        fw_log(b, "", "inj", want);
        static const char c[] = "%s%s: T ready*100+fail=%d\n";
        fw_log(c, "", "inj", ready * 100 + fail);
        static const char d[] = "%s%s: T end=%d\n";
        fw_log(d, "", "inj", I32(S, OFF_CUR_END));
    }

    /* heartbeat: which entry is still being reached, once every ~64 calls */
    if ((++ticks & 63) == 0) {
        static const char h[] = "%s%s: T alive what*1000000+calls=%d\n";
        fw_log(h, "", "inj", what * 1000000 + (int)(U32(S, OFF_CALLS) & 0xFFFFF));
    }

    uint32_t saved = U32(S, OFF_CODE_MAGIC);
    U32(S, OFF_CODE_MAGIC) = 0;          /* stop the inner call re-entering us */
    if (what == 1) FLASH_PREPARE();
    else           FLASH_RENDER();
    U32(S, OFF_CODE_MAGIC) = saved;
}
