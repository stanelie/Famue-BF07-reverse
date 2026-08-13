# Famue BF07 — Reverse Engineering Notes

Research notes and working code for the **Famue BF07**, a 2.7" e-ink e-reader / audio
player. The goal was a better ebook reader; the result is a **replacement reader
injected into the vendor firmware**.

Everything here was derived from a live device over its debug UART plus static analysis
of publicly available files. No proprietary source was used, and no vendor firmware is
redistributed here — the tools read firmware from the user's own device.

> **Status: a replacement ebook reader is running on the device, and it is the reader.**
> It owns the file handle, the wrapping, the pagination, the input, the drawing and the
> progress display; the vendor's reader no longer decodes, draws or paginates. Twelve
> reflowed lines per page, glyph widths measured with the renderer's own font, page
> turns and a percent seek driven by touch input we read ourselves. Read/write/verify/
> restore over ADFU all work, and reverting to stock is byte-exact.
> See [docs/status.md](docs/status.md) for what is and isn't done, and
> [docs/dead-ends.md](docs/dead-ends.md) for what was ruled out.

## The device

| | |
|---|---|
| Product | Famue BF07 (board name `xlx_58120_bf07`, ODM "XLX") |
| SoC | Actions Technology, **LARK** family (ARM Cortex-M) |
| ADFU chip id | `0x2351` |
| OS | **Zephyr RTOS 2.7.0** |
| GUI | **LVGL v8** |
| Display | 176 × 264 e-ink |
| Flash | **4 MB SPI NOR**, Puya (JEDEC `0x85`), *inside the SoC package* |
| Firmware ver | `1.00_2506301055`, version_code `0x00010000` |

## Documents

| Doc | Contents |
|---|---|
| [docs/status.md](docs/status.md) | What works, what's blocked, what was ruled out |
| [docs/user-tool.md](docs/user-tool.md) | **Back up and patch your own device over USB** — no case, no soldering |
| [docs/flashing.md](docs/flashing.md) | **Backup, restore, patch** — the complete write path and its rules |
| [docs/reader-architecture.md](docs/reader-architecture.md) | The replacement reader: threading, memory, pre-render, reflow |
| [docs/reader-map.md](docs/reader-map.md) | Map of the vendor ebook app (lifecycle, scenes, pagination, input) |
| [docs/hardware.md](docs/hardware.md) | Board, UART, chip identification |
| [docs/debug-shell.md](docs/debug-shell.md) | The Zephyr shell: commands, quirks, traps |
| [docs/firmware-extraction.md](docs/firmware-extraction.md) | How to dump the decrypted firmware |
| [docs/ebook-layout.md](docs/ebook-layout.md) | **The original goal** — text layout internals and patch points |
| [docs/ota-format.md](docs/ota-format.md) | The `AOTA` OTA container, reverse engineered then verified |
| [docs/adfu-protocol.md](docs/adfu-protocol.md) | LARK ADFU USB protocol, recovered from the vendor tool |
| [docs/actions-formats.md](docs/actions-formats.md) | Actions `.fw` package formats and how to decrypt them |
| [docs/sdk.md](docs/sdk.md) | The official Actions LARK SDK (public!) and what it proves |
| [docs/dead-ends.md](docs/dead-ends.md) | Things that don't work, and why — read this first |

## Tools

