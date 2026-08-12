/* BF07 replacement ebook reader -- injected into the vendor firmware.
 *
 * Integration (each point established by measurement, see docs/):
 *   0x1004a288  line height   -> hook()          display thread
 *   0x100493a8  after render  -> render_hook()   display thread
 *   0x1004c002  msg receive   -> prepare_hook()  EBOOK thread (owns the file)
 *
 * Threading: the display thread never touches the filesystem. It records what
 * it wants and draws; the ebook thread reads and wraps in the idle time before
 * its message loop blocks. Doing I/O on the display thread raced
 * ebook_calculate_pages and corrupted kernel state.
 *
 * Memory: the vendor decodes book text into EVERY page context, so no context
 * RAM is reusable. State is allocated with lv_mem_alloc and anchored in the one
 * region proven free by a canary under the real workload (reading + audio).
 */
#include "fw.h"

#define PITCH     19        /* +1 base in the caller = 20px pitch */
#define INJ_LINES 12
#define MAXW      44        /* buffer per displayed line          */
#define CPL       25        /* fallback characters per line       */
#define LINE_PX  168        /* label width, measured on hardware  */
#define BACKSTACK 48
#define INJ_MAGIC 0x52444252u
/* Bumped on every build. recover() adopts a heap block only if this
   matches: sizeof alone let one build inherit another's state, whose
   fields meant different things -- results that looked like code bugs. */
#define INJ_BUILD_ID 1786541162u
/* The vendor paginates 8 of its own lines to a page (its reading_line at
   +0x194 advances by 8 per turn; +0x19c holds its total page count). */
#define VENDOR_LINES_PER_PAGE 8

/* ---- state ---------------------------------------------------------- */

struct page {
    int32_t  start;                   /* byte offset this page begins at   */
    int32_t  end;                     /* byte offset of the following page */
    uint16_t nlines;
    char     text[INJ_LINES][MAXW];
};

struct inj_state {
    uint32_t magic;
    uint32_t gen;                     /* generation: newest block wins     */
    uint32_t calls;
    int32_t  last_line;               /* vendor position: a SIGNAL only    */
    int32_t  want;                    /* offset we want shown, or -1       */
    uint8_t  nxt_valid;
    uint8_t  sp;
    volatile uint8_t need_prep;
    int32_t  jump_line;               /* absolute seek requested, or -1    */
    uint32_t book_sig;                /* identity of the open book         */
    int32_t  size;                    /* file size, found once per book    */
    int32_t  last_sy;                 /* container scroll offset, observed */
    int32_t  last_pm;                 /* last shown progress, in tenths    */
    uint8_t  want_prev;               /* back with no history: find it      */
    /* OUR OWN file handle.
     *
     * The FS layer takes no lock anywhere (verified by reading fs_read/fs_seek:
     * thin vtable dispatchers), so our seek-before-every-read moves the file
     * position under the vendor's decoder, which runs on the display thread.
     * Its decode then fails -- and the render call our hook rides on is skipped
     * when it does, which silently switches this reader off. That is the stall.
     *
     * Identified by PROBING the length, never by fs_file_t+0x0c: that field is
     * only filled on the vendor's long-lived handle and reads 0 on a fresh
     * open, so a size comparison rejects the correct file every time. */
    fs_file_t my_file;
    uint8_t  file_ready;
    uint8_t  io_fail;
    uint32_t open_try;                /* throttle: one sweep per render pass */
    struct page cur;                  /* on screen                         */
    struct page nxt;                  /* pre-rendered, ready to swap in    */
    int32_t  back[BACKSTACK];
};

struct inj_anchor { uint32_t magic; struct inj_state *st; };
#define ANCHOR ((volatile struct inj_anchor *)INJ_ANCHOR)

/* The anchor lives at 0x18018e98, which measurement showed is inside the
   READING SCENE's own data -- _reading_unload_resource owns 0x18018e20 /
   0x18018e35 / 0x18018e95, three bytes below us, and clears the area. Pressing
   the speaker icon unloads scene resources, wiping the anchor, so we allocated
   a fresh state and restarted the book at offset 0 while the vendor's own page
   counter carried on. That is the reported bug.

   The state BLOCK survives: lv_mem_alloc'd and never freed. Only the pointer
   is lost. So when the anchor is gone, search the LVGL heap for the newest
   block still carrying our magic and adopt it. Nothing durable is required.

   Bounds are the observed allocation window (both live states seen at
   0x010059xx / 0x010086xx); the ADFU payload loads at 0x01010000, so this is
   mapped RAM and safe to read. */
#define HEAP_LO 0x01000000u
#define HEAP_HI 0x01020000u

static struct inj_state *recover(void)
{
    struct inj_state *best = 0;
    uint32_t best_gen = 0;
    for (uint32_t a = HEAP_LO; a < HEAP_HI - sizeof(struct inj_state); a += 4) {
        struct inj_state *c = (struct inj_state *)a;
        if (c->magic != INJ_MAGIC) continue;
        /* cheap plausibility check so heap junk can't impersonate a state */
        if (c->cur.nlines > INJ_LINES || c->nxt.nlines > INJ_LINES) continue;
        if (c->cur.start < 0 || c->sp > BACKSTACK) continue;
        if (c->gen >= best_gen) { best_gen = c->gen; best = c; }
    }
    return best;
}

