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
#include "hyphen.h"

#define PITCH     18        /* +1 base in the caller = 19px pitch */
/* We draw 12; the vendor is still TOLD 8 (VENDOR_LINES_PER_PAGE).
 *
 * These no longer have to agree. Page turns now come from the touch driver and
 * our page extents are our own, so the vendor's line counter is not a signal we
 * consume -- it only has to stay a value it can service, so that its decode
 * does not overflow its 8-record page context and its paginator terminates.
 *
 * The container holds 12 labels (our geometry patch makes it 228px at 19px
 * pitch). Writing only 8 left the bottom four showing the vendor's stale text:
 * the reported "8 lines, then the bottom 4 redraw".
 *
 * The container height must stay an exact multiple of the pitch. It really has
 * EIGHTEEN label children and we fill twelve; the rest still hold the vendor's
 * text. They are invisible only because label 12 starts exactly at the bottom
 * edge. Shrink the pitch without shrinking the container and label 12 moves
 * inside it -- a thirteenth line of stale vendor text appears. */
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
    /* Gesture capture ring. The vendor dispatches input through a gesture/view
       layer ABOVE LVGL (gesture_scroll_begin / "gesture %d, start (%d %d),
       view %d..."), which is why no LVGL object callback ever fired and why a
       press changed no word in the reading scene. Its handler at 0x100d92e8
       takes the gesture context as its FIRST ARGUMENT, so it must be hooked,
       not polled. Recorded here rather than logged: our fw_log output does not
       reach the UART, though the vendor's own logging does. */
    uint32_t gest_n;                  /* total gestures seen               */
    uint32_t gest[8];                 /* id<<24 | (x&0xfff)<<12 | (y&0xfff) */
    /* Touch driver capture. 0x100d92e8 recorded nothing -- that dispatcher
       serves the swipeable multi-view UI, not the reader. _lvgl_pointer_put
       (0x100e07b4) is the driver feeding the whole system, so every press must
       pass through it: r0+0x00 is the point, r0+0x08 the press state. */
    uint8_t  touch_down;              /* edge detect: a hold repeats samples */
    uint32_t last_turn;               /* S->calls at the last accepted turn  */
    uint8_t  want_snap;               /* jump landed mid-word: skip to a break */
    /* Scene identity captured at the last press, to find a gate that also holds
       when the menu is a popup INSIDE the reading scene: taps still turned
       pages under an open menu, so app_global+0x3c alone is not enough. */
    uint32_t last_rd;
    uint32_t last_cont;
    uint32_t last_scr;
    uint32_t draw_cont;               /* container our last render drew into */
    int32_t  last_rect;               /* diag: page rect at the last press    */
    int32_t  last_rect2;
    uint32_t last_top;                /* diag: sibling count at the press     */
    uint32_t last_node;               /* diag: our branch of the screen       */
    uint32_t kid_min;                 /* fewest siblings ever seen while drawing */
    /* Real glyph widths, in 1/16 px, for ASCII 32..126.
       The estimate they replace was systematically wide, so words that would
       have fitted were pushed to the next line and left a gap. */
    uint32_t font;                    /* captured from the font callback     */
    uint8_t  wtab_ok;
    uint16_t wtab[95];
    uint16_t wpunct[8];               /* curly quotes, dashes, ellipsis       */
    uint8_t  custom_font;             /* 0 unknown, 1 present, 2 absent       */
    fs_file_t probe_file;             /* existence check, kept OFF the stack  */
    uint32_t fo_calls;                /* font opens seen                      */
    uint32_t fo_subst;                /* ...redirected to the user's font     */
    uint32_t fo_fail;                 /* ...that failed and fell back         */
    int32_t  fo_last;                 /* last open's return code              */
    /* The user's own font, read by US and drawn by OUR callbacks. */
    uint32_t cf_buf;                  /* whole file, in the LVGL heap         */
    uint32_t cf_len;
    uint32_t cf_cmap, cf_loca, cf_glyf;   /* chunk payloads, within cf_buf    */
    uint32_t cf_glyf_len;
    uint32_t cf_nloca;
    uint8_t  cf_ready;                /* 0 untried, 1 loaded, 2 unusable      */
    uint8_t  cf_installed;            /* our callbacks are in the font struct */
    uint8_t  cf_advbits, cf_xybits, cf_whbits;
    int16_t  cf_line_height, cf_base_line;
    uint8_t  cf_bm[96];               /* one decoded glyph, for the bitmap cb */
    int32_t  saved_pos;               /* offset last written to the bookmark  */
    uint8_t  bmk_tried;               /* bookmark read attempted this book    */
    uint8_t  lang;                    /* HY_LANG_*, detected once per book     */
    int32_t  drawn_start;             /* page offset currently in the labels  */
    uint16_t drawn_lines;
    int32_t  drawn_pm;                /* percent currently in the top bar     */
    uint8_t  typing;                  /* the select-page keypad is up and used */
    int32_t  typed;                   /* percent being typed, 0..100           */
    uint32_t touch_n;                 /* every call, including idle polls   */
    uint32_t touch_nz;                /* calls carrying non-zero data       */
    uint32_t touch[16];               /* last 16 presses: x<<16 | y          */
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
        n->kid_min = 0xffffffffu;
    }
    ANCHOR->st = n;
    ANCHOR->magic = INJ_MAGIC;
    return n;
}

static const char inj_empty[] = "";

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

/* ---- our own bookmark ------------------------------------------------
 *
 * Position must survive a power cycle, and RAM does not: the state block lives
 * in the LVGL heap and a reset clears it (proven with a marker). The vendor's
 * .bmk cannot stand in either -- we deliberately keep its decode failing, so
 * its reading_line never advances and it saves 0, which is exactly the
 * "restarts from the beginning after a reset" bug.
 *
 * So we keep our own: a tiny file of (book signature, byte offset) records, so
 * several books each remember their place. Signature, not filename, because we
 * already hash the first 64 bytes to identify a book.
 */
/* Ask whichever backend is actually installed. With our font in the struct the
   vendor's callback would answer from ITS dsc at font+0x14 -- still pointing at
   the card font -- and hand back the wrong widths for the glyphs on screen. */
int cf_get_dsc(const void *font, void *dsc, uint32_t letter, uint32_t next);

static int glyph_dsc(struct inj_state *S, unsigned short *dsc,
                     uint32_t cp, uint32_t next)
{
    if (S->cf_installed) return cf_get_dsc((void *)S->font, dsc, cp, next);
    return fw_glyph_dsc((void *)S->font, dsc, cp, next);
}

static void cfont_size(struct inj_state *S);
static void cfont_read(struct inj_state *S);
static void cfont_install(struct inj_state *S);
static int path_ends_with(const char *p, const char *tail);

/* Which FILE is behind the vendor's font object?
 *
 * lvgl_bitmap_font_open stores the bitmap_font slot at dsc[0] (the font's dsc
 * lives at font+0x14), and each slot carries its own path at +8 -- that is the
 * string bitmap_font_open strcmps against when it looks a font up. Walking that
 * chain lets the display thread tell which font is loaded WITHOUT having been
 * present when it was opened. */
static const char *font_path(uint32_t font)
{
    if (!font) return 0;
    uint32_t dsc = *(volatile uint32_t *)(font + 0x14);
    if (dsc < 0x18000000u || dsc >= 0x18100000u) return 0;
    uint32_t slot = *(volatile uint32_t *)dsc;
    if (slot < 0x18000000u || slot >= 0x18100000u) return 0;
    return (const char *)(slot + 8);
}

#define BMK_PATH  "/SD1://bf07read.pos"
/* User-installed font, on the FAT volume the host sees over USB. See the
   font-open hook below for why this slot exists and which menu row reaches it. */
#define CUSTOM_FONT_PATH "/SD1://custom.font"
#define BMK_SLOTS 8

struct bmk_rec { uint32_t sig; int32_t pos; };

