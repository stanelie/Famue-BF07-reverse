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

#define INJ_MAGIC 0x52444243u   /* bump on every state-layout change */
#define MAXW      44      /* buffer per displayed line          */
#define CPL       25      /* fallback: measured by eye at 168px      */
#define LINE_PX  168      /* label width, measured on hardware       */
#define BACKSTACK 48      /* how many page-starts we can go back through */

/* Lives in the freed standalone page context (0x18018a4c, 0x3cc bytes).
   Nothing zeroes it, so `magic` gates initialisation. */
struct inj_state {
    uint32_t magic;
    uint32_t calls;
    int32_t  offset;                  /* byte offset: start of current page */
    int32_t  next_offset;             /* byte offset: start of next page    */
    int32_t  last_line;               /* last vendor reading_line seen      */
    uint16_t nlines;                  /* lines currently cached             */
    uint8_t  sp;                      /* back-stack depth                   */
    volatile uint8_t need_prep;       /* display -> ebook: please prepare    */
    int32_t  have_offset;             /* offset the cached page actually is  */
    uint32_t gen;                     /* bumped by every completed prepare   */
    char     text[INJ_LINES][MAXW];   /* the wrapped page                   */
    int32_t  back[BACKSTACK];
};
static struct inj_state st;

void after_render(void);
static void repaginate(void);
static void push_back(int32_t off);

/* ---- M4: draw the page ourselves ----------------------------------- */

/* Instrumented: we have no writable data section, so instead of a call
   counter we display the LIVE reading position, read straight out of the
   reader object. If a page turn re-enters this hook, the number on screen
   changes; if it does not, the hook is not running on page turns. Also logs
   every call so the UART shows the firing pattern. */
