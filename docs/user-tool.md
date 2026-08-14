# Backing up and patching your own BF07

`tools/bf07.py` does the whole job over the USB cable you charge the device
with. No case to open, no soldering.

```
bf07.py backup  -o mybf07.bin                 full 4 MB image + SHA-256
bf07.py verify  -b mybf07.bin                 what differs from that image
bf07.py restore -b mybf07.bin                 put it back, byte-exact
bf07.py install -b mybf07.bin -p plain.bin    install the replacement reader
```

**Take a backup first.** `install` refuses to run without one, and the backup is
the only way back.

## What you need

| | |
|---|---|
| Python | 3.8+ with **pyusb** |
| USB | libusb |
| ADFU payload | from Actions' **public** LARK SDK — see [../reference/README.md](../reference/README.md) |

Nothing here contains vendor firmware. Your firmware is read from your own
device and stays on your machine.

## How it gets into ADFU without a serial cable

The device's own mass-storage stack answers the classic Actions handshake: ask
for `ACTIONSUSBD`, then send the switch command, and it reboots as `10d6:10d6`.
That is all `bf07.py` does — no `dbg reboot adfu`, no UART.

**Leaving** ADFU takes a physical reset (the reset button, no case opening).

### Platform reality

The obstacle is your operating system, not the device: while the BF07 is a USB
disk, the OS owns that interface.

| | |
|---|---|
| **Linux** | works; run as root (or detach `usb-storage` from `10d6:b00b`) |
| **Windows** | works once that interface is bound to WinUSB (e.g. with Zadig) |
| **macOS** | **blocked** — the kernel's mass-storage driver cannot be detached, and unmounting does not release it. Enter ADFU another way (a serial cable, or do the switch on another machine), after which everything else works normally. |

## Why restore is safe

Writes with **address bit 31 clear are stored verbatim** — no encryption. So an
encrypted backup goes straight back without anyone needing to decrypt anything.
Verified on hardware: write plaintext with bit 31 set, read the ciphertext the
SoC produced, erase, write that ciphertext raw, read it back identical.

`restore` compares every sector against your backup and rewrites only what
differs, then reads each one back to confirm.

## Installing without serial: the patch file

`install --patch reader-patch.bin` needs **ADFU only** -- no serial cable, no
decrypted image, no vendor firmware file.

The insight is that installing never needed decrypted *reads*. It needs patched
**plaintext**, and almost all of that plaintext is either ours or already on the
device:

| what | where it comes from |
|---|---|
| the reader sectors (13, and growing) | ours -- written as plaintext, the SoC encrypts on write |
| unchanged blocks of the 6 vendor sectors | **the device's own ciphertext**, rewritten verbatim (bit 31 clear) |
| the blocks we actually edit | the patch file -- **352 bytes** of stock context |

So the only vendor content anyone needs is 352 bytes, and it is
**firmware-version-specific, not device-specific**: identical on every unit
running the same firmware. One person makes the patch once from a decrypted
dump; everyone else installs over USB alone.

**That dump no longer needs a serial cable either.** `tools/usb_plaindump.py`
reads plaintext `fw0_sys` over ADFU, because the flash cipher is live in ADFU
with its key loaded (see [adfu-xip.md](adfu-xip.md)). On Linux or Windows the
whole chain -- decrypted dump, patch, install -- is USB-only. On macOS the one
remaining need for a cable is *entering* ADFU, which is an OS limitation, not a
decryption one.

Nothing here assumes anything about the flash encryption key -- it works whether
the key is per-device or global, because the device encrypts our blocks with its
own key and its own ciphertext is never decrypted.

```
# once, by anyone with a device (no serial needed on Linux/Windows):
usb_plaindump.py -o fw_code_full.bin
mkpatch.py -p fw_code_full.bin -o reader-patch.bin

# by everyone else, ADFU only:
bf07.py backup  -o mybf07.bin
bf07.py install -b mybf07.bin --patch reader-patch.bin
```

## The "Custom" font label is a separate, optional step

`install --patch` covers everything in `fw0_sys`, including the word patch that
points the font menu row at the label id `0xf40f37ea`. It does **not** cover the
string that id resolves to, which lives in the NOR resource partition -- a
different region, and one whose plaintext is 64 bytes of vendor strings that
this repo will not carry.

So a patch-only install gives you a working reader and a working user font; the
row simply keeps its vendor name ("Fangsong Small Font"). To make it say
"Custom":

```
set_menu_label.py            # dry run: reads YOUR device's strings over the UART
set_menu_label.py --write
```

It reads the current strings through the mapping the running firmware sets up
(the region is encrypted in flash, plaintext through the mapping), so nothing
vendor-specific is shipped, and it refuses to write unless it finds the exact
string it expects. Re-running on an already-labelled device is a no-op.

