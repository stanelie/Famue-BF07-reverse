/* RAM payload: open the book on our own handle, and report every step.
 *
 * The flashed open block logs nothing on this device, so rather than reason
 * about code I cannot watch, this does the same job from RAM where each stage
 * is visible: which list entry, what path, what fs_open returned, what size the
 * handle reports.
 *
 * On success it fills in S->my_file and sets file_ready, so `ramload.py off`
 * afterwards leaves the FLASHED reader running with a valid private handle.
 *
 * Offsets from struct inj_state: my_file +0x23d, file_ready +0x251.
 * Runs only on the ebook thread (what == 1) -- file I/O on the display thread
 * races the vendor's decode.
 */
#include "fw.h"

#define U8(S, off) (*(volatile unsigned char *)((uint32_t)(S) + (off)))
#define U32(S, off) (*(volatile uint32_t *)((uint32_t)(S) + (off)))
#define OFF_GUARD   0x038          /* probe_calls, reused as a one-shot flag */
#define GUARD_DONE  0xD09E0001u
#define OFF_MY_FILE 0x23d
#define OFF_READY   0x251

#define LIST_BASE 0x18007800u
#define LIST_STEP 0x100u
#define LIST_NAME 3

void inj_entry(int what, void *S)
{
    /* No statics: an initialised one still landed in .bss, which the packing
       script discards (an uploaded blob carries no zeroed section). The guard
       lives in the state instead -- probe_calls is unused while this runs. */
    /* Heartbeat first: the previous build logged nothing at all, which could
       mean either entry point is never reached or a guard rejects it. */
    {
        static const char hb[] = "%s%s: O tick what=%d\n";
        fw_log(hb, "", "inj", what);
    }
    if (what != 1) return;
    if (U32(S, OFF_GUARD) == GUARD_DONE) return;
    if (U8(S, OFF_READY)) { U32(S, OFF_GUARD) = GUARD_DONE; return; }

    uint32_t want = *(volatile uint32_t *)0x1801a090u;   /* the open book's size */
    static const char w[] = "%s%s: O want=%d\n";
    fw_log(w, "", "inj", (int)want);
    if (!want) return;

    void *fh = (void *)((uint32_t)S + OFF_MY_FILE);

    for (int i = 0; i < 16; i++) {
        const char *nm = (const char *)(LIST_BASE + i * LIST_STEP + LIST_NAME);
        if (nm[0] < 0x20 || nm[0] > 0x7e) continue;

        char path[96];
        int k = 0;
        const char *pre = "/SD1://";
        while (pre[k]) { path[k] = pre[k]; k++; }
        for (int j = 0; nm[j] && j < 80 && k < 95; j++) path[k++] = nm[j];
        path[k] = 0;

        fw_memset(fh, 0, 20);
        int rc = fs_open((fs_file_t *)fh, path, 1);
        static const char a[] = "%s%s: O entry=%d\n";
        fw_log(a, "", "inj", i);
        static const char b[] = "%s%s: O rc=%d\n";
        fw_log(b, "", "inj", rc);
        if (rc >= 0) {
            /* Size cannot be read from a fresh handle: fs_file_t+0x0c is 0
               until the vendor's own bookkeeping fills it, which is why the
               correct file (entry 8, the open book) was rejected and closed.
               Probe the size with reads instead: the last byte must exist and
               one byte past the end must not. */
            char probe;
            int at_end = 0, past_end = 1;
            if (fs_seek((fs_file_t *)fh, (int32_t)want - 1, FS_SEEK_SET) >= 0)
                at_end = (fs_read((fs_file_t *)fh, &probe, 1) == 1);
            if (fs_seek((fs_file_t *)fh, (int32_t)want, FS_SEEK_SET) >= 0)
                past_end = (fs_read((fs_file_t *)fh, &probe, 1) == 1);
            static const char c[] = "%s%s: O end*10+past=%d\n";
            fw_log(c, "", "inj", at_end * 10 + past_end);
            if (at_end && !past_end) {
                U8(S, OFF_READY) = 1;
                U32(S, OFF_GUARD) = GUARD_DONE;
                static const char d[] = "%s%s: O OPENED entry=%d\n";
                fw_log(d, "", "inj", i);
                return;
            }
            fs_close((fs_file_t *)fh);
            fw_memset(fh, 0, 20);
        }
    }
    U32(S, OFF_GUARD) = GUARD_DONE;    /* one sweep only; do not spam the FS */
    static const char e[] = "%s%s: O gave up\n";
    fw_log(e, "", "inj", 0);
}
