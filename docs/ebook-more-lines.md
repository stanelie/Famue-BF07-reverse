# Increasing lines per page (the full, correct recipe)

Raising `cmp r3, #7` alone **corrupts memory**. This document records why, and what the
complete change actually requires. See [ebook-layout.md](ebook-layout.md) for how the
layout code was found in the first place.

## The data structure

The reader keeps **four page contexts**, all of them **static** — there is no allocation
to enlarge:

| address | role | referenced from |
|---|---|---|
| `0x18018a4c` | standalone context, stored at `[ctx+0x18c]` | `0x100493c0`, `0x1004a110`, `0x101ac01c` |
| `0x18019098` | array[0], stored at `[ctx+0x190]` | `0x100493c4`, `0x1004a114`, `0x1004a4f0`, `0x101ac02c` |
| `0x18019464` | array[1] | `0x100493c8` |
| `0x18019830` | array[2] | `0x100493cc` |

Each context is `0x3cc` bytes:

```
+0x00 .. +0x2b   header fields (only 0x14, 0x1c, 0x20, 0x24, 0x28 are touched)
+0x2c .. +0x3cb  8 line records x 0x74 bytes      <- trailing member
```

`0x2c + 8 * 0x74 = 0x3cc` exactly. So `cmp r3, #7` in `_decode_one_page` is an **array
bound**, not a display limit — raising it writes past the end of the struct.

The three array entries are contiguous and exactly fill their static block:

```
0x18019098 + 3 * 0x3cc = 0x18019bfc   <- the next symbol in the image's symbol table
```

There is **no slack**. The `0x280` bytes between the standalone context and the array are
*not* padding — `0x18018e18`, `0x18018e1c`, `0x18018e20` are live variables read by the
reader itself (`0x10049220`, `0x10049228`, `0x1004a128`, `0x1004a728`, `0x1004a738`).
Growing the standalone context in place overwrites them. **This is what the earlier
10-line patch was silently doing.**

## How the pointers get set

```
10049f84  ldr  r0, =0x18018a4c
10049f86  ldr  r7, =0x18019098
10049f90  mov  sl, r7
...
10049f1a  bl   memset            ; (ctx_struct, 0, 0x1f4)
10049f1e  mov.w r2, #0x3cc
10049f24  ldr  r0, =0x18018a4c
10049f28  bl   memset            ; (standalone, 0, 0x3cc)
10049f30  movw r2, #0xb64        ; 3 * 0x3cc
10049f36  ldr  r0, =0x18019098
10049f38  bl   memset            ; (array, 0, 0xb64)
...
10049fc0  ldr  r7, =0x18018a4c
10049fc2  strd r7, sl, [r4, #0x18c]   ; [+0x18c]=standalone, [+0x190]=array
```

Three loops walk the array with `+= 0x3cc, i < 3`, and one uses an **end pointer**:

```
1004c360  addw r3, r5, #0xb64
1004c364  str  r3, [sp, #0xc]
...
1004c39a  add.w r5, r5, #0x3cc
1004c39e  cmp  r5, r3
```

## The instruction-encoding constraint

The array end pointer at `0x1004c360` is a single `ADDW`, whose immediate is **12 bits
(max 0xFFF)**, and there is no spare room to lengthen the instruction. For 12 lines the
natural size gives `3 * 0x59c = 0x10d4`, which does not fit.

It is still reachable: `ADD.W` takes a **modified immediate** instead (an 8-bit value with
bit 7 set, rotated). Padding the context by 4 bytes to `0x5a0` makes the array end
`0x10e0 = 0x87 << 5`, which encodes as `add.w r3, r5, #0x10e0`. `tools/patch_lines.py`
does this automatically — it picks the smallest word-aligned size whose `3 * size` is
encodable either way.

| lines | context size | 3 contexts | end-pointer instruction | line height (236/N) |
|---|---|---|---|---|
| 8 (stock) | 0x3cc | 0xb64 | `addw` | 29 px |
| 10 | 0x4b4 | 0xe1c | `addw` | 23 px |
| 11 | 0x528 | 0xf78 | `addw` | 21 px |
| 12 | 0x5a0 (+4 pad) | 0x10e0 | `add.w` | 19 px |
| 13 | 0x620 (+16 pad) | 0x1260 | `add.w` | 18 px |

The padding is harmless: line records still start at `+0x2c` with a `0x74` stride, and the
few spare bytes simply sit at the end of each context.

**11 is the recommended default.** 12 lines drops the line height to 19 px, which is
exactly where descender clipping was observed; 11 gives 21 px.

