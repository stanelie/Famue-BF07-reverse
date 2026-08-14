# Backup, restore and patch — the complete write path

Everything needed to read a BF07's flash, write it back, and verify the result
byte-exactly. This is the reference for a user-facing backup/restore/patch tool:
no vendor files are redistributed, because the firmware comes off the user's own
device.

Every rule here was established on hardware. Where something was inferred and
later corrected by measurement, the correction is what is written down.

## Host requirements

| | |
|---|---|
| Python | 3.9+ with **pyusb** and **pyserial** |
| USB | libusb (macOS: nothing extra; Linux: udev rule for `10d6:10d6`) |
| Serial | a 3.3 V UART adapter on the debug pads, **2,000,000 baud** |
| Toolchain (patching only) | `arm-none-eabi-ld`/`objcopy`, plus clang or arm-none-eabi-gcc |

Serial is not optional for development. **The only recovery from a bad write
requires the Zephyr shell to start.** Keep the UART wired whenever you write.

## The two connections

The BF07 has *two* independent links, and both are needed:

- **UART** — the Zephyr shell (`dbg …`), and the way to command a reboot into ADFU.
- **USB** — ADFU itself. Reading and writing flash happen only here.

A common failure is having only the UART attached: `dbg reboot adfu` succeeds, the
device reboots into ADFU, and the host then reports *"device never reached ADFU"*
because nothing is on the USB bus. Check for `10d6:10d6` before blaming the device.

## Entering ADFU

```bash
# over serial (device running normally)
printf 'dbg reboot adfu\r\n' > /dev/cu.usbserial-XXXX
```

`tools/adfu_enter_usb.py` does the same over USB alone (~1 s). Either way the
device enumerates as **`10d6:10d6`**. There is no software route *out* of ADFU:
leaving it takes a physical reset.

## The payload handoff

The boot ROM cannot touch flash. `adfus_u_go.bin` (48,792 B) must be uploaded and
started first:

```
cd 13   write_mem  -> 0x01010000     (the payload, padded to 256 B)
cd 20   exec       -> 0x01010000
is      mode 0                        binds NOR; must follow the exec
```

`CDB[0]` is a constant escape byte `0xCD`; **the opcode goes in `CDB[1]`**. Getting
this wrong is why every command in this project's first weeks returned CSW status 2.

Then the flash commands are available: `rs` read sector, `es` erase sector,
`ws` write sector.

## The five rules of writing

These are hard constraints, each found by a failure:

1. **Writes are 32-byte transactions.** A 4 KB burst with bit 31 set encrypts
   differently — the engine carries per-transaction state. One 4 KB sector is
   **128 separate 32-byte writes**.
2. **Erase granularity is 4 KB.** Patching a byte means read-modify-erase-write of
   the whole containing sector.
3. **Write plaintext with bit 31 of the address set.** The SoC encrypts on write.
   Writing pre-encrypted data, or writing without bit 31, both produce garbage.
4. **Command acks are 4 bytes.** Waiting for more costs a USB timeout per command.
5. **An ACK is not proof of a program.** The first encrypted write issued after a
   run of verbatim writes is acknowledged and never programmed -- the block stays
   at `0xff`. Write the encrypted blocks *first*, and read back to confirm a block
   is not still erased. Checking "did it differ from stock?" does NOT catch this:
   an unwritten block differs from stock too. This silently produced a menu label
   of decrypted `0xff` three times before it was found.

## Verification, and why it is exact

Flash encryption is **32-byte ECB with no address tweak**. That has a useful
consequence: identical plaintext always encrypts to identical ciphertext, anywhere.

So after writing a sector, read it back raw and compare against the encrypted
backup. Every 32-byte block that you did not intend to change **must** match the
backup byte-for-byte, and every block you did change must differ. Both halves
matter — checking only that your blocks changed will not catch collateral damage.

`tools/mkflash.py` generates a flasher that asserts exactly this, printing:

```
0x1e7000: differing blocks [...]  expected [...]  OK
```

