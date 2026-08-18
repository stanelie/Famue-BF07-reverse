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
| USB | libusb (macOS: nothing extra; Linux: udev rule for `10d6:10d6`, below) |
| Serial | a 3.3 V UART adapter on the debug pads, **2,000,000 baud** |
| Toolchain (patching only) | `arm-none-eabi-ld`/`objcopy`, plus **clang ≤ 17** or arm-none-eabi-gcc |

On Linux, ADFU needs the device readable without root:

```
# /etc/udev/rules.d/99-actions-adfu.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="10d6", MODE="0666", TAG+="uaccess"
```

The clang ceiling is not cosmetic. The reader is compiled `-O2` into a 52 KB
hole and fills 99.5% of it, so the compiler version is load-bearing: clang-15
builds 52,978 bytes, 16 gives 53,050, 17 gives 53,106, and **18 overflows** at
53,519. Every version builds at ~46,100 with `-Os` if that headroom is ever
needed — but that is different machine code, and has to be re-validated on the
device before it is trusted.

Serial is not optional for development. **The only recovery from a bad write
requires the Zephyr shell to start.** Keep the UART wired whenever you write.

### Choosing the serial port

The same adapter is `/dev/ttyUSB0` on Linux, `/dev/cu.usbserial-*` on macOS and
`COM3` on Windows, so the tools do not hardcode a path. `tools/serialport.py`
asks pyserial what is attached and picks the USB-serial adapter, in this order:

1. `--port` / `-p` on the command line — accepted by every tool that opens the
   UART, including the ones with no other arguments
2. `$BF07_PORT`
3. autodetect

The value may be a device path *or* any substring of the port's serial number
or description, which is the portable form: `--port AV7K776E` selects one
specific cable on all three operating systems, where a device path cannot.

Autodetect refuses to choose between two candidates rather than guess, because
picking the wrong adapter means writing flash commands at something that is not
the reader. If it stops with a list, name one. Note that a Nordic PPK2 — often
on the bench precisely when this board is being measured — presents two CDC-ACM
ports; it is filtered out by USB VID, but other instruments may not be.

Run `python3 tools/serialport.py` to see what it finds and what it would pick.

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

### Never `set_configuration()` after the handoff

The payload owns the USB endpoints from `cd 20` onward. Calling pyusb's
`set_configuration()` after that point issues a `SET_CONFIGURATION`, which
resets the endpoint state out from under it: the `is` that must follow the exec
then fails, and every raw packet after it returns `EIO`. On Linux the kernel has
already configured the device at enumeration, so the call is redundant as well
as destructive. macOS tolerated it, which is why this survived unnoticed until
the project moved hosts.

Configure only if nothing has:

```python
try:
    d.get_active_configuration()
except usb.core.USBError:
    d.set_configuration()
```

This bit twice, because the *liveness probe* made the same call. `payload_alive()`
destroyed the payload it was probing, always reported dead, and the flasher
compensated by re-uploading unconditionally. That workaround then failed the
other way: uploading over a live payload sends CBW framing to something that no
longer speaks it, and the upload EIOs. Fix the probe and both directions come
right -- **a workaround for a lying test outlives the bug and becomes the bug.**

### Entering and leaving ADFU without the UART

Both directions work over USB alone, verified on Linux:

**In** — `tools/adfu_enter_usb.py`, or `bf07.py` does it automatically. The
normal-mode device answers the classic Actions handshake as ordinary
mass-storage CBWs:

```
0xCC  , CDB[7]=11, dlen=11  -> "ACTIONSUSBD", CSW 0
0xCB21, CDB[7]=2 , dlen=2   -> ff 55, CSW 0 ; the device reboots into ADFU
```

Measured: ADFU reached ~1 s later. On Linux the interface must first be taken
from `usb-storage` (`detach_kernel_driver`), which the udev rule above makes
possible unprivileged. **macOS cannot do this at all** -- its mass-storage
driver cannot be detached -- so there the UART is still needed to get *in*.

**Out** — `adfu_reset.py reset_via_payload()`. ADFU's own software reset is a
dead end (see dead-ends.md) and this board has no ADFU button, but the running
payload's `wm` op can do it in two register writes:

```
RTC_REMAIN3 (0x4000c03c) = 0x42520000   clear the "boot to ADFU" request
WATCHDOG    (0x4000c020) = 0x5f         arm it; the reset follows
```

Clearing `RTC_REMAIN3` first is essential: `SYSRESETREQ` alone lands straight
back in ADFU, because the boot ROM re-reads the reboot type that put it there.

