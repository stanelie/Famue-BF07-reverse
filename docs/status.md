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
| **Starting the payload (handoff)** | ✅ `cd 20` @ 0x01010000 |
| **Reading flash over ADFU** | ✅ byte-identical to the serial dump, 693 KB/s |
| **Writing anything to flash** | ⚠️ primitives known (`ws`/`es`), untested |

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

---

# 2026-08-05: ADFU SOLVED — full flash read over USB

The core blocker of this project is gone.

```
$ lark_adfu_u.py dump adfu_full.bin --compare bf07_flash_full.bin
read 4194304 bytes in 5.9s (693 KB/s) -> adfu_full.bin
compared 4194304 bytes against bf07_flash_full.bin: IDENTICAL
```

A complete 4 MB flash image over ADFU in **5.9 seconds**, byte-identical to the
UART dump that took ~2 hours and was independently verified block by block. Two
fully independent read paths agree on all 4,194,304 bytes.

## What was actually wrong

Three compounding mistakes, each of which alone would have blocked everything:

1. **Wrong payload.** `adfus.bin` is not the USB build — `adfus_u.bin` is
   (`_u` = USB). `adfus.bin`'s poller reads 16 bytes through a non-USB context
   stamped with type byte `8`.
2. **Wrong storage type.** Both payloads are built for SPI NAND. `adfus_u`
   tries storage types 1 then 2, never 0. The BF07 is SPI NOR.
3. **Wrong framing.** There is **no CBW/CSW wrapper**. The payload reads a raw
   **16-byte command packet** off the bulk OUT endpoint. Every CBW dialect this
   repo tried — `0xCD` ROM framing, both `CCommUSB` variants, classic ATJ — was
   wrapping commands in something the payload never reads.

## The working recipe

```
dbg reboot adfu                       # UART shell
lark_cd.py handover adfus_u_go.bin    # cd 13 upload + cd 20 start
lark_adfu_u.py dump out.bin           # raw 16-byte packet protocol
```

`adfus_u_go.bin` = stock `adfus_u.bin` with two RAM-only patches:

| offset | from | to | why |
|---|---|---|---|
| `0x274c` | `01 20` | `00 20` | first storage attempt -> type 0 (SPI NOR) |
| `0x2752` | `60 b9` | `0c e0` | `cbnz r0` -> unconditional branch; a failed storage init otherwise loops forever without ever reaching the command dispatcher |

## Command set

Dispatcher `0x01014a88`; IDs at `0x01010e06` (16 x u16), handlers at
`0x01010e28` (16 x u32). Opcodes are two ASCII characters read as a u16 LE.

```
[0..1] opcode   [4..7] length u32   [8..11] address u32 (BYTE address)
```

| op | meaning | op | meaning |
|---|---|---|---|
| `gf` | get flash info | `rs` | **read sector** ✅ verified |
| `rm` | **read memory** ✅ verified | `ws` | **write sector** ⚠️ untested |
| `wm` | write memory | `es` | **erase sector** ⚠️ untested |
| `ic` | ic version | `cf` | config |
| `is` / `si` | init / storage info | `rr` `rx` `sf` `af` `cr` | misc |

**Gotcha:** a stale status packet (`01 <seq4> 00 00 63 <cksum>`) may be queued
on EP `0x81`. Drain the endpoint before each command or every reply arrives
shifted by one — this cost real debugging time.

## Next

`ws` (write sector) and `es` (erase sector) are the remaining primitives. They
are the same 16-byte protocol, so the transport work is done. A byte-verified
4 MB backup exists, so a mistake is recoverable — **but only `fw0_sys`
(`0x14000`, len `0x1e0000`) should ever be written.**

## Flash WRITE and ERASE verified (2026-08-05)

Both write primitives work. Tested in the blank tail of the flash — chosen so
that even a coarse erase alignment could not damage anything.

**Target selection.** `0x3f0000`–`0x400000`: the last 64 KB, entirely `0xFF` in
the verified dump, 64 KB-aligned, inside the unused tail of `fw0_sdfs20` (and
containing `fw0_temp`, the OTA scratch partition). Because it was already
erased, the first write needed no erase at all — NOR programs 1→0 freely.

```
ws 0x3f0000 len=256   -> reply aa 00 00 00, readback byte-exact
es 0x3f0000 size=1000 -> reply aa 00 00 00, region returns to 0xFF
```

**Erase granularity is 4 KB.** Verified empirically: a marker written at
`0x3f1000` survived intact while `0x3f0000` was erased. So `fw0_sys` can be
patched surgically — erase and rewrite only the 4 KB sectors containing a
change.

`0xaa` is the ACK byte in a reply (`aa 00 00 00`).

**After cleanup, a full 4 MB ADFU dump compares IDENTICAL to the pre-test
backup** — the device is byte-for-byte as it started.

