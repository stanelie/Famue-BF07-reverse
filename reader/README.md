# Replacement ebook reader (Route A: injected into the vendor firmware)

The vendor reader cannot be made to show more lines per page cleanly, because a clean
page turn needs `visible == decode granularity`, and raising the decode granularity means
staying consistent across three modules **and** a pagination state machine whose loop
terminates on an exact equality involving the divisor. See
[../docs/ebook-more-lines.md](../docs/ebook-more-lines.md).

So: keep the vendor firmware (music, settings, USB all keep working) and replace only the
reading scene with our own code, injected into free flash.

## Why this is viable

- **Free flash**: `0x1e7000..0x1f4000`, 52 KB, erased. XIP `0x101d3000`.
- **Code injection works** and is proven on hardware.
- **A toolchain exists**: Apple clang emits Thumb-2, GNU binutils links and extracts.
- **Replacing the reader frees its RAM.** The four static page contexts
  (`0x18018a4c` + `0x18019098..0x18019bfc`, `0xf30` bytes) are known-good SRAM at fixed
  addresses that we no longer need for the vendor's scheme. This dissolves the RAM
  shortage that killed the relocation attempts.
- **No pagination state machine.** We keep our own byte offset, so nothing has to agree
  with `ebook_calculate_pages`.

## Build

```
make            # -> reader.bin, a flat image linked at 0x101d3000
```

## Milestones (each verified on hardware before the next)

1. **Compiled C runs in context** — hook logs from inside the reader.
2. Draw one label from our own code.
3. Draw N labels from a fixed string, our own geometry.
4. Read the book file and wrap text ourselves.
5. Key handling and position tracking.
6. Persist the reading position.

## Rules

- Every `fw.h` entry is marked `[OBSERVED]` or `[INFERRED]`. A wrong signature crashes
  rather than misbehaves, so confirm before relying on an inferred one.
- Measure on the device before theorising. See
  [../docs/ebook-more-lines.md](../docs/ebook-more-lines.md) for the instrumentation:
  `global 0x18018978 -> [+0x3c] -> reading position at [+0x194]`, and the LVGL object
  coords at `+0x14`/`+0x18`.
- Keep a byte-exact revert staged at all times.

## OPEN BUG: scheduler assertion on book open (as of 2026-08-07)

Replacing a book file with a different one (same name) makes the injected reader
crash on open. **Stock firmware opens the same file fine**, so this is ours.

```
ASSERTION FAIL [thread->base.pended_on] @ zephyr/kernel/sched.c:595
ZEPHYR FATAL ERROR 4: Kernel ...
Current thread: 0x18010e00 (ebook)
[<1004bd89>] ebook_calculate_pages+0x1d  <-  ebook_reading+0x125
```

### Ruled out by measurement, not reasoning

- **Not the file.** Fully stock firmware opens it without complaint.
- **Not the 13-line / geometry change.** Reverting to the known-good 12-line
  layout still crashes.
- **Not our file I/O.** `repaginate`/`book_read` never log before the crash.
- **Not the standalone-context pointer.** Nothing in the ebook region reads
  `[rX,#0x18c]`; the only ebook-region access is the write at `0x10049fc2`.
- **Not a lock-pairing mistake.** `0x100fd8b8`/`0x100fd8c2` are not locks at all:
  they set and clear bit 0 of `[r0+0x14]`. We call them exactly as the original
  did.

### The remaining hypothesis

`ebook_calculate_pages` runs on the **`ebook` thread**, and the assertion is
scheduler state corruption (a thread unpended while `pended_on` is NULL).

Our `after_render()` calls LVGL (`lv_obj_child_cnt`, `lv_obj_get_child`,
`lv_label_set_text`) **after** `0x1004922c` has returned -- and that function
takes a semaphore (`0x100f12a0` with K_FOREVER) around its own LVGL work and
releases it (`0x100eb2fc`) before returning. So we touch LVGL **unprotected**.
With the old file the timing happened to be benign; the new file changes it.

If that is right, the fix is to hold the same semaphore around our drawing
rather than drawing after it is released. That needs the locking discipline
confirmed first -- both previous assumptions about these helpers were wrong, so
this one gets verified before any more flashing.

### State

Device is on **fully stock firmware** and working. All reader work is committed;
nothing is lost.