## The patch (11 lines)

All replacements are the **same byte length** as the originals — no code motion.

| XIP | flash | old bytes | new bytes | change |
|---|---|---|---|---|
| `0x1004934a` | `0x05d34a` | `07 2b` | `0a 2b` | `cmp r3,#7` → `#10` |
| `0x100493c0` | `0x05d3c0` | literal | `NEW+0x0000` | standalone |
| `0x100493c4` | `0x05d3c4` | literal | `NEW+0x0528` | array[0] |
| `0x100493c8` | `0x05d3c8` | literal | `NEW+0x0a50` | array[1] |
| `0x100493cc` | `0x05d3cc` | literal | `NEW+0x0f78` | array[2] |
| `0x10049f1e` | `0x05df1e` | `4f f4 73 72` | `40 f2 28 52` | memset size → `0x528` |
| `0x10049f30` | `0x05df30` | `40 f6 64 32` | `40 f6 78 72` | memset size → `0xf78` |
| `0x10049fba` | `0x05dfba` | `07 f5 73 77` | `07 f2 28 57` | stride → `0x528` |
| `0x1004a110` | `0x05e110` | literal | `NEW+0x0000` | standalone |
| `0x1004a114` | `0x05e114` | literal | `NEW+0x0528` | array[0] |
| `0x1004a318` | `0x05e318` | `4f f4 73 7a` | `40 f2 28 5a` | stride → `0x528` |
| `0x1004a440` | `0x05e440` | `07 f5 73 77` | `07 f2 28 57` | stride → `0x528` |
| `0x1004a4f0` | `0x05e4f0` | literal | `NEW+0x0528` | array[0] |
| `0x1004c360` | `0x060360` | `05 f6 64 33` | `05 f6 78 73` | end ptr → `0xf78` |
| `0x1004c39a` | `0x06039a` | `05 f5 73 75` | `05 f2 28 55` | stride → `0x528` |

`NEW` defaults to `0x18210000` (see below). Three flash sectors: **`0x5d000`, `0x5e000`, `0x60000`**.

`NEW` is a relocated RAM block of `4 * 0x528 = 0x14a0` bytes. The existing memsets zero it,
so it needs no other initialisation.

**Leave `0x101ac01c` / `0x101ac02c` alone.** They belong to a sorted 1018-entry address
table (`0x101abc6c..0x101acc50`) spanning `0x18000000..0x181ed400`. Editing them would
break its sort order, and nothing depends on those entries for correctness — the reader
memsets its contexts explicitly.

## Finding the relocated RAM block

The image contains a **sorted 1018-entry table of every static's address** at
`0x101abc6c..0x101acc50`. It gives the whole RAM layout for free:

```
0x18000000 .. 0x18073000   dense statics (also all three kernel heaps:
                           0x180225d8, 0x18025248, 0x1806ee48 -- all small)
0x18073000 .. 0x180f3000   one 512 KB object
0x180f3400 .. 0x181d2c00   one ~894 KB object
0x181d5400 .. 0x181ed400   96 KB       <- last static ends here
```

**But the symbol table only covers statics.** `dbg uimem` shows the real address space is
much larger:

```
heap at 0x18291400, block size 172800, count 4
ptr 0x18291400 / 0x182bb700 / 0x182e5a00 / 0x1830fd00     (4 UI framebuffers)
```

and `0x18400000` reads identical to `0x18000000` — the window **wraps at 4 MB**.

So the map is:

```
0x18000000 .. ~0x181f4000   statics (all three kernel heaps live in here, all small)
~0x181f5000 .. 0x18210000   UNSOUND -- do not use (see below)
0x18210000 .. 0x18280000    free, 448 KB, verified sound
0x18291400 .. 0x1833a000    UI framebuffer heap (4 x 172800)
0x18400000                  wraps to 0x18000000
```

### Two traps worth knowing

**`dbg mdw`/`mww` parse the count as HEX**, not decimal. A fill of "6144" writes `0x6144`
words, four times the intended length.

**Zeros do not mean free, and writable does not mean sound.** `0x181ee000..0x181f4000`
reads as zero after boot, accepts writes, and held a canary for 9 minutes at seven sample
points — yet a *full* scan found live writes at `0x181eeb5c`, `0x181efb1c`, `0x181efd08`,
`0x181f03bc`, `0x181f0bec`. Sample points are not enough; scan every word.

Above `0x181f5000` a canary came back with 6326 of 17356 words wrong, all differing from
`a5a5a5a5` by a **single bit** (`a5a5a5ad`, `a5a4a5a5`, `a5a5a5a7`). That is bit rot, not
code writing data — writes appear to stick and then decay. Repeated reads of the same
address are the cheap way to tell a real write from a floating bus.