static struct inj_state *state(void)
{
    if (ANCHOR->magic == INJ_MAGIC && ANCHOR->st) return ANCHOR->st;

    struct inj_state *n = recover();
    if (n) {
        /* Adopted an existing page: redraw where the reader actually was.
           Bump the generation so this block outranks any stale twin later. */
        n->gen++;
        n->want = n->cur.start;
        n->nxt_valid = 0;
        n->need_prep = 1;
    } else {
        void *p = lv_mem_alloc(sizeof(struct inj_state));
        if (!p) return 0;
        fw_memset(p, 0, sizeof(struct inj_state));
        n = (struct inj_state *)p;
        n->magic = INJ_MAGIC;
        n->gen = 1;
        n->last_line = -1;
        n->want = 0;
        n->jump_line = -1;
    }
    ANCHOR->st = n;
    ANCHOR->magic = INJ_MAGIC;
    return n;
}

/* ---- helpers (no libc) ---------------------------------------------- */

static void *reader_obj(void)
{
    uint32_t app = *(volatile uint32_t *)FW_APP_GLOBAL;
    if (app < 0x18000000 || app >= 0x18200000) return 0;
    uint32_t rd = *(volatile uint32_t *)(app + 0x3c);
    if (rd < 0x18000000 || rd >= 0x18200000) return 0;
    return (void *)rd;
}

/* Reuses the vendor's OPEN handle (ebook_file_init -> 0x1801a084). Opening the
   book a second time corrupted the FS layer's mutex bookkeeping. */
static int book_read(struct inj_state *S, int32_t off, char *buf, uint32_t len)
{
    if (S && S->file_ready) {
        int rc = fs_seek(&S->my_file, off, FS_SEEK_SET);
        if (rc >= 0) {
            rc = fs_read(&S->my_file, buf, len);
            if (rc > 0) { S->io_fail = 0; return rc; }
        }
        /* Do not fall back to the vendor's handle -- sharing it is the bug.
           Tolerate a couple of failures, then close and allow one reopen. */
        if (++S->io_fail >= 3) {
            fs_close(&S->my_file);
            fw_memset(&S->my_file, 0, sizeof(fs_file_t));
            S->file_ready = 0;
            S->io_fail = 0;
        }
        return -1;
    }
    int rc = fs_seek(FW_BOOK_FILE, off, FS_SEEK_SET);
    if (rc < 0) return rc;
    return fs_read(FW_BOOK_FILE, buf, len);
}

/* Open the book ourselves, from the picker's file list in RAM (0x100-byte
   entries at FW_FILE_LIST, filename at +3). The right entry is the one whose
   length matches the open book: last byte readable, one past the end not.
 *
 * The base was 0x18007800 for days, and every open failed: that address is
 * inside the buffer the vendor reuses for book TEXT once reading starts, so we
 * were building paths like "/SD1://uromancer sho". Dumping the region showed
 * the real list 0x800 lower, holding the two book names verbatim. */
static void book_open_own(struct inj_state *S)
{
    uint32_t want = *FW_BOOK_SIZE;
    if (!want || want >= 0x400000u) return;
    for (int i = 0; i < 16 && !S->file_ready; i++) {
        const char *nm = (const char *)(FW_FILE_LIST + i * 0x100u + 3);
        if (nm[0] < 0x20 || nm[0] > 0x7e) continue;
        char path[96];
        int k = 0;
        const char *pre = "/SD1://";
        while (pre[k]) { path[k] = pre[k]; k++; }
        for (int j = 0; nm[j] && j < 80 && k < 95; j++) path[k++] = nm[j];
        path[k] = 0;
        fw_memset(&S->my_file, 0, sizeof(fs_file_t));
        if (fs_open(&S->my_file, path, FS_O_READ) < 0) continue;
        char probe;
        int at_end = 0, past_end = 1;
        if (fs_seek(&S->my_file, (int32_t)want - 1, FS_SEEK_SET) >= 0)
            at_end = (fs_read(&S->my_file, &probe, 1) == 1);
        if (fs_seek(&S->my_file, (int32_t)want, FS_SEEK_SET) >= 0)
            past_end = (fs_read(&S->my_file, &probe, 1) == 1);
        if (at_end && !past_end) {
            S->file_ready = 1;
            S->size = want;
            static const char ok[] = "%s%s: OWNFILE entry=%d\n";
            fw_log(ok, "", "inj", i);
            return;
        }
        fs_close(&S->my_file);
        fw_memset(&S->my_file, 0, sizeof(fs_file_t));
    }
}

/* Proportional width estimate in 1/8 px. The firmware's own measurer
   (0x100eb4b8) was tried and rejected: its third argument is a mode selector,
   not an encoding, and the path our value takes returns the width argument
   instead of measuring. */
static int char_w8(unsigned char c)
{
    if (c == ' ') return 36;
    if (c < 0x20) return 0;
    if (c >= 0x80) return 88;
    switch (c) {
    case 'i': case 'j': case 'l': case '.': case ',': case ':': case ';':
    case '\'': case '!': case '|': case '`':          return 26;
    case 'f': case 'r': case 't': case '(': case ')':
    case '[': case ']': case '-':                     return 38;
    case 'm': case 'w': case 'M': case 'W': case '@': return 88;
    default:
        if (c >= 'A' && c <= 'Z') return 72;
        if (c >= '0' && c <= '9') return 60;
        return 58;
    }
}

