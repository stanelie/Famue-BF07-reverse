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

#define INJ_MAGIC 0x5244423au   /* bump on every state-layout change */
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
    char     text[INJ_LINES][MAXW];   /* the wrapped page                   */
    int32_t  back[BACKSTACK];
};
static struct inj_state st;

void after_render(void);

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
static const char book[] = "/SD1:/The Last Town - Blake Crouch.txt";

/* Reads `len` bytes at `off`. Returns bytes read, or a negative error.
   Called only when the page changes -- never per frame. */
static int book_read(int32_t off, char *buf, uint32_t len)
{
    fs_file_t f;
    fw_memset(&f, 0, sizeof f);          /* Zephyr wants a zeroed handle */
    int rc = fs_open(&f, book, FS_O_READ);
    if (rc < 0) return rc;
    if (off) {
        rc = fs_seek(&f, off, FS_SEEK_SET);
        if (rc < 0) { fs_close(&f); return rc; }
    }
    rc = fs_read(&f, buf, len);
    fs_close(&f);
    return rc;
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

    static const char ok[] = "%s%s: repaginate off=%d rc=%d\n";
    fw_log(ok, "", "inj", st.offset);

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
}

static void push_back(int32_t off)
{
    if (st.sp < BACKSTACK) st.back[st.sp++] = off;
}

void reader_body(void)
{
    if (st.magic != INJ_MAGIC) {
        st.magic = INJ_MAGIC;
        st.calls = 0;
        st.offset = 0;
        st.next_offset = 0;
        st.last_line = -1;
        st.nlines = 0;
        st.sp = 0;
    }
    st.calls++;

    /* Keep the vendor's three array contexts decoded so its own paging keeps
       working; the standalone context is ours now. */
    fw_prep();
    void *a0 = (void *)FW_CTX_ARRAY;
    void *a1 = (void *)(FW_CTX_ARRAY + 0x3cc);
    void *a2 = (void *)(FW_CTX_ARRAY + 2 * 0x3cc);
    /* Faithful to the original at 0x10049394..0x100493a8: render ONLY if some
       decode returned 0. The original's `cbnz r0, 0x100493ac` skips the render
       when the last decode fails; calling it anyway renders an un-decoded
       context and corrupts the semaphore inside 0x1004922c, which surfaces as
       ASSERTION FAIL [thread->base.pended_on] in the scheduler. */
    void *last = 0;
    if (fw_decode(a0) == 0)      last = a0;
    else if (fw_decode(a1) == 0) last = a1;
    else if (fw_decode(a2) == 0) last = a2;
    if (last) fw_render(last);

    /* Use the vendor's position purely as a page-turn SIGNAL. Its magnitude
       is its own (8 lines/page); ours is however much text we consumed. */
    void *rd = reader_obj();
    if (!rd) return;
    int line = *(volatile int *)((uint32_t)rd + RD_OFF_LINE);

    if (st.last_line < 0 || st.nlines == 0) {
        /* SRAM survives a warm reset, so stale state can otherwise leave us
           with an empty cache and no page-change to trigger a fill. */
        repaginate();
    } else if (line > st.last_line) {          /* next */
        push_back(st.offset);
        st.offset = st.next_offset;
        repaginate();
    } else if (line < st.last_line) {          /* previous */
        if (st.sp) st.offset = st.back[--st.sp];
        else       st.offset = 0;
        repaginate();
    }
    st.last_line = line;

    after_render();
}

/* Replaces 0x1004937c entirely. Reproduces its lock/unlock contract:
   `push {r4,lr}; mov r4,r0` at entry, `mov r0,r4; pop; b.w unlock` at exit. */
__attribute__((naked)) void reader_main(void)
{
    __asm__ volatile(
        "push  {r4, lr}\n"
        "mov   r4, r0\n"
        "movw  r12, #0xd8b9\n"      /* 0x100fd8b8 | thumb -- lock  */
        "movt  r12, #0x100f\n"
        "blx   r12\n"
        "bl    reader_body\n"
        "mov   r0, r4\n"
        "pop   {r4, lr}\n"
        "movw  r12, #0xd8c3\n"      /* 0x100fd8c2 | thumb -- unlock */
        "movt  r12, #0x100f\n"
        "bx    r12\n");
}
