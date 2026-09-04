/* Minimal RAM payload: proves the trampoline runs, and nothing else.
   Entry ABI: inj_entry(what, state) -- 0 = after_render, 1 = prepare_body. */
#include "fw.h"

void inj_entry(int what, void *S)
{
    (void)S;
    static const char m[] = "%s%s: RAMCODE what=%d\n";
    fw_log(m, "", "inj", what);
}
