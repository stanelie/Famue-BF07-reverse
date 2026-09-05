# Famue BF07 — better ebook reading, installed over USB

The Famue BF07 is a small e-ink e-reader. This installs a replacement ebook
reader onto it — real reflow, hyphenation, page turns and a percent-seek
driven by the touch screen — over the USB cable you already charge it with.
**No case to open, no soldering.**

## What you get

- Real text reflow and hyphenation, instead of the stock reader's fixed
  layout.
- Page turns and a percent seek driven directly by the touch screen.
- Roughly 3x faster page rendering (the stock reader's own decode/layout code
  is switched off, not just hidden behind the new one).
- An optional custom font, installed by dropping one file on the drive — no
  tool needed for that part.

Everything here was reverse engineered from a device its owner already owned,
using only its own debug UART and files Actions Semiconductor and lvgl
publish themselves. See [research/](research/) for the full writeup of how
this was figured out and how it works.

The installer above reads your firmware from your own device and ships none of
its own. Stock firmware images from the author's units are archived under
[research/firmware/](research/firmware/) as a recovery fallback, since the BF07
looks to be out of production with no working vendor update path.

## Before you start

**Take a backup before you do anything else.** The install tool refuses to
run without one, and it is the only way back if anything looks wrong
afterward — restoring from it is byte-exact and has been proven on hardware
repeatedly.

**At least two BF07 firmware builds exist in the wild** — a unit bought later
shipped the *older* one, so a newer purchase is no guarantee of newer firmware.
This bundle carries a patch for each, and the installer reads your device to
pick the right one. If it recognises neither, it refuses and writes nothing,
and tells you how to get your build supported. A refusal is it working, not a
fault.

Once the install step starts, **do not disconnect the USB cable or power off
the device** until it prints that it has finished. Interrupting a normal
`backup`, `verify`, or `restore` is harmless — nothing is being written. Only
`install` (and `restore`, when it has something to fix) writes to the device,
and each of those tells you clearly when it's safe to disconnect.

## Prerequisites

| OS | What you need |
|---|---|
| **Linux** | Python 3.8+ with `pyusb` (`pip install pyusb`) and `libusb`. Add this udev rule so the tool can talk to the device without `sudo`: |

```
# /etc/udev/rules.d/99-actions-adfu.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="10d6", MODE="0666", TAG+="uaccess"
```

Then `sudo udevadm control --reload-rules && sudo udevadm trigger`, and
unplug/replug the device once. (No rule yet? Just run the commands below with
`sudo`.)

| OS | What you need |
|---|---|
| **Windows** | Python 3.8+ with `pyusb` (`pip install pyusb`), and the device's ADFU interface bound to **WinUSB** using [Zadig](https://zadig.akeo.ie/) (device shows as USB VID `10D6`, PID `B00B` or `10D6` depending on mode). **This path hasn't been dry-run tested yet** — everything validated so far is on Linux; if you hit something the steps below don't cover, please open an issue. |
| **macOS** | **Not supported yet.** macOS's kernel keeps the device's USB mass-storage interface for itself and won't release it, which this tool needs to switch the device into ADFU mode. There's a workaround using a serial cable soldered to the debug pads — see [research/docs/flashing.md](research/docs/flashing.md) — but it isn't a no-case-opening path, so it isn't documented here as the standard route. |

## Download

Get it from the [Releases page](../../releases) — one file,
`bf07-installer-<version>.py`. Nothing to unpack.

```
python3 bf07-installer-<version>.py
```

Everything the installer needs is inside that file. It unpacks into a
temporary directory while it runs and removes it on exit, so nothing is left
in your folder. Check its sha256 against the release page if you want to
confirm the download.

(A `.zip` of the same thing is also attached, if you would rather see the
individual files.)

## Run it

```
python3 bf07-installer-<version>.py
```

That's the whole thing. It opens a menu, explains the risks once at the top,
and walks you through everything:

```
  1) INSTALL THE READER  -- backs up, checks the backup, then installs
  2) Back up only                       (safe, reads only)
  3) Check a backup against the device  (safe, reads only)
  4) Copy the custom font to the drive  (safe, just a file copy)
  5) Restore from a backup / go stock   (WRITES to the device)
  6) Quit
```

**Pick 1.** It does the whole job in the right order and stops at the first
sign of trouble:

- **Backs up** your firmware to a file (default `mybf07.bin`). **Keep a copy
  somewhere other than this computer** — it is the only way back, and the only
  copy of your particular firmware build.
