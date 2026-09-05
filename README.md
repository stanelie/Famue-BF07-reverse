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

Get the latest bundle from the [Releases page](../../releases) — download
`bf07-bundle-<version>.zip`, and check its sha256 against the one printed on
the release page if you want to confirm nothing got corrupted in transit.
Unzip it, then open a terminal in the folder it created.

## Step 1 — Back up

```
python3 tools/bf07.py backup -o mybf07.bin
```

Expect something like:

```
reading 4096 KB ...
wrote mybf07.bin in 7.5s
sha256 82f09263...
Keep this file. It is the only way back.
```

**Copy `mybf07.bin` somewhere off the device** — a USB drive, cloud storage,
anywhere else. If your computer's drive fails, you want this file to still
exist.

## Step 2 — Verify

```
python3 tools/bf07.py verify -b mybf07.bin
```

Expect:

```
comparing 0x014000-0x200000 against mybf07.bin ...
device matches the backup
```

This doesn't change anything — it just re-reads the device and checks it
against the file you just saved, confirming your computer, cable, and the
tool can all talk to the device reliably before step 3 writes anything.

If it reports sectors that differ, **stop and get in touch** (open an issue)
rather than continuing — that shouldn't happen on a device you just backed up
and haven't touched since.

## Step 3 — Install the reader

```
python3 tools/bf07.py install -b mybf07.bin --patch reference
```

**You don't need to know which firmware your device has.** More than one BF07
build exists, the bundle ships a patch for each, and the installer reads your
device and picks the right one. (Don't go by the version shown on the device —
it isn't reliable: a unit running one build reported the other's version.)

Three possible outcomes, and two of them write nothing:

- **`firmware recognised -> …`** — it found the matching patch and proceeds.
- **`already installed on this device -- nothing to do`** — you're up to date,
  and it skips the write rather than erasing and rewriting for no gain.
- **`None of the available patches match`** — your device runs a build we
  haven't seen. Nothing was written. The message explains both ways forward:
  send us your backup so we can support it, or flash one of the archived stock
  firmwares and patch that. **Don't use `--force` to get past this** — it would
  leave the device unable to boot, recoverable only by opening the case.

When it does proceed, you'll see a warning:

```
!!  DO NOT DISCONNECT POWER, and do not unplug the USB cable, until this
!!  command prints that it is finished.
```

**Respect it.** It normally takes well under a minute. When it's done, you'll
see:

```
verified -- safe to disconnect now. Power-cycle the device to boot it.
If anything is wrong: bf07.py restore -b mybf07.bin
```

Press the reset button (or power-cycle) to boot into the new reader. Open a
book and try turning a few pages.

## Step 4 (optional) — Custom font

Drop [`fonts/custom.font`](fonts/custom.font) onto the drive the device
exposes over USB, then pick **Custom** in the reader's font menu. That's the
whole step — no tool involved. See [fonts/README.md](fonts/README.md) if you
want to build your own from a different font file.

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
| `The device's storage is still mounted... Unmount it first` | Your OS auto-mounted the device's drive | Run the `udisksctl unmount` (or equivalent) command it prints, then retry |
| A `verify` or `install` run stalls or every USB transfer times out | ADFU got wedged | **Physically power-cycle the device** (a soft `usb reset` won't clear this) and retry from `backup` |
| `install` reports a `VERIFY FAILED` and tells you to stay connected | A write didn't take | Do exactly what it says — **stay connected, don't power off** — and immediately run the `restore` command it prints |
| `This patch was NOT built for the firmware on this device` | Your BF07 runs a different firmware build than this patch targets | Nothing was written — your device is fine. You need a patch built for your firmware; open an issue with the sector addresses it printed. **Don't use `--force`** |
| `already has exactly this patch installed` | It's already up to date | Nothing to do — this is the tool declining a pointless write |
| Device won't boot at all after install | Something went wrong during the write | With the device still connected: `python3 tools/bf07.py restore -b mybf07.bin` |
| None of the above works, device is completely unresponsive | Extremely rare, but recoverable with physical access to the debug UART pads | See [research/docs/flashing.md](research/docs/flashing.md#recovery-of-last-resort-short-tx-and-rx-then-reset) — this is the true last resort and needs opening the case |

## Rolling back

```
python3 tools/bf07.py restore -b mybf07.bin
```

This works at any time, as many times as you like, and is byte-exact — it
puts the device back precisely as `backup` found it, stock reader included.

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
