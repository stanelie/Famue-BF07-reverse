# Where things stand

A snapshot for picking this up later, or handing it to someone else.

## The reader is finished as a reading experience

It owns every part of the path: the file handle, wrapping and reflow,
pagination as byte extents, glyph widths measured from the renderer's own font,
hyphenation, touch input, drawing, the progress display, a percent seek, and
position persistence across power cycles. The vendor's reader contributes
nothing — its decode is deliberately kept failing, its paginator is off, and its
line counter is ignored.

See [reader-architecture.md](reader-architecture.md) for how each piece works
and, more usefully, for the failures that shaped it.

## Everything works over USB

| task | needs |
|---|---|
| back up / verify / restore | ADFU |
| install the reader | ADFU (`bf07.py install --patch`) |
| dump DECRYPTED firmware | ADFU (`usb_plaindump.py`) |
| build a patch | a decrypted dump — which you can now take over USB |
| **live logs from a running device** | **still the UART** |

The last row is the only device-side gap. Logs need the firmware *running*, so
ADFU cannot serve them; it would take a vendor SCSI command added to our reader
(unused opcodes exist in the dispatcher at `~0x100e3400`).

**macOS cannot send the ADFU switch** — the kernel owns the device's only USB
interface. Linux and Windows can. On macOS a serial cable is still the way *in*,
even though nothing after that needs it. **Developing on Linux removes the last
cable.**

## The distribution model

`install --patch` needs a patch file carrying our reader plus **256 bytes** of
stock vendor context at the hook sites. That 256 bytes is
firmware-version-specific, not device-specific, and stays 256 bytes however
large the reader grows. Everything else comes from the device itself: untouched
blocks are its own ciphertext rewritten verbatim, and our blocks are encrypted
by its own key on write. **Nothing depends on the flash key being shared between
units.**

## What is deliberately not in this repo

- vendor firmware images (yours, from your own device)
- the ADFU payload (`adfus_u_go.bin`) — from Actions' public SDK
- boot ROM dumps under `reference/rom/` — silicon code, kept local

`.gitignore` excludes `*.bin`, which covers all three.

## Hard-won facts worth not re-learning

- **The flash cipher is live in ADFU**, key loaded. Earlier notes claiming
  otherwise were wrong; every "keyless" result was a stale cache line, because
  ADFU is entered by a *warm* reboot and decrypted lines survive it.
- `SPICACHE_CTL` silently ignores writes until `RMU_MRCR0` bit 8 is out of reset.
- The cache mapping address must be **4 KB aligned**; a 2 KB chunk size yields a
  dump that is 49% correct, which looks like corruption but is a rejected mapping.
- **`rm` dereferences the address given.** Reading below `0x1000` faults the CPU
  *and* the USB stack — only a physical reset clears it.
- Re-uploading the ADFU payload while one is running wedges ADFU. Probe first.
- Our `fw_log` output never reaches the UART. Any "listened and heard nothing"
  measurement made with it proves nothing.
- Verify **content**, not the changed/unchanged pattern. A wrong block passes a
  pattern check, and one did — it bricked the boot until restored.

## Natural next steps

1. **Move development to Linux** — removes the last cable.
2. **A vendor SCSI command in the reader** for live memory reads and log
   streaming over USB. This is the remaining half of a fully USB workflow.
3. Faster page turns, if the e-ink refresh can be driven in partial-update mode.
