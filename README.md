# Famue BF07 — Reverse Engineering Notes

Research notes on the **Famue BF07**, a 2.7" e-ink e-reader / audio player, with the
goal of modifying its ebook renderer (line spacing, reflow, hyphenation).

Everything here was derived from a live device over its debug UART plus static analysis
of publicly available files. No proprietary source was used.

> **Status: analysis complete, modification not yet achieved.**
> The firmware is fully understood and the exact bytes to change are known. What is
> missing is a working *write* path. See [docs/status.md](docs/status.md) for an honest
> account of what works, what doesn't, and what was ruled out.

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

## Key results

- **Full engineering shell** over UART at **2,000,000 baud**.
- **Byte-exact decrypted firmware dump** (1,966,080 B = the whole `fw0_sys` partition),
  verified 20/20 against the live device.
- **Exact patch offsets** for the text layout — wrap width, lines/page, word-break set.
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
