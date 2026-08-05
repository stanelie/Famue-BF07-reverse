# Dead ends

Documented so nobody repeats them. Several of these looked promising for a long time.

## `dbg dumpbuf` silently writes zeros

Reports `done dumping to …` and produces an all-zero file for the XIP region. Cost us a
full extraction cycle before we checked the output. Use `dbg mdw` instead.

## ADFU mode cannot see the real firmware

ADFU runs *before* clock/memory/XIP init, so it's a different address space:

| address | via `dbg mdw` (booted) | via ADFU `read_mem` |
|---|---|---|
| `0x10000000` | real decrypted code | all zeros |
| `0x18010e00` | live RAM | uninitialised fill |

ADFU is **not** a shortcut to dumping firmware.

## ADFU's software `reset` doesn't work

`actions_dump reset` (SCSI opcode `0xb0`) produces no UART output and no re-enumeration.
Recovery from ADFU requires the physical reset button. **Never enter ADFU without
physical access to the device.**

## There is no hardware ADFU button

Every button and combination was tried while applying USB power, across two sessions —
always normal boot (`10d6:b00b`), never `10d6:10d6`.

The SDK explains it: `check_adfu()` supports **two** mechanisms —
`CONFIG_TXRX_ADFU` (serial-line based) and `CONFIG_GPIO_ADFU` (a button). This board
evidently isn't built with the button variant. `zephyr/tools/jlink_script/uart/uart_adfu.txt`
contains literally `dbg reboot adfu` — the UART shell command *is* the documented method.

## SD-card OTA never runs on this board

The bootloader probes storage device `sd` (MMC_0); the microSD is on `SD1` (MMC_1):

```
!!!ERR: dev sd not found        sdfs cannot found device sd
main I: upgrade not allowed
main I: REBOOT_TYPE_GOTO_SYSTEM
```

The OTA backend looks for `/SD:/ota.bin`, and `/SD:` has no mount (`fs ls` shows only
`SD1:`, `NOR:K`, `SD1:C`). A probe `ota.bin` placed on the card was never even examined —
no "found" *or* "cannot found" line appears. The mount is a Kconfig choice
(`CONFIG_APP_FAT_DISK` in `ota_backend_sdcard.c`).

NVRAM `SD_OTA_FLAG`, `REC_OTA_FLAG`, `OTA_BP` do not exist. `dbg reboot` accepts only
`adfu`/`jtag`.

## Forcing `REBOOT_TYPE_GOTO_OTA` via RTC register — didn't work

`sys_pm_reboot()` is just two register writes, and both registers are reachable and
*verified correct* on LARK:

```
dbg mww 0x4000c03c 0x42520700   # RTC_REMAIN3 = magic 'RB' | REBOOT_TYPE_GOTO_OTA
dbg mww 0x4000c020 0x5f         # WD_CTL — watchdog reset
```

Readback confirmed `0x42520700`, and the watchdog did reset the device — but mbrec still
reported `upgrade not allowed` / `REBOOT_TYPE_GOTO_SYSTEM`, identical to a normal boot.

Reason: `soc_boot_get_reboot_reason()` doesn't read `RTC_REMAIN3` directly — it reads a
`boot_info_t` populated by the **boot ROM**, which consumes the register first. Also our
mbrec is a customised **Aug 2022** build predating the SDK source.

Useful by-product: **`dbg mww` works**, and the leopard register map applies to LARK
(verified by write/readback/restore on `RTC_REMAIN3`).

## `actions_flash` cannot drive LARK

Payload uploads fine, then every command hangs. Full explanation in
[adfu-protocol.md](adfu-protocol.md). Not a wiring or USB problem — the transport works
(`adfu_info` replies from the boot ROM).

## The flash encryption is not a simple XOR

Raw `dbg fread spi_flash` returns ciphertext; XIP returns plaintext. XORing known
plaintext against ciphertext gives no periodic keystream at **any** alignment
(brute-forced over 0x8000 offsets). It's a real cipher.

Fortunately this turned out not to matter: `<enable_encryption>` is a per-partition
**build flag** in the SDK's `firmware.xml`, applied by the PC tools via `encrypt.bin`.
Shipped images can be plaintext — the D53's `sdfs_c.bin` is.

## Sample firmwares from other devices are the wrong platform

Five `.fw` packages (MECHEN D53, 2× Oilsky, XLX D53/M15) all decrypt with `atjboottool`,
but all are the **classic Actions** architecture:

| | those | BF07 |
|---|---|---|
| OS | classic (`.ap`/`.al`/`.drv`, `KER_TEXT.BIN`) | Zephyr + LVGL |
| storage | NAND (or NOR on the speakers) | SPI NOR |
| type | `US215A` / `US212A` | LARK |

Their `ADFUS.BIN` / `fwsc` / `brec` blobs are platform- and storage-specific and are
**not safe to load on a LARK device**.

## `ACTSFWFMT001` is not firmware

The inner `upgrade.fw` in XLX `ACTTEST0` packages has magic `ACTSFWFMT001` and is *not*
a firmware image — it's the **PC production tool** packaged as a FAT32 volume. Cracking
it gains nothing.

Also: an earlier claim that "there is no FAT table" was **wrong** — the FAT32 table is at
`0x4010` (searched for in the wrong region originally). But extracting properly via
cluster chains gives byte-identical results to a naive contiguous read, because the
tool's `.PYD` modules and its `ADFUS.BIN` are **themselves encrypted on disk**
(the tool ships `SdkCrypt.dll`). Only `LAUNCH.PYO` and `UPGRADE.PYD` are in the clear.

That 48,192-byte `ADFUS.BIN` is a *different, encrypted* artefact — the genuine plaintext
one from an FWU SQLite DB is 4,980 bytes, and the official LARK one is 47,608 bytes.

## No external flash chip to clip onto

The 4 MB Puya SPI NOR is **inside the SoC package**. The only two 8-pin parts on the
board are an **AiP4890** audio amplifier (corroborated by NVRAM `PA_OUTPUT_GPIO = 54`)
and an **RDA5807M** FM tuner (corroborated by `device list` showing `FM (READY)` and
NVRAM `APP_RADIO_INFO`).

A CH341A + SOIC-8 clip **cannot help here**. Don't buy one for this device.

## `SdkCrypt.dll` is not the decryptor (and the .PYDs may not be encrypted)

Assumed from its *filename* that `SdkCrypt.dll` decrypts the tool's Python modules. That
was never evidenced, and checking it:

- **No other file in the tool imports it** — no static linkage anywhere.
- Export directory present but pefile resolves **no named symbols** (ordinal-only or malformed).
- **No readable strings at all** — it is packed.
- Imports only MSVCRT/KERNEL32/MSVCP60/ole32 — **no crypto APIs**.

Separately, the premise that the `.PYD` modules are encrypted is also shaky. The FAT32
chains are provably correct:

```
COMMONEX.PYD  clusters 40..69 EOC   (30 clusters = 122880 >= 121856 bytes)  contiguous
UPGRADE.PYD   clusters  4..39 EOC   (36 clusters)
```

So the extraction is right, and the modules really do not begin with `MZ`. But their first
bytes decode as valid x86 — `8b 15 a4 9e 01 10` = `mov edx,[0x10019ea4]`, referencing an
imagebase-`0x10000000` address. They look like **headerless / transformed PE images**, not
ciphertext. Note `UPGRADE.PYD` *does* start with a valid `MZ`, and the `MZ` found at
cluster 35 lies inside `UPGRADE.PYD`'s own chain (an embedded PE), so the layout is
self-consistent.

Reconstructing loadable modules from these would mean rebuilding PE headers — possible in
principle, but a large effort with no guarantee the ADFU call sequence is even in the
module we pick.

## `cmd 0x10` vs `cmd 0x11` — untestable against the boot ROM

Both `CDB[0]=9` (cmd `0x10`) and `CDB[0]=8` (cmd `0x11`), with sector *and* byte addresses,
return CSW **status 2** from the boot ROM. The revised memory-vs-storage reading in
[adfu-protocol.md](adfu-protocol.md) is therefore neither confirmed nor refuted — the ROM
rejects that whole command class until the payload runs.


## Every hand-built ADFU command, because the opcode was in the wrong byte

Superseded by the live capture (2026-08-05). Every probe in this repo's history
placed the opcode in `CDB[0]`. The boot ROM wants a constant `0xCD` there, with
the opcode in `CDB[1]`. Status `2` was the correct answer to a genuinely
malformed command, every single time.

The `CCommUSB` reverse engineering was not wrong about the opcode *values*
(`0x10`, `0x20`, `0x13`…) — it was wrong about where they go. Reading exported
dispatch tables told us what the numbers were but not the wire layout, and
nothing in the DLL made the `0xCD` prefix visible at the level we were reading.

**Lesson:** four sessions of protocol inference lost to a field-offset
assumption that a single capture settled in under a minute. When a protocol has
a reachable implementation, capture before inferring.