| Tool | Purpose |
|---|---|
| [tools/extract_fw.py](tools/extract_fw.py) | Dump decrypted firmware over UART via `dbg mdw` |
| [tools/ota_tool.py](tools/ota_tool.py) | Build/verify `AOTA` OTA images |
| [tools/fwdis.py](tools/fwdis.py) | ARM Thumb-2 disassembler for the extracted image |
| [tools/disasm.py](tools/disasm.py) | Annotated disassembler, resolves `bl` targets against `symbols.txt` |
| [tools/extract_symbols.py](tools/extract_symbols.py) | Recovers 1267 function names from the firmware's own log calls |
| [tools/bf07.py](tools/bf07.py) | **User-facing**: backup / verify / restore / install, over USB alone |
| [tools/patchset.py](tools/patchset.py) | The reader's patch table: plaintext image in, patched sectors out |
| [tools/mkflash.py](tools/mkflash.py) | Builds a verifying sector flasher from a patch table |
| [tools/lark_cd.py](tools/lark_cd.py) | LARK ADFU host implementation (CBW framing, `cd` opcodes) |
| [tools/verify_repair.py](tools/verify_repair.py) | Audit every sector against the backup and rewrite what differs |
| [tools/xipdiff.py](tools/xipdiff.py) | Diff the **live** device against stock in plaintext, no ADFU needed |
| [tools/state.py](tools/state.py) | Dump the reader's state by name, offsets read from DWARF |
| [tools/screen.py](tools/screen.py) | Read the rendered page and check every line's fit |
| [tools/gestures.py](tools/gestures.py) | Read captured touch/gesture input from the device |
| [tools/devflash.sh](tools/devflash.sh) | Developer loop: build the reader, flash it, verify every sector |
| [tools/regdiff.py](tools/regdiff.py) | Diff SoC registers between a running device and ADFU |
| [tools/grid.py](tools/grid.py), [keypad.py](tools/keypad.py), [digits.py](tools/digits.py) | Touch/keypad capture used to map the soft keypad |
| [tools/recover.py](tools/recover.py), [adfu_reset.py](tools/adfu_reset.py), [cap.py](tools/cap.py) | Recovery, ADFU entry and UART capture helpers |
| [reader/](reader/) | The replacement ebook reader — C, built for XIP `0x101d3000` |

## Key results

- **A replacement ebook reader running on the device** — own wrapping, reflow,
  pagination and pre-render, injected into 53 KB of free space in the XIP partition.
- **Input taken from the touch driver**, not from the vendor's reader. The firmware
  dispatches input *above* LVGL, which is why probing the object tree found nothing for
  days; `_lvgl_pointer_put` (`0x100e07b4`) gives raw coordinates, and page turns, the
  keypad and the percent seek are all built on it.
- **Typography measured, not estimated** — glyph advances come from the renderer's own
  font (captured by hooking its glyph callback), so lines fill to the exact 168 px the
  labels are wide. Hyphenated compounds split at their hyphens.
- **The vendor's reader switched off in place** — its decode, layout, drawing and
  background paginator are all disabled, which made the render loop **3x faster**
  (304 ms -> 100 ms per tick) and removed a multi-minute scan per book open.
- **1267 firmware functions recovered by name**, because every function passes its own
  name to the logger. This turned static analysis from guesswork into map-reading.
- **Full read/write/verify/restore of flash over ADFU**, byte-exact in both directions.
- **Full engineering shell** over UART at **2,000,000 baud**.
- **Byte-exact decrypted firmware dump** (1,966,080 B = the whole `fw0_sys` partition),
  verified 20/20 against the live device.
- **Exact patch offsets** for the text layout — wrap width, lines/page, word-break set.
  (Constant-patching turned out to be a dead end for line count; see
  [docs/ebook-more-lines.md](docs/ebook-more-lines.md) for why, and what replaced it.)
- **OTA container format** reverse engineered by hand, then confirmed field-for-field
  against Actions' own `build_ota_image.py`.
- **LARK ADFU protocol** recovered from `HardwareEx.dll` — CBW framing, valid opcodes,
  and the exact CDB layouts.
- Discovery that the **official Actions LARK SDK is public**, which corroborated most of
  the above independently.

## Credits / prior art

- [ilyakurdyukov/actions_flash](https://github.com/ilyakurdyukov/actions_flash) — ADFU tool
  for ATJ2127/ATJ2157, plus a reverse-engineered ADFU payload in C.
- [nfd/atj2127decrypt](https://github.com/nfd/atj2127decrypt) — ATJ2127 firmware decryption
  and a clean Python ADFU implementation.
- [Rockbox `atjboottool`](https://github.com/Rockbox/rockbox/tree/master/utils/atj2137/atjboottool)
  — decrypts the older Actions FWU format.
- [lvgl/lv_port_actions_technology](https://github.com/lvgl/lv_port_actions_technology) —
  the official Actions LARK SDK.

## Licence

Notes and tools here: public domain / CC0. Third-party projects retain their own licences.
