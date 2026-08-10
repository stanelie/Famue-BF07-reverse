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

/* ---- state ---------------------------------------------------------- */

struct page {
    int32_t  start;                   /* byte offset this page begins at   */
    int32_t  end;                     /* byte offset of the following page */
    uint16_t nlines;
    char     text[INJ_LINES][MAXW];
};

struct inj_state {
    uint32_t magic;
    uint32_t calls;
    int32_t  last_line;               /* vendor position: a SIGNAL only    */
    int32_t  want;                    /* offset we want shown, or -1       */
    uint8_t  nxt_valid;
    uint8_t  sp;
    volatile uint8_t need_prep;
    struct page cur;                  /* on screen                         */
    struct page nxt;                  /* pre-rendered, ready to swap in    */
    int32_t  back[BACKSTACK];
};

struct inj_anchor { uint32_t magic; struct inj_state *st; };
#define ANCHOR ((volatile struct inj_anchor *)INJ_ANCHOR)

static struct inj_state *state(void)
{
    if (ANCHOR->magic == INJ_MAGIC && ANCHOR->st) return ANCHOR->st;
    void *p = lv_mem_alloc(sizeof(struct inj_state));
    if (!p) return 0;
    fw_memset(p, 0, sizeof(struct inj_state));
    struct inj_state *n = (struct inj_state *)p;
    n->magic = INJ_MAGIC;
    n->last_line = -1;
    n->want = 0;
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
static int book_read(int32_t off, char *buf, uint32_t len)
{
    int rc = fs_seek(FW_BOOK_FILE, off, FS_SEEK_SET);
    if (rc < 0) return rc;
    return fs_read(FW_BOOK_FILE, buf, len);
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

static void fill_page(struct page *p, int32_t off)
{
    /* reflow packs ~50% more text per page, so the read window grew with it */
    char raw[768];
    p->start = off;
    p->nlines = 0;
    int rc = book_read(off, raw, sizeof raw);
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
        {
            /* one int per log call -- fw_log takes exactly one.
               px can exceed 99, so it gets its own decade: the first packing
               let px*100 carry into the why field and corrupted the reason. */
            static const char rl[] = "%s%s: L why*100000+px*100+chars=%d\n";
            int n = 0;
            while (p->text[i][n]) n++;
            fw_log(rl, "", "inj", why * 100000 + px * 100 + n);
        }
    }
    p->end = off + pos;
}

void prepare_body(void)
{
    /* Services only what the display thread asked for; never creates state. */
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st) return;
    struct inj_state *S = ANCHOR->st;
    if (!S->need_prep) return;
    S->need_prep = 0;

    if (S->want >= 0) {
        if (!S->nxt_valid || S->nxt.start != S->want) {
            fill_page(&S->nxt, S->want);
            S->nxt_valid = 1;
        }
    } else if (!S->nxt_valid) {
        fill_page(&S->nxt, S->cur.end);     /* PRE-RENDER while the user reads */
        S->nxt_valid = 1;
    }
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
        "bl    prepare_body\n"
        "mov   r0, r5\n"
        "pop   {r4, r5, pc}\n");
}

/* ---- drawing and turn detection (display thread) -------------------- */

static void push_back(struct inj_state *S, int32_t off)
{
    if (S->sp < BACKSTACK) S->back[S->sp++] = off;
}

void after_render(void)
{
    struct inj_state *S = state();
    if (!S) return;
    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;

    uint32_t n = lv_obj_child_cnt(cont);
    if (n > INJ_LINES) n = INJ_LINES;

    int line = *(volatile int *)((uint32_t)rd + RD_OFF_LINE);
    S->calls++;

    if (S->last_line < 0) {
        S->last_line = line;
        S->want = 0;
        S->need_prep = 1;
    } else if (S->want < 0 && line != S->last_line) {
        if (line > S->last_line) {
            push_back(S, S->cur.start);
            S->want = S->cur.end;
        } else {
            S->want = S->sp ? S->back[--S->sp] : 0;
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

    /* Always write the labels: the vendor fills them on every render, so
       skipping would let its text show through. The content only changes when
       a whole page is swapped in, so repeated draws are identical. */
    for (uint32_t i = 0; i < n; i++) {
        void *c = lv_obj_get_child(cont, i);
        if (!c) continue;
        lv_label_set_text(c, (i < S->cur.nlines) ? S->cur.text[i] : "", 0);
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