/* REFLOW. Measured on a real book: 143 of 178 lines ended at a newline in the
   FILE and 84 of those were blank, so half the page was the file's own layout
   and the text lines ran ~16 chars against a 24-char width. So a single newline
   is soft -- it becomes a space -- and only a blank line is a real break.

   Emits the line into `out` and returns SOURCE bytes consumed, so the caller
   keeps a true file offset without a second buffer or an index map.
   why: 1 = paragraph break, 2 = ran out of width, 3 = hit MAXW, 4 = end of buf */
#define WHY_PARA 1
#define WHY_WIDTH 2
#define WHY_MAXW 3
#define WHY_EOB 4

static int wrap_one(const char *p, int avail, char *out, int indent,
                    int *px_out, int *why_out)
{
    *px_out = 0;
    *why_out = 0;
    out[0] = 0;
    if (avail <= 0) return 0;

    const int limit = (LINE_PX - 4) * 8;    /* margin: erring short is invisible */
    int i = 0, k = 0, w8 = 0;
    int sp_src = -1, sp_out = -1;           /* last space, in both spaces */

    for (int n = 0; n < indent && k < MAXW - 1; n++) {
        out[k++] = ' ';
        w8 += char_w8(' ');
    }

    while (i < avail && k < MAXW - 1) {
        unsigned char c = (unsigned char)p[i];

        if (c == '\r') { i++; continue; }

        if (c == '\n') {
            /* look ahead: a second newline (blank line) is a paragraph break */
            int j = i + 1, nl = 1;
            while (j < avail && (p[j] == '\r' || p[j] == ' ' || p[j] == '\t')) j++;
            if (j < avail && p[j] == '\n') nl = 2;

            if (nl == 2) {
                while (j < avail && (p[j] == '\n' || p[j] == '\r' ||
                                     p[j] == ' ' || p[j] == '\t')) j++;
                out[k] = 0;
                *px_out = w8 / 8;
                *why_out = WHY_PARA;
                return j;
            }
            if (j >= avail) {              /* can't tell yet -- stop cleanly */
                out[k] = 0;
                *px_out = w8 / 8;
                *why_out = WHY_EOB;
                return i + 1;
            }
            c = ' ';                        /* soft: join the lines */
            i = j - 1;                      /* resume at the next real char */
        }

        if (c == ' ' || c == '\t') {
            if (k == 0) { i++; continue; }  /* no leading space on a line */
            if (k > 0 && out[k - 1] == ' ') { i++; continue; }  /* collapse runs */
            c = ' ';
        }

        w8 += char_w8(c);
        if (w8 > limit) { *why_out = WHY_WIDTH; break; }

        if (c == ' ') { sp_src = i; sp_out = k; }
        out[k++] = (char)c;
        i++;
    }

    *px_out = w8 / 8;
    if (!*why_out) *why_out = (i >= avail) ? WHY_EOB : WHY_MAXW;

    if (i >= avail && *why_out == WHY_EOB) { out[k] = 0; return i; }

    /* break at the last space so words stay whole */
    if (sp_out > 0) {
        out[sp_out] = 0;
        return sp_src + 1;
    }
    out[k] = 0;
    return i > 0 ? i : 1;
}

/* ---- page preparation (EBOOK thread only) --------------------------- */

static void fill_page(struct inj_state *S, struct page *p, int32_t off)
{
    /* reflow packs ~50% more text per page, so the read window grew with it */
    char raw[768];
    p->start = off;
    p->nlines = 0;
    int rc = book_read(S, off, raw, sizeof raw);
    {
        static const char r1[] = "%s%s: fill off=%d\n";
        static const char r2[] = "%s%s: fill rc=%d\n";
        fw_log(r1, "", "inj", off);
        fw_log(r2, "", "inj", rc);
    }
    if (rc <= 0) { p->end = off; return; }

    int pos = 0;
    int blank = 0;                  /* a paragraph break owes a blank line */
    for (int i = 0; i < INJ_LINES && pos < rc; i++) {
        /* Spend a real line on the paragraph gap -- reflow consumes the file's
           blank line, so the separation has to be put back deliberately.
           Never at the top of a page: a page opening on blank looks broken. */
        if (blank && i > 0) {
            p->text[i][0] = 0;
            p->nlines++;
            blank = 0;
            continue;
        }
        blank = 0;

        int px = 0, why = 0;
        int take = wrap_one(raw + pos, rc - pos, p->text[i], 0, &px, &why);
        if (take <= 0) break;
        /* Ran off the end of the read window with more file behind it: the
           line would break mid-word, so drop it rather than show a fragment. */
        if (why == WHY_EOB && rc == (int)sizeof raw) { p->text[i][0] = 0; break; }
        pos += take;
        p->nlines++;
        blank = (why == WHY_PARA);
        (void)px; (void)why;
    }
    p->end = off + pos;
}

/* File size, by binary search on the last readable byte.
   Zephyr's fs_tell is not in the symbol map, and fs_seek reports only success,
   so the size is probed: ~23 one-byte reads, once per book. */
