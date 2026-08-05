# Status — what works, what's blocked

Written to be honest rather than encouraging. If you're picking this up, read this and
[dead-ends.md](dead-ends.md) before anything else.

## Achieved

| | |
|---|---|
| Engineering shell over UART (2,000,000 baud) | ✅ |
| Decrypted `fw0_sys` dump, byte-exact, verified 20/20 | ✅ |
| Text-layout internals located, exact patch offsets | ✅ |
| OTA container format (verified against vendor tool) | ✅ |
| OTA image builder + validator | ✅ |
| ADFU entry (`dbg reboot adfu`) | ✅ |
| Official LARK `adfus.bin` payload obtained | ✅ |
| LARK ADFU USB protocol (framing, opcodes, CDB layouts) | ✅ |
| Register write primitive (`dbg mww`) | ✅ |
| LARK ADFU host implementation (CBW/CSW verified live) | ✅ |
| Payload upload via `write_mem` | ✅ |
| **Starting the payload (handoff)** | ⚠️ decoded, untested |
| **Reading flash over ADFU** | ⚠️ blocked on handoff |
| **Writing anything to flash** | ❌ |

## The blocker — identified 2026-08-05

**`CDB[0]` is a constant escape byte `0xCD`; the opcode goes in `CDB[1]`.**
Every command this project ever sent put the opcode in `CDB[0]`, so the ROM
saw an unknown opcode and answered CSW status `2` — correctly. The handover is
`cd 20` (execute at address). Full decode in
[adfu-protocol.md](adfu-protocol.md#solved--the-real-boot-rom-protocol-from-a-live-capture).

Recovered by capture, not analysis: `tools/adfu-mock` put a Raspberry Pi 4 in
USB gadget mode as `10d6:10d6` and let the Windows tool talk to it. The BF07
was never connected.

**Not yet tried against the BF07.** `tools/lark_cd.py` implements the corrected
protocol. The capture was of the classic-ATJ path (`0x118000`, ~5 KB probe);
LARK loads 47,608 bytes at `0x01010000`, so the framing should carry over but
the addresses and payload do not.

## Historical: what the blocker looked like before

**Starting the uploaded payload.** Everything either side of it works.

`tools/lark_adfu.py` implements the recovered protocol and **provably talks to the
device** — it sends a valid CBW and receives a well-formed CSW. The boot ROM answers
with status `2` (unsupported opcode) for flash commands, which is correct behaviour:
flash access requires `adfus.bin` to be *running*.

Uploading the payload works (`actions_dump write_mem 0x118000 0 0 adfus.bin`).
Starting it does not — `actions_flash`'s `switch` sends the ATJ2157 command, which LARK
ignores; the device simply leaves ADFU and boots normally (reproduced twice).

The correct handoff has been decoded from the vendor DLL —
`CallingEntry` = `CDB[0] 0x20`, `Switch` = `CDB[0] 0x10`, each with a 2-byte parameter —
but **has not yet been tried**. That single command is what stands between here and a
full 4 MB dump.

Validate any dump against `dbg fread spi_flash <off>`; a ground-truth capture helper is
in `tools/lark_adfu.py`.

## Risk situation

There is **no external flash chip** — the 4 MB SPI NOR is inside the SoC package, so
there is no clip-on recovery. That makes the following non-negotiable:

1. **Get a full flash dump before writing anything.**
2. **Only ever write `fw0_sys`** (`0x14000`, length `0x1E0000`). Never `fw0_boot`/mbrec
   at `0x0` — the bootloader is what provides the recovery fallbacks below.

Mitigating factors, from the SDK's `bootloader/application/ota_app/src/main.c`:

```c
if (partition_valid_check())             goto exit_to_ota;   /* bad partition -> OTA */
if (reboot_type == REBOOT_TYPE_GOTO_OTA) goto exit_to_ota;
exit_to_ota:
    if (ota_main()) sys_pm_reboot(REBOOT_TYPE_GOTO_ADFU);    /* OTA fails -> ADFU */
```

So a corrupt `fw0_sys` routes to OTA, and a failed OTA reboots to ADFU. This is *source
for the SDK's bootloader*, not necessarily the exact mbrec on the device (ours is a
customised **Aug 2022** build) — treat it as likely, not guaranteed.

## If you only do one thing

Implement `ADFURead(cmd=0x11)` against LARK and dump the flash. Everything else in this
repo is already done and waiting on it.

---

# 2026-08-05 session: ADFU cracked, plus a second write path found

## ADFU

The `0xCD` framing works on hardware. `cd 23` returns CSW 0 with data, `cd 13`
uploads, and `cd 20` is a **working handover** — the payload demonstrably runs:

```
Ver1.1-adfu (build Apr 24 2023 15:17:38)
system_set_svcc: 0x02680be4
WIO0_CTL: 0x00000000
WAKE_CTL_SVCC: 0x00001157
[D] adfus run
<output corrupts here>
```

Two things previously recorded here were wrong and are corrected in
[adfu-protocol.md](adfu-protocol.md): the UART was never "held low" (wrong baud
— the payload uses 115200, the shell uses 2,000,000), and the payload does not
hang (`b .` in `main()` is by design; USB is interrupt-driven).

