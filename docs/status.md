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