### Complete verified capability

| operation | status |
|---|---|
| `rm` read memory | ✅ |
| `rs` read flash | ✅ byte-identical to the UART dump |
| `ws` write flash | ✅ verified by readback |
| `es` erase flash | ✅ 4 KB granularity, verified |

Everything needed to patch `fw0_sys` now exists and is proven. The remaining
work is applying the ebook layout patches (offsets in
[ebook-layout.md](ebook-layout.md)) — `0x4903e` wrap width, `0x4934a`
lines-per-page, `0x164a32` word-break table — as flash offsets
`0x14000 + file_offset`, sector-aligned.

**Write only `fw0_sys` (`0x14000`, len `0x1e0000`). Never `fw0_boot` at `0x0`.**

## USB-ONLY workflow verified end to end (2026-08-05)

No serial cable at any point. Full chain, on a device that started in normal
mode:

```
diskutil unmountDisk /dev/disk2            # macOS: release the MSC interface
sudo python3 tools/adfu_enter_usb.py       # -> ADFU in ~1s
sudo python3 tools/lark_cd.py handover adfus_u_go.bin --start-only
sudo python3 tools/lark_adfu_u.py dump out.bin
```

Results:

```
ADFU mode reached after 1.0s
uploaded 48896 bytes, all CSW 0        cd 20 @ 0x01010000 csw=0
read 4194304 bytes in 5.9s (690 KB/s)  compared: IDENTICAL
ws 0x3f0000 -> aa 00 00 00, readback OK; es -> aa 00 00 00, blank again
final full dump vs backup: IDENTICAL
```

Read, write and erase all confirmed over USB alone.

### Platform notes

* **macOS** needs the volume unmounted **and** elevated privileges — libusb
  cannot detach `IOUSBMassStorageDriver` otherwise. Unmounting alone is not
  enough.
* **Linux** is easier: libusb detaches kernel drivers directly, so a Raspberry
  Pi or any Linux box works as a dev host with no special handling.
* **LARK answers `ff 55`** to the `0xCB/0x21` reboot, not the `ff 00` that
  `actions_flash` expects — its `adfu_reboot` would misreport this as a failure.
* Use `--start-only`. Any extra boot-ROM command after `cd 20` is a 31-byte CBW
  to a payload that reads raw 16-byte packets, and it desynchronises the command
  stream unrecoverably (endpoints will not even clear halt — only a power-on
  reset recovers).

### Exiting ADFU

There is no plain reboot opcode. `sf` (`0x01014fed`) is an arbitrary-execution
command — it acks, then `blx` to `packet[8..11]` — so it is the plausible route
to a programmatic reboot. **Untested.**

**Serial is still the recovery path**: if a payload wedges USB, the physical
reset button is the only way back. A serial-free host is fine, but physical
access to the device is still required.

---

# Recovery paths for a risky fw0_sys write (2026-08-06, from SDK source)

Two independent recovery mechanisms exist in the boot chain, both living in
partitions we would NOT modify (`fw0_boot` mbrec, `fw0_rec` recovery app).

## 1. TX<->RX loopback -> ADFU  (the strong net)

`bootloader/soc/arm/actions/leopard/soc.c:check_adfu_connect()` drives the
pattern `0x55aa55aa` (32 bits) out on the ADFU TX GPIO and reads each bit back
on the RX GPIO. If they match — i.e. **TX and RX are shorted together** — it
returns success and the recovery app reboots to ADFU:

```
ota_app/src/main.c:
    if(check_adfu()){                       // CONFIG_TXRX_ADFU
        sys_pm_reboot(REBOOT_TYPE_GOTO_ADFU);
    }
    ...
    boot_to_application();                   // only reached if no ADFU
```

This runs on **every boot**, before `boot_to_application()`, from `fw0_rec` —
so it works even if `fw0_sys` is completely destroyed. Combined with our proven
ADFU write path, this is a clean recovery loop:

    short TX<->RX, reset  ->  ADFU  ->  reflash fw0_sys from the verified backup

**Needs confirming on our build:** that `CONFIG_TXRX_ADFU` is compiled in and
which GPIOs. A normal-boot UART log will print `check txrx adfu` if so. (Our
board has no GPIO/button ADFU — `CONFIG_GPIO_ADFU` — per earlier testing, so
TXRX is the expected variant.)

## 2. SD-card /SD:/ota.bin  (real, but must be armed)

`recovery_main.c` reads `/SD:/ota.bin` — the **user** card slot (MMC_0), empty
on our device. But it is NOT passive: `ota_upgrade_is_allowed()` returns true
only if

```
soc_pstore SOC_PSTORE_TAG_OTA_UPGRADE != 0   OR   nvram REC_OTA_FLAG == "yes"
```

