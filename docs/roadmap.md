# Where this project stands, and the routes forward

Written 2026-08-06, after the first successful firmware modifications.

## What is proven

| capability | status | evidence |
|---|---|---|
| Enter ADFU over USB only | ✅ | `adfu_enter_usb.py`, ~1 s |
| Enter ADFU over serial | ✅ | `dbg reboot adfu` |
| Escape a crash loop into ADFU | ✅ | hammering `dbg reboot adfu` during boot cycles |
| Dump 4 MB raw flash | ✅ | 5.9 s, byte-identical to an independent UART dump |
| Read the decrypted image | ✅ | `fw_code_full.bin` (XIP view) |
| Erase a 4 KB sector | ✅ | granularity measured, not assumed |
| **Write patched code** | ✅ | plaintext + `addr \| (1<<31)`, 32-byte writes |
| Verify a write byte-exactly | ✅ | ECB no-tweak: unchanged blocks must re-encrypt identically |
| Revert exactly | ✅ | zero differing blocks vs backup, twice |
| Observe a change on screen | ✅ | 8 → 7 → 8 lines, then 10, then 12 |

**Not yet done:** writing the whole `fw0_sys` partition (only individual
sectors), and any recovery from firmware that fails *before* the shell starts.

## Hard constraints discovered

* Writes must be **32-byte transactions**. A 4096-byte burst with bit 31
  encrypts differently — the engine carries per-transaction state.
* Erase granularity is **4 KB** — patching means read-modify-erase-write of the
  containing sector.
* Command acks are **4 bytes**; waiting for more costs a USB timeout each.
* `is` mode 0 must be sent after `cd 20` to bind NOR.
* The only recovery from a bad write requires the **shell to start**. Serial
  must stay wired during firmware work.

## Route A — patch constants (works today, limited reach)

Proven end to end. Good for anything that is genuinely just a number and is not
an allocation bound. Wrap width (`0x4903e`) is the remaining safe target.

Limitation: most interesting behaviour is not a constant. Lines-per-page looked
like one and was an array bound.

> **Update:** Route B was taken and works. A replacement reader now runs from the
> padding at `0x1e7000`. Routes A, C and D below are kept for the reasoning; the
> current state is in [reader-architecture.md](reader-architecture.md) and the write
> path in [flashing.md](flashing.md).

## Route B — new code in free space (TAKEN — this is what runs today)

`fw0_sys` has **53 KB of unused `0xFF` padding at `0x1e7000`–`0x1f4000`** —
inside the XIP-mapped, hardware-encrypted partition, so code written there
executes like any other firmware code.

That is enough room for a real reimplementation of line breaking, hyphenation,
and layout. The method is standard binary patching:

1. write new routines into the padding (plaintext + bit 31, as usual)
2. redirect an existing call site to them with a patched `bl`
3. the replacement can allocate its own structures, so it is **not** bound by
   the existing 8-entry line array

This avoids the two things that block Route A: fixed array sizes and the
inability to add logic. It needs an ARM cross-compiler and care with the ABI,
but no new capability from the device.

## Route C — reverse the whole app to source

Not recommended. `fw0_sys` is a single 1.875 MB Zephyr image containing every
app (music, radio, settings, recorder, ebook…), not a separable binary. There is
no supported way to rebuild just the reader and relink it, and we would have to
reproduce the vendor's exact toolchain and configuration to rebuild the image.
Route B gets the same result for a fraction of the work.

## Route D — load the reader from SD at runtime

Attractive (edit a file on the card, no flashing) but it needs work the device
does not currently do:

* Code **cannot execute in place from SD** — the card is a block device, not
  memory-mapped. Only NOR is XIP.
* So a loader must copy the code into RAM and call it. No such loader exists;
  one would have to be written and installed via Route B.
* The code would need to be position-independent or built for a fixed RAM
  address, and RAM is far smaller than the 1.875 MB image (the ADFU payload
  region at `0x01010000` is the known-free area, and it is tens of KB).

**Verdict:** feasible for a *component* the size of a reader view, not for the
whole app. It is Route B plus a loader — worth doing only after Route B works,
and its real payoff is iteration speed (no flash write per change).

## Done

1. ~~Wrap width via Route A~~ — done, then superseded by owning layout outright.
2. ~~Route B toolchain~~ — done: C compiled for XIP, `bl` redirection, verified flashing.
3. ~~Reimplement line layout~~ — done: own wrapping, reflow, pagination, pre-render,
   back-paging, 12 lines unbound by the vendor's 8-line array.

## Next

1. **Exact glyph metrics** — call `bitmap_font_get_glyph_dsc` (`0x100decbc`) instead of
   estimating character widths. Fixes the occasional one-character overshoot and lets
   lines fill confidently to the margin.
2. **Hyphenation** — the original goal, and now trivially ours to implement: nothing in
   the firmware does it, and we own the wrapper.
3. **User-facing backup/restore/patch tool** — spec in [flashing.md](flashing.md).
4. **Font size / line count as user settings**, rather than compile-time constants.
5. Optionally the SD loader (Route D) for fast iteration without a flash write.

## For a user-facing tool

The architecture the user described is sound and avoids redistributing vendor
code: the tool dumps firmware **from the user's own device**, applies patches
locally, and writes back. Nothing copyrighted needs shipping. Required pieces
already exist here: `adfu_enter_usb.py`, `lark_cd.py`, `lark_adfu_u.py`, plus
the 32-byte/bit-31 write rule and the re-encrypt-and-compare verification.