void after_render(void)
{
    void *rd = reader_obj();
    if (!rd) return;
    void *cont = *(void **)((uint32_t)rd + RD_OFF_LIST);
    if ((uint32_t)cont < 0x01000000) return;

    uint32_t n = lv_obj_child_cnt(cont);
    if (n > INJ_LINES) n = INJ_LINES;

    /* The vendor's timer callback is left INTACT so its decode chain and list
       scrolling keep working -- replacing it wholesale left all three contexts
       with ln=0 and froze reading_line, so no page turn was ever detectable.
       We use reading_line only as a SIGNAL (direction, not magnitude). */
    int line = *(volatile int *)((uint32_t)rd + RD_OFF_LINE);
    if (st.magic != INJ_MAGIC) {
        st.magic = INJ_MAGIC; st.calls = 0; st.offset = 0; st.next_offset = 0;
        st.last_line = -1; st.nlines = 0; st.sp = 0; st.need_prep = 1;
        st.have_offset = -1; st.gen = 0;
    }
    st.calls++;

    /* This runs up to THREE times per turn -- the vendor decodes and renders up
       to three contexts and we wrap that call. Two consequences to handle:
       (a) a turn must be accepted only ONCE, and only after the previous
           preparation finished, or we advance using a stale next_offset and
           redisplay the same text;
       (b) the labels are rewritten on every pass regardless, otherwise the
           vendor's own text shows through between our redraws. */
    if (st.last_line < 0) {
        /* Establish the baseline ONCE and commit it. The previous version
           guarded the commit with `if (!need_prep)`, but the initial fill sets
           need_prep, so last_line stayed -1 forever, every pass re-took this
           branch, and no turn was ever detected -- the trace showed
           `prep off=0` repeating with no TURN+ at all. */
        st.last_line = line;
        st.need_prep = 1;
    } else if (st.need_prep) {
        /* Preparation outstanding. Deliberately leave last_line alone so a turn
           arriving now is still seen once the ebook thread has caught up. */
    } else if (line != st.last_line) {
        if (line > st.last_line) {
            push_back(st.offset);
            st.offset = st.next_offset;
        } else {
            if (st.sp) st.offset = st.back[--st.sp];
            else       st.offset = 0;
        }
        {
            static const char t[] = "%s%s: TURN off->%d\n";
            fw_log(t, "", "inj", st.offset);
        }
        st.last_line = line;
        st.need_prep = 1;
    }

    for (uint32_t i = 0; i < n; i++) {
        void *c = lv_obj_get_child(cont, i);
        if (!c) continue;
        lv_label_set_text(c, (i < st.nlines) ? st.text[i] : "", 0);
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

/* ---- M6: replace the decode+render function outright --------------- */

#define fw_lock    ((void (*)(void))0x100fd8b9)   /* 0x1004937c entry pair   */
#define fw_prep    ((void (*)(void))0x100491b1)   /* called before decoding  */
#define fw_decode  ((int  (*)(void *))0x10049299) /* _decode_one_page        */
#define fw_render  ((void (*)(void *))0x1004922d) /* fills the labels        */

/* OUR RAM -- the freed standalone context. Nothing zeroes .bss here, so a
   magic word tells us whether it has been initialised. */

/* ---- book file access ---------------------------------------------- */

/* The mount is "/SD1:", NOT "/SD:" -- found by searching live SRAM, where the
   firmware's own paths read "/SD1:C/sans16.font" and "/SD1://EBOOK.LIB".
   Using "/SD:" made fs_open return -ENOENT (-2). */
/* No open, no close: the vendor already has the book open via ebook_file_init
   (handle at 0x1801a084). We only seek and read on it, from the SAME thread it
   uses, so there is no second handle and no cross-thread access. */
static int book_read(int32_t off, char *buf, uint32_t len)
{
    int rc = fs_seek(FW_BOOK_FILE, off, FS_SEEK_SET);
    if (rc < 0) return rc;
    return fs_read(FW_BOOK_FILE, buf, len);
}

/* Our own word wrap.
 *
 * fw_wrap_line (0x10049074) was [INFERRED] from a single call site to return
 * "bytes in one line". It does not: one call swallowed the whole 512-byte
 * buffer, so a page rendered as a single line. Rather than keep guessing at an
 * undocumented signature, wrap ourselves -- predictable, and independent of
 * the vendor entirely.
 *
 * Returns bytes to consume for one displayed line, including any newline. */
/* Proportional width estimate.
 *
 * 0x100eb4b8 was tried and rejected: its THIRD argument is a mode selector
 * (0x7b/0x7c/0x7d are special-cased), not an encoding as the call site
 * suggested, and the path our value takes simply returns the width argument
 * instead of measuring -- the log showed used == n for every line. That is the
 * second wrong signature inferred from txt_analy_one_line, so we stop guessing
 * and estimate ourselves.
 *
 * Widths are in 1/8 px for a ~16px sans face. Not pixel-exact, but it is
 * proportional, self-contained and cannot overrun. Tune WIDTH_SCALE if lines
 * come out consistently short or long. */
static int char_w8(unsigned char c)
{
    if (c == ' ') return 36;
    if (c < 0x20) return 0;
    if (c >= 0x80) return 88;                       /* non-ASCII: assume wide */
    switch (c) {
    case 'i': case 'j': case 'l': case '.': case ',': case ':': case ';':
    case '\'': case '!': case '|': case '`':          return 26;
    case 'f': case 'r': case 't': case '(': case ')':
    case '[': case ']': case '-':                    return 38;
    case 'm': case 'w': case 'M': case 'W': case '@': return 88;
    default:
        if (c >= 'A' && c <= 'Z') return 72;
        if (c >= '0' && c <= '9') return 60;
        return 58;                                   /* lower-case average */
    }
}

#define WIDTH_SCALE 8                                /* 1/8 px units */

static int wrap_one(const char *p, int avail)
{
    if (avail <= 0) return 0;

    int w8 = 0, i = 0, last_space = -1;
    /* 4px of margin: the estimate is occasionally a hair narrow and a single
       character was still overshooting. Erring short is invisible; erring long
       is not. */
    int limit = (LINE_PX - 4) * WIDTH_SCALE;

    while (i < avail && i < MAXW - 1) {
        unsigned char c = (unsigned char)p[i];
        if (c == '\n') return i + 1;                 /* hard newline wins */
        if (c == ' ') last_space = i;
        w8 += char_w8(c);
        if (w8 > limit) break;
        i++;
    }

    if (i >= avail) return avail;                    /* tail of the buffer */
    if (last_space > 0) return last_space + 1;       /* break at a word */
    return i > 0 ? i : 1;                            /* long word: hard break */
}

/* Re-wrap one page starting at st.offset. Reads once, only when the page
   changes -- the render function polls ~3x/second, so per-frame I/O is out. */
static void repaginate(void)
{
    char raw[512];
    int rc = book_read(st.offset, raw, sizeof raw);
    st.nlines = 0;
    if (rc <= 0) {
        static const char e[] = "%s%s: book_read rc=%d\n";
        fw_log(e, "", "inj", rc);
        st.next_offset = st.offset;
        return;
    }

    static const char r1[] = "%s%s: prep off=%d\n";
    static const char r2[] = "%s%s: prep rc=%d\n";
    fw_log(r1, "", "inj", st.offset);
    fw_log(r2, "", "inj", rc);

    int pos = 0;
    for (int i = 0; i < INJ_LINES && pos < rc; i++) {
        int take = wrap_one(raw + pos, rc - pos);
        if (take <= 0) break;

        int k = 0;
        for (int j = 0; j < take && k < MAXW - 1; j++) {
            char ch = raw[pos + j];
            if (ch == '\n' || ch == '\r') continue;
            st.text[i][k++] = ch;
        }
        st.text[i][k] = 0;
        pos += take;
        st.nlines++;
    }
    st.next_offset = st.offset + pos;
    st.have_offset = st.offset;
    st.gen++;
    {
        static const char r3[] = "%s%s: prep next=%d\n";
        static const char r4[] = "%s%s: prep nlines=%d\n";
        fw_log(r3, "", "inj", st.next_offset);
        fw_log(r4, "", "inj", st.nlines);
    }
}

static void push_back(int32_t off)
{
    if (st.sp < BACKSTACK) st.back[st.sp++] = off;
}

/* ---- page preparation, on the EBOOK thread ------------------------- */

/* Wraps `bl 0x100ff06c` (msg_manager_receive_msg) at 0x1004c002, the top of
 * _ebook_reading_event_handle's message loop. That call is unconditional, runs
 * on the ebook thread -- which already owns the file via ebook_file_init -- and
 * happens just before the loop blocks for the next message. So this is idle
 * time while the user reads: exactly where page preparation belongs. */
void prepare_body(void)
{
    if (st.magic != INJ_MAGIC) return;      /* timer initialises the state */
    if (!st.need_prep) return;
    st.need_prep = 0;
    repaginate();                            /* file I/O + wrap, safe here */
}

__attribute__((naked)) void prepare_hook(void)
{
    __asm__ volatile(
        "push  {r0-r3, lr}\n"
        "bl    prepare_body\n"
        "pop   {r0-r3, lr}\n"
        "movw  r12, #0xf06d\n"      /* 0x100ff06c | thumb */
        "movt  r12, #0x100f\n"
        "bx    r12\n");
}