static void bmk_load(struct inj_state *S)
{
    fs_file_t f;
    fw_memset(&f, 0, sizeof f);
    if (fs_open(&f, BMK_PATH, FS_O_READ) < 0) return;
    struct bmk_rec recs[BMK_SLOTS];
    int rc = fs_read(&f, recs, sizeof recs);
    fs_close(&f);
    if (rc < (int)sizeof(struct bmk_rec)) return;
    int n = rc / (int)sizeof(struct bmk_rec);
    for (int i = 0; i < n; i++) {
        if (recs[i].sig == S->book_sig && recs[i].pos > 0) {
            S->jump_line = -1;             /* our offset wins over any line */
            S->want = recs[i].pos;
            S->want_snap = 0;
            S->saved_pos = recs[i].pos;
            S->need_prep = 1;
            static const char m[] = "%s%s: BMK resume=%d\n";
            fw_log(m, "", "inj", recs[i].pos);
            return;
        }
    }
}

static void bmk_save(struct inj_state *S)
{
    if (S->cur.start == S->saved_pos || S->cur.start < 0) return;
    struct bmk_rec recs[BMK_SLOTS];
    fw_memset(recs, 0, sizeof recs);
    fs_file_t f;
    fw_memset(&f, 0, sizeof f);
    if (fs_open(&f, BMK_PATH, FS_O_READ) >= 0) {    /* keep other books' slots */
        fs_read(&f, recs, sizeof recs);
        fs_close(&f);
    }
    int slot = -1, free_slot = -1;
    for (int i = 0; i < BMK_SLOTS; i++) {
        if (recs[i].sig == S->book_sig) { slot = i; break; }
        if (free_slot < 0 && recs[i].sig == 0) free_slot = i;
    }
    if (slot < 0) slot = (free_slot >= 0) ? free_slot : 0;   /* else evict [0] */
    recs[slot].sig = S->book_sig;
    recs[slot].pos = S->cur.start;
    fw_memset(&f, 0, sizeof f);
    if (fs_open(&f, BMK_PATH, FS_O_RDWR | FS_O_CREATE) < 0) return;
    if (fs_write(&f, recs, sizeof recs) == (int)sizeof recs)
        S->saved_pos = S->cur.start;
    fs_close(&f);
}

/* Proportional width estimate in 1/8 px. The firmware's own measurer
   (0x100eb4b8) was tried and rejected: its third argument is a mode selector,
   not an encoding, and the path our value takes returns the width argument
   instead of measuring. */
