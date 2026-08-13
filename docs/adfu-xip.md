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

## What is left

The cache now fetches from the flash for real; the bytes are wrong. The most
likely cause is the **flash chip's transfer mode**: the running firmware puts
the chip into a fast/quad read mode that the XIP configuration expects, while
the payload has been driving it with plain SPI commands. Resetting the chip
(`0x66`/`0x99`) through the payload's `rx` op was tried and wedged ADFU — the
packet layout assumed for `rx` is wrong.

Next, in order of promise:

1. **Work out `rx`'s real packet layout** (disassemble its handler properly),
   then reset the flash and/or issue the mode command the XIP read expects.
2. **Call the ROM's own spinor vtable** (`0x5d10`) through `cf` — its init entry
   would put the controller and chip into the ROM's known-good state, which is
   also what `p_brom_nor_read` needs.
3. Capture the **full** SPI0 register block from a running device (only 16 words
   were compared) in case a configuration register beyond `+0x3c` matters.

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