static int32_t book_size(struct inj_state *S)
{
    char b;
    int32_t lo = 0, hi = 1 << 23;              /* 8 MB ceiling */
    if (S->size > 0) return S->size;
    /* The vendor already knows it -- it logs "open ebook ok, size: N" -- so
       take the exact value and skip the probe entirely. */
    {
        uint32_t v = *FW_BOOK_SIZE;
        if (v > 0 && v < 0x400000u) { S->size = (int32_t)v; return S->size; }
    }
    while (lo < hi) {
        int32_t mid = lo + (hi - lo + 1) / 2;
        if (book_read(S, mid - 1, &b, 1) > 0) lo = mid;
        else hi = mid - 1;
    }
    S->size = lo;
    return lo;
}

/* Byte offset where the vendor's line N begins.
 *
 * The vendor stores only a line index (+0x194) and a total page count
 * (+0x19c); it never records a byte offset, so this has to be derived.
 *
 * An earlier version counted newlines, on the theory that the vendor honours
 * the file's own line breaks. That holds only for a HARD-WRAPPED file. Given a
 * book of long flowing paragraphs the vendor wraps each paragraph into many
 * lines while the file holds one newline, so counting overshot and landed near
 * the end -- one book resumed correctly and the other did not.
 *
 * Interpolating on file size works for either format: the vendor's lines are a
 * fixed width, so line number is proportional to position, whatever the source
 * line breaks look like. The result is snapped forward to a line start so a
 * page never begins mid-word.
 *
 * Done in 32-bit arithmetic on purpose: a 64-bit divide would emit a call to
 * __aeabi_ldivmod, which does not exist in this freestanding image.
 */
static int32_t offset_of_line(struct inj_state *S, int32_t target,
                              int32_t total_lines)
{
    char buf[128];
    int32_t size, q, r, off;

    if (target <= 0 || total_lines <= 0) return 0;
    size = book_size(S);
    if (size <= 0) return 0;
    if (target >= total_lines) return size;

    q = size / total_lines;
    r = size % total_lines;                    /* r < total_lines, so r*target */
    off = q * target + (r * target) / total_lines;   /* cannot overflow int32 */

    int rc = book_read(S, off, buf, sizeof buf);
    for (int i = 0; i < rc; i++) {
        if (buf[i] == '\n') { off += i + 1; break; }
    }
    return off;
}

void prepare_body(void)
{
    /* Services only what the display thread asked for; never creates state. */
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st) return;
    struct inj_state *S = ANCHOR->st;
    /* Try the open a few times, not once per render pass forever. With the
       wrong list address this retried 16 fs_open calls on every tick -- 1117
       sweeps by the time it was measured -- which is what made page turns take
       two seconds. Wait for the vendor to publish the size before counting an
       attempt, so a slow open does not burn the budget. */
    if (!S->file_ready && *FW_BOOK_SIZE && S->open_try < 8) {
        S->open_try++;
        book_open_own(S);
    }

    if (!S->need_prep) return;
    S->need_prep = 0;

    /* Tell the vendor how many lines a page actually holds.
     *
     * ebook_calculate_pages computes pages as (total - 1 + n) / n with a
     * hardware udiv, where n is this BYTE in RAM -- not the hardcoded /8 shift
     * that defeated the old constant-patching route. Setting it to our line
     * count makes the vendor's page numbers, its select-page menu, its totals
     * and its .bmk index describe the pages we actually draw. */

    /* Detect a different book and resync to ITS saved position.
     *
     * Our state block survives across books (it is recovered by magic, not
     * owned by the scene), so without this the previous book's byte offset
     * carried over -- which is what made a return visit resume in the wrong
     * place. Identity is a hash of the first bytes plus the vendor's total
     * page count; the vendor's own line index then supplies the position. */
    {
        char sig[64];
        uint32_t h = 2166136261u;
        int rc = book_read(S, 0, sig, sizeof sig);
        if (rc > 0) {
            for (int i = 0; i < rc; i++)
                h = (h ^ (unsigned char)sig[i]) * 16777619u;
            /* Identity comes from the FILE only. Mixing in the vendor's page
               count via reader_obj() made the hash unstable -- that pointer
               alternates between two reader objects and is sometimes null, so
               the signature changed constantly and every prepare re-jumped to
               the vendor's line, dragging the page back on each turn. */
            void *rd = reader_obj();
            int32_t vline = 0;
            if (rd) vline = *(volatile int32_t *)((uint32_t)rd + RD_OFF_LINE);
            if (h != S->book_sig) {
                S->book_sig = h;
                S->size = 0;              /* re-probe: different file */
                S->sp = 0;
                S->nxt_valid = 0;
                S->last_line = vline;
                S->jump_line = vline;
                {
                    static const char b[] = "%s%s: BOOK line=%d\n";
                    fw_log(b, "", "inj", vline);
                }
            }
        }
    }

    /* Previous page with no history (we arrived by a jump).
     *
     * Interpolating the vendor's line was what made this wander, because
     * total_lines is still growing. Instead paginate FORWARD from a point
     * safely before the current page and keep the last page that ends at or
     * after it -- that is the previous page, exactly, using only our own
     * layout. S->nxt is the scratch buffer, so nothing large goes on the
     * ebook thread's stack. */
    if (S->want_prev) {
        S->want_prev = 0;
        int32_t target = S->cur.start;
        if (target > 0) {
            int32_t off = target - (INJ_LINES * 48);
            if (off < 0) off = 0;
            int32_t start = off;
            for (int i = 0; i < 40; i++) {
                fill_page(S, &S->nxt, off);
                if (S->nxt.end >= target || S->nxt.end <= off) break;
                start = off;
                off = S->nxt.end;
            }
            if (off < target) start = off;
            fill_page(S, &S->nxt, start);
            S->nxt_valid = 1;
            S->want = start;
            static const char pv[] = "%s%s: PREV off=%d\n";
            fw_log(pv, "", "inj", start);
        }
        return;
    }

    if (S->jump_line >= 0) {
        /* total LINES, read straight from the ebook context -- no more
           deriving it from a page count times an assumed page size */
        int32_t off = offset_of_line(S, S->jump_line, (int32_t)*FW_TOTAL_LINES);
        S->jump_line = -1;
        fill_page(S, &S->nxt, off);
        S->nxt_valid = 1;
        S->want = off;                     /* after_render swaps it in */
        {
            static const char j[] = "%s%s: JUMP off=%d\n";
            fw_log(j, "", "inj", off);
        }
        return;
    }

    if (S->want >= 0) {
        if (!S->nxt_valid || S->nxt.start != S->want) {
            fill_page(S, &S->nxt, S->want);
            S->nxt_valid = 1;
        }
    } else if (!S->nxt_valid) {
        fill_page(S, &S->nxt, S->cur.end);     /* PRE-RENDER while the user reads */
        S->nxt_valid = 1;
    }
}

