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
| **Writing anything to flash** | ❌ |

## The one blocker

**No verified write path.** Everything else is solved.

`actions_flash` implements the ATJ2127/ATJ2157 opcodes; LARK's `adfus.bin` uses a
different set, so every command after payload upload hangs
(see [adfu-protocol.md](adfu-protocol.md) for exactly why).

The protocol is now known, so the remaining work is *implementation*: retarget a host
tool to LARK's CDB layouts. `nfd/atj2127decrypt`'s `dfu/adfu.py` is the best starting
point — it already builds the correct CBW.

**Do the read path first.** `ADFURead(cmd=0x11)` should dump all 4 MB. That is the
backup which makes every subsequent write reversible, and it can be validated against
`dbg fread` output we can already obtain.

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
