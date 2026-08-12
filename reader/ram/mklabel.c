/* Narrow the label crash to a single call.
 *
 * A plain object creates, initialises and places fine. A label crashed. This
 * logs between every step so the failing call names itself: class creation,
 * init, geometry, then text LAST -- setting text is the prime suspect, since a
 * label whose font has not resolved will fault when it lays the text out.
 */
#include "fw.h"

#define U32(S, off) (*(volatile uint32_t *)((uint32_t)(S) + (off)))
#define OFF_GUARD 0x038
#define GUARD_DONE 0x1AB50002u

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
    if (what != 0) return;
    if (U32(S, OFF_GUARD) == GUARD_DONE) return;
    U32(S, OFF_GUARD) = GUARD_DONE;

    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;

    static const char s1[] = "%s%s: N step1 create label\n";
    fw_log(s1, "", "inj", 0);
    void *obj = lv_obj_class_create_obj(LV_CLASS_LABEL, cont);
    static const char s2[] = "%s%s: N step2 obj=0x%x\n";
    fw_log(s2, "", "inj", (int)(uint32_t)obj);
    if (!obj) return;

    lv_obj_class_init_obj(obj);
    static const char s3[] = "%s%s: N step3 init ok\n";
    fw_log(s3, "", "inj", 0);

    lv_obj_set_pos(obj, 4, 44);
    lv_obj_set_size(obj, 168, 20);
    static const char s4[] = "%s%s: N step4 geometry ok\n";
    fw_log(s4, "", "inj", 0);

    /* what the label already holds: a fresh one should have a text pointer
       set by its constructor; garbage here would explain the fault */
    static const char s5[] = "%s%s: N step5 text_ptr=0x%x\n";
    fw_log(s5, "", "inj", (int)U32(obj, 0x24));

    lv_label_set_text_copy(obj, "OURS");
    static const char s6[] = "%s%s: N step6 SET TEXT SURVIVED\n";
    fw_log(s6, "", "inj", 0);
}
