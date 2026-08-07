/* Injected replacement-reader groundwork.
 *
 * Milestone 1 (DONE, confirmed on hardware): compiled C runs in the reader,
 * reaches its own rodata, and calls a firmware function by absolute address.
 *
 * Milestone 2 (this): call LVGL from our own code and confirm the [INFERRED]
 * signatures in fw.h against values already measured over UART -- the
 * container has 18 children and child 0 sits at y1=24.
 */
#include "fw.h"

#define PITCH 19        /* +1 base in the caller = 20px pitch */

/* ---- milestone 1 hook: line height -------------------------------- */

void hook_body(void)
{
    static const char fmt[] = "%s%s: INJECTED C ALIVE, pitch=%d\n";
    fw_log(fmt, "", "hook", PITCH);
}

__attribute__((naked)) void hook(void)
{
    __asm__ volatile(
        "push  {r0-r3, lr}\n"
        "bl    hook_body\n"
        "pop   {r0-r3, lr}\n"
        "add.w r11, r11, #%c0\n"
        "bx    lr\n"
        :: "i"(PITCH));
}

/* ---- milestone 2: read the live LVGL tree from C ------------------- */

static void *reader_obj(void)
{
    uint32_t app = *(volatile uint32_t *)FW_APP_GLOBAL;
    if (app < 0x18000000 || app >= 0x18200000) return 0;
    uint32_t rd = *(volatile uint32_t *)(app + 0x3c);
    if (rd < 0x18000000 || rd >= 0x18200000) return 0;
    return (void *)rd;
}

void probe_body(void)
{
    static const char f_cnt[] = "%s%s: probe children=%d\n";
    static const char f_y1[]  = "%s%s: probe child0.y1=%d\n";
    static const char tag[]   = "inj";

    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;      /* containers live low */

    uint32_t n = lv_obj_child_cnt(cont);          /* [INFERRED] */
    fw_log(f_cnt, "", tag, (int)n);

    if (n) {
        void *c0 = lv_obj_get_child(cont, 0);     /* [INFERRED] */
        if (c0) {
            /* coords: +0x14 holds (x1,y1) as int16 pair */
            int32_t xy = *(volatile int32_t *)((uint32_t)c0 + 0x14);
            fw_log(f_y1, "", tag, (int)(xy >> 16));
        }
    }
}

/* Wraps `bl 0x100491b0` at 0x10049384, which runs at render time when the
   labels already exist, then tail-calls the original. */
__attribute__((naked)) void probe(void)
{
    __asm__ volatile(
        "push  {r0-r3, lr}\n"
        "bl    probe_body\n"
        "pop   {r0-r3, lr}\n"
        "movw  r12, #0x91b1\n"      /* 0x100491b0 | thumb */
        "movt  r12, #0x1004\n"
        "bx    r12\n");
}

/* ---- milestone 3: write to the display from our own code ----------- */

void after_render(void)
{
    static const char marker[] = "== INJECTED READER ==";
    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;
    if (lv_obj_child_cnt(cont) == 0) return;
    void *c0 = lv_obj_get_child(cont, 0);
    if (c0) lv_label_set_text(c0, marker, 0);     /* [OBSERVED] sig */
}

/* Wraps `bl 0x1004922c` at 0x100493a8. The original fills every label's text;
   we call it first, then write our own into label 0 so it survives. */
__attribute__((naked)) void render_hook(void)
{
    __asm__ volatile(
        "push  {r0-r3, lr}\n"
        "movw  r12, #0x922d\n"      /* 0x1004922c | thumb */
        "movt  r12, #0x1004\n"
        "blx   r12\n"
        "bl    after_render\n"
        "pop   {r0-r3, lr}\n"
        "bx    lr\n");
}