### Read the CSW, always

Bulk-Only Transport is CBW, data, **CSW** -- and the CSW is not bookkeeping you
can skip. Leaving it unread halts the endpoint, and every later transfer then
fails with `EOVERFLOW`, `EPIPE`, or a plausible-looking wrong status, on a
device that is answering perfectly well. An afternoon was lost concluding "the
switch command does not work" from measurements taken on endpoints the
measuring code had itself stalled. A clean `dbg reboot` and a correct sequence
got it first try.

The corollary: read a *packet*, not exactly `dlen`. A rejected command skips the
data phase and replies with the 13-byte CSW, which overflows a `dlen`-sized
buffer and buries the real status inside an errno.

### Unmount before entering ADFU

Entering ADFU detaches `usb-storage` and reboots the device. Anything mounted
from its card goes away underneath the kernel. `bf07.py` refuses to start while
a volume of its own is mounted and prints the `udisksctl unmount` line to run.

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
| nothing on serial at all | write broke pre-shell init | see below — assume not recoverable |

### There is no automatic fallback. Read this before writing.

Four plausible safety nets are all closed on this device, each checked rather
than assumed:

* **mbrec does not validate the system image.** The boot log prints
  `app offset=0x14000 ,crc=0`, and that `%d` is `crc_is_enabled` in
  `soc_boot.c`. A corrupt `fw0_sys` is jumped into regardless. Nothing detects
  it, and nothing routes to OTA or ADFU.
* **No serial-loopback ADFU.** `check_adfu_connect()` bit-bangs `0x55aa55aa` out
  on TX and reads it back on RX, so a wire between two pads would force ADFU
  from the bootloader, before any application code. But `check txrx adfu` never
  appears in the boot log: `CONFIG_TXRX_ADFU` is compiled out. Every reference
  board in the SDK also ships it as `0`. It would be GPIO_28/29 if enabled.
* **No ADFU button** — `CONFIG_GPIO_ADFU` likewise disabled.
* **The recovery app runs and gives up.** `fw0_rec` executes on every boot, then:
  `cannot found storage device sd` -> `ota app init error` -> `skip ota
  recovery`. It probes `MMC_0`; the microSD is `MMC_1`. Forcing `GOTO_OTA` or
  `GOTO_RECOVERY` through `RTC_REMAIN3` does not work either (see
  [dead-ends.md](dead-ends.md)).

**A serial cable does not rescue a trashed system.** The Zephyr shell *is* the
firmware that was trashed. mbrec prints to the UART but takes no input, so you
get a clear view of the failure and no way to act on it.

### What to rely on instead: the ADFU flag

`REBOOT_TYPE_GOTO_ADFU` (`0x100` in `RTC_REMAIN3`, magic `0x4252`) is the one
reboot type that behaves differently: **the boot ROM consumes it, before mbrec
and long before `fw0_sys`.** That is why `dbg reboot adfu` works at all, and why
`reset_via_payload()` has to clear the flag explicitly to get a normal boot.
Being handled in mask ROM, it does not care how corrupt the firmware is.

So during a flashing session the flag is already set, and the safe order is:

1. flash in ADFU as usual;
2. reset **without** clearing the flag — the device returns to ADFU rather than
   to a possibly-broken application;
3. verify, and re-flash if needed;
4. only then `reset_via_payload()`, which clears the flag and boots normally.

That is a boot-once-into-recovery that exists today, and with a verified backup
plus `bf07.py restore` it is a real net.

### Its limit: the flag does not survive a power cycle

Measured, not assumed:

```
normal boot          RTC_REMAIN3 = 0x00000000
dbg reboot adfu      -> ADFU in 1 s, flag set to 0x42520100
*** physical power cycle ***
comes back as        10d6:b00b  (disk drive mode -- NOT ADFU)
RTC_REMAIN3 =        0x00000000  (cleared)
```

So the net covers **warm resets only**. Pull the power mid-write and the flag
is gone, mbrec boots the half-written `fw0_sys` without checking it, and none
of the four fallbacks above will catch you.

That makes the interval between the first erase and a passing verify the only
genuinely dangerous part of this whole process, and it is dangerous only to
loss of power -- a failed verify is harmless and simply re-run. The tools now
say so: `bf07.py` (`install`, `install --patch`, `restore`) and the development
flasher all print a DO-NOT-DISCONNECT banner before the first erase, print
"safe to disconnect" only after the verify passes, and every abort inside the
window tells you to STAY IN ADFU rather than power off.

**Keep the device on mains or a charged battery for any write.**

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