and `recovery_main()` then also requires `ota_upgrade_is_in_progress()`. So a
card alone does nothing (matching our earlier probe). To use it as brick-
recovery it must be **armed in advance** while the app still runs:
`REC_OTA_FLAG` is settable from the shell (`nvram REC_OTA_FLAG yes`), the app
sets the in-progress breakpoint state, and a validly-formatted `ota.bin` must be
on the card. More moving parts than path 1, and building a bootable `ota.bin` is
itself unverified.

`ota_main()` (the `exit_to_ota` fallback) is a **stub infinite loop** in this
SDK — not a recovery mechanism.

## Does mbrec auto-detect a bad fw0_sys?

`partition_valid_check()` only tests that the partition **table** is non-null —
it does NOT checksum `fw0_sys` (no partition carries `PARTITION_FLAG_BOOT_CHECK`
`0x04`). `boot_to_application()` -> `boot_to_app()` (SoC layer) is where any
integrity check would live; unconfirmed for our custom mbrec. **Do not assume a
corrupt fw0_sys auto-routes to recovery.** Rely on path 1 (TX/RX loopback),
which is triggered by hardware state, not by fw0_sys validity.

## The real boot chain, from a captured boot log (2026-08-06)

```
Ver1.1-mbrec (build Aug 26 2022)      <- fw0_boot, 0x0
  nor: 0x00000000, 1
  para=0x1000,r=1, i=0, crc=0
  of=0x200, entry 0x1100057d          <- loads the NEXT stage at 0x11000000
  z_sys_init dev(spi_flash) ...       <- a full Zephyr init at 0x1100xxxx
  ** Showing partition infomation **  <- partition_dump(), matches our table exactly
  !!!ERR: dev MMC_0 not found         <- the USER card slot, empty
  main I: upgrade not allowed         <- ota_upgrade_is_allowed() == false
  main I: REBOOT_TYPE_GOTO_SYSTEM     <- so: boot the application
<I> main: ### reboot_type:3 ###       <- fw0_sys now running at 0x10000000
```

**The recovery app (`fw0_rec`) runs on EVERY boot**, at `0x11000000`, before the
application. It is a full Zephyr image — this is the `ota_app/main.c` read from
the SDK. It lives in a partition we would never modify.

Two consequences:

1. **`check txrx adfu` never prints** -> `CONFIG_TXRX_ADFU` is NOT compiled into
   this build. The TX<->RX loopback ADFU recovery does not exist here. (An
   earlier note in adfu-protocol.md proposed relying on it — do not.)
2. **`upgrade not allowed` is the only reason it booted the system.** That comes
   from `ota_upgrade_is_allowed()`, which is just:
   `pstore OTA flag != 0 || nvram REC_OTA_FLAG == "yes"`.

### Therefore: the recovery net is ARMABLE

We do not need mbrec to detect a corrupt `fw0_sys`. We can decide *in advance*
that the next boot runs recovery instead of the application:

```
nvram REC_OTA_FLAG yes      (shell, while the app still works)
+ a valid ota.bin on the USER card slot (/SD:, MMC_0)
then perform the risky fw0_sys write
```

On the next boot the recovery app sees the flag, runs `recovery_main()` and
flashes from the card — **without ever jumping into the broken application.**
This is app-independent and does not rely on any integrity check.

### Why the factory does not need this

The factory flashes **blank** chips: with no valid mbrec, the boot ROM finds
nothing to load and simply stays in ADFU, which is how the production tool
talks to it. That net exists because mbrec is *absent*. In our case mbrec stays
valid, so the boot ROM hands control onward and the decision falls to the
recovery app — hence the flag above.

**Still unverified:** that `nvram REC_OTA_FLAG yes` persists and is read by the
recovery app, and that a `tools/ota_tool.py`-built `ota.bin` is accepted. Both
are testable with **no risk at all** — arm the flag, put an image on a card,
reboot, and watch the log. A failed OTA attempt on an intact device costs
nothing.

## PROVEN: the recovery net arms via `REC_OTA_FLAG` (2026-08-06)

Tested on an intact device, zero risk. `dbg nvram REC_OTA_FLAG yes`, then reset.

**Flag unset (normal boot):**
```
main I: upgrade not allowed
main I: REBOOT_TYPE_GOTO_SYSTEM
```

**Flag armed:**
```
main I: ota recovery main                            <- recovery_main() RUNS
ota_app_init I: ota app init
ota_app_init I: OTA_STORAGE_DEVICE_NAME: spi_flash   <- it writes the NOR
ota_app_init I: OTA_STORAGE_EXT_DEVICE_NAME: sd      <- it reads the user card
ota_upgrade_init I: init
ota_upgrade_init I: enable no version control        <- same/older version accepted
ota_storage_init I: init storage spi_flash
ota_storage_init E: cannot found storage device sd   <- user slot is EMPTY
ota_upgrade_init I: storage ext open err
ota_app_init I: ota app init error
main I: skip ota recovery
main I: REBOOT_TYPE_GOTO_SYSTEM                      <- falls through, boots normally
```