static int char_w8(unsigned char c)
{
    /* Measured width when we have it.
     *
     * The value at dsc+0 is in WHOLE PIXELS in this firmware's bitmap font
     * callback -- not the 8.4 fixed point upstream LVGL documents for adv_w.
     * Read back from the device: 'i' 4, 'e' 8, 'm' 12, 'W' 14, space 4, which
     * are sane pixel widths for a 16px font and absurd as sixteenths. Dividing
     * by two made every glyph ~8x too narrow and ran the text off the edge. */
    {
        struct inj_state *S = (ANCHOR->magic == INJ_MAGIC) ? ANCHOR->st : 0;
        if (S && S->wtab_ok && c >= 32 && c < 127) {
            unsigned w = S->wtab[c - 32];
            if (w && w < 64) return (int)w * 8;
        }
    }
    if (c == ' ') return 36;
    if (c < 0x20) return 0;
    /* UTF-8 continuation byte: it is the tail of a glyph already charged. */
    if ((c & 0xC0) == 0x80) return 0;
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

/* Width of one code point, in 1/8 px, for the non-ASCII we measured. */
static int cp_w8(unsigned cp)
{
    struct inj_state *S = (ANCHOR->magic == INJ_MAGIC) ? ANCHOR->st : 0;
    static const unsigned short cps[8] = {
        0x2018, 0x2019, 0x201C, 0x201D, 0x2013, 0x2014, 0x2026, 0x00A0 };
    if (S && S->wtab_ok)
        for (int i = 0; i < 8; i++)
            if (cps[i] == cp && S->wpunct[i]) return (int)S->wpunct[i] * 8;
    return 8 * 8;                      /* unmeasured: a plausible glyph */
}

/* Width of a UTF-8 run in 1/8 px. The hyphenation path needs this: summing
   char_w8 per BYTE charges an accented letter as a fallback-width lead byte
   plus a zero continuation, which is wrong for French. */
static int text_w8(const char *t, int n)
{
    int w = 0;
    for (int i = 0; i < n; ) {
        unsigned char c = (unsigned char)t[i];
        if (c >= 0xC0) {
            int seq = ((c & 0xF0) == 0xE0) ? 3 : 2;
            unsigned cp = (seq == 3) ? (c & 0x0Fu) : (c & 0x1Fu);
            for (int k = 1; k < seq && i + k < n; k++)
                cp = (cp << 6) | ((unsigned char)t[i + k] & 0x3Fu);
            w += cp_w8(cp);
            i += seq;
        } else {
            w += char_w8(c);
            i++;
        }
    }
    return w;
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

    /* The full label width, no cushion.
     *
     * The 4px margin was insurance against an ESTIMATED width. Now every glyph
     * is measured with the renderer's own font, and the labels were read off
     * the live tree at exactly 168px (x 4..171), so the cushion only threw away
     * words: "Dedication to" left 81px free while "commerce?" needed 84, and
     * missed solely because of it. */
    const int limit = LINE_PX * 8;
    int i = 0, k = 0, w8 = 0;
    int sp_src = -1, sp_out = -1;           /* last space, in both spaces */
    int hy_src = -1, hy_out = -1;           /* last hyphen we may break AFTER */

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

        if (c >= 0xC0) {
            /* Lead byte: decode the code point and charge it ONCE. */
            unsigned cp = 0;
            int len = ((c & 0xF0) == 0xE0) ? 3 : 2;
            cp = (len == 3) ? (c & 0x0Fu) : (c & 0x1Fu);
            for (int t = 1; t < len && i + t < avail; t++)
                cp = (cp << 6) | ((unsigned char)p[i + t] & 0x3Fu);
            w8 += cp_w8(cp);
        } else {
            w8 += char_w8(c);
        }
        if (w8 > limit) {
            /* A trailing space is invisible, so it must not evict a word that
               fitted. Measured: "...shouldered his" was 162px against a 164
               limit, the following space took it to 166, and the backtrack to
               the previous space dropped "his" to the next line -- the reported
               gap. End the line here and swallow the space instead. */
            if (c == ' ') {
                out[k] = 0;
                *px_out = (w8 - char_w8(' ')) / 8;
                *why_out = WHY_WIDTH;
                return i + 1;
            }
            *why_out = WHY_WIDTH;
            break;
        }

        if (c == ' ') { sp_src = i; sp_out = k; }
        /* A hyphen INSIDE a word is a legal break point, and long compounds
           ("seven-function", "force-feedback") otherwise strand 70-83px of
           empty line. Only between two letters/digits: that excludes a dash
           used as punctuation, a leading minus, and the "--" em-dash spelling,
           none of which should split. */
        if (c == '-' && k > 0 && i + 1 < avail) {
            unsigned char prev = (unsigned char)out[k - 1];
            unsigned char next = (unsigned char)p[i + 1];
            int prev_ok = (prev >= 'a' && prev <= 'z') || (prev >= 'A' && prev <= 'Z')
                       || (prev >= '0' && prev <= '9');
            int next_ok = (next >= 'a' && next <= 'z') || (next >= 'A' && next <= 'Z')
                       || (next >= '0' && next <= '9');
            if (prev_ok && next_ok) { hy_src = i; hy_out = k; }
        }
        out[k++] = (char)c;
        i++;

        /* A break is also legal AFTER an em or en dash, which prose uses
           without surrounding spaces: "Because-still smiling-they were going".
           Without this the whole run is one unbreakable token, so a line ends
           early and the next starts with a word that would have fitted -- the
           reported gap before "smiling". Recorded like an explicit hyphen: the
           dash stays on this line and the next resumes after it. */
        if (k >= 3 && (unsigned char)out[k - 3] == 0xE2
                   && (unsigned char)out[k - 2] == 0x80
                   && ((unsigned char)out[k - 1] == 0x94       /* em dash */
                    || (unsigned char)out[k - 1] == 0x93)) {   /* en dash */
            hy_src = i - 1;
            hy_out = k - 1;
        }
    }

    *px_out = w8 / 8;
    if (!*why_out) *why_out = (i >= avail) ? WHY_EOB : WHY_MAXW;

    if (i >= avail && *why_out == WHY_EOB) { out[k] = 0; return i; }

    /* Break at the LATEST legal point: a hyphen inside a word beats an earlier
       space, because keeping the first half on this line is what fills it. The
       hyphen stays on this line (out[hy_out + 1]) and the next line resumes at
       the character after it. */
    if (hy_out > sp_out && hy_out > 0) {
        out[hy_out + 1] = 0;
        return hy_src + 1;
    }
    /* Knuth-Liang: split the word that did not fit.
     *
     * Tried after an explicit hyphen (a word already containing "-" breaks
     * there) and before falling back to the last space, which is what leaves
     * the ragged edge this is meant to fix. The whole word must be measured
     * from the SOURCE, because `out` holds only the part that fitted. */
    if (sp_out >= 0 && *why_out == WHY_WIDTH) {
        int ws_out = sp_out + 1;
        int ws_src = sp_src + 1;
        int we = ws_src;
        while (we < avail) {
            unsigned char c2 = (unsigned char)p[we];
            if (c2 == ' ' || c2 == '\n' || c2 == '\r' || c2 == '\t') break;
            we++;
        }
        unsigned char pts[8];
        struct inj_state *St = (ANCHOR->magic == INJ_MAGIC) ? ANCHOR->st : 0;
        int npts = hyphenate(p + ws_src, we - ws_src,
                             St ? St->lang : HY_LANG_EN, pts, (int)sizeof pts);
        if (npts > 0) {
            int base = text_w8(out, ws_out);
            int hyw = char_w8('-');
            int best = -1, bestw = 0;
            for (int t = 0; t < npts; t++) {          /* the longest that fits */
                int plen = pts[t];
                int sum = base + text_w8(p + ws_src, plen);
                if (sum + hyw <= limit && ws_out + plen + 1 < MAXW - 1) {
                    best = plen;
                    bestw = sum + hyw;
                }
            }
            if (best > 0) {
                for (int u = 0; u < best; u++)
                    out[ws_out + u] = p[ws_src + u];
                out[ws_out + best] = '-';
                out[ws_out + best + 1] = 0;
                *px_out = bestw / 8;
                *why_out = WHY_WIDTH;
                return ws_src + best;
            }
        }
    }

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

    /* Paginator off. A percent seek needs no page count: our pages are byte
       extents, so any offset is a legal destination. It only ever mattered
       because the vendor's select-page dialog is built from its count -- and we
       are about to stop needing that dialog's logic, only its keypad surface. */
    *FW_REPAGINATE = 0;
    /* Try the open a few times, not once per render pass forever. With the
       wrong list address this retried 16 fs_open calls on every tick -- 1117
       sweeps by the time it was measured -- which is what made page turns take
       two seconds. Wait for the vendor to publish the size before counting an
       attempt, so a slow open does not burn the budget. */
    if (!S->file_ready && *FW_BOOK_SIZE && S->open_try < 8) {
        S->open_try++;
        book_open_own(S);
    }

    /* The user's font, sized and read HERE because this is the thread where
       file I/O is legal. Steps 1 and 3; the display thread allocates between
       them. Doing this from the display hook raced the book open and cost the
       bookmark -- resetting stopped resuming the book. */
    if (S->custom_font == 0) cfont_size(S);
    else if (S->custom_font == 1 && S->cf_buf && S->cf_ready == 0) cfont_read(S);

    /* One bookmark read per book, then persist every settled page. Both run on
       the ebook thread, where file I/O is legal -- the display thread must not
       touch the filesystem. */
    if (!S->bmk_tried && S->book_sig && S->file_ready) {
        S->bmk_tried = 1;
        bmk_load(S);
    }
    bmk_save(S);

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
                {   /* Pick the hyphenation language from the opening text.
                     *
                     * Read into the SCRATCH PAGE, not the stack. A 512-byte
                     * local here overflowed the ebook thread's 2280-byte stack
                     * and reset the device on every book open -- the same
                     * mistake a 768-byte buffer caused earlier in this project.
                     * S->nxt is rebuilt below anyway. */
                    char *probe = (char *)S->nxt.text;
                    int pr = book_read(S, 0, probe, sizeof S->nxt.text);
                    S->lang = (pr > 64) ? (uint8_t)hy_detect(probe, pr) : HY_LANG_EN;
                    S->nxt_valid = 0;
                    static const char lg[] = "%s%s: LANG=%d\n";
                    fw_log(lg, "", "inj", S->lang);
                }
                S->size = 0;              /* re-probe: different file */
                S->sp = 0;
                S->nxt_valid = 0;
                S->last_line = vline;
                S->jump_line = vline;
                S->saved_pos = -1;
                S->bmk_tried = 0;
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

    /* A percentage jump lands on an arbitrary byte, usually mid-word. Nudge it
       forward to just past the next space or newline so the page starts on a
       word. Done here, on the ebook thread: the touch handler that requests the
       jump runs on the input thread and must not touch the filesystem. */
    if (S->want >= 0 && S->want_snap) {
        S->want_snap = 0;
        char b[64];
        int rc = book_read(S, S->want, b, sizeof b);
        if (rc > 0) {
            int k = 0;
            while (k < rc && b[k] != '\n' && b[k] != ' ') k++;
            if (k < rc) S->want += k + 1;
        }
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

    /* Tell the vendor the TRUTH about its own page size.
     *
     * This used to write INJ_LINES (12), and that one byte was the stall.
     * Its page context holds 8 line records, so a 12-line page overflows the
     * decode -- and the decode failure aborts the page-turn path at the cbnz
     * before 0x100493a8, which is the same path our render call sits on. So a
     * press did nothing at all: measured, zero words changed anywhere in the
     * reading scene, while exit still worked.
     *
     * It also made ebook_calculate_pages never terminate: it loops on exact
     * equality against a divisor of 8, so the background paginator ground on
     * forever (11,449 bytes per 20 s, and still going after 231 KB).
     *
     * We keep our own wrapping and pagination; the vendor's line count is used
     * only as the page-turn SIGNAL, so it must stay something it can service. */
    if (t == 8 && c == 1) {
        uint32_t pl = *(volatile uint32_t *)((uint32_t)msg + 4);
        /* Tell it 12 -- MORE than its page context holds -- on purpose.
         *
         * Its decode then fails, and the `cbnz r0` at 0x100493a4 skips the
         * render call at 0x100493a8, so it never draws. That is what we want:
         * it was refilling all 12 labels with ITS page on every render (their
         * text pointers were measured pointing at its buffers, 0x180190cc and
         * up), which is why a turned page was overwritten ~0.3s later by the
         * page the book opened on -- its line never moves, because we stopped
         * consuming it as a signal.
         *
         * This was catastrophic when our render hook rode on that same skipped
         * call, and it made the paginator loop forever. Neither applies now: we
         * hook the timer tail, and the paginator is disabled. */
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
    /* While the keypad is in use, the top line shows the TARGET being typed --
       that is the number the user is setting, not where they currently are. */
    int pm = S->typing ? (int)(S->typed * 10)
                       : (int)((S->cur.start / 64) * 1000 / (size / 64 + 1));
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
            /* Only when it actually changed: this is the copying setter, so it
           reallocates and invalidates on every call. */
        if (pm != S->drawn_pm) {
            S->drawn_pm = pm;
            lv_label_set_text_copy(k, buf);
        }
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

    /* We publish NOTHING into the vendor's counters.
     *
     * A previous build wrote total_lines / total_pages / reading_line every
     * render pass, to give the "select page" dialog a range after we disabled
     * the paginator. With the paginator running again those writes corrupt the
     * count it is building: total_lines was measured oscillating
     * 8664 -> 8672 -> 8736 -> 8808 while it scanned, and its page count
     * collapsed to 1 -- which is exactly why the keypad digits had nothing to
     * select. Its bookkeeping is its own.
     *
     * Our progress display and page extents are byte offsets and owe nothing to
     * these fields. */

    /* The keypad closed with a number typed: seek there. Committing on close
       rather than on a particular key means we do not have to know which key
       is "enter" -- and it works however the dialog is dismissed. */
    if (S->typing) {
        uint32_t par = *(volatile uint32_t *)((uint32_t)cont + 4);
        uint32_t sp4 = (par >= HEAP_LO && par < HEAP_HI)
                     ? *(volatile uint32_t *)(par + 8) : 0;
        uint32_t kn2 = (sp4 >= HEAP_LO && sp4 < HEAP_HI)
                     ? *(volatile uint32_t *)(sp4 + 4) : 0;
        if (kn2 && S->kid_min != 0xffffffffu && kn2 <= S->kid_min) {
            int32_t size = (int32_t)*FW_BOOK_SIZE;
            int32_t pct = S->typed;
            S->typing = 0;
            S->typed = 0;
            if (size > 0 && pct >= 0 && pct <= 100) {
                S->sp = 0;                        /* same as the enter key */
                S->want = (size / 100) * pct;     /* no overflow, ~1% grain */
                if (S->want >= size) S->want = size - 1;
                S->want_snap = 1;
                S->need_prep = 1;
            }
        }
    }

    /* Lift the text 2px inside its line.
     *
     * The font ships with base_line = -2, and LVGL draws each glyph at
     *     y + (line_height - base_line) - box_h - ofs_y
     * so a negative base line pushes every glyph DOWN: descenders were clipped
     * at the bottom of the 20px label while 2px sat unused above. Measured on
     * device: line_height 17, base_line -2, label height 20. Setting it to 0
     * moved the text up 2px and fixed the clipping (confirmed by eye).
     *
     * Re-applied every pass rather than once: the font is shared and reloaded
     * when the user changes it from the menu, which would restore the -2. */
    /* Only a NEGATIVE base line, which is the case this was written for: the
       vendor's own font reported -2 and that pushed glyphs down into the
       bottom of the label. Forcing 0 unconditionally harmed any font whose
       base line is legitimately positive -- the fang18 slot has line_height 24,
       and zeroing its base line clipped the descenders of the fallback font
       when no custom.font is installed. */
    if (S->font && !S->cf_installed) {
        volatile short *base_line = (volatile short *)(S->font + 10);
        if (*base_line < 0) *base_line = 0;
    }

    /* Does the user have a font on the volume the host can see? Answered ONCE,
     * here, and only while no book file is open.
     *
     * Here rather than in the font-open hook because that hook runs on the
     * ebook thread deep inside the vendor's loader, where there is not enough
     * stack for fs_open. And only with no book open because the FS layer takes
     * no lock anywhere: opening from this thread while the ebook thread is
     * mid-read is the same reentrancy that produced the original stall. At
     * boot the display thread is already ticking and no book is open yet, so
     * the answer is settled long before the ebook app can ask for a font. */
    /* Step 2 of the font load: the buffer. NO file I/O on this thread -- see
       cfont_size. The ebook thread sized it and will fill it. */
    if (S->custom_font == 1 && S->cf_len && !S->cf_buf) {
        void *p = lv_mem_alloc(S->cf_len);
        if (p) S->cf_buf = (uint32_t)p;
        else S->custom_font = 2;       /* no heap for it: stay on the vendor's */
    }

    /* Install our font whenever the slot it took over is the one loaded --
     * not only when we happen to see the open.
     *
     * On a cold boot the ebook opens its font BEFORE this state exists, so
     * fontopen_body passes through with S null and nothing installs: measured
     * fo_calls 0, cf_installed 0, and the font object still holding the
     * vendor's callbacks with line_height 24. The base_line forcing below then
     * pushed every glyph 4px down inside a 20px label and cut the descenders
     * off. Switching fonts and back was the only thing that fixed it, because
     * that finally ran the open through our hook. */
    if (S->cf_ready == 1 && !S->cf_installed && S->font) {
        const char *p = font_path(S->font);
        if (p && path_ends_with(p, "fang18.font")) cfont_install(S);
    }

    /* Re-measure when the user picks a different font from the menu.
     *
     * The font STRUCT is reused across a change -- the same reload that puts
     * base_line back to -2 above -- so S->font stays valid and the pointer
     * never changes, but every width behind it does. Nothing else announces
     * the switch, so probe a few glyphs each pass and compare against the
     * table we built. Four lookups against a full page render is nothing.
     *
     * Without this the table keeps the widths of whichever font was loaded at
     * boot. Measured: after switching from Literata (wide) back to the
     * vendor's Song face (narrow), every line broke 2-3 characters early
     * because wrapping charged Literata's advances for Song's glyphs. */
    if (S->font && S->wtab_ok) {
        static const char probe[4] = { 'm', 'W', 'i', 'l' };
        unsigned short dsc[6];
        unsigned now = 0, had = 0;
        int usable = 1;
        for (int j = 0; j < 4; j++) {
            dsc[0] = 0;
            if (!glyph_dsc(S,dsc, (uint32_t)probe[j], 0) || !dsc[0]) {
                usable = 0;
                break;
            }
            now += dsc[0];
            had += S->wtab[probe[j] - 32];
        }
        if (usable && now != had) {
            S->wtab_ok = 0;               /* re-measured just below */
            S->want = S->cur.start;       /* re-wrap the page we are on */
            S->nxt_valid = 0;
            S->need_prep = 1;
            /* Force the repaint too: a re-wrap keeps the same start offset and
               often the same line count, which is exactly what the redraw test
               below treats as "nothing changed". */
            S->drawn_start = -1;
        }
    }

    /* Measure the alphabet once, with the real font. adv_w lands at dsc+0 in
       1/16 px (verified in the callback's stores). Done on the display thread,
       which is where the vendor calls this from; the ebook thread only reads
       the finished table. */
    if (S->font && !S->wtab_ok) {
        unsigned short dsc[6];
        int ok = 0;
        for (int c = 32; c < 127; c++) {
            dsc[0] = 0;
            if (glyph_dsc(S,dsc, (uint32_t)c, 0) && dsc[0]) {
                S->wtab[c - 32] = dsc[0];
                ok++;
            } else {
                S->wtab[c - 32] = 0;
            }
        }
        /* The non-ASCII that real books are full of: typographic quotes,
           dashes, ellipsis, nbsp. Charged per BYTE at the ASCII fallback they
           cost ~33px for a glyph drawn in ~5, which pushed words off lines that
           had room for them. */
        static const unsigned short cps[8] = {
            0x2018, 0x2019, 0x201C, 0x201D, 0x2013, 0x2014, 0x2026, 0x00A0 };
        for (int j = 0; j < 8; j++) {
            dsc[0] = 0;
            S->wpunct[j] = (glyph_dsc(S,dsc, cps[j], 0) && dsc[0])
                         ? dsc[0] : 0;
        }
        if (ok > 64) S->wtab_ok = 1;      /* enough of the alphabet to trust */
    }

    /* Remember what we drew into, and how many siblings the page had.
     *
     * A popup is added as an extra child of the page's PARENT (the reading
     * scene's container): measured 6 siblings while reading, 7 with the
     * "select page" keypad up. The minimum ever seen is the popup-free
     * baseline -- taking the minimum rather than the latest keeps a popup that
     * is open while we draw from raising the bar and disarming the test. */
    S->draw_cont = (uint32_t)cont;
    {
        uint32_t par = *(volatile uint32_t *)((uint32_t)cont + 4);
        if (par >= HEAP_LO && par < HEAP_HI) {
            uint32_t sp2 = *(volatile uint32_t *)(par + 8);
            if (sp2 >= HEAP_LO && sp2 < HEAP_HI) {
                uint32_t k = *(volatile uint32_t *)(sp2 + 4);
                if (k && k < 64 && k < S->kid_min) S->kid_min = k;
            }
        }
    }

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
    } else if (line != S->last_line) {
        /* TRACK the vendor's line, never ACT on it.
         *
         * This used to detect page turns from its reading_line, because that
         * was the only signal we could see. It is now a second, competing turn
         * source: telling the vendor the truth about its page size revived its
         * own turn path, so one tap advanced the page twice -- ours plus its.
         * Input comes from the touch driver now, so this is only kept so a
         * resume still knows where the vendor thinks we are. */
        S->last_line = line;
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

    /* Write the labels only when the PAGE changed.
     *
     * This ran on every tick, so all 12 labels were marked dirty ~3 times a
     * second and the display never stopped redrawing -- measured at 3.3
     * passes/s, i.e. 304ms per tick, which is also the floor on how fast a tap
     * can show. The content only changes when a page is swapped in.
     *
     * cur.text is a stable buffer written in place, so the pointer does not
     * change when the page does: the offset is what identifies what is drawn. */
    int need = (S->cur.start != S->drawn_start || S->cur.nlines != S->drawn_lines);
    if (!need) {
        /* Cheap check that the labels are still SHOWING OURS. lv_label_set_text
           is the static setter: it stores our pointer at label+0x24. If anything
           else has written them, that pointer is not ours and we repaint.
           Twelve word reads per tick, against a full repaint per tick. */
        for (uint32_t i = 0; i < n; i++) {
            void *c = lv_obj_get_child(cont, i);
            if (!c) continue;
            const char *mine = (i < S->cur.nlines) ? S->cur.text[i] : inj_empty;
            if (*(volatile uint32_t *)((uint32_t)c + 0x24) != (uint32_t)mine) {
                need = 1;
                break;
            }
        }
    }
    if (need) {
        S->drawn_start = S->cur.start;
        S->drawn_lines = S->cur.nlines;
        for (uint32_t i = 0; i < n; i++) {
            void *c = lv_obj_get_child(cont, i);
            if (!c) continue;
            lv_label_set_text(c, (i < S->cur.nlines) ? S->cur.text[i] : inj_empty, 0);
        }
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
/* The glyph callback's first argument is the font. We cannot look a font up --
   it is reached through style lookups we have no symbols for -- but the vendor
   calls this for every glyph it draws, so one capture is enough. */
void font_body(void *f)
{
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st) return;
    struct inj_state *S = ANCHOR->st;
    if (!S->font && f) S->font = (uint32_t)f;
}

/* ---- user font, from the volume the host can actually see ----------------
 *
 * The vendor's font menu stores an index 0..5 and a small if-chain at
 * 0x10047af6 maps it to a path inside the sdfs container (/SD1:C/...). That
 * container lives in the hidden region BEFORE the FAT partition, and the USB
 * mass-storage LUN starts AT the partition -- measured: 55609941 sectors
 * exposed, partition at LBA 5457067, and those sum to exactly the card size.
 * So nothing a user drops on the drive they can see is ever reachable by one
 * of those paths, and installing a font meant opening the case.
 *
 * The menu's own row table (0x10128e50, 28-byte rows, group id 0xcefe2df9 on
 * all five font rows, with a spare all-zero row right after them) would take a
 * sixth entry, but each row names its label by an id into a localised string
 * resource that is not plaintext in the firmware -- a new id would draw blank.
 * So instead take over the one slot that is not wanted, fang18, the row shown
 * as "Imitation Song large".
 *
 * Hooked at the loader rather than at the two call sites that pass this path,
 * so any caller is covered. Falls back to the vendor path when the file is
 * missing: selecting the row without having installed a font must still
 * render, since a failed open returns -1 and the ebook reports "layout
 * failed" with nothing on screen. */

/* Bounded and range-checked on purpose. This hook sits at the head of a
   function the WHOLE system calls, and it runs BEFORE the vendor's own null
   check on the path, so a caller that leaves a stale r1 would have us walking
   a pointer the vendor never dereferences. Scanning to a NUL that is not there
   faults, and a fault here is a silent reset. */
static int path_ends_with(const char *p, const char *tail)
{
    uint32_t a = (uint32_t)p;
    int xip = (a >= 0x10000000u && a < 0x10200000u);
    int ram = (a >= 0x18000000u && a < 0x18100000u);
    if (!xip && !ram) return 0;

    int lp = 0, lt = 0;
    while (lp < 128 && p[lp]) lp++;
    if (lp >= 128) return 0;
    while (tail[lt]) lt++;
    if (lp < lt) return 0;
    for (int i = 0; i < lt; i++)
        if (p[lp - lt + i] != tail[i]) return 0;
    return 1;
}

/* ---- our own LVGL font backend ------------------------------------------
 *
 * The vendor's loader is sdfs-only, so a font on the USB-visible volume can
 * only be drawn if WE read it and WE answer LVGL's two glyph callbacks. Both
 * structures below were read off the firmware rather than assumed:
 *
 *   lv_font_t          +0x00 get_glyph_dsc   +0x04 get_glyph_bitmap
 *                      +0x08 line_height     +0x0a base_line   (int16)
 *                      +0x14 dsc             (we never use it -- our state is
 *                                             reached through the anchor)
 *   lv_font_glyph_dsc_t +0 adv_w  +2 box_w  +4 box_h  +6 ofs_x  +8 ofs_y
 *                      +10 bpp,  callback returns 1 on success
 *
 * from the stores in the vendor's own callback at 0x100e1344. adv_w is in
 * WHOLE PIXELS in this firmware, which is also what mkfont.py writes, so the
 * file's advances pass straight through. */
static uint32_t cf_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint16_t cf_u16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

static uint32_t cf_bits(const uint8_t *b, uint32_t *pos, int n)
{
    uint32_t v = 0;
    for (int i = 0; i < n; i++) {
        v = (v << 1) | ((b[*pos >> 3] >> (7 - (*pos & 7))) & 1);
        (*pos)++;
    }
    return v;
}
static int cf_sbits(const uint8_t *b, uint32_t *pos, int n)
{
    uint32_t v = cf_bits(b, pos, n);
    return (v & (1u << (n - 1))) ? (int)v - (1 << n) : (int)v;
}

struct cf_glyph { int adv, bx, by, w, h; uint32_t bitpos; };

static int cf_gid(struct inj_state *S, uint32_t cp)
{
    const uint8_t *cm = (const uint8_t *)(S->cf_buf + S->cf_cmap);
    uint32_t n = cf_u32(cm);
    if (n > 64) return 0;
    const uint8_t *sub = cm + 4;
    for (uint32_t i = 0; i < n; i++, sub += 16) {
        uint32_t start = cf_u32(sub + 4);
        uint32_t len   = cf_u16(sub + 8);
        uint32_t gid0  = cf_u16(sub + 10);
        if (cp >= start && cp < start + len) return (int)(gid0 + (cp - start));
    }
    return 0;
}

static int cf_glyph_at(struct inj_state *S, int gid, struct cf_glyph *g)
{
    if (gid <= 0 || (uint32_t)gid >= S->cf_nloca) return 0;
    const uint8_t *loca = (const uint8_t *)(S->cf_buf + S->cf_loca);
    uint32_t off = cf_u32(loca + 4 + 4 * (uint32_t)gid);
    if (off < 8) return 0;
    uint32_t payload = off - 8;               /* loca counts the chunk header */
    if (payload >= S->cf_glyf_len) return 0;

    const uint8_t *gl = (const uint8_t *)(S->cf_buf + S->cf_glyf);
    uint32_t bit = payload * 8;
    g->adv = (int)cf_bits(gl, &bit, S->cf_advbits);
    g->bx  = cf_sbits(gl, &bit, S->cf_xybits);
    g->by  = cf_sbits(gl, &bit, S->cf_xybits);
    g->w   = (int)cf_bits(gl, &bit, S->cf_whbits);
    g->h   = (int)cf_bits(gl, &bit, S->cf_whbits);
    g->bitpos = bit;
    if (g->w < 0 || g->h < 0 || g->w > 64 || g->h > 64) return 0;
    if ((bit + (uint32_t)(g->w * g->h) + 7) / 8 > S->cf_glyf_len) return 0;
    return 1;
}

int cf_get_dsc(const void *font, void *dsc, uint32_t letter, uint32_t next)
{
    (void)font; (void)next;
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st) return 0;
    struct inj_state *S = ANCHOR->st;
    if (S->cf_ready != 1 || !dsc) return 0;

    struct cf_glyph g;
    if (!cf_glyph_at(S, cf_gid(S, letter), &g)) return 0;

    uint8_t *d = (uint8_t *)dsc;
    *(uint16_t *)(d + 0) = (uint16_t)g.adv;      /* whole pixels here */
    *(uint16_t *)(d + 2) = (uint16_t)g.w;
    *(uint16_t *)(d + 4) = (uint16_t)g.h;
    *(int16_t  *)(d + 6) = (int16_t)g.bx;
    *(int16_t  *)(d + 8) = (int16_t)g.by;
    d[10] = 1;                                   /* bpp */
    return 1;
}

const uint8_t *cf_get_bitmap(const void *font, uint32_t letter)
{
    (void)font;
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st) return 0;
    struct inj_state *S = ANCHOR->st;
    if (S->cf_ready != 1) return 0;

    struct cf_glyph g;
    if (!cf_glyph_at(S, cf_gid(S, letter), &g)) return 0;

    /* The file's bitmap is already a continuous 1bpp stream, but it starts
       mid-byte (the glyph header is 26 bits), so it has to be shifted into a
       buffer LVGL can read from directly. */
    uint32_t n = (uint32_t)(g.w * g.h);
    if (n > sizeof S->cf_bm * 8) return 0;
    fw_memset(S->cf_bm, 0, sizeof S->cf_bm);
    const uint8_t *gl = (const uint8_t *)(S->cf_buf + S->cf_glyf);
    for (uint32_t i = 0; i < n; i++) {
        uint32_t p = g.bitpos + i;
        if ((gl[p >> 3] >> (7 - (p & 7))) & 1) S->cf_bm[i >> 3] |= 0x80u >> (i & 7);
    }
    return S->cf_bm;
}

/* Loading is split across two threads on purpose.
 *
 * File I/O belongs to the EBOOK thread -- the FS layer takes no lock anywhere,
 * so touching it from the display thread races the book open. Doing exactly
 * that is what stopped books resuming: our font read ran underneath
 * book_open_own and the bookmark never survived a reset.
 *
 * Allocation stays on the DISPLAY thread, which is where every other
 * allocation in this reader happens.
 *
 *   1. cfont_size()  ebook thread   -- how big is it? (also validates it)
 *   2. display hook  display thread -- lv_mem_alloc(cf_len)
 *   3. cfont_read()  ebook thread   -- fill the buffer, parse it
 */
static void cfont_size(struct inj_state *S)
{
    S->custom_font = 2;                          /* absent unless proven */
    fw_memset(&S->probe_file, 0, sizeof S->probe_file);
    if (fs_open(&S->probe_file, CUSTOM_FONT_PATH, FS_O_READ) < 0) return;

    uint8_t hdr[8];
    uint32_t total = 0;
    int have_glyf = 0;
    for (int i = 0; i < 8 && !have_glyf; i++) {
        if (fs_seek(&S->probe_file, (int32_t)total, FS_SEEK_SET) < 0) break;
        if (fs_read(&S->probe_file, hdr, 8) != 8) break;
        uint32_t len = cf_u32(hdr);
        if (len < 8 || len > 0x20000) break;
        total += len;
        if (hdr[4] == 'g' && hdr[5] == 'l' && hdr[6] == 'y' && hdr[7] == 'f')
            have_glyf = 1;
    }
    fs_close(&S->probe_file);
    if (!have_glyf || total < 32 || total > 0x8000) return;
    S->cf_len = total;
    S->custom_font = 1;
}

static void cfont_read(struct inj_state *S)
{
    S->cf_ready = 2;
    uint8_t *buf = (uint8_t *)S->cf_buf;
    fw_memset(&S->probe_file, 0, sizeof S->probe_file);
    if (fs_open(&S->probe_file, CUSTOM_FONT_PATH, FS_O_READ) < 0) return;
    int rc = fs_read(&S->probe_file, buf, S->cf_len);
    fs_close(&S->probe_file);
    if ((uint32_t)rc != S->cf_len) return;

    uint32_t total = S->cf_len;
    uint32_t off = 0, head = 0;
    while (off + 8 <= total) {
        uint32_t len = cf_u32(buf + off);
        if (len < 8 || off + len > total) return;
        const uint8_t *t = buf + off + 4;
        if      (t[0]=='h'&&t[1]=='e'&&t[2]=='a'&&t[3]=='d') head = off + 8;
        else if (t[0]=='c'&&t[1]=='m'&&t[2]=='a'&&t[3]=='p') S->cf_cmap = off + 8;
        else if (t[0]=='l'&&t[1]=='o'&&t[2]=='c'&&t[3]=='a') S->cf_loca = off + 8;
        else if (t[0]=='g'&&t[1]=='l'&&t[2]=='y'&&t[3]=='f') {
            S->cf_glyf = off + 8;
            S->cf_glyf_len = len - 8;
        }
        off += len;
    }
    if (!head || !S->cf_cmap || !S->cf_loca || !S->cf_glyf) return;

    const uint8_t *h = buf + head;
    int ascent  = (int)cf_u16(h + 8);
    int descent = (int)(int16_t)cf_u16(h + 10);      /* negative */
    S->cf_advbits = h[32];
    S->cf_xybits  = h[30];
    S->cf_whbits  = h[31];
    if (h[29] != 1) return;                          /* bpp: 1 only */
    if (!S->cf_advbits || !S->cf_xybits || !S->cf_whbits) return;
    S->cf_line_height = (int16_t)(ascent - descent);
    S->cf_base_line   = (int16_t)(-descent);
    S->cf_nloca = cf_u32(buf + S->cf_loca);
    if (S->cf_nloca < 2 || S->cf_nloca > 4096) return;

    S->cf_ready = 1;
}

static void cfont_install(struct inj_state *S)
{
    if (!S->font || S->cf_ready != 1) return;
    volatile uint32_t *f = (volatile uint32_t *)S->font;
    f[0] = (uint32_t)cf_get_dsc | 1u;
    f[1] = (uint32_t)cf_get_bitmap | 1u;
    *(volatile int16_t *)(S->font + 8)  = S->cf_line_height;
    *(volatile int16_t *)(S->font + 10) = S->cf_base_line;
    S->cf_installed = 1;
    S->wtab_ok = 0;                    /* widths belong to the old font */
    S->want = S->cur.start;
    S->nxt_valid = 0;
    S->need_prep = 1;
    S->drawn_start = -1;
}

/* Re-enters the vendor's function past the four bytes we replaced, replaying
   them first. Its epilogue is `pop {r4,r5,r6,pc}`, so reaching it with `bl`
   makes it return HERE and hands us the result. */
__attribute__((naked)) int fontopen_real(void *font, const char *path)
{
    __asm__ volatile(
        "push  {r4, r5, r6, lr}\n"
        "cmp   r0, #0\n"
        "movw  r12, #0x1445\n"
        "movt  r12, #0x100e\n"
        "bx    r12\n");
}

/* Reads a flag the display thread already worked out -- deliberately no file
   I/O here. The first version called fs_open from this hook and the player
   reset when the ebook launched: this runs deep inside the vendor's font open,
   on the ebook thread, whose stack was measured at 2280 bytes with ~328 to
   spare, and fs_open's own frame on top of that is the project's oldest
   failure mode showing up a third time.
 *
 * Wrapped rather than merely redirected because a redirected open MUST NOT be
 * allowed to fail. The menu handler at 0x1004ae28 does:
 *
 *     close(font); rc = open(font, path); if (rc) close(font);
 *
 * -- so a failure closes a font that was already closed, and that double free
 * is very likely what reset the player when switching back to this slot. If
 * our path does not open, close and serve the vendor's own font instead: the
 * user sees the stock face rather than a reboot. The counters make the next
 * one diagnosable over serial instead of by guessing. */
int fontopen_body(void *font, const char *path)
{
    struct inj_state *S = (ANCHOR->magic == INJ_MAGIC) ? ANCHOR->st : 0;

    /* NO substitution. Measured on the UART, the whole reason this hook was
     * built does not work:
     *
     *     sdfs:stor_id=1, p=255
     *     sd_fopen no this file /SD1://custom.font
     *     <E> bitmap_font_open: open font file /SD1://custom.font failed!
     *
     * The loader opens through sd_fopen -- the sdfs resource filesystem -- not
     * through Zephyr FS. Our own fs_open on that identical path succeeds, which
     * is why the probe says the file is there; the two calls simply address
     * different filesystems. Nothing in the sdfs namespace can name a file on
     * the FAT partition, so no spelling of the path can ever work here.
     *
     * And a failed open must not be "recovered" from: lvgl_bitmap_font_open
     * frees its own descriptor before returning -1 (0x100e14c0), so the close
     * this used to issue was a second free -- "0x18006b40 freed already",
     * rom_buddy_free with a nil info, and the reboot that looked like the
     * vendor's bug was ours.
     *
     * Getting a user font off the USB-visible volume therefore needs our own
     * glyph callbacks rather than the vendor's loader. The wrapper stays as the
     * place to install that, and the counters stay to keep it measurable. */
    if (S) S->fo_calls++;
    int rc = fontopen_real(font, path);
    if (S) S->fo_last = rc;

    /* The vendor's open has just written ITS callbacks and metrics into the
       font struct. If this is the slot we took over, put ours back on top --
       after the open, never instead of it, so the struct is fully initialised
       and the vendor's own font stays loaded as the fallback. */
    if (S && rc == 0 && path_ends_with(path, "fang18.font")) {
        if (S->cf_ready == 1) {
            S->font = (uint32_t)font;
            cfont_install(S);
            S->fo_subst++;
        }
    } else if (S && rc == 0 && (uint32_t)font == S->font) {
        /* Only when the open targeted the struct we installed into. A font
           switch triggers five opens (measured: fo_calls 0 -> 5), most of them
           for other UI fonts, and clearing the flag on those would leave our
           callbacks live while we asked the vendor's for widths. */
        S->cf_installed = 0;
    }
    return rc;
}

/* ---- the menu label follows the file --------------------------------------
 *
 * The row's label id is a FLASH patch, so on its own it says "Custom" whether
 * or not a custom.font exists -- and with no file the row loads the vendor's
 * fang18, which is not custom anything. The id is only static until it is
 * copied: app_menulist_load_res_id builds a 16-byte item per row and puts the
 * id at item+0x0c. Correcting it there costs nothing and needs no flash.
 *
 * ID_CUSTOM is the string we rewrote in NOR; ID_ORIGINAL is the entry the row
 * pointed at before, still present and still correct for the vendor's font. */
#define ID_CUSTOM   0xf40f37eau        /* "Custom" (was Fangsong Small Font) */
#define ID_ORIGINAL 0xf40f37edu        /* "Imitation Song large font"        */

__attribute__((naked)) uint32_t menulist_real(void *ctx, void *rows, uint32_t max)
{
    __asm__ volatile(
        "stmdb sp!, {r4, r5, r6, r7, r8, lr}\n"
        "movw  r12, #0x9351\n"
        "movt  r12, #0x1005\n"
        "bx    r12\n");
}

uint32_t menulist_body(void *ctx, void *rows, uint32_t max)
{
    uint32_t items = menulist_real(ctx, rows, max);
    if (!items || !ctx) return items;
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st) return items;
    struct inj_state *S = ANCHOR->st;
    if (S->custom_font == 1) return items;      /* the file is there */

    unsigned n = *(volatile uint8_t *)((uint32_t)ctx + 3);   /* item count */
    if (n > 32) return items;
    for (unsigned i = 0; i < n; i++) {
        volatile uint32_t *id = (volatile uint32_t *)(items + i * 16 + 0x0c);
        if (*id == ID_CUSTOM) *id = ID_ORIGINAL;
    }
    return items;
}

