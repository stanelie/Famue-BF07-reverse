/* Milestone: compiled C, with its own rodata, running inside the reader and
   calling the firmware's logger by absolute address.
   Hooked at 0x1004a288, which stock computes as `fp = fp + content/8`. */
#include <stdint.h>

/* printf-like: (fmt, s1, s2, value). Thumb bit set. */
#define fw_log ((void (*)(const char *, const char *, const char *, int))0x100ee68b)

#define PITCH 19        /* +1 base in the caller = 20px pitch */

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
        "add.w r11, r11, #%c0\n"   /* fp = fp + PITCH, as the stock code did */
        "bx    lr\n"
        :: "i"(PITCH));
}
