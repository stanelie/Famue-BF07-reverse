# Reading decrypted flash in ADFU — how far this got

**Goal:** make ADFU able to read the *decrypted* firmware, so `bf07.py install`
stops needing a serial cable. Not finished — but the remaining gap is now one
specific unknown rather than a mystery.

## Corrections to earlier notes

- **"ADFU can't see XIP" was the right conclusion for the wrong reason.** The
  window is not dead: `SPICACHE` is **clock-gated and held in reset** in ADFU.
- **Plaintext read from XIP in ADFU is not proof decryption is live.** ADFU is
  normally entered with `dbg reboot adfu`, a *warm* reboot, so decrypted lines
  left in the cache survive. Reads returned real firmware bytes from arbitrary
  offsets, with the low 12 bits tracking and the page effectively random —
  the signature of a cache serving stale lines it can never refill.
- **`0x40054000` is `SD0`**, the SD controller — not a crypto block. It reads
  zero in ADFU because the card is not initialised. An earlier guess that it
  held key material was wrong.

## The ADFU payload has 16 ops, not 8

Dispatch table at file offset `0x0e06`, handlers at `0x0e28`:

```
gf ri rm wm is ic si rc rs ws es cf rr sf rx af
```

| op | what it does |
|---|---|
| `rm` | **read memory.** `>= 0x40000000` is copied via a buffer; below that the bytes are sent straight from the address (so it dereferences whatever you pass — `rm 0` faults and wedges ADFU) |
| `wm` | **write memory** — verified by writing a marker to RAM and reading it back |
| `cf`, `sf` | **call an arbitrary address**: `ldr r3,[r0,#8]; blx r3` |

`wm` + `cf` is arbitrary code execution on the device from the host, confirmed
by uploading a thunk that stored `0xDEADBEEF` and seeing it land. **`cf`
dereferences the return value if it is non-zero**, so a thunk must return 0.

## The boot ROM API is real

`brom_interface.h` in the public SDK documents a function table at **`0x188`**,
and it is present on this device:

```
+0x00 mbrc_brec_data_check = 0x00002a05      +0x18 launch      = 0x0000021d
+0x04 brom_nor_read        = 0x00001fbd      +0x1c spinor_api  = 0x00005d10
+0x08 brom_snand_read      = 0x000021c1      +0x20 memset      = 0x00002ccf
+0x0c brom_card_read       = 0x00001b35      +0x24 memcpy      = 0x00002c91
```

`p_spinor_api` points at a 10-entry vtable of ROM flash routines.

**`p_brom_nor_read(offset, len, buf)` runs and returns 0 but reads nothing** in
ADFU, on a clean entry. It presumably expects the SPI controller in the state
the ROM left it, which the payload's `is` has since changed.

## Register map (public SDK, leopard — confirmed to match this SoC)

| base | block | evidence |
|---|---|---|
| `0x40000000` | RMU (`RMU_MRCR0`) | reset bit 8 = SPI0CACHE |
| `0x40001004` | `CMU_DEVCLKEN0` | clock bit 8 = `CLOCK_ID_SPI0CACHE` |
| `0x40010000` | memory controller | `SPI_CACHE_MAPPING_ADDR0` at `+0x300` |
| `0x40014000` | `SPICACHE_CTL`, `+4` = `INVALIDATE` | running device reads `0x21` |
| `0x40028000` | SPI0 (the flash port) | JEDEC `0x85` (Puya) readable here |
| `0x40038000` | UART0 | `0xdeaddead` |
| `0x40054000` | SD0 | zero in ADFU |
| `0x40068000` | GPIO | |

## The sequence that gets the cache fetching again

Each step was needed; the order matters.

```python
w32(0x40001004, r32(0x40001004) | (1 << 8))   # CMU_DEVCLKEN0: SPI0CACHE clock
w32(0x40000000, r32(0x40000000) | (1 << 8))   # RMU_MRCR0: deassert its reset
w32(0x40014000, 0x00000021)                   # SPICACHE_CTL (running value)
w32(0x40010300, 0x10000001)                   # map cpu 0x10000000 ...
w32(0x40010304, 0x00014000)                   # ... onto nor 0x14000 (fw0_sys)
w32(0x40014004, 1)                            # invalidate, wait for bit 0
w32(0x40028000, 0x203b1c38)                   # SPI0 ctl, copied from running
w32(0x40028010, 0x0000000b)
```

**Until the reset is deasserted, `SPICACHE_CTL` silently ignores writes** —
reads back 0. That single line is what moved this forward.

Result, in order:

| state | `rm 0x10000000` returns |
|---|---|
| untouched ADFU | stale decrypted lines, page effectively random |
| clock + reset + mapping | `00e800e8…` repeated — fetching, nothing served |
| + SPI0 control copied | **live data that changes between reads** — but not the plaintext |

