/* Settle the input question: register OUR OWN event callback and see what
 * arrives.
 *
 * Filter 0 is LV_EVENT_ALL, so every event on that object reaches us and the
 * log tells us the codes rather than us guessing which one to ask for. It is
 * registered on the SCREEN and on the reading container, because a child may
 * consume a press before it reaches either.
 *
 * The callback lives in this RAM payload, so do NOT deactivate the trampoline
 * while it is installed -- the object would keep a pointer into freed code.
 */
#include "fw.h"

#define U32(S, off) (*(volatile uint32_t *)((uint32_t)(S) + (off)))
#define OFF_GUARD 0x038
#define GUARD_DONE 0x1E7E0003u

static void *reader_obj(void)
{
    uint32_t app = *(volatile uint32_t *)FW_APP_GLOBAL;
    if (app < 0x18000000 || app >= 0x18200000) return 0;
    uint32_t rd = *(volatile uint32_t *)(app + 0x3c);
    if (rd < 0x18000000 || rd >= 0x18200000) return 0;
    return (void *)rd;
}

/* lv_event_get_code(e) -- used by every vendor callback we disassembled */
#define lv_event_get_code ((uint32_t (*)(void *))0x100f6871)

static void my_cb(void *e)
{
    uint32_t code = lv_event_get_code(e);
    /* only the interesting ones: press, click, release, gesture, key */
    if (code == 1 || code == 4 || code == 7 || code == 8 || code == 0x0c || code == 13) {
        static const char m[] = "%s%s: IN code=%d\n";
        fw_log(m, "", "inj", (int)code);
    }
}

void inj_entry(int what, void *S)
{
    if (what != 0) return;
    if (U32(S, OFF_GUARD) == GUARD_DONE) return;

    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;
    uint32_t scr = *(volatile uint32_t *)((uint32_t)cont + 4);
    if (scr < 0x01000000) return;

    U32(S, OFF_GUARD) = GUARD_DONE;
    lv_obj_add_event_cb(cont, (void *)my_cb, 0, 0);          /* 0 = LV_EVENT_ALL */
    lv_obj_add_event_cb((void *)scr, (void *)my_cb, 0, 0);
    static const char r[] = "%s%s: IN registered on cont+screen\n";
    fw_log(r, "", "inj", 0);
}
