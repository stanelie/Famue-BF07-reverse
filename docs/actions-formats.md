# Actions firmware package formats

Three nested container formats, plus how to decrypt them.

## 1. `ACTTEST0` — outer production package

```
header 0x200 B:  magic "ACTTEST0" @0x00, directory entries from 0x20
entry (32 B):    char name[16]  u32 offset @0x10  u32 size @0x14  u32 (0) @0x18  u32 crc @0x1C
```

**Entry checksums are plain zlib CRC-32** (verified: 10/10 matched).

Typical members: `sdfs_c.bin`, `udisk.bin`, `acttest.ap`, `atttest.bin`, `att_adfu.bin`,
`config.xml`, `resource.zip`, `SutPatch.bin`, `upgrade.fw`, `config.txt`.

## 2. `ACTSFWFMT001` — inner "upgrade.fw"

12-byte magic stored as three LE u32s spelling `ACTSFWFMT001` (raw bytes read
`100TMFWFSTCA`); version tag `ACTS-FWT001` at 0x10.

**This is not firmware.** It's the PC production tool packaged as a **FAT32 volume**:
FAT at `0x4010`, root dir `0x800410`, data `0xe17420`, cluster size `0x1000`. The tool's
`.PYD` modules are themselves encrypted on disk (`SdkCrypt.dll`); only `LAUNCH.PYO` and
`UPGRADE.PYD` are in the clear.

## 3. FWU — magic `11 22 33 44 55 66 77 88 99 aa bb cc dd ee ff 75`

This one is real firmware, and **[Rockbox's `atjboottool`](https://github.com/Rockbox/rockbox/tree/master/utils/atj2137/atjboottool) decrypts it**:

```
atjboottool --fwu -o out_ firmware.fw
```

It reports and passes: EC public-key check, **ECIES decryption over a 233-bit elliptic
curve**, then descrambles. Builds on macOS with `make CC=cc LD=cc` after fetching all 12
sources (`afi.c/h atj_tables.c/h atjboottool.c fw.c/h fwu.c/h misc.c/h Makefile`, `-lz`).

**The decrypted output is a SQLite database.** Open it directly:

```sql
.tables                          -- ADFUS, HWSC, FWSC, BREC, FWIM, DGSC, FW_TYPE, FileTable...
select * from FW_TYPE;           -- e.g. US215A / US212A
select FileName, FileLength from FileTable where Keyword='ADFUS';
select writefile('out/'||FileName, File) from FileTable;   -- extract everything
```

`FileTable(Keyword, FileName, FileLength, File blob)` holds every component. The
manifest tables give load addresses, e.g. `ADFUS.BIN | 1146880` = **0x118000**, and
`nandhwsc.bin | 1171456` = **0x11E000** — matching what `actions_flash` documents.

## 4. sdfs — resource partition

Used both inside packages (`sdfs_c.bin`) and on-device (`fw0_sdfs`):

```
header: char name[12]="sdfs.bin"  u32 entry_count @0x0C  u32 total_size @0x10
entry (32 B, from 0x20): char name[12]  u32 offset @0x0C  u32 size @0x10  8 rsvd  u32 cksum @0x1C
```

Entry checksum is **not** CRC-32 (0/72 matched) — algorithm unidentified.

## Platform survey

| package | FW_TYPE | storage | OS |
|---|---|---|---|
| MECHEN D53, Oilsky D26/X50 | US215A | NAND | classic Actions |
| DALEK / TARDIS BT speakers | US212A | SPI NOR | classic Actions |
| **BF07** | **LARK** | **SPI NOR** | **Zephyr + LVGL** |

`atjboottool` handles the classic generations. **No LARK firmware package has been
found**, and classic-platform blobs (`ADFUS.BIN`, `fwsc`, `brec`) are storage- and
platform-specific — not safe to load on LARK.

## Encryption is a build flag

The SDK's `prebuilt/lark/common/firmware.xml` defines each partition with
`<enable_crc> <enable_encryption> <enable_ota> <enable_raw> <enable_dfu>
<enable_boot_check>`. In the reference config `enable_encryption` is **false**.

Flash-at-rest encryption is therefore applied by the PC build tools (via `encrypt.bin`,
4.2 MB of key material), configured per partition — which explains why the BF07's
`fw0_sys` reads as ciphertext through `fread` but plaintext through XIP, while the D53's
`sdfs_c.bin` ships in the clear.