A run that reports fewer differing blocks than expected did not fully write. This
is not theoretical: an aborted run left a code sector with 40 of 43 blocks
written, and the reader would have jumped into a half-written function.

## Address mapping

`fw0_sys` starts at flash offset **`0x14000`** and is XIP-mapped at
**`0x10000000`**:

```
xip_addr   = 0x10000000 + (flash_off - 0x14000)
flash_off  = xip_addr - 0x10000000 + 0x14000
```

Confirmed by the injected reader: flash `0x1e7000` executes at `0x101d3000`.

**Free space:** `0x1e7000`–`0x1f4000` (53 KB) is unused `0xFF` padding inside the
XIP partition — code written there executes like any other firmware code. This is
where the replacement reader lives.

## Backing up

A full 4 MB read over ADFU takes about 6 s (~693 KB/s) and is byte-identical to an
independent UART dump. **Take a backup before the first write and keep it**; the
verification step above compares against it, so it is not merely insurance — it is
part of the normal write path.

Restoring is the same operation in reverse: erase and rewrite the affected sectors
from the backup. Reverting to stock has been done twice with zero differing blocks.

## Recovery

| symptom | cause | fix |
|---|---|---|
| `device never reached ADFU` | USB cable not connected (UART alone is not enough) | plug in USB, check for `10d6:10d6` |
| `is failed` after a clean entry | payload state stale from an aborted run | re-upload and re-exec the payload, do not just retry `is` |
| every USB transfer times out | ADFU itself is wedged | **physical power-cycle**; `usb reset` does not clear it |
| device boots to a crash loop | bad write, shell still starts | hammer `dbg reboot adfu` during the boot cycles |
| nothing on serial at all | write broke pre-shell init | not recoverable in software |

## What a user-facing tool needs

The architecture is sound and ships nothing copyrighted — the firmware is read
from the user's own device, patched locally, and written back:

1. **Backup** — enter ADFU, upload payload, read 4 MB, save with a hash. Refuse to
   proceed without one.
2. **Patch** — apply changes to the plaintext image; keep the stock image intact
   as the comparison baseline.
3. **Write** — 32-byte transactions, bit 31 set, sector at a time.
4. **Verify** — re-read and compare every block against the backup, and fail loudly
   on any unexpected difference.
5. **Restore** — rewrite affected sectors from the backup.

Existing pieces: `tools/adfu_enter_usb.py`, `tools/lark_cd.py`, `tools/lark_adfu_u.py`,
`tools/mkflash.py`, `tools/dump_flash.py`.

The one thing a distributable tool must **not** bundle is `adfus_u_go.bin` and the
stock firmware. The payload derives from Actions' own SDK (which is public — see
[sdk.md](sdk.md)); firmware belongs to the device owner.

## Two-sector writes can truncate -- always read the verifier

A 6830-byte build wrote only **39 of 86 blocks** into its SECOND code sector
(`0x1e8000`), twice in succession, while a 6488-byte build wrote all of both
sectors cleanly. The verify step reported `MISMATCH` and the run ended
`RESULT: PROBLEM` -- exactly what it is for.

The device was left with a partially written reader, which would have looked
like a code bug rather than a flashing failure. Restoring the previous build
came back `7 OK / FLASHED`.

**Rules that follow:**

- Never treat a flash as done without reading the final line. `RESULT: PROBLEM`
  means the device is in an unknown state.
- After a truncated write, reflash a known-good build before testing anything.
- Suspect size: the truncation appeared between 6488 and 6830 bytes, i.e. as the
  second sector filled past ~1.2 KB. Not yet diagnosed.

## Scratchpad files are ephemeral

`flash_full.py` (the flasher template) and `stock/sector_*.bin` both vanished
mid-session when the scratchpad was garbage-collected. The template now lives in
`tools/flash_template.py`; the stock sectors regenerate from the backup image:

```python
FW0 = 0x14000
img = open("<backups>/fw_code_full.bin", "rb").read()
for s in (0x5f000, 0xff000):
    open(f"stock/sector_{s:06x}.bin", "wb").write(img[s-FW0 : s-FW0+0x1000])
```