## The ROM read path, disassembled

`rm` can read the ROM, so the routines were dumped and disassembled locally
rather than called blind.

**`p_brom_nor_read` (`0x1fbc`)** — the signature IS the documented
`(addr, len, buf)`; it loops over 512-byte chunks calling `0x27b0`:

```
1fc0  mov r5, r0        ; byte address, += 512 per chunk
1fc2  mov r6, r2        ; buffer,       += 512 per chunk
1fc8  lsrs r7, r1, #9   ; chunks = (len + 511) / 512
1fda  bl 0x27b0
```

**`0x27b0`** issues a **Fast Read (0x0B)** on SPI0 and moves the data by DMA:

| what | where |
|---|---|
| SPI0 base | `0x40028000` (literal at `0x2884`) |
| ROM state struct | `0x01000010` (literal at `0x2880`); `+0x44`, `+0x4c` are mode flags |
| transfer length | SPI0 `+0x10` |
| DMA channel | `0x4001c100`, source SPI0 `+0x0c`, config `0x00200087` |

**Correction:** SPI0 `+0x10` is a **length register**, not mode bits. An earlier
experiment wrote `0x0b` there thinking it was a mode field copied from a running
device; that write was meaningless.

### Measured: the DMA is set up correctly and moves nothing

After calling `p_brom_nor_read(0x14000, 512, buf)` the channel reads back:

```
+0x00 00200087   config      +0x10 01008400   destination = our buffer
+0x04 00000001   started     +0x18 00000200   length 512
+0x08 4002800c   source      +0x1c 00000200   REMAINING still 512
```

So the transfer was programmed and started, and **zero bytes flowed** — the SPI
controller never produced data. The ROM's own spinor init (`vtable[0]`, callable
via `cf`, returns 0) does not change this, and neither does running it straight
after a successful payload `rs`, which leaves SPI0 CTL at `0x38`.

Throughout all of this the **payload's own `rs` keeps working** and returns the
correct ciphertext, so the flash and the controller are healthy. What is missing
is whatever mode the chip must be in for the ROM's Fast Read — and for the XIP
engine, which fetched live-but-wrong bytes for the same likely reason.

## SOLVED: the fetch path. Remaining: the key.

Bringing the cache up needs **three** clock+reset pairs, not one. With
`CLOCK_ID_OTFD` (3), `CLOCK_ID_SPI0` (4) and `CLOCK_ID_SPI0CACHE` (8) all
ungated and out of reset, and the mapping programmed, `rm 0x10000000` returns:

```
7b55eecf846814875305716add958a4f1607ef45c30d5a0eb008d7f04203c382
```

**That is byte-exact the ciphertext at flash `0x14000`.** Correct address,
correct data, no decryption. The address-mapping problem is solved:

```python
for bit in (3, 4, 8):                       # OTFD, SPI0, SPI0CACHE
    w32(0x40001004, r32(0x40001004) | (1 << bit))   # CMU_DEVCLKEN0
    w32(0x40000000, r32(0x40000000) | (1 << bit))   # RMU_MRCR0
w32(0x40014000, 0x00000021)                 # SPICACHE_CTL
w32(0x40010300, 0x10000001)                 # cpu 0x10000000 ...
w32(0x40010304, 0x00014000)                 # ... -> nor 0x14000
w32(0x40014004, 1)                          # invalidate, wait for bit 0
```

Then setting SPI0 CTL to the value a running device uses (`0x203b1c38`, versus
the payload's `0x38`) changes the output to bytes that are **in neither the
plaintext nor the ciphertext image**. Read together, those two results say:

- in the payload's SPI mode the fetch is perfect and nothing decrypts;
- the running mode's upper CTL bits engage **inline decryption in the SPI0 path**,
  which then produces garbage.

Decrypting the correct ciphertext with a missing key produces exactly that:
output matching neither image. So the engine is there and switched on, and
**the key is not loaded**.

The boot ROM loads it from eFuse on the normal boot path; entering ADFU skips
that. Replicating registers cannot help — every visible register in the
memory controller, SPICACHE and CMU blocks was compared against a running
device and set to match.

### The next step, precisely

Find the ROM routine that loads the flash decryption key from eFuse and call it
through `cf`. Leads: the SDK ships `soc_atp.h` and `libsocatp.a` for leopard
(ATP is the eFuse/trim interface), and the ROM's own boot path must call the
equivalent before it can load the encrypted `fw0_boot`. `rm` can read the whole
ROM, so this is static analysis, not guesswork.

## What is left

The cache now fetches from the flash for real; the bytes are wrong. The most
likely cause is the **flash chip's transfer mode**: the running firmware puts
the chip into a fast/quad read mode that the XIP configuration expects, while
the payload has been driving it with plain SPI commands. Resetting the chip
(`0x66`/`0x99`) through the payload's `rx` op was tried and wedged ADFU — the
packet layout assumed for `rx` is wrong.

Next, in order of promise:

1. **The flash chip's own mode.** Both the ROM read and the XIP engine behave
   like the chip is not answering the command they issue. Reading its status
   register would settle it. That needs a raw SPI command path: either the
   payload's `rx` op (its packet layout is still unknown -- the guess wedged
   ADFU) or the ROM spinor vtable's read-status / write-status entries, which
   are callable via `cf`.