Confirmed:

* `REC_OTA_FLAG` is written from the shell (`dbg nvram <key> <val>`), persists,
  and **is read by the recovery app** — this is the arming mechanism.
* The recovery app targets **`spi_flash`** for writing and **`sd`** (MMC_0, the
  user slot) for reading, exactly as needed.
* **`enable no version control`** — it will not reject an image for being the
  same or an older version. Reflashing the current firmware is acceptable to it.
* With no card it degrades safely: logs the error, skips, boots the application.
  The device remains fully usable while armed.

**Divergence from SDK source:** the flag did **not** self-clear. The SDK's
`recovery_main()` calls `nvram_config_set("REC_OTA_FLAG","no")` on the
`skip ota recovery` path, but `ota_app_init()` errored first in our build so
that never ran. Practically this is *better* — the net stays armed across
repeated boot attempts instead of disarming after one. Disarm manually with
`dbg nvram REC_OTA_FLAG no`.

### The one remaining requirement

A card in the **user slot** (MMC_0 — the empty one) containing a valid
`/SD:/ota.bin`. The slot is physically present but unpopulated, which is the
sole reason recovery cannot complete today.

### Remaining unknown before any risky write

Whether a `tools/ota_tool.py`-built `ota.bin` is accepted by this OTA stack.
Testable at zero risk: build one from the **current, unmodified** firmware, put
it in the user slot, arm the flag, reboot. A successful reflash-with-identical-
firmware proves the whole loop end to end. Only then is rewriting `fw0_sys`
a defensible risk.

## How SD-card OTA actually works on this device (2026-08-06, tested)

Three findings, all verified on hardware.

### 1. The recovery app can NEVER read the card

`fw0_rec` runs on every boot, but its Zephyr build has **no `MMC_0` device**:

```
z_sys_init dev(sd) func:0x11008739
I: sd_card_init
!!!ERR: dev MMC_0 not found
E: Cannot find mmc device MMC_0!
...
ota_storage_init E: cannot found storage device sd
ota_app_init I: ota app init error
main I: skip ota recovery
```

Arming `REC_OTA_FLAG` correctly makes `recovery_main()` run — that part is
proven — but it always dies at `ota_app_init` because the card is unreachable
from that image. **No flag or file can fix this**; it is a build-level omission
in `fw0_rec`. The earlier plan to use the recovery app as a brick-recovery net
is therefore **not viable**.

### 2. The application CAN mount the card — but not while USB is attached

With USB unplugged:

```
<I> CSD: capacity 15193 MB
normal card
<I> fs_manager_init: fs (/SD:) init success
<I> sys_status_init: sys status: card=1, usb=0, charge=0
```

and `fs ls /SD:` lists our `ota.bin`.

**With USB attached the device exports both cards as USB mass storage and
unmounts them locally** — `/SD:` then reports `mount point not found`, and the
host sees the files instead. This is why every earlier attempt failed: the
laptop was holding the card. Any SD-based OTA work requires USB detached
(serial is unaffected and remains the way to observe).

### 3. The trigger is a UI app, not a boot-time scan

A full boot with the card mounted produces **no** `found ota file` /
`cannot found ota file` — the application does not scan at startup. The OTA is
driven by an on-device upgrade application:

```
upgrade_app_main   upgrade_init   upgrade_view_paint
upgrade_ing_cb     upgrade_event_deal   upgrade_app_notify
```

`/SD:/ota.bin` (`0x1016f084`) is referenced from `0x10061b64` and `0x101aaed4`.
`libota: version 1.0.0` does initialise in the application at boot.

**So the OTA must be started from the device's own menu.** The shell offers
`app input` (inject key events), which could drive the UI from serial if manual
navigation proves awkward.

### Consequence for the recovery plan

The SD path is a **user-initiated update mechanism**, not an automatic
brick-recovery net. It cannot rescue a device whose `fw0_sys` is broken, because
the thing that reads the card is the application itself.

That leaves **no automatic recovery** from a bad `fw0_sys`:

* recovery app  -> cannot read the card
* app OTA       -> requires a working app
* ADFU entry    -> both known methods need a working app
* no hardware ADFU button; `CONFIG_TXRX_ADFU` not built

**Before any risky `fw0_sys` write, this must be resolved** — most plausibly by
confirming whether mbrec/the boot ROM falls back to ADFU when `fw0_sys` fails to
boot (the factory case, where a blank chip simply stays in ADFU).
