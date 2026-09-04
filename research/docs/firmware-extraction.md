# Extracting the decrypted firmware

The SPI NOR contents are **encrypted at rest**, but the SoC's XIP window decrypts
transparently on read. So you dump through the XIP mapping, not the raw flash.

## The trap: `dumpbuf` silently lies

```
dbg dumpbuf 0x10000000 0x200000 /SD1:/fw.bin
  → "done dumping to /SD1:/fw.bin"
```

The file is **all zeros**. It reports success and writes nothing useful for the XIP
region. Do not trust it. (Path syntax also needs a leading slash before the drive:
`/SD1:/x.bin`, not `SD1:/x.bin`.)

## What works: `dbg mdw`

```
dbg mdw 0x10000000 1024
10000000: 01002140 1007f259 100f1b5d 1007f22d    @!..Y...]...-...
```

Returns genuine decrypted data. Roughly 1024 words per call is the practical maximum.
[`tools/extract_fw.py`](../tools/extract_fw.py) scripts this and reassembles the image.

## Regions

| XIP address | Length | Contents |
|---|---|---|
| `0x10000000` | `0x1E0000` | application code = **the entire `fw0_sys` partition** |
| `0x13000000` | `0xA0000` | sdfs resource partition (starts with ASCII `sdfs.bin`) |

`0x1E0000` is exactly `0x1f4000 - 0x14000`, i.e. the `fw0_sys` partition span from the
boot-time partition table. **The dump is therefore a directly usable partition image.**

## Practical notes

- **The console interleaves async log noise** into command output. The extractor validates
  strict address continuity per chunk and retries on corruption.
- **Sessions wedge.** Once a chunk returns 0 bytes on every retry, the *entire session*
  stays dead — including unrelated address ranges. The fix is reconnecting the serial
  port mid-run; the script does this automatically. The device itself is fine.
- Expect ~30–60 minutes for 1.9 MB.

## Verifying the dump

Essential before any flashing. Re-read random offsets live and compare:

```
dbg mdw <addr> 8      # compare against the same offset in the dump
```

Our dump verified 20/20 at random offsets (160 words), including the bytes around the
wrap-width constant. Do this again after any long extraction.

## Partition table (printed at every boot)

```
id  name      offset    type  file_id  mirror_id
0   fw0_boot  0x0       1     1        0
1   fw0_para  0x1000    6     2        0
2   fw1_boot  0x2000    1     1        1
3   fw1_para  0x3000    6     2        1
4   fw0_rec   0x4000    3     3        0
5   fw0_sys   0x14000   2     4        0     <-- the application
6   fw0_sdfs  0x1f4000  4     5        0
7   nvram_fa  0x294000  4     6        15
8   nvram_fa  0x295000  4     7        15
9   nvram_us  0x297000  4     8        15
10  fw0_sdfs  0x299000  4     20       0     <-- /NOR:K/ (UI resources)
11  fw0_temp  0x3ff000  5     254      1     <-- OTA scratch, erased (0xFF)
```

Total 4 MB, matching the JEDEC capacity byte (see [hardware.md](hardware.md)).

## The sdfs container

Both `fw0_sdfs` partitions use the same simple format (identical to `sdfs_c.bin` shipped
inside Actions `.fw` packages):

```
header:  char name[12] = "sdfs.bin"   u32 entry_count @0x0C   u32 total_size @0x10
entry (32 B, from 0x20):
         char name[12]   u32 offset @0x0C   u32 size @0x10   8 reserved   u32 checksum @0x1C
```

The entry checksum is **not** CRC-32 (0/72 matched on known-good data) — algorithm
unidentified. Don't treat a mismatch as dump corruption.

Parsing our `0x13000000` dump yields 12 entries: `video.dsp`, `admusic.dsp`,
`bmp_dec.dsp`, `v950big.tbl`, `v936gbk.tbl`, `1251l.tbl`, `hfreq.bin`, `bt_rf.bin`, …

---

# Full 4 MB raw flash backup — complete and verified (2026-08-05)

```
~/Documents/bf07-backups/bf07_flash_full_2026-08-05.bin
  4,194,304 bytes   md5 86fed84c9954fe482364799bb3f68bc2
  8192/8192 blocks captured, 8192/8192 re-read and byte-compared, 0 mismatches
```

Captured with `tools/dump_flash.py` over the UART shell (`dbg fread spi_flash`)
— **no ADFU required**. This is the *raw* image (ciphertext for `ENCRYPT`-
flagged partitions), which is what a restore needs; `dbg mdw` gives the
decrypted XIP view of the code partition only.

