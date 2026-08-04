# LARK ADFU protocol

Recovered by static analysis of the Actions **Multimedia Product Tool** (Windows).
No device interaction was needed.

## Why existing tools fail on LARK

[`actions_flash`](https://github.com/ilyakurdyukov/actions_flash) implements the
**ATJ2127/ATJ2157** opcodes: `0xcc` (adfu_info), `0xcb` (reboot), `0xb0` (reset),
`0x12` (INQUIRY). On LARK, `ADFUWrite` does `cmp cmd, 0x47; ja default` and indexes a
table — so all of those either exceed `0x47` or land on the unsupported handler. Every
command after payload upload blocks forever.

**Observed exactly this:** `simple_switch 0x118000 adfus.bin` succeeds, then
`read_mem2` hangs indefinitely.

Note `adfu_info` (`0xcc`) *does* work before the payload is uploaded — that is answered
by the **boot ROM**. The opcode set changes once `adfus.bin` is running.

## Where the implementation lives

`ProductionCC.dll` imports the ADFU API from **`HardwareEx.dll`** (class `CCommUSB`):

```cpp
CCommUSB::CCommUSB(int type)     // 5-way jump table -> 5 transport backends
CCommUSB::SwitchToAdfu()
CCommUSB::ADFUWrite(uchar* buf, ulong len, uchar cmd, uchar* extra)
CCommUSB::ADFURead (uchar* buf, ulong len, uchar cmd, uchar* extra)
CCommUSB::ExtCommand / Switch / CallingEntry / PollingReady / GetSysInfo / DoUserCommand
```

Exported RVAs (imagebase `0x10000000`): `ADFURead 0x00cf80`, `ADFUWrite 0x00cfc0`,
`ExtCommand 0x00d190`, `SwitchToAdfu 0x00ce90`. These are **thin stubs** dispatching via
the inner object's vtable — `[vt+0x1c]` = read, `[vt+0x20]` = write.

Backend type 0: ctor `0x10001030` sets **vtable `0x1001e6fc`** →
real `ADFURead = 0x10001880`, real `ADFUWrite = 0x100012b0`.

## Framing — USB Bulk-Only Mass Storage CBW

Built on the stack at `ebp-0x40`:

```
CBW[0..3]  = "USBC"  (0x43425355)
CBW[4..7]  = tag
CBW[8..11] = data_len
CBW[12]    = flags       write: 0x00     read: 0x80
CBW[13]    = lun
CBW[14]    = cdb_len     write: 0x10     read: 0x0c
CBW[15+n]  = CDB[n]
```

Status reply is the usual `USBS` (`0x53425355`). This is the same wrapper
`actions_flash` and `nfd/atj2127decrypt`'s `adfu.py` already build — **only the CDB
contents differ between generations.**

## ADFUWrite command map

Dispatch `cmp 0x47`; index table `0x100017d0` (72 entries), jump table `0x100017bc`.
`CDB[1] &= 0x7f`.

| cmds | CDB |
|---|---|
| `0x00-0x06, 0x08` | `CDB[0]=5`, `[2..3]=extra[0..1]`, `[6]=cmd`, `[7..8]=len` (bytes, 16-bit) |
| `0x10, 0x46, 0x47` | `CDB[0]=9`, `[1]=0x70` (cmd `0x46`) / `0x71` (cmd `0x47`), `[2..5]=extra[0..3]` (32-bit addr), `[7]=len>>9`, `[8]=len>>17` (**512-byte sectors**) |
| `0x11` | `CDB[0]=8`, `[2..5]=addr32`, `[7]=len>>9`, `[8]=len>>17` |
| `0x15, 0x16, 0x17` | `CDB[0]=0xb0`, `[1]=cmd-0x15`, `[2..5]=extra[0..3]`, `[7..8]=len` (bytes) |

## ADFURead command map

Dispatch `cmp 0x42`; index table `0x10001d98`, jump table `0x10001d84`.
`flags=0x80`, `cdb_len=0x0c`, `CDB[1] |= 0x80`.

| cmds | CDB |
|---|---|
| `0x00-0x06, 0x08` | `CDB[0]=5`, `[2..3]=extra`, `[6]=cmd`, `[7..8]=len`; also sets `data_len` |
| `0x10` | `CDB[0]=9`, `[2..5]=addr32`, `[7]=len>>9`, `[8]=len>>17` |
| `0x11` | `CDB[0]=8`, `[2..5]=addr32`, `[7]=len>>9`, `[8]=len>>17` |
| `0x42` | `CDB[0]=0xca`, `[1]=0xf6`, `[7..8]=len` |

## Semantics

`CDB[0]` is a SCSI-style opcode: **8 = READ, 9 = WRITE**. The Read/Write function only
sets the USB *direction*. Therefore:

```
flash read   ->  ADFURead (buf, len, cmd=0x11, extra=addr32)
flash write  ->  ADFUWrite(buf, len, cmd=0x10, extra=addr32)
```

address in `CDB[2..5]`, length in **512-byte sectors**.

## Cross-check against the older generation

`nfd/atj2127decrypt` `dfu/adfu.py` (ATJ2127) and `ilyakurdyukov/actions_flash`
`payload_arm/adfus.c` (ATJ2157, a reverse-engineered payload **in C**) both confirm the
concepts, with a *different* CDB encoding:

```c
/* payload_arm/adfus.c — device side, ATJ2157 */
uint32_t cmd  = *(uint32_t*)&usb_buf[0x10];   /* CDB[1..4] */
uint32_t len  = *(uint32_t*)&usb_buf[0x14];
uint32_t addr = *(uint32_t*)&usb_buf[0x18];
switch (cmd & 0x7f) {
  case 0x10: cmd_flash(addr >> 24, addr & 0xffffff, len);  /* type in top byte */
  case 0x13: RAM read/write;  case 0x20: switch;  case 0x21: execute;
}
static void cmd_flash(...) {
  if (cmd == 0x80) { flash_fn(...); usb_send_buf(buf, n << 9); }   /* 0x80 = READ */
  else             { usb_recv_buf(buf, n << 9); flash_fn(...); }   /* WRITE */
}
```

Sector addressing (`n << 9`) and the read/write split match. ATJ2127/2157 packs the
command as a u32 at `CDB[1..4]` with `CDB[0]=0xcd`; LARK uses `CDB[0]=8/9` with the
address at `CDB[2..5]`. **Two genuinely different generations.**

## Implementing a LARK host tool

Start from `nfd/atj2127decrypt`'s `dfu/adfu.py` — its `make_msc_cmd()` already emits the
correct CBW, uses EP_IN `0x81`, and checks the `USBS` status. Replace only
`make_adfu_cmd()` with the CDB layouts above.

Sequence:
1. `dbg reboot adfu` on the UART shell → device enumerates `10d6:10d6`
2. Upload the official LARK `adfus.bin` to `0x118000` (boot-ROM path; `actions_flash`'s
   `simple_switch` already does this correctly — it is only the *subsequent* commands
   that fail)
3. `ADFURead(cmd=0x11, extra=addr, len=n*512)` to read flash

**Unverified detail:** whether `CDB[2..5]` holds a *sector* or *byte* address. Probe at
offset 0 and compare against known-good `dbg fread spi_flash 0x0` output before trusting it.

---

# Live results — what has actually been tried

## USB endpoints (measured, not assumed)

```
device 10d6:10d6, 1 config, interface 0, class 0xff/0xff/0xff (vendor specific)
  ep 0x81  IN   bulk  maxpkt 512
  ep 0x02  OUT  bulk  maxpkt 512
```

**`EP_OUT` is `0x02`, not `0x01`.** Enumerate rather than assume — `actions_flash`
hardcodes different values.

## Confirmed: the framing is correct

Sending a `cmd 0x11` flash-read CBW to the **boot ROM** (payload uploaded but not yet
executed) returns a well-formed CSW:

```
55 53 42 53   "USBS"
00 00 00 00   tag
00 00 00 00   residue
02            status = 2
```

Status `2` is exactly what `adfus.c` sets for an unsupported opcode
(`default: usbs_error = 2;`). So CBW construction, endpoints and transport are all
correct — **the boot ROM simply does not implement flash commands. The payload must be
running.**

## The payload handoff — why `actions_flash` fails here

`actions_flash`'s `switch` sends the ATJ2157 `CMD_ADFU_SWITCH`. LARK does not honour it:
the device silently leaves ADFU and boots normally (observed twice, reproducible).

The vendor tool uses different commands entirely. From `CCommUSB::Switch` (`0x10001e40`)
and `CCommUSB::CallingEntry` (`0x10001f80`), CBW base `ebp-0x3c` so `CDB[0]` = `ebp-0x2d`:

| function | vtable | cdb_len | CDB[0] | CDB[1..2] |
|---|---|---|---|---|
| `Switch` | `+0x24` | `0x10` | **`0x10`** | 2-byte param from `arg[0..1]` |
| `CallingEntry` | `+0x28` | `0x10` | **`0x20`** | 2-byte param from `arg[0..1]` |
| `PollingReady` | `+0x2c` | — | — | — |
| `SwitchToAdfu` | `+0x0c` | — | — | — |

Crucially these put the opcode **directly in `CDB[0]`**, unlike `ADFUWrite`/`ADFURead`
which map `cmd` through a dispatch table to `CDB[0] = 5/8/9/0xb0`.

`CDB[0]=0x20` matches ATJ2127's documented `case 0x20: switch_addr = addr | 1;` — two
independent sources agreeing that `0x20` is the execute/switch command.

**Open question:** the parameter is only **2 bytes**, not a 32-bit address. Either the
entry address is implicit (the payload always lives at `0x118000`) or it is a selector.
Probe carefully.

## Working sequence (next to try)

1. `dbg reboot adfu` on the UART shell → `10d6:10d6`
2. `actions_dump write_mem 0x118000 0 0 adfus.bin` — upload only, **no** `switch`
   (this step works today)
3. `CallingEntry`: CBW with `cdb_len=0x10`, `CDB[0]=0x20`, `CDB[1..2]=param`
4. `read_flash`: `ADFURead` `cmd 0x11` → `CDB[0]=8`, `CDB[2..5]=addr`, `CDB[7..8]=sectors`

Steps 1, 2 and 4 are implemented and proven to exchange valid CBW/CSW. **Only step 3 is
untested.**

## Side effect observed

After executing a payload via `actions_flash`'s (wrong) `switch`, the device returns to
normal mode and boots fine — SD card mounts, USB enumerates — but **the UART console
stays silent**. A warm reset does not restore it; a full power cycle is likely needed.
Nothing is written to flash, and the device is otherwise unharmed.
