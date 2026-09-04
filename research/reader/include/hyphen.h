#ifndef HYPHEN_H
#define HYPHEN_H
/* Knuth-Liang hyphenation. Header-only so the device build and the host test
   share one implementation -- the host test (tools/test_hyphen.c) compares it
   against the Python reference in tools/mkhyphen.py, which is how the packed
   table format is verified without flashing anything. */
#include "hyphen_data.h"

#define HY_LANG_EN 0
#define HY_LANG_FR 1

/* Keeps hyphenate()'s frame small: it runs on the ebook thread, whose stack is
   2280 bytes and has historically had only ~328 to spare. */
#define HY_MAXWORD 32
#define HY_LEFTMIN  2          /* standard TeX values */
#define HY_RIGHTMIN 3
#define HY_MINWORD  5

struct hy {
    const unsigned char *base;
    unsigned short npat, nalpha, nindex;
    unsigned char maxlen, stride;
    const unsigned short *cp;
    const unsigned char *index, *letters, *values;
    unsigned int letters_len;
    unsigned char entry;
};

static int hy_open(struct hy *h, int lang)
{
    const unsigned char *b = hyph_data + (lang == HY_LANG_FR ? HYPH_FR_OFF : HYPH_EN_OFF);
    h->base = b;
    h->npat   = (unsigned short)(b[0] | (b[1] << 8));
    h->nalpha = (unsigned short)(b[2] | (b[3] << 8));
    h->maxlen = b[4];
    h->stride = b[5];
    h->nindex = (unsigned short)(b[6] | (b[7] << 8));
    unsigned int loff = (unsigned int)(b[8] | (b[9] << 8) | (b[10] << 16) | (b[11] << 24));
    unsigned int voff = (unsigned int)(b[12] | (b[13] << 8) | (b[14] << 16) | (b[15] << 24));
    h->cp = (const unsigned short *)(b + 16);
    h->entry = (unsigned char)(2 + 2 + 1 + h->maxlen);
    h->index = b + 16 + 2 * h->nalpha;
    h->letters = b + loff;
    h->values = b + voff;
    h->letters_len = voff - loff;
    return h->npat && h->nalpha;
}

/* Map one lowercase code point to its alphabet index, or -1. */
static int hy_index_of(struct hy *h, unsigned cp)
{
    for (int i = 0; i < h->nalpha; i++)
        if (h->cp[i] == cp) return i;
    return -1;
}

static int hy_cmp(const unsigned char *a, int alen, const unsigned char *b, int blen)
{
    int n = alen < blen ? alen : blen;
    for (int i = 0; i < n; i++)
        if (a[i] != b[i]) return (int)a[i] - (int)b[i];
    return alen - blen;
}

/* Find `q` in the table. Returns its value bytes, or 0. `*prefix` is set when
   some pattern merely STARTS with q -- that is what lets the caller stop
   extending a substring that can never match anything longer. */
static const unsigned char *hy_find(struct hy *h, const unsigned char *q, int qlen,
                                    int *prefix)
{
    *prefix = 0;
    int lo = 0, hi = h->nindex - 1, blk = 0;
    while (lo <= hi) {                       /* which block can contain q */
        int mid = (lo + hi) / 2;
        const unsigned char *e = h->index + (unsigned)mid * h->entry;
        int c = hy_cmp(e + 5, e[4], q, qlen);
        if (c <= 0) { blk = mid; lo = mid + 1; } else hi = mid - 1;
    }
    const unsigned char *e = h->index + (unsigned)blk * h->entry;
    unsigned lp = (unsigned)(e[0] | (e[1] << 8));
    unsigned vp = (unsigned)(e[2] | (e[3] << 8));

    unsigned char cur[16];
    int curlen = 0;
    /* Scan PAST the block if needed. Stopping at exactly `stride` entries lost
       any pattern sitting just after a block boundary, and the prefix test then
       reported "nothing longer exists" and pruned the search -- 99 of 5000 words
       hyphenated differently from the reference because of it. Crossing is safe:
       a block start re-encodes its string whole (shared = 0). */
    for (int n = 0; n < 2 * h->stride && lp < h->letters_len; n++) {
        unsigned char hdr = h->letters[lp++];
        int shared = hdr >> 4, suf = hdr & 15;
        if (shared > curlen) break;                    /* malformed: stop */
        for (int i = 0; i < suf; i++) cur[shared + i] = h->letters[lp + i];
        lp += suf;
        curlen = shared + suf;
        int nv = h->values[vp];
        int c = hy_cmp(cur, curlen, q, qlen);
        if (c == 0) return h->values + vp;             /* exact hit */
        if (c > 0) {
            /* past q: does this entry still start with q? then longer
               patterns with this prefix exist further on */
            if (curlen >= qlen && hy_cmp(cur, qlen, q, qlen) == 0) *prefix = 1;
            return 0;
        }
        if (curlen >= qlen && hy_cmp(cur, qlen, q, qlen) == 0) *prefix = 1;
        vp += 1 + nv;
    }
    return 0;
}