By contrast `0x18210000..0x18280000` fills cleanly, survives repeated reads, and a full
24 KB scan returned **0 deviations**.

**`NEW = 0x18210000`**, using `0x14a0` of the 448 KB.

Still to confirm before flashing: fill the block with `0xa5a5a5a5`, exercise the reader
and audio paths, and re-scan **every word** — anything that overwrites it disqualifies it.

## Tooling

`tools/patch_lines.py` applies all 15 sites offline and emits the three sectors. Every
site asserts its expected stock bytes first, so a wrong offset aborts rather than
corrupting the image:

```
python3 tools/patch_lines.py fw_code_full.bin --lines 11 --ram 0x181ee000 --outdir out/
```

Related: [ebook-layout.md](ebook-layout.md), [firmware-extraction.md](firmware-extraction.md)

---

# WHY THIS DOES NOT WORK YET (tested on hardware)

The 11-line build above was flashed and **fails**: it crashes on page change, and the
11th line is cut off by the bottom of the screen. Reverted to stock, byte-exact.

## Root cause: lines-per-page is a SHIFT, in nine places

The reader stores the reading position as a **line index** and derives the page number
from it by dividing by 8 — as a hardcoded arithmetic shift:

```
    ldr.w r2, [r4, #0x198]   ; reading position, in lines
    cmp   r2, #0
    it    lt
    addlt r2, #7             ; signed round-toward-zero
    asrs  r2, r2, #3         ; page = line / 8
    adds  r2, #1
```

That exact idiom appears at **nine** sites:

| site | role |
|---|---|
| `0x1004944a` | page from position |
| `0x100494ce` | page from position |
| `0x10049560` | page from position |
| `0x10049674` | page from position |
| `0x1004981a` | **the page number printed on screen** (`'%d'`) |
| `0x10049e38` | page from position |
| `0x10049eb0` | page from position |
| `0x1004a288` | line height (`content_height / 8`) — the only one patched |
| `0x1004a312` | page written into each context (`[ctx+0x18] = page + i`) |

Decoding 11 lines per page while the page arithmetic still assumes 8 makes the page index
run ahead of reality; `0x1004a312` then stores a wrong page into every context, which is
what faults on a page turn.

## Consequence: only powers of two are reachable in place

A shift can only divide by a power of two, so an in-place same-length patch can reach
**8 (stock) or 16** lines and nothing else. 16 lines means `236/16 = 14 px`, far below the
~20 px glyph height (descenders already clipped at 19 px). So 16 is not usable.

**Arbitrary line counts require real division**, which does not fit in the 6 bytes the
idiom occupies.

## The way forward: injected division stubs

This is tractable, and the two hard parts are already proven:

- **Hardware division exists.** The firmware already uses `sdiv` 255 times and `udiv` 494
  times, so a stub is a few instructions and fast.
- **Code injection into free flash works** — see the stub at `0x1e7000`.

Each site is a 6-byte `it lt / addlt #7 / asr #3`, and `bl <stub>` + `nop` is exactly 6.
One stub per (register, divisor) pair, e.g. for `r1`:

```
    push {r0}
    movs r0, #11
    sdiv r1, r1, r0
    pop  {r0}
    bx   lr
```

`lr` is free to clobber at these sites because each enclosing function already saves it in
its prologue — **verify this per site before patching.**

Open questions before attempting this:

- The inverse conversion (page -> line, `* 8`) has not been located. A byte scan for
  `lsls rX, rY, #3` returns ~200 hits in the reader region, overwhelmingly misaligned
  decodes of data, so it needs a different method — ideally disassembling from known
  function entry points rather than linearly.
- Whether `[r4+0x194]`/`[r4+0x198]` is truly a line index or something finer.
- The `.BMK` bookmark files on the SD card cache reading positions; a stale one may need
  deleting after any change to the line count.

## Note on the ATJ21xx/22xx documents

The Actions datasheets and programming guides collected for ATJ2135 / 2137 / 2236 / 2253 /
2256 / 2259 / 227x are a **different chip generation** — proprietary-core MP3/MP4 parts
from 2007-2012. The BF07 is a LARK (ATJ2158, ARM Cortex-M, Zephyr + LVGL). Likewise
`atj2127decrypt` and the ATJ227x-era `ruizu-x02-rev` target the older families. The public
LARK SDK remains the applicable reference; see [sdk.md](sdk.md).