- **Checks that backup** against the device. If it doesn't match, it stops
  there and does not install: a backup you can't trust isn't a way back.
- **Installs**, picking the patch that matches your firmware automatically,
  writing 21 sectors, verifying each one, and rebooting the device. You don't
  need to know which firmware you have — and shouldn't trust the version shown
  on the device, which isn't reliable.

Options 2 and 3 do those steps individually if you want them separately.
- **4 — Font.** Copies `custom.font` onto the drive. Then pick it in the
  reader's font menu on the device. This is the one option that needs the
  drive *mounted*; the others unmount it for you.
- **5 — Restore.** Puts the device back exactly as your backup found it —
  stock reader and all, then reboots it. Works as many times as you like.

If option 3 says **`None of the available patches match`**, your device runs a
firmware build we haven't seen. Nothing was written. It explains how to get it
supported — please do send the backup, it's how the next build gets added.

### Or drive it from the command line

Every option is also a subcommand, for scripting or if you prefer:

```
python3 bf07-installer-<version>.py backup  -o mybf07.bin
python3 bf07-installer-<version>.py verify  -b mybf07.bin
python3 bf07-installer-<version>.py install -b mybf07.bin
python3 bf07-installer-<version>.py font
python3 bf07-installer-<version>.py restore -b mybf07.bin
```

`install` finds the bundled patches by itself; `--patch` is only needed to
point it somewhere else.

`install` asks for confirmation before writing; `--yes` skips it, `--no-reboot`
leaves the device in update mode afterwards.

## Advanced (optional, needs a serial cable) — the "Custom" label

The font menu row above will say **"Fangsong Small Font"** rather than
"Custom" unless one extra, serial-cable-only step is run once. This is
cosmetic only — the font itself works either way. See
[research/docs/user-tool.md](research/docs/user-tool.md#the-custom-font-label-is-a-separate-optional-step)
if you want it.

## Troubleshooting

| Message / symptom | What it means | What to do |
|---|---|---|
| `No BF07 found. Connect it over USB and choose disk drive mode on the boot menu` | The device isn't visible yet | Reconnect the cable, pick disk-drive mode on the device's own boot menu, try again |
| `still mounted and could not be unmounted automatically` | The tool unmounts the device's drive for you, but something is still using it | Close anything open on the drive (file manager, terminal sitting in it), then retry. It prints the manual command if you need it |
| A `verify` or `install` run stalls or every USB transfer times out | ADFU got wedged | **Physically power-cycle the device** (a soft `usb reset` won't clear this) and retry from `backup` |
| `install` reports a `VERIFY FAILED` and tells you to stay connected | A write didn't take | Do exactly what it says — **stay connected, don't power off** — and immediately run the `restore` command it prints |
| `This patch was NOT built for the firmware on this device` | Your BF07 runs a different firmware build than this patch targets | Nothing was written — your device is fine. You need a patch built for your firmware; open an issue with the sector addresses it printed. **Don't use `--force`** |
| `already has exactly this patch installed` | It's already up to date | Nothing to do — this is the tool declining a pointless write |
| Device won't boot at all after install | Something went wrong during the write | With the device still connected: `python3 tools/bf07.py restore -b mybf07.bin` |
| None of the above works, device is completely unresponsive | Extremely rare, but recoverable with physical access to the debug UART pads | See [research/docs/flashing.md](research/docs/flashing.md#recovery-of-last-resort-short-tx-and-rx-then-reset) — this is the true last resort and needs opening the case |

## Rolling back / returning to stock

Menu option **5**, or:

```
python3 tools/bf07.py restore -b mybf07.bin
```

Works at any time, as many times as you like, and is byte-exact — it puts the
device back precisely as your backup found it, stock reader included, then
reboots it. Use the backup you took *before* installing.

## How this works

The install writes a small, verified patch: a replacement reader dropped into
unused flash space, plus a handful of one-word hooks in the stock firmware
that redirect it there. Nothing else on the device changes, and every write
is read back and checked before the tool tells you it's safe to disconnect.
The full story — how the device was reverse engineered, how the reader was
built, and everything that didn't work along the way — is in
[research/](research/).

## License

Everything in this repository — notes, tools, and the reader patch itself —
is released into the public domain (CC0), see [LICENSE](LICENSE). The one
exception is `reference/adfus_u_go.bin`, a third-party ADFU payload from
Actions' own public SDK, included in the release bundle for convenience — see
[THIRD_PARTY_NOTICE.md](THIRD_PARTY_NOTICE.md).
