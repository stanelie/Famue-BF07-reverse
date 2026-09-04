# Famue BF07 — `ota.bin` container format

Reverse-engineered from the extracted firmware (`fw_code_full.bin`, XIP base `0x10000000`).
All multi-byte fields little-endian. All CRCs are **standard zlib CRC-32**
(reflected, poly `0xEDB88320`, init/final `0xFFFFFFFF`) — verified byte-exact against
`zlib.crc32` by emulating the firmware's nibble-table routine at `0x1007f864`
(table at `0x1012b4c8`).

The device looks for the image at **`/SD:/ota.bin`** (string `0x1016f084`), via the
SD-card backend `ota_backend_sdcard_*`.

## Layout

```
offset      size    contents
0x0000      0x20    header fields (see below)
0x0020      0x1E0   padding / reserved (inside CRC'd region)
0x0200      0x200   file directory: 16 entries x 0x20 bytes
0x0400      ...     file data (payload region)
```

Header is **exactly 0x400 bytes**. `ota_image_open` (`0x100bbff0`) reads 0x400 bytes,
then `memcpy(img+0x04, hdr+0x000, 0x20)` and `memcpy(img+0x24, hdr+0x200, 0x200)` —
which is what proves the split above (32 header bytes + 512 directory bytes).

### Header fields (file offset)

| off    | type | field            | notes                                                        |
|--------|------|------------------|--------------------------------------------------------------|
| 0x00   | u32  | `magic`          | `0x41544F41` = ASCII **"AOTA"**                              |
| 0x04   | u32  | `header_crc`     | `crc32(hdr[0x008 .. 0x400))` — length `0x3F8`                |
| 0x0A   | u16  | `header_size`    | must equal `0x400`                                           |
| 0x0C   | u16  | `file_count`     | number of directory entries in use                           |
| 0x12   | u16  | `data_offset`    | start of CRC'd payload region (normally `0x400`)             |
| 0x14   | u32  | `total_size`     | whole image size; must be `<= 0x3000000` (48 MiB)            |
| 0x18   | u32  | `data_checksum`  | `crc32(image[data_offset .. total_size))`                    |

Bytes `0x00..0x08` are **outside** the header CRC (it starts at `0x08`), so `magic`
and `header_crc` itself are not covered — matching `crc32(0, hdr+8, 0x3F8)` at `0x100bbf6e`.

### Directory entry (0x20 bytes, at `0x200 + i*0x20`)

| off    | type      | field      | notes                                            |
|--------|-----------|------------|--------------------------------------------------|
| 0x00   | char[12]  | `name`     | compared with `memcmp(..., 12)` in `ota_image_find_file` |
| 0x10   | u32       | `offset`   | absolute offset of file data within the image    |
| 0x14   | u32       | `length`   | file length in bytes                             |
| 0x18   | u32       | (unknown)  | not read by any path examined                    |
| 0x1C   | u32       | `checksum` | `crc32` of the file's data (`ota_image_check_file`) |

## Validation order (what the device enforces)

1. `ota_image_check` (`0x100bbf1c`): magic == "AOTA"; `header_size == 0x400`;
   `total_size <= 0x3000000`; `header_crc` matches and is not `0xFFFFFFFF`.
2. `ota_image_check_data` (`0x100bbe5c`): CRC over `[data_offset .. total_size)`
   in 0x800 chunks == `data_checksum`.
3. Manifest: find file **`ota.xml`** in the directory, read, parse, check.
4. `ota_manifest_check_file_size`: each `<file_size>` in the XML must equal the
   directory entry's `length` for the same file, else
   *"file size 0x%x in manifest file is not equal to image dir 0x%x"*.
5. `ota_image_check_file`: per-file `crc32` vs entry `checksum`.
6. Policy gates: `board_name` must match the device, and `version_code` must be
   newer or the device logs *"ota image is same or older, skip ota"*.
   (A *"enable no version control"* path exists, `0x1018e1fd`.)

## `ota.xml` manifest schema

Must begin with `<?xml`. Tag names the parser looks for (all confirmed as literal
strings; parsing is naive substring `<tag>`/`</tag>` matching via `xml_get_data`):

```xml
<?xml version="1.0" encoding="utf-8"?>
<ota>
  <firmware_version>
    <version_code>...</version_code>
    <version_res>...</version_res>
    <version_name>...</version_name>
    <board_name>...</board_name>
  </firmware_version>
  <partitionsNum>N</partitionsNum>
  <partitions>
    <partition>
      <type>SYSTEM</type>
      <file_id>...</file_id>
      <file_name>...</file_name>
      <file_size>...</file_size>
      <checksum>...</checksum>
    </partition>
    ...
  </partitions>
</ota>
```

Known `<type>` values (string table `0x1018ee22`+): `RESERVED`, `BOOT`,
`SYS_PARAM`, `SYSTEM`, `DATA`, `TEMP`.