/* Replaces the `stmdb sp!, {r4-r8, lr}` at app_menulist_load_res_id. */
__attribute__((naked)) void menulist_hook(void)
{
    __asm__ volatile(
        "push  {r4, lr}\n"
        "bl    menulist_body\n"
        "pop   {r4, pc}\n");
}

/* Takes over lvgl_bitmap_font_open entirely: r0 and r1 are already the font
   and the path, so the body is called with them untouched and its result is
   the function's result. fontopen_real above is what re-enters the original. */
__attribute__((naked)) void fontopen_hook(void)
{
    __asm__ volatile(
        "push  {r4, lr}\n"
        "bl    fontopen_body\n"
        "pop   {r4, pc}\n");
}

/* Replaces `push {r4,r5,r6,lr}` + `cmp r2,#13`, replays both and rejoins at
   0x100e134c. Order matters: the cmp must be the LAST thing before the jump,
   because the beq at 0x100e134e reads its flags (the sub sp in between does
   not touch them). */
__attribute__((naked)) void font_hook(void)
{
    __asm__ volatile(
        "push  {r0-r3, r12, lr}\n"
        "bl    font_body\n"
        "pop   {r0-r3, r12, lr}\n"
        "push  {r4, r5, r6, lr}\n"
        "cmp   r2, #13\n"
        "movw  r12, #0x134d\n"
        "movt  r12, #0x100e\n"
        "bx    r12\n");
}