**Remaining gap:** the payload's CBW ISR is never installed, so it never
answers USB. The only handler reachable is the stub at `0x010125dc`, which
exists to print `'Adfus_Irq(%x) - usb_receive_cbw_isr - null'`. Output corrupts
right after `adfus run` because something in `storage_bind()` changes the clock,
shifting the effective baud (the image has exactly one `uart_init` call site and
one baud immediate, so nothing reprograms the UART).

## A second, possibly easier write path

The **running Zephyr firmware** can erase and write its own NOR:

```
'snort'  'nor test : snort address size'
'nor write speed test, size=%d kb, offset=0x%x'
'flash erase fail'   'erase use %d ms, erase speed=%d kb/s'
'flash write fail'   '--cmp write and read---'
```

`dbg snort <address> <size>` erases, writes and verifies. It writes a generated
test pattern rather than arbitrary data, so it is not directly a firmware
writer — but it proves erase+write works from the shell with no ADFU at all.

The application also carries a full OTA stack, including
**`ota_storage_erase_spinor`** — its OTA writes the SPI NOR:

```
ota_upgrade_init / ota_upgrade_attach_backend / ota_do_upgrade
ota_storage_erase_spinor / ota_storage_write_default
ota_backend_sdcard_{init,open,read,close,ioctl}
"found ota file '%s' in sdcard" / "cannot found ota file '%s' in sdcard"
```

Only the **sdcard** backend is compiled in, and the path is hardcoded:

```
0x1016f084  '/SD:/ota.bin'
```

Our card mounts as `/SD1:`. Note the firmware carries `/SD:` *and* `/SD1:`
variants of many other paths (`RECORDER`, `FMRECORDER`, `XWdict.lib`) but only
a `/SD:` form for `ota.bin`. The XLX sibling firmwares are named 双卡
("dual card"), so `/SD:` may be an internal slot — worth checking whether this
board has a second, unpopulated card position.

**This is a distinct and possibly shorter route to a write path than finishing
the ADFU payload**, and unlike ADFU it needs no payload reverse engineering —
only a correctly built `ota.bin` (we already have `tools/ota_tool.py`) and a
way to make the app see it.

## …but the running-firmware paths CANNOT rewrite fw0_sys

Followed up on `snort` and the OTA stack. Both are dead ends for our actual
goal (patching `fw0_sys`), for a fundamental reason:

**`fw0_sys` is the XIP partition the firmware executes from.** You cannot erase
the flash you are fetching instructions from — and the firmware's own OTA code
knows it:

```
0x1018dc85  '%s%s: update file_id %d: storage %d is xip, skip erase'
```

`ota_partition_erase_part` explicitly **skips XIP partitions**. So even a
correctly-built `ota.bin` on the right card would not rewrite the code partition.

The `snort` handler makes this concrete and dangerous rather than useful:

- Its write function `malloc`s a 512-byte buffer and writes it **uninitialised**
  in a loop — it is a speed benchmark, not a data writer. `mww`+`snort` cannot
  inject controlled bytes.
- **Bare `snort` (no args) targets `offset=0x20000, size=0xe0000`** — inside
  `fw0_sys`. Running it would erase 896 KB of the live, executing firmware and
  brick the session until reset. **Do not run `snort` without arguments.**

The firmware *does* contain a working Zephyr `spi_flash` write primitive
(`flash_dev->api->write(dev, off, buf, len)`, reached at `0x10077b0e`), but:
(a) there is no shell command to call an arbitrary function with arguments, and
(b) it could not write the XIP partition anyway.

### Conclusion: ADFU is not merely easier — it is the only path

ADFU's `adfus.bin` runs **from RAM at `0x01010000`**, so it can erase and
rewrite `fw0_sys` while nothing executes from XIP. That is precisely why the
platform has ADFU. The entire ADFU effort is therefore on the critical path,
and the remaining sub-problem is exact and bounded:

> **make the uploaded LARK payload install its CBW interrupt handler and
> service USB**, instead of falling through to the `usb_receive_cbw_isr - null`
> stub.

## Correction: full flash dump needs no ADFU at all

`dbg fread spi_flash <offset>` reads **raw** flash 512 bytes at a time straight
from the shell, at ~5 blocks/s — a full 4 MB in roughly 25 minutes. Validated
against ground truth at `0x0` and `0x1000`: byte-exact.

`tools/dump_flash.py` does this, resumably (it journals completed blocks to
`<out>.state`). Two parsing traps, both hit and fixed:

- The device interleaves unrelated log lines (`lcd idle`, charge messages), so
  a read loop that extends its deadline on *any* serial activity never
  terminates. Use a hard deadline.
- `fread` prints with `%2x`, so single-digit bytes are **space padded**
  (`" 1"`, not `"01"`). Parse on whitespace, not fixed width.

**So ADFU was never needed for reading — only for writing.** The dump
prerequisite in the risk section above is satisfiable today.

## The BF07 is definitively LARK

Its own firmware references `lcdc_lark.c`, `de_lark.c`, `lark_bt`, `DSP_LARK`
and contains **zero** leopard references.

