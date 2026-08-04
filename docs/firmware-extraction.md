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