void pointer_body(void *pt)
{
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st || !pt) return;
    struct inj_state *S = ANCHOR->st;
    /* Record the struct RAW. The first attempt decoded x/y/state from assumed
       offsets and got zeros from all 142 samples -- but this function is also
       called on idle polls (~1.3/s with nothing touched), so zeros prove
       nothing about the layout. Store the words and let the host decode. */
    uint32_t w0 = *(volatile uint32_t *)((uint32_t)pt + 0);
    uint32_t w1 = *(volatile uint32_t *)((uint32_t)pt + 4);
    uint32_t w2 = *(volatile uint32_t *)((uint32_t)pt + 8);
    S->touch_n++;
    /* An all-zero sample means "nothing is touching" -- which is also how the
       RELEASE arrives. Returning here without clearing touch_down latched it at
       1 forever, so every press after the first looked like a continuing hold
       and was swallowed by the edge test below. Measured: 18 taps recorded,
       cur frozen at [2585..2783], sp stuck at 12. */
    if ((w0 | w1 | w2) == 0) { S->touch_down = 0; return; }
    S->touch[S->touch_nz & 15] = w0;   /* w0 is already x<<16 | y */
    S->touch_nz++;

    /* OUR OWN PAGE TURN.
     *
     * Measured layout: +0x00 x, +0x02 y (int16), +0x08 press state. A tap at
     * the right of a 176px screen came in at x=140/139, the middle at x=84.
     *
     * This is the point of the whole exercise: the vendor's reading_line was
     * our only turn signal, and it dies whenever its decode aborts -- which is
     * every page, since its page context holds 8 lines. Input read here owes
     * nothing to its reader, its scene, or the LVGL object tree (none of which
     * ever saw a press: measured, zero words changed).
     *
     * Edge-triggered: a hold repeats samples at ~1.3/s and would turn on each.
     * The top strip is left alone so the vendor's back/speaker icons work. */
    int32_t tx = (int32_t)(short)(w0 & 0xffff);
    int32_t ty = (int32_t)(short)((w0 >> 16) & 0xffff);
    uint8_t down = (w2 & 0xff) ? 1 : 0;
    uint8_t was = S->touch_down;
    S->touch_down = down;
    if (!down || was) return;                 /* only the press edge */

    /* Only act when the READING VIEW is on screen.
     *
     * The touch driver is global: it fires in the menu, the file picker and
     * every other scene. Without this, taps on menu entries also turned pages
     * underneath, which is why the menu button looked dead. Measured with the
     * menu open: app_global+0x3c moves to another scene object and the field
     * that is the label container while reading reads 0x18007d80 -- vendor RAM,
     * not the LVGL heap. That difference is the test. */
    {
        void *rd = reader_obj();
        S->last_rd = (uint32_t)rd;
        S->last_cont = 0;
        S->last_scr = 0;
        if (!rd) return;
        uint32_t cont = *(volatile uint32_t *)((uint32_t)rd + RD_OFF_LIST);
        S->last_cont = cont;
        if (cont >= HEAP_LO && cont < HEAP_HI)
            S->last_scr = *(volatile uint32_t *)(cont + 4);   /* parent/screen */
        if (cont < HEAP_LO || cont >= HEAP_HI) return;

        /* The menu is a POPUP INSIDE the reading scene, so neither the scene
           pointer nor the container identity can gate this: our render reads
           the same field the popup repoints, so it follows the popup and then
           agrees with it. Comparing against draw_cont was circular.
           
           Geometry is independent of all that. LVGL keeps absolute screen
           coords at obj+0x14 (x1,y1,x2,y2 as int16), and the touch point is in
           the same space. A tap only turns a page if it lands INSIDE the page
           we drew -- so the keypad, which sits below the shrunken book area,
           cannot reach the reader. */
        int32_t x1 = *(volatile short *)(cont + 0x14);
        int32_t y1 = *(volatile short *)(cont + 0x16);
        int32_t x2 = *(volatile short *)(cont + 0x18);
        int32_t y2 = *(volatile short *)(cont + 0x1a);
        S->last_rect  = (x1 << 16) | (y1 & 0xffff);
        S->last_rect2 = (x2 << 16) | (y2 & 0xffff);

        /* Is a popup open over the page?
         *
         * Not answerable by geometry: at a keypad press the page rect was
         * (4,24)-(179,263) -- the whole screen, keypad drawn inside it. Not by
         * container identity: our render reads the same field the popup
         * repoints, so it follows the popup and agrees with it. And not by
         * "is the page the topmost child of the screen": a sibling
         * (0x01004314) sits above our branch even while reading, so that test
         * blocked every tap.
         *
         * What does change is the number of children of the page's parent: the
         * popup is added there. Compare against the fewest ever seen while
         * actually drawing the page. */
        uint32_t par = *(volatile uint32_t *)(cont + 4);
        if (par < HEAP_LO || par >= HEAP_HI) return;
        uint32_t sp2 = *(volatile uint32_t *)(par + 8);
        if (sp2 < HEAP_LO || sp2 >= HEAP_HI) return;
        uint32_t kids = *(volatile uint32_t *)(sp2 + 4);
        S->last_top = kids;
        S->last_node = S->kid_min;

        /* Popup open: this is the "select page" keypad. Read it OURSELVES.
         *
         * Its own logic is built on the vendor's page count, which needs a
         * five-minute scan we do not want and do not need -- a percent seek is
         * just an offset, because our pages are byte extents. So we take the
         * keypad as a surface: the taps already arrive here with coordinates.
         *
         * Grid measured by tapping 1-9 then 0 (consecutive duplicates are the
         * press and release of one tap):
         *     (34,155) (89,157) (147,156)      1 2 3
         *     (29,190) (89,189) (151,188)      4 5 6
         *     (29,221) (86,221) (148,220)      7 8 9
         *              (74,259)                  0
         * Columns at x ~31/88/149, rows at y ~156/189/221, 0 centred at 259.
         * The outer keys of the bottom row are the dialog's own (enter/erase)
         * and are left to it. */
        if (S->kid_min != 0xffffffffu && kids > S->kid_min) {
            int col = (tx < 60) ? 0 : (tx < 120 ? 1 : 2);
            int row = (ty < 172) ? 0 : (ty < 205 ? 1 : (ty < 240 ? 2 : 3));
            int dig = -1;
            if (ty >= 140 && row <= 2) dig = row * 3 + col + 1;
            else if (row == 3 && col == 1) dig = 0;
            if (dig >= 0) {
                if (!S->typing) { S->typing = 1; S->typed = 0; }
                S->typed = S->typed * 10 + dig;
                if (S->typed > 100) S->typed = dig;   /* rolled over: restart */
            } else if (row == 3 && col == 0) {        /* backspace, left of 0 */
                if (S->typing) S->typed /= 10;
            } else if (row == 3 && col == 2) {        /* enter, right of 0    */
                int32_t size = (int32_t)*FW_BOOK_SIZE;
                int32_t pct = S->typed;
                if (S->typing && size > 0 && pct >= 0 && pct <= 100) {
                    /* Clear the history rather than pushing the old spot.
                       Pushing it made the first back tap teleport to where the
                       jump came FROM; after a deliberate jump, back should mean
                       "the page before this one". An empty stack takes the
                       want_prev path, which computes that exactly. */
                    S->sp = 0;
                    S->want = (size / 100) * pct;
                    if (S->want >= size) S->want = size - 1;
                    S->want_snap = 1;
                    S->need_prep = 1;
                }
                S->typing = 0;
                S->typed = 0;
            }
            return;                                   /* never turn a page */
        }
    }

    if (ty < 30 || ty > 250) return;          /* header / footer: vendor's */

    /* No debounce. The doubling was never contact bounce: it was a second turn
       source -- the vendor's own page-turn path, revived once we stopped lying
       about its page size -- and removing that fixed it. A press-edge test plus
       a real release is enough, and dropping the lockout keeps taps snappy. */

    /* Left half back, right half forward. Nothing else.
     *
     * A middle column that jumped by height was tried and removed: an accidental
     * brush in the middle of the text threw the position across the book, which
     * is not a trade a reader should ever make. Jumping belongs in the menu's
     * "select page", where it is deliberate. */
    if (tx >= 88) {
        push_back(S, S->cur.start);
        S->want = S->cur.end;
        S->need_prep = 1;
    } else {
        if (S->sp) S->want = S->back[--S->sp];
        else if (S->cur.start > 0) S->want_prev = 1;
        S->need_prep = 1;
    }
}

