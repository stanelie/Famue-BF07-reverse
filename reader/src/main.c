/* Injected replacement-reader groundwork.
 *
 * Confirmed on hardware:
 *   M1  compiled C runs in the reader, own rodata, firmware calls by address
 *   M2  LVGL read: children=18, child0.y1=24 (both predicted before measuring)
 *   M3  wrote our own text into label 0
 *   M4  filled all 12 visible labels from our code
 *
 * HOOK SITES -- hard-won:
 *   0x100493a8  `bl 0x1004922c`   USABLE. M3/M4 both rendered here.
 *   0x1004925a  render tail call  UNUSABLE. Hooking it reboots with no Zephyr
 *               fault (a hang, not a crash). Giving the semaphore first did
 *               not help. Do not retry without new evidence.
 */
#include "fw.h"

#define PITCH     19        /* +1 base in the caller = 20px pitch */
#define INJ_LINES 12

/* ---- M1: line height ---------------------------------------------- */

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

/* ---- helpers (no libc; the image is flash-only) -------------------- */

static char *put(char *p, const char *s)
{
    while (*s) *p++ = *s++;
    return p;
}

static char *put_u(char *p, unsigned v)
{
    char t[8];
    int n = 0;
    do { t[n++] = (char)('0' + v % 10); v /= 10; } while (v);
    while (n) *p++ = t[--n];
    return p;
}

static void *reader_obj(void)
{
    uint32_t app = *(volatile uint32_t *)FW_APP_GLOBAL;
    if (app < 0x18000000 || app >= 0x18200000) return 0;
    uint32_t rd = *(volatile uint32_t *)(app + 0x3c);
    if (rd < 0x18000000 || rd >= 0x18200000) return 0;
    return (void *)rd;
}

/* ---- M2: read the live LVGL tree ----------------------------------- */

void probe_body(void)
{
    static const char f_cnt[] = "%s%s: probe children=%d\n";
    static const char tag[]   = "inj";
    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;
    fw_log(f_cnt, "", tag, (int)lv_obj_child_cnt(cont));
}

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

/* ---- M4: draw the page ourselves ----------------------------------- */

void after_render(void)
{
    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;

    uint32_t n = lv_obj_child_cnt(cont);
    if (n > INJ_LINES) n = INJ_LINES;

    for (uint32_t i = 0; i < n; i++) {
        void *c = lv_obj_get_child(cont, i);
        if (!c) continue;
        char line[40], *p = line;
        p = put(p, "INJ line ");
        p = put_u(p, i + 1);
        p = put(p, " of ");
        p = put_u(p, n);
        *p = 0;
        lv_label_set_text(c, line, 0);
    }
}

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
