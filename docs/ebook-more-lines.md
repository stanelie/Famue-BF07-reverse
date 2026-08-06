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

## Why 11 lines and not 12

`ADDW` at `0x1004c360` takes a **12-bit immediate (max 0xFFF)** and cannot be widened —
there is no spare room in the instruction stream.

| lines | context size | 3 contexts | `addw` encodable? | line height (236/N) |
|---|---|---|---|---|
| 8 (stock) | 0x3cc | 0xb64 | yes | 29 px |
| 11 | 0x528 | **0xf78** | **yes** | 21 px |
| 12 | 0x59c | 0x10d4 | **no — exceeds 0xFFF** | 19 px |

11 is also the better choice visually: 12 lines gives a 21→19 px drop, which is exactly
where descender clipping was observed.

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

Three flash sectors: **`0x5d000`, `0x5e000`, `0x60000`**.

`NEW` is a relocated RAM block of `4 * 0x528 = 0x14a0` bytes. The existing memsets zero it,
so it needs no other initialisation.

**Leave `0x101ac01c` / `0x101ac02c` alone.** They belong to a sorted 1018-entry address
table (`0x101abc6c..0x101acc50`) spanning `0x18000000..0x181ed400`. Editing them would
break its sort order, and nothing depends on those entries for correctness — the reader
memsets its contexts explicitly.

## Open item

`NEW` is not yet chosen. It needs `0x14a0` contiguous bytes of RAM that no other code
uses. Candidate approach: probe above the highest static symbol on the device (read-only
first), write a canary, exercise the reader and audio paths, and confirm it is untouched.

Related: [ebook-layout.md](ebook-layout.md), [firmware-extraction.md](firmware-extraction.md)
