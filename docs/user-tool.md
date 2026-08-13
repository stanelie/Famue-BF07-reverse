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

## The one thing that still needs a serial cable

`install` needs the **decrypted** image (`-p`), because patches are built by
editing plaintext.

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