/* Replaces `push {lr}` + `ldr r3,[r1,#0]`, replays both, rejoins at 0x100e07b8. */
__attribute__((naked)) void pointer_hook(void)
{
    __asm__ volatile(
        "push  {lr}\n"
        "ldr   r3, [r1, #0]\n"
        "push  {r0-r3, r12, lr}\n"
        "bl    pointer_body\n"
        "pop   {r0-r3, r12, lr}\n"
        "movw  r12, #0x07b9\n"
        "movt  r12, #0x100e\n"
        "bx    r12\n");
}

/* Record one gesture. Pure observation: nothing is acted on yet, because we do
   not know the id vocabulary. Read it back with tools/gestures.py, then wire the
   ids that mean next/previous to our own page turn. */
void gesture_body(void *ctx)
{
    if (ANCHOR->magic != INJ_MAGIC || !ANCHOR->st || !ctx) return;
    struct inj_state *S = ANCHOR->st;
    int32_t id = *(volatile signed char *)((uint32_t)ctx + 0x21);
    int32_t x  = *(volatile short *)((uint32_t)ctx + 0x2c);
    int32_t y  = *(volatile short *)((uint32_t)ctx + 0x2e);
    S->gest[S->gest_n & 7] = ((uint32_t)(id & 0xff) << 24)
                           | ((uint32_t)(x & 0xfff) << 12)
                           | ((uint32_t)(y & 0xfff));
    S->gest_n++;
}

/* Entry hook: replaces `push {r4-r7,lr}` + `sub sp,#36`, replays both, then
   rejoins the original function at 0x100d92ec (`mov r4, r0`). */
__attribute__((naked)) void gesture_hook(void)
{
    __asm__ volatile(
        "push  {r4, r5, r6, r7, lr}\n"
        "sub   sp, #36\n"
        "push  {r0-r3, r12, lr}\n"
        "bl    gesture_body\n"
        "pop   {r0-r3, r12, lr}\n"
        "movw  r12, #0x92ed\n"
        "movt  r12, #0x100d\n"
        "bx    r12\n");
}

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