/* Inspect each message the reading loop receives.
 *
 * Confirmed layout: the loop does `add r0, sp, #0x18` before the receive, and
 * then reads the TYPE from [sp+0x19] and the COMMAND from [sp+0x1a] -- so from
 * the pointer we already hold, type is +1 and command is +2.
 *
 * Step one of owning input: learn which message each physical press and each
 * touch actually sends, so the reader can act on them itself instead of
 * inferring a page turn from the vendor's line counter moving.
 */
void prepare_msg(void *msg)
{
    if (!msg) return;
    unsigned t = *(volatile unsigned char *)((uint32_t)msg + 1);
    unsigned c = *(volatile unsigned char *)((uint32_t)msg + 2);

    /* Give the vendor our real page size, at the source.
     *
     * cmd 1 carries a 16-byte layout block. Its handler copies it to
     * 0x1801a098 and immediately computes pages = (total - 1 + n) / n by
     * hardware divide, where n is the block's FIRST BYTE. Writing 0x1801a098
     * directly lost the race -- this message arrives about 120 times a second
     * and simply overwrote our value, which is why lines_per_page stayed 8 and
     * the select-page menu totalled the book in 8-line pages.
     *
     * The payload pointer is at msg+4: the loop does `add r0, sp, #0x18` for
     * the message and the handler reads the payload from [sp+0x1c]. */
    /* A page selection arrives as cmd 4 with a 4-byte payload -- the handler
       stores it to 0x1801a080. That is an explicit user action, unlike a line
       delta, which also moves when the background pagination recalculates. */
    if (t == 8 && c == 4 && ANCHOR->magic == INJ_MAGIC && ANCHOR->st) {
        uint32_t pl = *(volatile uint32_t *)((uint32_t)msg + 4);
        if (pl >= 0x01000000 && pl < 0x18200000) {
            int32_t sel = *(volatile int32_t *)pl;
            if (sel > 0) {
                struct inj_state *S = ANCHOR->st;
                S->jump_line = sel;
                S->sp = 0;
                S->need_prep = 1;
            }
        }
    }

    if (t == 8 && c == 1) {
        uint32_t pl = *(volatile uint32_t *)((uint32_t)msg + 4);
        if (pl >= 0x01000000 && pl < 0x18200000)
            *(volatile unsigned char *)pl = INJ_LINES;
    }

    /* cmd 1 and 8 are periodic housekeeping -- 10946 and 806 of them in one
       short session -- and drown the timeline. Only user-facing traffic. */
    if (t == 8 && (c == 1 || c == 8)) return;
    static const char m[] = "%s%s: MSG type*1000+cmd=%d\n";
    fw_log(m, "", "inj", (int)(t * 1000 + c));
}

__attribute__((naked)) void prepare_hook(void)
{
    __asm__ volatile(
        "push  {r4, r5, lr}\n"
        "mov   r4, r0\n"
        "movw  r12, #0xf06d\n"      /* 0x100ff06c | thumb */
        "movt  r12, #0x100f\n"
        "blx   r12\n"
        "mov   r5, r0\n"
        "mov   r0, r4\n"
        "bl    prepare_msg\n"
        "bl    prepare_body\n"
        "mov   r0, r5\n"
        "pop   {r4, r5, pc}\n");
}

/* Show progress as a PERCENTAGE instead of a page number.
 *
 * Page numbers here are worth little: the vendor derives them from a
 * background pagination that is still running (total_lines was measured
 * climbing 224 -> 3416 over ninety idle seconds), they change whenever the
 * line count changes, and they describe this device's layout only. A byte
 * offset over the file size is exact from the first page, costs nothing, and
 * means the same thing anywhere.
 *
 * The counter is in the status bar, which is the screen child spanning the top
 * 25 px. Labels are identified by CLASS -- read from one of our own text lines,
 * which is certainly a label -- so this cannot write text into a widget that is
 * not one, whatever the resource layout puts there.
 */
