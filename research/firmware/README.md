# Stock firmware archive

Unmodified firmware images read off two Famue BF07 units the owner of this
repository bought. The BF07 appears to be out of production and no vendor
update channel exists for it (the SD-card OTA path is non-functional on this
board — see [../docs/dead-ends.md](../docs/dead-ends.md)), so these images are
archived here as the only route back for anyone who damages their device.

**To the rights holder:** these are kept purely so owners can restore their own
hardware. Open an issue and they will be removed.

## Two firmware versions exist in the wild

| build | version | archived from |
|---|---|---|
| **2025-06-30 10:51:24** | `1.00_2506301055` | unit 1 |
| **2025-05-27 13:30:26** | — | unit 2, bought later but shipped with *older* firmware |

A newer purchase does not mean newer firmware — unit 2 was old stock. The two
builds share an identical string set (13,150) but almost nothing at the same
address: 465 of 480 `fw0_sys` sectors differ. It is the same feature set
recompiled and relinked, which is the worst case for address-based patching —
it looks identical and behaves differently. Installing a patch built for one
build onto the other hangs the device at boot (measured, not theorised).

## Files

| file | what it is |
|---|---|
| `stock-full-flash-2025-06-30.bin` | complete 4 MB flash, unit 1, as stored (encrypted) |
| `stock-full-flash-2025-05-27.bin` | complete 4 MB flash, unit 2, as stored (encrypted) |
| `stock-fw0_sys-plain-2025-06-30.bin` | `fw0_sys` only, decrypted, unit 1 |
| `stock-fw0_sys-plain-2025-05-27.bin` | `fw0_sys` only, decrypted, unit 2 |

`SHA256SUMS` covers all four. Verify with `sha256sum -c SHA256SUMS`.

All four are **stock** — the reader sectors (`0x1e7000`, `0x1e8000`) are erased
in every one of them, which is how "unmodified" was established rather than
assumed.

## Which one to use for a restore

**Prefer your own backup** (`bf07.py backup`) over anything here. These are a
fallback for a device with no backup of its own.

- **The plaintext `fw0_sys` images are the safe choice** for restoring someone
  else's device: `bf07.py restore --plain <image>` writes plaintext and the
  target SoC encrypts it with its own key, so it works regardless of whether
  the flash key is shared. It touches only `fw0_sys` — `mbrec`, the recovery
  partition and nvram are never written, so the TX/RX rescue keeps working.
- **The full-flash images carry the originating unit's `nvram`** (`nvram_fa` at
  `0x294000`, outside `fw0_sys`) — that unit's own settings and calibration.
  Writing one to a different device replaces that device's per-unit data with
  another's. Use these for reference and for rebuilding a completely erased
  chip, not as a casual restore.

The full-flash images are ciphertext. Measured across 17,848 shared plaintext
blocks spanning both builds, these two units encrypt identically — so the key
is not per-device, and that also means the archived `nvram` regions are
readable by anyone holding a BF07. Nothing sensitive is *known* to be in there,
but it has not been audited.

## How they were captured

Over USB, no case opening, using `../tools/bf07.py backup` (full flash) and
`../tools/usb_plaindump.py` (decrypted `fw0_sys`, read through the XIP window).
See [../docs/flashing.md](../docs/flashing.md) and
[../docs/adfu-xip.md](../docs/adfu-xip.md).
