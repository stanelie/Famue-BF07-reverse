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