static void show_percent(void *cont, struct inj_state *S)
{
    uint32_t scr = *(volatile uint32_t *)((uint32_t)cont + 4);
    if (scr < 0x01000000) return;
    /* Size from the vendor's own field -- S->size is only filled during a
       jump, so relying on it meant this bailed out on a normal read. */
    int32_t size = (int32_t)*FW_BOOK_SIZE;
    if (size <= 0) size = S->size;
    if (size <= 0) return;

    /* Tenths, not whole percent. Scaling by 128 and multiplying by only 100
       left the whole book as ~100 steps, so an early page sat right on the 0/1
       boundary and each turn flipped it. A page is ~267 bytes of a ~500 KB
       book -- about 0.05% -- so tenths actually move. /64 keeps the multiply
       inside int32 for books up to 8 MB. */
    int pm = (int)((S->cur.start / 64) * 1000 / (size / 64 + 1));
    if (pm < 0) pm = 0;
    if (pm > 1000) pm = 1000;

    char buf[10];
    int n = 0;
    int whole = pm / 10, frac = pm % 10;
    if (whole >= 100) { buf[n++] = '1'; buf[n++] = '0'; buf[n++] = '0'; }
    else {
        if (whole >= 10) buf[n++] = (char)('0' + whole / 10);
        buf[n++] = (char)('0' + whole % 10);
        buf[n++] = '.';
        buf[n++] = (char)('0' + frac);
    }
    buf[n++] = '%';
    buf[n] = 0;

    if (pm != S->last_pm) {
        S->last_pm = pm;
        static const char pf[] = "%s%s: PCT tenths=%d\n";
        fw_log(pf, "", "inj", pm);
        static const char po[] = "%s%s: PCT off=%d\n";
        fw_log(po, "", "inj", S->cur.start);
    }

    uint32_t sn = lv_obj_child_cnt((void *)scr);
    if (sn > 16) sn = 16;
    for (uint32_t i = 0; i < sn; i++) {
        uint32_t bar = (uint32_t)lv_obj_get_child((void *)scr, i);
        if (bar < 0x01000000) continue;
        int16_t by1 = *(volatile int16_t *)(bar + 0x16);
        int16_t by2 = *(volatile int16_t *)(bar + 0x1a);
        int16_t bx1 = *(volatile int16_t *)(bar + 0x14);
        int16_t bx2 = *(volatile int16_t *)(bar + 0x18);
        if (by1 != 0 || by2 > 30 || (bx2 - bx1) < 100) continue;  /* status bar */
        /* The counter is the WIDE widget in the bar (73 px; the icons are 16-22
           px). It is not a label -- class 0x1012c828, probably the textarea the
           scene log mentions -- and it holds one leaf child, class 0x1012c7f0,
           which is what actually shows the number. Write to that child, chosen
           by geometry and by being a leaf, never to the container itself. */
        uint32_t bn = lv_obj_child_cnt((void *)bar);
        if (bn > 8) bn = 8;
        for (uint32_t j = 0; j < bn; j++) {
            uint32_t o = (uint32_t)lv_obj_get_child((void *)bar, j);
            if (o < 0x01000000) continue;
            int16_t ox1 = *(volatile int16_t *)(o + 0x14);
            int16_t ox2 = *(volatile int16_t *)(o + 0x18);
            if ((ox2 - ox1) < 50) continue;            /* skip the icons */
            void *k = lv_obj_get_child((void *)o, 0);
            if (!k || lv_obj_child_cnt(k) != 0) continue;
            /* Class-checked again, and with the COPYING setter this time. The
               crash came from using the static-text variant (which stores a
               pointer and sets a flag) on a widget of a different class. */
            if (*(volatile uint32_t *)k != LV_CLASS_COUNTER_IN) continue;
            /* Only when it actually differs. This setter strlens and REALLOCS,
               and calling it on every render churned the LVGL heap about three
               times a second -- the same heap our own state is allocated from.
               The text pointer lives at +0x24. */
            const char *cur = *(const char *volatile *)((uint32_t)k + 0x24);
            if (cur) {
                int i = 0;
                while (buf[i] && cur[i] == buf[i]) i++;
                if (buf[i] == 0 && cur[i] == 0) continue;   /* identical */
            }
            lv_label_set_text_copy(k, buf);
        }
    }
}

/* Record our position for the vendor's bookmark, ONCE, on the way out.
 *
 * ebook_bmk_update saves 0x1801a080 into the .bmk and _reading_create_content
 * restores it on open. Syncing it on every render fed back: total_lines is
 * still being computed, so the converted value kept changing, the vendor's line
 * moved with it, and our own turn detection read that as a page turn -- which
 * is why advancing past 1.0% snapped back to 0.6%. Written only here, when the
 * return button is pressed, there is nothing to feed back into.
 */
void exit_body(void)
{
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st) return;
    struct inj_state *S = ANCHOR->st;
    int32_t tl = (int32_t)*FW_TOTAL_LINES;
    int32_t sz = (int32_t)*FW_BOOK_SIZE;
    if (tl <= 0 || sz <= 0 || S->cur.start <= 0) return;
    *FW_CUR_LINE = (uint32_t)((S->cur.start / 64) * tl / (sz / 64 + 1));
    static const char e[] = "%s%s: EXIT line=%d\n";
    fw_log(e, "", "inj", (int)*FW_CUR_LINE);
}