The reader does not depend on any of this: with no `custom.font` on the drive
the menu hook reverts the row to its original label at runtime.

### How it has been validated

**Currently, in software:** `patchset.build()` is diffed against the sectors the
development flasher writes -- the build that is verified working on the device.
All **21** common sectors are byte-identical. (The dev flasher writes two extra
sectors, `0x5c000` and `0xff000`, which are historical hook sites it rewrites
from stock to clear stale branches; they carry no patch, so the patch file does
not need them.) This check costs nothing and catches exactly the drift that made
this section necessary: the font-open and menu hooks and the label word patch
lived only in `mkflash.py` for a while, so `install --patch` would have produced
a reader with no user-font backend and no "Custom" label.

**Previously, on hardware** (`tools/validate_patch.py`, at 3 reader sectors):
the working flash was captured as the reference, the sectors restored to stock,
the patch installed, and all 9 sectors compared byte for byte -- identical, with
the device booting and the reader live. The script writes the reference back if
the comparison fails, so the device is never left in an unknown state. Before
that, at 2 reader sectors, the same path was checked against a legacy
full-plaintext install: all 8 sectors identical.

An earlier run of this same path produced a device that bus-faulted in the font
hook at boot, and it was reported as validated because the check only compared
which blocks *changed* against stock -- never what they changed *to*. A wrong
block passes that test. The installer now verifies content: every patched block
must differ from its pre-erase value and every untouched block must equal it,
and it aborts telling you to restore if not.

Note the mixed-write worry that motivated the investigation was **disproved** on
hardware: writing some blocks encrypted (bit 31 set) and others raw within one
sector produced 128/128 blocks identical to a full-sector plaintext write. The
cipher is stateless per 32-byte transaction.

## The one thing that still needs a serial cable

Only **building** a patch needs a decrypted image, and only once per firmware
version. Installing an existing patch needs nothing but ADFU. The legacy
`install -p <image>` path remains for anyone who has their own dump.

- ADFU reads **ciphertext**.
- ADFU cannot see the decrypted XIP window: measured, `rm 0x10000000` returns
  neither the plaintext nor the ciphertext of that address.
- Only the running firmware can read decrypted code, through `dbg mdw` on the
  UART (see [firmware-extraction.md](firmware-extraction.md)).

So installing currently needs one serial session per device to capture the
plaintext, even though backup, verify and restore need none.

**Routes out of this**, in the order worth trying:

1. **Teach the ADFU payload to enable XIP.** The decryption path exists in
   silicon; ADFU simply starts before it is initialised. A payload that sets it
   up would make `rm 0x10000000` return plaintext and remove the serial step
   entirely.
2. **Patch a vendor firmware file instead of the device.** Shipped images are
   plaintext, so a vendor update file could be the base — the blocker is that
   the newer LARK `ACTSFWFMT001` container is not yet unpacked (see
   [actions-formats.md](actions-formats.md)).
3. **Install by OTA.** The device flashes `/SD:/ota.bin` itself, CRC-checked and
   unsigned ([ota-format.md](ota-format.md)) — no USB at all — but building that
   image still needs a plaintext base, so it depends on 1 or 2.

## Validated on hardware

The whole user journey was run against a real device, in one ADFU session:

```
backup   4 MB in 7.5 s, sha256 82f09263...
verify   8 sectors differ  (the reader's two, plus six one-word hook sites)
restore  all 8 restored -> "device matches the backup"
install  8 sectors written, block counts identical to what verify reported
```

`restore` used **only the encrypted backup** -- no plaintext anywhere -- and the
device came back byte-exact.

Two bugs the run caught, both fixed:

- **Re-uploading the ADFU payload wedges ADFU.** Every subcommand used to
  upload it, so `backup` followed by `verify` wedged every time. The payload is
  now probed first (it answers a raw `is` with 0xAA; the boot ROM does not) and
  only uploaded when nothing answers. This is the cause of the ADFU wedges seen
  throughout the project.
- **Writing 0xFF padding as plaintext encrypts it.** An erased sector is already
  0xFF, and writing 0xFF with bit 31 set turns it into ciphertext -- the padding
  after the reader showed up as 2 extra changed blocks. All-0xFF blocks are now
  skipped.

## What `install` writes

Eight sectors, from [patchset.py](../tools/patchset.py):

- **`0x1e7000`, `0x1e8000`** — the reader itself, in unused `0xFF` padding
  inside the XIP partition.
- **six vendor sectors**, one word each: the line-height and container
  constants, the render tail hook, the message hook, the touch driver hook, and
  the font callback hook.

Every write is 32 bytes at a time, and each sector is read back and compared
against your backup afterwards.
