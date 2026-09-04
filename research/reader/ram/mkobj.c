/* Isolate the widget-creation crash: build a PLAIN OBJECT, not a label.
 *
 * Creating a label crashed both from the render pass and from scene
 * construction, so the timing explanation was wrong. A base object draws
 * nothing and needs no font, so if this survives, creation is fine and the
 * fault is label/font specific -- which is a completely different fix.
 */
#include "fw.h"

#define U32(S, off) (*(volatile uint32_t *)((uint32_t)(S) + (off)))
#define OFF_GUARD 0x038
#define GUARD_DONE 0x0B1EC001u

static void *reader_obj(void)
{
    uint32_t app = *(volatile uint32_t *)FW_APP_GLOBAL;
    if (app < 0x18000000 || app >= 0x18200000) return 0;
    uint32_t rd = *(volatile uint32_t *)(app + 0x3c);
    if (rd < 0x18000000 || rd >= 0x18200000) return 0;
    return (void *)rd;
}

void inj_entry(int what, void *S)
{
    if (what != 0) return;                    /* display thread */
    if (U32(S, OFF_GUARD) == GUARD_DONE) return;
    U32(S, OFF_GUARD) = GUARD_DONE;           /* one attempt, whatever happens */

    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;

    static const char a[] = "%s%s: M creating on cont=0x%x\n";
    fw_log(a, "", "inj", (int)(uint32_t)cont);

    void *obj = lv_obj_class_create_obj(LV_CLASS_OBJ, cont);
    static const char b[] = "%s%s: M obj=0x%x\n";
    fw_log(b, "", "inj", (int)(uint32_t)obj);
    if (!obj) return;

    lv_obj_class_init_obj(obj);
    static const char c[] = "%s%s: M init returned\n";
    fw_log(c, "", "inj", 0);

    lv_obj_set_pos(obj, 4, 44);
    lv_obj_set_size(obj, 60, 12);
    static const char d[] = "%s%s: M placed -- survived\n";
    fw_log(d, "", "inj", 0);
}