/* Detour on _ebook_return_btn_event_cb (0x100494dc); replicates its prologue
   and re-enters past the four patched bytes. */
__attribute__((naked)) void exit_hook(void)
{
    __asm__ volatile(
        "push  {r0-r3, lr}\n"
        "bl    exit_body\n"
        "pop   {r0-r3, lr}\n"
        "push.w {r4, r5, r6, r7, r8, lr}\n"   /* the overwritten insn */
        "movw  r12, #0x94e1\n"                /* 0x100494e0 | thumb  */
        "movt  r12, #0x1004\n"
        "bx    r12\n");
}

/* ---- scroll probe: stage 1 of owning input -------------------------- */

/* Detour on _reading_scroll_event_cb (0x10049684).
 *
 * Page turns never reach the ebook thread -- captured message traffic shows
 * open (cmd 4,5), the menu (cmd 3) and back (cmd 12), but nothing at all for a
 * turn. They arrive here instead, as LV_EVENT_SCROLL (0x0b): the reading view
 * is a tall scrolled container and reading_line is derived from where it sits.
 *
 * This first version only reports, so the takeover is built on measurement
 * rather than assumption: event code, the container's scroll offset and the
 * vendor's line, once per event.
 */
void scroll_probe(void *e)
{
    unsigned code = lv_event_get_code(e);
    void *rd = reader_obj();
    int32_t sy = 0, line = -1;
    if (rd) {
        uint32_t cont = *(volatile uint32_t *)((uint32_t)rd + RD_OFF_LIST);
        line = *(volatile int32_t *)((uint32_t)rd + RD_OFF_LINE);
        if (cont >= 0x01000000) {
            uint32_t spec = *(volatile uint32_t *)(cont + 8);
            if (spec >= 0x01000000)
                sy = *(volatile int32_t *)(spec + 0x14);
        }
    }
    {
        static const char m[] = "%s%s: SCROLL code=%d\n";
        fw_log(m, "", "inj", (int)code);
        static const char n[] = "%s%s: SCROLL sy=%d\n";
        fw_log(n, "", "inj", sy);
        static const char o[] = "%s%s: SCROLL line=%d\n";
        fw_log(o, "", "inj", line);
    }
}

/* Replicates the original prologue, then re-enters it past the patched bytes. */
__attribute__((naked)) void scroll_hook(void)
{
    __asm__ volatile(
        "push  {r0-r3, lr}\n"
        "bl    scroll_probe\n"
        "pop   {r0-r3, lr}\n"
        "push.w {r4, r5, r6, r7, r8, r9, lr}\n"   /* the overwritten insn */
        "movw  r12, #0x9689\n"                    /* 0x10049688 | thumb  */
        "movt  r12, #0x1004\n"
        "bx    r12\n");
}

/* ---- drawing and turn detection (display thread) -------------------- */

static void push_back(struct inj_state *S, int32_t off)
{
    if (S->sp < BACKSTACK) S->back[S->sp++] = off;
}

/* Disable the TTS (speaker) button.
 *
 * It starts the vendor's text-to-speech, which walks ITS OWN reading-line
 * counter automatically. Our reader treats a change in that counter as a page
 * turn, so the page ran away on its own; and the scene resource unload it
 * triggers clears the anchor. The two position systems -- our byte offsets and
 * the vendor's line index -- are not reconcilable, so the button is turned off.
 *
 * It is a 16x16 icon at (33,8), a direct child of the SCREEN (not of the status
 * bar container), sitting just right of the back button. Identified by geometry
 * rather than child index, and re-applied every render pass because the scene
 * reallocates its objects -- writing the flag once from the debugger only ever
 * hit a stale object.
 *
 * lv_obj_t: coords at +0x14 (x1,y1,x2,y2 as int16), flags at +0x1c.
 * Clearing LV_OBJ_FLAG_CLICKABLE (1<<1) takes it out of hit-testing.
 */
static void disable_tts_button(void *cont)
{
    uint32_t scr = *(volatile uint32_t *)((uint32_t)cont + 4);   /* parent */
    if (scr < 0x01000000) return;

    /* Only while the READING scene is actually on screen. A loose match here
       froze the device: the geometry test also caught a button in the ebook
       menu, and re-clearing its CLICKABLE every render pass left the UI
       unusable. The reading scene's signature is that our label container is
       the screen's first child. */
    if ((uint32_t)lv_obj_get_child((void *)scr, 0) != (uint32_t)cont) return;

    uint32_t n = lv_obj_child_cnt((void *)scr);
    if (n > 16) n = 16;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t c = (uint32_t)lv_obj_get_child((void *)scr, i);
        if (c < 0x01000000) continue;
        int16_t x1 = *(volatile int16_t *)(c + 0x14);
        int16_t y1 = *(volatile int16_t *)(c + 0x16);
        int16_t x2 = *(volatile int16_t *)(c + 0x18);
        int16_t y2 = *(volatile int16_t *)(c + 0x1a);
        /* exactly the measured icon: 16x16 at (33,8) */
        if (x1 == 33 && y1 == 8 && (x2 - x1) == 15 && (y2 - y1) == 15) {
            *(volatile uint32_t *)(c + 0x1c) &= ~2u;
        }
    }
}