All partitions complete: `fw0_boot`, `fw0_para`, `fw1_boot`, `fw1_para`,
`fw0_rec`, `fw0_sys`, both `fw0_sdfs`, nvram, `fw0_temp`.

## Traps hit along the way

**The device degrades under sustained reading.** After ~1,300 blocks every
offset starts failing — including ones that read fine minutes earlier — and it
recovers after idle time. This is device-side print/log exhaustion, not bad
sectors. Both the dumper and the verifier now pace reads, back off on failure,
and rest periodically. Without this a run dies every ~20 minutes.

**Stale output can be misattributed.** After a read times out, the previous
command's output may still be in flight; a naive parser accepts it as the *next*
block's data. `dump_flash.py` now requires a `fread 512b: offset=0x...` header
matching the requested offset before accepting any hex lines. (The full
verification found 0 mismatches, so this never actually corrupted the image —
the 32-well-formed-lines requirement rejected partial junk — but the hazard was
real.)

**`--size` used to truncate the image.** A targeted re-read invoked as
`--start X --size Y` sized its buffer from `--size` and rewrote the whole file,
destroying 1,138 already-captured blocks while the journal still claimed them.
Image size is now `max(--size, existing file size)` and `--count` selects a
block range. **This was caught only by spot-verifying against the device** —
the file looked complete.

**Journal ordering.** The `.state` journal was flushed per block while the data
file was flushed every 64, so any hard kill left the journal claiming blocks
whose bytes were never written — a resume would then skip them and silently keep
`0xff`. Data is now `fsync`ed *before* the corresponding offsets are journalled.

The general lesson: **a backup you have not read back from the device is not a
backup.** Three independent bugs here would each have produced a
complete-looking image with wrong bytes in it.

---

# The fw0_sys cipher: 32-byte ECB (2026-08-05)

`fw0_sys` is stored encrypted (flag `0x02` = `PARTITION_FLAG_ENABLE_ENCRYPTION`)
and the XIP window decrypts it transparently. Characterised using the 1.875 MB
of matched pairs we hold — `bf07_flash_full.bin` at `0x14000` (ciphertext) and
`fw_code_full.bin` (plaintext, read through XIP):

* **Block size is 32 bytes.** The 53 KB of erased padding at `0x1e7000`–`0x1f4000`
  is constant `0xFF` in raw flash and decrypts to a pattern that **repeats with
  period exactly 32**.
* **Mode is ECB, with no address tweak.** Of 61,440 blocks, 50 plaintext blocks
  occur more than once; **every one maps to a single ciphertext**. Identical
  plaintext at different addresses always produces identical ciphertext.
* It is **not** a plain XOR stream: `raw XOR plaintext` is not constant across
  32-byte boundaries in the code region (which is why the earlier XOR
  brute-force found nothing — it was looking for a stream, not a block cipher).

A 256-bit block is unusual for a standard cipher, which suggests a custom
construction.

## What this means for patching

An arbitrary code patch changes a 32-byte block to one we have never seen, so we
would need `E(new_block)` — and we cannot compute that without the algorithm or
key. The codebook of 59,271 known pairs only lets us **reuse blocks that already
exist somewhere in the image**.

### The question that decides this — UNTESTED

**Does the SoC encrypt on write?** If the flash controller applies encryption to
writes in the XIP-mapped range, we simply write plaintext and the hardware does
the rest.

Our verified write test was at `0x3f0000`, which is in `fw0_sdfs20` and outside
the XIP mapping — it came back byte-identical, so *that* path is raw. **No write
inside `fw0_sys` has been attempted.**

Safe test: `fw0_sys` has 53 KB of unused `0xFF` padding at `0x1e7000`–`0x1f4000`.
Write a known 32-byte pattern there, then read it two ways:

| result | meaning |
|---|---|
| `rs` (raw) returns the pattern | write is raw -> we must encrypt ourselves |
| XIP (`dbg mdw 0x100d3000`) returns the pattern | **hardware encrypts on write — patching is straightforward** |

(`0x1e7000 - 0x14000 = 0x1d3000`, so the XIP address is `0x100d3000`.)

Failing that, the other avenue is turning decryption **off**: `<enable_encryption>`
is a per-partition build flag in the SDK's `firmware.xml`, so the hardware
behaviour is presumably configured somewhere reachable (mbrec, param partition,
or a controller register) rather than fused.
