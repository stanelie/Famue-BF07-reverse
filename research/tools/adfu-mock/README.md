# adfu-mock — capture the vendor tool's ADFU handover safely

Makes a Raspberry Pi 4 impersonate a BF07 in ADFU mode so the Windows
**Actions Multimedia Product Tool** will talk to it, and logs every command it
sends.

**The BF07 is never connected.** That is the entire point: pressing *Flash*
with the real device attached risks an unrecoverable write (the 4 MB SPI NOR is
inside the SoC package — there is no chip to clip onto, and no BF07 firmware
exists publicly for recovery).

## What we're after

The single unsolved problem is the **boot-ROM handover** — the command that
transfers control to an uploaded `adfus.bin`. Everything either side of it
works: we can enter ADFU, upload the payload, and frame valid CBWs. But the
handover command is in mask ROM, present in no file we have, and every command
recovered from `HardwareEx.dll` turned out to belong to the *running payload*
rather than the ROM.

The vendor tool knows the command. This makes it say it out loud.

The interesting trace line is **whatever the tool sends immediately after the
~47 KB payload upload**.

## Which OS

**Raspberry Pi OS (64-bit) Lite, Bookworm.**

- `dwc2` and `libcomposite` are in-tree and well tested on this hardware;
  gadget mode on other distros means fighting the device tree.
- Lite because this is headless and a desktop just adds USB services that can
  interfere.
- 64-bit vs 32-bit makes no difference here — use 64-bit as the current default.

Note Bookworm moved the boot partition to `/boot/firmware/`. On older releases
the same files are at `/boot/`.

## Why the Pi 4 and not the ESP32/RP2040

The real device enumerates at **USB High Speed with 512-byte bulk endpoints**:

```
10d6:10d6   Speed: Up to 480 Mb/s
  ep 0x81 IN   bulk  maxpkt 512
  ep 0x02 OUT  bulk  maxpkt 512
```

The RP2040 and ESP32-S3 are **Full Speed only** (64-byte bulk max), so they
cannot match this; whether the tool would tolerate the mismatch is unknown.
The ESP32-C3's USB Serial/JTAG block is fixed-function and cannot emulate
arbitrary devices at all. The Pi 4's `dwc2` controller does High Speed, so it
matches the real descriptors exactly.

## Hardware setup

On the Pi 4, **only the USB-C port** can act as a USB device. The four USB-A
ports hang off a VL805 xHCI controller, which is host-only silicon.

Since USB-C carries the data, power the Pi from the GPIO header:

| Pi 4 | connection |
|---|---|
| GPIO pin 4 | +5 V from a regulated supply |
| GPIO pin 6 | GND |
| USB-C | data cable to the Windows PC |

GPIO power **bypasses the Pi's polyfuse and reverse-current protection** — use
a solid 5 V / 3 A supply. A weak supply causes brownouts that look exactly like
mysterious gadget-mode failures.

Use a USB-C cable known to carry data, not a charge-only one.

## One-time Pi configuration

`/boot/firmware/config.txt` — add at the end:

```
dtoverlay=dwc2
```

`/boot/firmware/cmdline.txt` — append to the **single existing line**, space
separated (do not add a newline):

```
modules-load=dwc2
```

Then reboot and confirm a UDC appeared:

```bash
ls /sys/class/udc
```

You should see something like `fe980000.usb`. If it's empty, the overlay didn't
take.

## Running

Copy this folder to the Pi, then:

```bash
chmod +x gadget-up.sh gadget-down.sh
```

```bash
sudo ./gadget-up.sh
```

```bash
sudo python3 adfu_mock.py
```

`gadget-up.sh` builds the gadget in configfs but deliberately leaves it
**unbound** — FunctionFS needs its descriptors written before the host is
allowed to enumerate. `adfu_mock.py` writes them, then binds the UDC itself.

Now plug USB-C into the Windows PC. The tool should detect an ADFU device.
Press *Flash*. Watch the console.

When finished:

```bash
sudo ./gadget-down.sh
```

## Output

| file | contents |
|---|---|
| `adfu_trace.jsonl` | one JSON record per CBW, data phase, and ep0 event |
| `payload_NNNN.bin` | any host→device transfer over 4 KB, saved whole |
| stdout | human-readable decode with opcode guesses |

Bring `adfu_trace.jsonl` back to the Mac. A captured `payload_*.bin` is also
worth diffing against the SDK's official
`adfus.bin` (47,608 bytes, MD5 `b4a8e3b9dc9f02d3db2e7e857aed5177`) — if they
match, the tool is uploading the same payload we already have, which confirms
the upload step and isolates the handover as the only missing piece.

## How far it will get

Unknown, and that's fine. The mock accepts every CBW and returns a success CSW,
so the tool should progress until something it actually validates comes back
wrong. Even an early abort is likely to capture what we need, because the
handover comes immediately after the upload.

If the tool stalls earlier than expected, the most likely cause is a canned
reply it checks. Edit the `CANNED` dict in `adfu_mock.py` — the `adfu_info`
(`0xCC`) reply is the obvious first candidate. Only the `CADFUD` portion of the
real device's reply was recorded verbatim; the exact full bytes can be
recaptured from the BF07 with `bf07-research/tools/lark_adfu.py` if needed.

## Endpoint addresses

FunctionFS declares IN before OUT so the ep files map as `ep1` → `0x81` IN and
`ep2` → `0x02` OUT. The *actual* addresses are assigned by the UDC driver, so
verify on the Windows side (or with `lsusb -v` from another Linux box) that
they came out as `0x81`/`0x02`. In practice the tool enumerates and uses the
first bulk pair it finds, so an exact match is desirable rather than critical.

## Safety

- The BF07 stays disconnected for this entire procedure.
- Nothing here writes to any flash.
- If the tool refuses to talk to the mock, we have lost setup time and nothing
  else.