`file_id` ties a partition to the flash partition table printed at boot
(`fw0_boot`=1, `fw0_para`=2, `fw0_rec`=3, `fw0_sys`=4, `fw0_sdfs`=5, ...).

## Still unknown / to confirm on real hardware

- Exact `board_name` string the device expects (not yet located; likely in the
  `fw0_para`/SYS_PARAM partition rather than in code).
- Current `version_code` to exceed. NVRAM `FW_VERS` reads `00 00 01 00 00 00 01 00`.
- Semantics of directory entry `+0x18`.
- Whether the payload files themselves must be encrypted (the running firmware is
  stored encrypted in SPI NOR, but the OTA writer may encrypt on write — this is the
  single biggest open risk and must be resolved before flashing anything).

---

## Verification against the vendor's own packer

After the above was derived by hand from the firmware, the official
`bootloader/tools/build_ota_image.py` was found in the public Actions SDK. It matches
**field for field**:

```python
OTA_FIRMWARE_MAGIC       = b'AOTA'
OTA_FIRMWARE_HEADER_SIZE = 0x400
head_data = struct.pack("<4sIHHHHHHII36x", MAGIC, 0, header_version, header_size,
                        file_cnt, header_flag, dir_offs, data_offset, data_len, data_crc)
dir_entry = struct.pack("<12s4xII4xI", file_name, data_offset, file.length, file.checksum)
header_crc = zlib.crc32(head_data[8:] + dir_data, 0) & 0xffffffff
```

Fields we had not named are now known: `header_version` @0x08, `header_flag` @0x0E,
`dir_offs` @0x10. The directory-entry field at `+0x18` that we marked "unknown" is simply
padding.

Sibling tools in the same directory: `build_firmware.py`, `build_boot_image.py`,
`build_sdfs.py`, `build_ota_patch.py`, `build_nvram_bin.py`, `part_dts2xml.py`.

## Device identity for a real image

From the boot log:

```
 Firmware Version: 0x00010000
 res Version:      0x00010000
 System Version:   0x00000000
 Version Name:     1.00_2506301055
 Board Name:       xlx_58120_bf07
```

`board_name` must be **`xlx_58120_bf07`** (an early guess of `"BF07"` was wrong) and
`version_code` must exceed `0x00010000`. The board check is a `strcmp` at `0x100bb6d0`,
the version compare at `0x100bb70c`, and both occur **before** the
`burn firmware image` step at `0x100bb718`.

## Caveat

None of this is currently reachable on the BF07 — the SD-card OTA backend never runs.
See [dead-ends.md](dead-ends.md).

---

# The `ota.xml` manifest (recovered 2026-08-06)

An AOTA image is not just the container — it must contain a file named
**`ota.xml`**, an XML manifest the device parses and validates before writing
anything. Recovered from the parser's own error strings in `fw0_sys`
(`0x1018e900`–`0x1018ed00`).

Required top-level tags (each has a `cannot found tag <...>` error):

```
<firmware_version>
    <version_code>      <version_res>      <version_name>
<board_name>            <- checked; mismatch => "unmatched board name, skip ota"
<partitionsNum>         <- "too many ota file cnt: %d" if excessive
<partitions>
    <partition>
        <type> <file_id> <file_name> <file_size> <checksum>
```

Parser behaviour:

* `'<?xml'` must be present or it logs `invalid manifest file`.
* Generic tag scanner: `cant find tag start %s` / `cant find tag end %s`.
* Per-partition it logs
  `part file %s: type %d, file_id %d, checksum 0x%x`.
* It cross-checks the container against the manifest:
  `file %s: file size 0x%x in man[ifest]...`.
* `board_name` must match this device: **`xlx_58120_bf07`**.
* Version is NOT a barrier — the recovery app logs
  `enable no version control`, so a same or older `version_code` is accepted.

For the SYSTEM partition the expected entry is **`file_name = app.bin`,
`file_id = 4`** (from the SDK's `firmware.xml`).

## Consequence for `tools/ota_tool.py`

`build()` currently produces only the AOTA container plus its two CRCs. **It
does not emit `ota.xml`**, so an image it builds today would be rejected with
`invalid manifest file` / `cannot found tag <board_name>`. Adding the manifest
is the remaining work before an OTA can be tested.

## What the payload data must contain

`fw0_sys` is stored **encrypted**, and the application contains **no flash
encryption routine** — every `encrypt`/`AES`/`crypt` string in `fw0_sys` belongs
to the Bluetooth stack (`bt_crypto`, `AES CMAC`, `LTK`, `DHKey`). Combined with
the verified fact that ADFU `ws` writes raw, the OTA must also write raw.

**Therefore `app.bin` inside `ota.bin` must be CIPHERTEXT** — the exact bytes
from the verified backup at `0x14000`, length `0x1e0000`. The vendor's
production tool applies encryption at build time (the `<enable_encryption>`
flag), not the device at write time.

Getting this backwards — shipping plaintext `app.bin` — would write plaintext
over an encrypted partition and brick the device. This is the single most
dangerous assumption in the whole OTA path.