void after_render(void)
{
    /* Establish the scene FIRST. state() can allocate, and allocating once per
       render pass outside the reading scene would leak ~1.1 KB a tick. */
    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;
    struct inj_state *S = state();
    if (!S) return;

    disable_tts_button(cont);
    show_percent(cont, S);

    uint32_t n = lv_obj_child_cnt(cont);
    if (n > INJ_LINES) n = INJ_LINES;

    int line = *(volatile int *)((uint32_t)rd + RD_OFF_LINE);
    S->calls++;

    if (S->last_line < 0) {
        /* Resume where the VENDOR says we were. It restores its saved line on
           open (observed reading 904 before any input), so its per-book
           persistence is reused rather than duplicated. */
        S->last_line = line;
        S->jump_line = line;
        S->need_prep = 1;
    } else if (S->want < 0 && line != S->last_line) {
        int delta = line - S->last_line;
        int lpp = (int)*FW_LINES_PER_PG;
        if (lpp <= 0) lpp = 8;
        /* Leaving the book resets the vendor's line to 0 as the scene tears
           down (traced: _ebook_return_btn_event_cb RETURN, then our jump to
           offset 0). Acting on that threw the position away on the way out and
           the zero then got saved, so the book reopened at the beginning.
           A line of 0 is only a real destination if we are already there. */
        {
            static const char dl[] = "%s%s: DELTA=%d\n";
            fw_log(dl, "", "inj", delta);
        }
        /* A press moves the line by about one page; the background
           recalculation moves it by far more. Bounding by 2 pages keeps the
           noise out without assuming the step is exactly lines_per_page --
           requiring exact equality stopped every turn from registering. */
        if (delta > 2 * lpp || delta < -2 * lpp) {
            /* Not a page turn. The background pagination keeps adjusting the
               line as total_lines grows, and mapping those through
               size * line / total_lines walked the reader BACKWARDS by a
               varying amount each time -- the 1.0% -> 0.6/0.7/0.8% hops.
               Selections now arrive as cmd 4 instead, so this is just noise. */
            S->last_line = line;
        } else if (delta > 0) {
            push_back(S, S->cur.start);
            S->want = S->cur.end;
        } else if (S->sp) {
            S->want = S->back[--S->sp];
        } else if (S->cur.start > 0) {
            S->want_prev = 1;          /* no history: compute it exactly */
        } else if (0) {
            /* No history -- we got here by an absolute jump, which clears the
               stack. Falling back to offset 0 sent "back" to the first page
               while the vendor's counter kept counting down. Map its new line
               instead; pagination is aligned, so that IS the previous page. */
            S->jump_line = line;
        }
        S->last_line = line;
        S->need_prep = 1;
        {
            static const char t[] = "%s%s: TURN want=%d\n";
            fw_log(t, "", "inj", S->want);
        }
    }

    /* Promote only a COMPLETE page. Drawing a half-prepared one is what made
       the screen repaint in two steps. */
    if (S->want >= 0 && S->nxt_valid && S->nxt.start == S->want) {
        /* fw_memcpy, not struct assignment: the compiler emits a call to
           memcpy for a ~540 byte copy and we have no libc to link against. */
        fw_memcpy(&S->cur, &S->nxt, sizeof(struct page));
        S->nxt_valid = 0;
        S->want = -1;
        S->need_prep = 1;               /* pre-render the following page */
    }

    /* Does a page turn move the container's scroll offset? If it does, turns
       can be taken from the scroll position in this very pass -- no vendor line
       and no event callback, which is what owning input actually needs. */
    {
        uint32_t spec = *(volatile uint32_t *)((uint32_t)cont + 8);
        if (spec >= 0x01000000) {
            int32_t sy = *(volatile int32_t *)(spec + 0x14);
            if (sy != S->last_sy) {
                S->last_sy = sy;
                static const char sm[] = "%s%s: SY=%d\n";
                fw_log(sm, "", "inj", sy);
            }
        }
    }

    /* Always write the labels: the vendor fills them on every render, so
       skipping would let its text show through. The content only changes when
       a whole page is swapped in, so repeated draws are identical. */
    for (uint32_t i = 0; i < n; i++) {
        void *c = lv_obj_get_child(cont, i);
        if (!c) continue;
        lv_label_set_text(c, (i < S->cur.nlines) ? S->cur.text[i] : "", 0);
    }
}

/* Hook the timer TAIL, not the conditional render call.
 *
 * Measured on a clean device: with the hook at 0x100493a8 our render pass ran
 * 8 times and stopped. That call is skipped whenever the vendor's fourth
 * decode returns non-zero (`cbnz r0` at 0x100493a4), and its decode fails
 * early because our layout asks it for 12 lines while its page context holds
 * only 8 line records -- the project's oldest finding.
 *
 * The tail at 0x100493b2 (the `b.w` to the timer-resume helper) is reached on
 * every tick regardless, and still runs after the vendor has drawn, so our text
 * is not overwritten. r0 holds the timer argument and lr is already restored,
 * so both survive our call.
 */
__attribute__((naked)) void tail_hook(void)
{
    __asm__ volatile(
        "push  {r0-r3, lr}\n"
        "bl    after_render\n"
        "pop   {r0-r3, lr}\n"
        "movw  r12, #0xd8c3\n"        /* 0x100fd8c2 | thumb */
        "movt  r12, #0x100f\n"
        "bx    r12\n");
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

/* ---- line height ---------------------------------------------------- */

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
