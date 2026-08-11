/* Step 1 of the scene replacement: create OUR OWN widget and draw into it.
 *
 * If this shows text, we can build the whole reading view ourselves -- and the
 * input problem that four probes failed to solve disappears, because we will
 * register the event callback on our own object.
 *
 * Created as a child of the vendor's label container rather than the screen, so
 * it inherits the reading font: a label with no font resolves to null and
 * crashes when drawn. Position is inside the container's area.
 *
 * Runs on the DISPLAY thread only (what == 0): LVGL is not thread-safe, and
 * allocating from the ebook thread rebooted the device earlier today.
 */
#include "fw.h"

#define U32(S, off) (*(volatile uint32_t *)((uint32_t)(S) + (off)))

#define OFF_GUARD 0x038          /* probe_calls, reused as a one-shot flag */
#define OFF_OBJ   0x02c          /* last_sy, reused to remember our widget  */
#define GUARD_MADE 0x1ABE10001u

#define LABEL_CLASS 0x1012c7f0u

#define lv_obj_class_create_obj ((void *(*)(uint32_t, void *))0x10096e21u)
#define lv_obj_class_init_obj   ((void (*)(void *))0x100f7925u)

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
    {   /* heartbeat: is this entry reached at all, and with which `what`? */
        static const char hb[] = "%s%s: L1 tick what=%d\n";
        fw_log(hb, "", "inj", what);
    }
    if (what != 0) return;                       /* display thread only */
    if (U32(S, OFF_GUARD) == (uint32_t)GUARD_MADE) {
        /* already built: keep proving it is ours by re-stamping the text */
        void *obj = (void *)U32(S, OFF_OBJ);
        if (obj) lv_label_set_text_copy(obj, "OUR OWN WIDGET");
        return;
    }

    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;

    void *obj = lv_obj_class_create_obj(LABEL_CLASS, cont);
    static const char a[] = "%s%s: L1 obj=0x%x\n";
    fw_log(a, "", "inj", (int)(uint32_t)obj);
    if (!obj) return;

    lv_obj_class_init_obj(obj);
    lv_obj_set_pos(obj, 4, 44);
    lv_obj_set_size(obj, 168, 20);
    lv_label_set_text_copy(obj, "OUR OWN WIDGET");

    U32(S, OFF_OBJ) = (uint32_t)obj;
    U32(S, OFF_GUARD) = (uint32_t)GUARD_MADE;
    static const char b[] = "%s%s: L1 created ok\n";
    fw_log(b, "", "inj", 0);
}