/* Break positions for `word` (UTF-8, `len` bytes). Fills `out` with byte
   offsets after which a hyphen may go; returns how many. */
/* Which language is this book in?
 *
 * The two pattern sets must not be merged -- measured, a fused table left only
 * 62% of English and 47% of French words correct -- so the language is chosen
 * per book instead. Accented characters are the strongest signal by far; the
 * stop words settle books that happen to open on an unaccented passage.
 */
static int hy_count(const char *hay, int n, const char *needle)
{
    int c = 0, m = 0;
    while (needle[m]) m++;
    for (int i = 0; i + m <= n; i++) {
        int k = 0;
        while (k < m && hay[i + k] == needle[k]) k++;
        if (k == m) c++;
    }
    return c;
}

static int hy_detect(const char *sample, int n)
{
    int fr = 0, en = 0;
    for (int i = 0; i + 1 < n; i++) {
        unsigned char a = (unsigned char)sample[i], b = (unsigned char)sample[i + 1];
        if (a == 0xC3 && b >= 0xA0 && b <= 0xBF) fr += 2;      /* à é è ê ç ù ... */
        else if (a == 0xC5 && b == 0x93) fr += 2;              /* oe ligature     */
    }
    static const char *frw[] = { " le ", " la ", " les ", " des ", " est ",
                                 " une ", " qui ", " que ", " dans ", " pour ", 0 };
    static const char *enw[] = { " the ", " and ", " of ", " to ", " is ",
                                 " that ", " with ", " was ", " it ", " he ", 0 };
    for (int i = 0; frw[i]; i++) fr += hy_count(sample, n, frw[i]);
    for (int i = 0; enw[i]; i++) en += hy_count(sample, n, enw[i]);
    return fr > en ? HY_LANG_FR : HY_LANG_EN;
}

static int hyphenate(const char *word, int len, int lang, unsigned char *out, int outmax)
{
    struct hy h;
    if (!hy_open(&h, lang) || len < HY_MINWORD || len > HY_MAXWORD - 2) return 0;

    unsigned char w[HY_MAXWORD];      /* alphabet indices, dot-wrapped */
    unsigned char bpos[HY_MAXWORD];   /* byte offset in `word` of each letter */
    int n = 0;
    int dot = hy_index_of(&h, '.');
    if (dot < 0) return 0;
    w[n++] = (unsigned char)dot;
    for (int i = 0; i < len; ) {
        unsigned cp = (unsigned char)word[i];
        int adv = 1;
        if (cp >= 0xC0) {                       /* decode UTF-8 */
            if ((cp & 0xE0) == 0xC0) { cp = ((cp & 0x1F) << 6) | (word[i+1] & 0x3F); adv = 2; }
            else if ((cp & 0xF0) == 0xE0) {
                cp = ((cp & 0x0F) << 12) | ((word[i+1] & 0x3F) << 6) | (word[i+2] & 0x3F);
                adv = 3;
            }
        }
        if (cp >= 'A' && cp <= 'Z') cp += 32;   /* fold case */
        int a = hy_index_of(&h, cp);
        if (a < 0) return 0;                    /* not a plain word: skip it */
        if (n >= HY_MAXWORD - 1) return 0;
        bpos[n] = (unsigned char)i;
        w[n++] = (unsigned char)a;
        i += adv;
    }
    w[n++] = (unsigned char)dot;

    unsigned char pts[HY_MAXWORD + 1];
    for (int i = 0; i <= n; i++) pts[i] = 0;
    for (int i = 0; i < n; i++) {
        int maxj = n - i;
        if (maxj > h.maxlen) maxj = h.maxlen;
        for (int j = 1; j <= maxj; j++) {
            int prefix = 0;
            const unsigned char *v = hy_find(&h, w + i, j, &prefix);
            if (v) {
                int cnt = v[0];
                for (int k = 1; k <= cnt; k++) {
                    int pos = v[k] >> 4, val = v[k] & 15;
                    if (i + pos <= n && pts[i + pos] < val) pts[i + pos] = (unsigned char)val;
                }
            }
            if (!v && !prefix) break;           /* nothing longer can match */
        }
    }

    int cnt = 0;
    int letters = n - 2;                        /* without the two dots */
    for (int k = HY_LEFTMIN; k <= letters - HY_RIGHTMIN; k++) {
        if (pts[k + 1] & 1) {                   /* odd = break allowed */
            if (cnt < outmax) out[cnt++] = bpos[k + 1];
        }
    }
    return cnt;
}


#endif