This matters because the SDK's only **NOR** ADFU payload
(`app_demo/lvgl_demo/prebuild/ATT/att_adfu.bin`, 15,080 B — strings
`'snor int ret=%d'`, `'nor id = {...}'`, no spinand at all) is a **leopard**
build that loads at `0x2ff90000`. Not usable here. Every LARK ADFU prebuilt in
the SDK is a NAND build.

Load address `0x01010000` is now confirmed *empirically*, not just by file
self-consistency: the payload uploaded there executes and prints its banner.

## Mirror partitions — how XIP firmware is legitimately replaced

The application's OTA is an **A/B mirror** scheme:

```
'id  name      offset    type  file_id  mirror_id  flag'
'cannt found mirror part entry for file_id %d'
'found part entry for file_id %d, cur_file_id %d'
'part[%d]: skip current used partition'
'[%d]: file %s, file_id %d write to nor addr 0x%x'
```

`struct partition_entry` (SDK `partition.h`) carries `mirror_id:4` and
`storage_id:4`, with `PARTITION_MIRROR_ID_A/B`. So
`'storage %d is xip, skip erase'` does **not** mean XIP is unwritable — it means
the *currently executing* partition is skipped, because the update is written to
its **mirror** and the bootloader swaps on reboot.

That is the intended mechanism for replacing XIP firmware in place, and it
implies a write path that needs no ADFU **if this device's table actually
defines mirror partitions**. Unverified — the table must be read first.

### How to read the live partition table

From the printer at `0x10078fb0`:

```
10078fc6  ldr r4, =0x1801d684     ; RAM: pointer to the loaded table
10078fd0  ldr r3, =0x54504341     ; magic 'ACPT'
10078fea  mov.w r2, #0x2e4        ; table size, CRC checked at +0x2e4
10078fa2  cmp r7, #0x1e           ; 30 entries
```

So: `dbg mdw 0x1801d684` for the pointer, then read `0x2e4` bytes from it.
There is no shell command that prints the table (the full command table is
enumerated in [debug-shell.md](debug-shell.md)) — read it via `mdw`.

## THE PARTITION TABLE — read from the live device

Read via `dbg mdw`. `g_part_table` (pointer at RAM `0x1801d684`) points to
**`0x12000000`**, not a normal RAM address — a separate mapped window. Magic
`ACPT` verified, `version=0x0101`, `part_cnt=15`, `entry_size=24`.

```
id  name      type      fid  mir  stor  offset      size        flags
0   fw0_boot  BOOT      1    0    NOR   0x0         0x1000      ENCRYPT
1   fw0_para  PARAM     2    0    NOR   0x1000      0x1000      ENCRYPT
2   fw1_boot  BOOT      1    1    NOR   0x2000      0x1000      ENCRYPT
3   fw1_para  PARAM     2    1    NOR   0x3000      0x1000      ENCRYPT
4   fw0_rec   RECOVERY  3    0    NOR   0x4000      0x10000     ENCRYPT
5   fw0_sys   SYSTEM    4    0    NOR   0x14000     0x1e0000    ENCRYPT
6   fw0_sdfs  DATA      5    0    NOR   0x1f4000    0xa0000     ENCRYPT
7   nvram_fa  DATA      6    -    NOR   0x294000    0x1000      -
8   nvram_fa  DATA      7    -    NOR   0x295000    0x2000      -
9   nvram_us  DATA      8    -    NOR   0x297000    0x2000      -
10  fw0_sdfs  DATA      20   0    NOR   0x299000    0x166000    ENCRYPT
11  fw0_temp  TEMP      254  1    NOR   0x3ff000    0x1000      ENCRYPT
12  mbr       DATA      11   0    SD    0x0         0x1000      -
13  fw1_sdfs  DATA      12   0    SD    0x10000     (dynamic)   -
14  udisk     DATA      40   0    SD    (dynamic)   (dynamic)   -
```

This independently confirms `fw0_sys` @ `0x14000`, size `0x1e0000` — the values
this project has used all along — and confirms it is `ENCRYPT`-flagged.

### The mirror path does NOT work — verdict reversed

Mirror pairs exist for exactly two file_ids:

- `file_id 1`: `fw0_boot` / `fw1_boot` — 4 KB each
- `file_id 2`: `fw0_para` / `fw1_para` — 4 KB each

**`fw0_sys` (file_id 4) has NO mirror.** There is a single 1.875 MB system
partition. `fw0_rec` and both `sdfs` partitions are likewise unmirrored, and
`fw0_temp` is **0x1000 bytes** — three orders of magnitude too small to stage a
system image.

So the A/B scheme protects only the bootloader and its parameters, which is
what makes a bootloader update safe. It cannot replace the code image.

> **Conclusion: ADFU is confirmed as the only way to rewrite `fw0_sys`.**
> This closes the last alternative. The earlier XIP reasoning was right, and the
> mirror discovery — while real — does not provide a way around it.

(Recorded because `tools/read_parttable.py` initially printed the opposite
verdict: it flagged "mirror pairs exist" without checking whether the *SYSTEM*
partition was among them. Fixed to test the SYSTEM file_id specifically.)
