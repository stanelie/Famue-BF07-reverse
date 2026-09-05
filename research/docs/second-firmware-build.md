# Supporting the second firmware build (May 27 2025)

At least two BF07 firmware builds ship. A unit bought in September 2026 came
with the **older** one, so a newer purchase says nothing about firmware version.

| build stamp | version | notes |
|---|---|---|
| `Jun 30 2025 10:51:24` | `1.00_2506301055` | what the reader and its patch were built against |
| `May 27 2025 13:30:26` | — | shipped on a later-purchased unit |

They have an **identical string set** (13,150) but 465 of 480 `fw0_sys` sectors
differ: the same source, separately compiled and linked. That is the worst case
for a patch — it looks like the same firmware and behaves like different one.
Installing the Jun 30 patch on a May 27 device hangs it before USB comes up,
recoverable only by shorting TX/RX with the case open. Measured, once, the
expensive way. `bf07.py install --patch` now refuses this outright.

## The shift is not one constant

| region | delta |
|---|---|
| ebook module (`0x10049xxx`–`0x1004cxxx`) | **0** — does not move |
| driver / library area (`0x1007f`, `0x100a0`, `0x100d9`, `0x100e0-100f9`) | **-0x24** |
| data tables (`0x10128`, `0x10129`, `0x1012c`) | **-0x20** |

So every site must be located independently. `tools/retarget.py` does this and
refuses to guess: an ambiguous or missing match is a hard failure, because a
hook written to the wrong address is exactly the brick being avoided.

## Two traps in locating sites

**Branches encode their own target.** A `bl`/`b.w` differs between builds even
where nothing moved, because the callee moved. A first attempt reported "the
surrounding code changed" for six sites that had not moved at all — the context
merely contained calls. Thumb-2 32-bit branch encodings are masked out of every
comparison.

**Function-start hooks have no usable context before them.** Several hooks sit
on a function's first instruction, where the preceding bytes are the tail of
whatever the linker happened to place before — unrelated code that moves
independently. Those need a body-only window.

**Data structures have the same problem as branches**, one level up: an LVGL
class struct is full of pointers that moved, so its bytes differ. Comparing
with pointer-shaped words masked out resolves them.

## Relocating the hooks is NOT enough

The reader calls ~20 vendor functions, declared in `reader/include/fw.h`, and
**the compiler materialises each as a `movw`/`movt` immediate pair** — 70 such
references in `reader.bin`, reached by 96 register-indirect calls. Those
addresses are baked into the binary. A Jun 30 reader on May 27 firmware would
call every one of them 0x24 bytes off, landing mid-instruction.

**A second build therefore needs the reader RECOMPILED, not just re-hooked.**

### And recompiling against a new fw.h is STILL not enough

The first May 27 build did exactly that — every `fw.h` address relocated and
verified, zero stale references in the binary — and **boot-looped every 9
seconds**. `main.c` also carries ten `movw`/`movt` pairs *inside inline-asm
string literals*: the trampolines that jump back into the vendor function after
a hook runs. **Seven of the ten still held Jun 30 addresses**, so every hook
returned into unrelated code.

They were missed because the dependency audit enumerated what the reader
**declares** (`fw.h`), not everything it **encodes**. An address split across
two 16-bit halves inside an asm string is invisible both to a header rewrite
and to a search for 32-bit literals in the built binary — searching
`reader.bin` for the literal address of a known dependency returns *zero
occurrences*, which looks like proof of absence and is not.

`tools/mkfwh.py --source/--source-out` now emits a relocated copy of the C
source alongside the header.

### One thing that did NOT need relocating

The reader places its `.bss` inside the vendor page context at RAM
`0x18018a4c + 0x2c`. That is a RAM address, so none of the XIP tooling checks
it — but `0x18018a4c` is referenced from the same two code sites
(`0x100493c0`, `0x1004a110`) with the same value in both builds, so the
placement holds. Worth re-checking for any future build: overwriting that
struct's header once destroyed a live kernel mutex and faulted far from the
damage.

## Complete map (Jun 30 -> May 27)

Hook sites, from `tools/retarget.py`:

| site | Jun 30 | May 27 | delta |
|---|---|---|---|
| `hook` | `0x1004A288` | `0x1004A288` | 0 |
| `prepare_hook` | `0x1004C002` | `0x1004C002` | 0 |
| `tail_hook` | `0x100493B2` | `0x100493B2` | 0 |
| `pointer_hook` | `0x100E07B4` | `0x100E0790` | -0x24 |
| `font_hook` | `0x100E1348` | `0x100E1324` | -0x24 |
| `gesture_hook` | `0x100D92E8` | `0x100D92C4` | -0x24 |
| `fontopen_hook` | `0x100E1440` | `0x100E141C` | -0x24 |
| `menulist_hook` | `0x1005934C` | `0x10059328` | -0x24 |
| `WORD` (font menu row) | `0x10128E98` | `0x10128E78` | -0x20 |
| `CONT_Y` | `0x1004A1FC` | `0x1004A1FC` | 0 |
| `CONT_SUB` | `0x1004A222` | `0x1004A222` | 0 |

`fw.h` dependencies: **all 21 code entries move -0x24** except `fw_wrap_line`
(`0x10049075`, in the unmoved ebook module). Both class pointers move -0x20:
`LV_CLASS_OUR_LINES` `0x10129B54`→`0x10129B34`, `LV_CLASS_COUNTER_IN`
`0x1012C7F0`→`0x1012C7D0`.

The reader's 13-sector space (`0x1e7000`–`0x1f4000`) is **fully erased on the
May 27 build too**, so it fits.

## What remains

1. Generate a May 27 `fw.h` from the map above.
2. Rebuild with the pinned `clang-15` (version is load-bearing: `-O2` fills
   99.5% of the 52 KB budget; 18 overflows).
3. Build the patch with the retargeted hook sites, `--ref-cipher` from
   `firmware/stock-full-flash-2025-05-27.bin`.
4. Install on the May 27 unit and confirm it boots and pages turn.

**Done — the May 27 reader boots and works on hardware.** Clean boot log, no
fault or assertion, through to `app_manager_init_app`, and the reader renders
and pages correctly.

Build it with:

```
tools/mkfwh.py -r <jun30 plain> -t <may27 plain> \
    -i reader/include/fw.h      -o reader/include-may27/fw.h \
    --source reader/src/main.c  --source-out reader/src-may27/main.c
clang-15 ... -Iinclude-may27 -Iinclude -c src-may27/main.c -o src-may27/main.o
arm-none-eabi-ld -T link.ld src-may27/main.o -o reader-may27.elf
arm-none-eabi-objcopy -O binary reader-may27.elf reader-may27.bin
tools/mkpatch.py -p <may27 plain> --ref-cipher firmware/stock-full-flash-2025-05-27.bin \
    -o reference/reader-patch-may27.bin
```

`patchset.py`'s `BUILDS` table keys the hook sites AND the matching reader
binary on the decrypted image's sha256, so the two cannot be mismatched by
hand; an unknown image is a hard failure, not a fallback.