2. **Capture the full SPI0 block** from a running device -- only 16 words were
   compared, and the XIP command/dummy-cycle configuration may live past
   `+0x3c`.
3. **Trace where the payload's `rs` differs.** It works, so its instruction
   sequence is a known-good recipe for this chip; diffing it against the ROM's
   `0x27b0` would show what the chip actually needs.

*Tried and ruled out:* calling `p_brom_nor_read` with every plausible argument
order (the disassembly since confirmed the documented one is right); calling the
ROM spinor init first; running it from the payload's working SPI state; setting
SPI0 CTL and the cache registers to the values a running device uses.

## Running reference (captured from this device)

```
SPICACHE_CTL = 0x00000021
map[0] addr=0x10000001 entry=0x00014000     <- fw0_sys
map[1] addr=0x1800000f entry=0x00000000
map[2] addr=0x1200000f entry=0x00001000
map[5] addr=0x12400003 entry=0x00299000
map[6] addr=0x13000003 entry=0x001f4000
map[7] addr=0x13000001 entry=0x001f4000
SPI0: +0x00=0x203b1c38 +0x04=0x14 +0x10=0x0000000b +0x18=0x00ffffe0
CMU_DEVCLKEN0=0xc10c0631  RMU_MRCR0=0x010c0e31
```

Note `map[0]` uses `| 0x01`, not the `| 0x1f` the leopard SDK writes — the
mapping-size encoding differs, which is why the running values were copied
rather than computed.

## Where the key actually comes from (SDK-confirmed)

Checked the public SDK end to end:

- `soc_memctrl.c` — the mapping's `enable_crc` is a **CRC** check, not the cipher.
- `soc_boot.c` — `sign_load_key()` loads a **public key** from the image TLV for
  **signature verification**. Not the flash key.
- `soc_atp.h` — the eFuse interface (`libsocatp.a`) exposes only RF/PMU/HOSC
  **calibration** reads. No flash-key accessor.
- the spinor ROM vtable (10 entries at `0x5d10`) is pure flash I/O — none of its
  functions touch the SE crypto block (`0x40020000`) or eFuse.

**Conclusion:** the flash decryption key is loaded by the **boot ROM**, before
the bootloader runs, and nothing in the SDK exposes it. Confirmed by the
register evidence: every visible CMU/SPICACHE/memctrl register was matched to a
running device and the cipher still ran keyless.

To find that routine, the low ROM (`0x1000`-`0x8000`) must be disassembled —
`tools/dump_brom.py` does it safely (never below `0x1000`; reading the vector
table faults the CPU and the USB stack together, needing a physical reset).

## A cleaner path that skips the key entirely

Cracking the key gives *general* decrypted reads, but **install does not need
it**, because the SoC encrypts on write (bit 31 set). Install needs patched
**plaintext** sectors:

- 2 reader sectors = `reader.bin` (ours);
- 6 vendor sectors = stock vendor plaintext + a few changed words.

The only device-side unknown is the stock plaintext of those 6 vendor sectors,
and there are two serial-free ways to get it:

1. **The vendor firmware update file is plaintext** (the SoC encrypts on write,
   so shipped images are not encrypted — see [actions-formats.md](actions-formats.md)).
   Obtain and unpack it and every sector's plaintext is known, for any unit, no
   serial and no redistribution (the user downloads vendor firmware themselves).
   **Blocker:** the newer LARK `ACTSFWFMT001` container is not yet unpacked.
2. **If the flash key is global** across BF07 units (not per-device), the
   plaintext of the 6 vendor sectors is identical on every unit with the same
   firmware — i.e. exactly what `fw_code_full.bin` already holds. Bundling those
   patched sectors would work, but 24 KB of mostly-vendor code crosses the
   project's no-redistribution line. **Untested:** global-vs-per-device needs a
   second unit to settle.

Path 1 is the clean long-term answer and makes the eFuse-key work unnecessary
for the install use case. The ROM-key route remains the only way to *read*
arbitrary decrypted flash over USB, and may yet be blocked if the key registers
are write-only.
