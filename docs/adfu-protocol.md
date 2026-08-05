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

## Handoff attempt — result (tested)

`Switch` (`CDB[0]=0x10`) and `CallingEntry` (`CDB[0]=0x20`) were both sent to the boot ROM
with the payload uploaded at `0x118000`, params 0 and 1, `cdb_len=0x10`, no data phase.

**All four returned CSW status 2 (unsupported).** Subsequent `cmd 0x11` flash reads also
returned status 2 rather than data.

Crucially the device **stayed in ADFU and the boot ROM kept responding** (`adfu_info` still
returns `CADFUD`) — unlike `actions_flash`'s `switch`, which reproducibly drops the device
out of ADFU into a normal boot.

### Interpretation

`Switch` / `CallingEntry` are almost certainly commands the **running payload** implements,
not boot-ROM commands. Compare `payload_arm/adfus.c` (ATJ2157), where `case 0x20` is handled
*inside the payload*. So they cannot be used to start it.

The boot ROM does speak `actions_flash`'s ATJ-style protocol — `write_mem 0x118000` succeeds
against it. What is still unknown is the boot-ROM command that **transfers control** to the
uploaded payload on LARK. `actions_flash`'s `CMD_ADFU_SWITCH` reaches the device (it visibly
reacts, by rebooting) but does not start the LARK payload correctly.

### Remaining leads

1. **`adfus_u.bin`** — the SDK ships this alongside `adfus.bin`; the `_u` variant may be the
   USB-entry build. Untried.
2. **Payload arguments.** `actions_flash`'s `nandread_init` for ATJ2157 writes an args block
   at `0x11fff0` (code `0x11e000`, buffer `0x11a000`) before executing. The LARK payload may
   likewise expect setup that a bare switch does not provide.
3. **Disassemble the vendor tool's caller.** `ProductionCC.dll` imports these functions —
   following the call sequence there would show the exact order and parameters used
   (`SwitchToAdfu` → `ADFUWrite` → ? → `PollingReady`).
4. The FWU manifest lists `ADFUS.BIN | 1146880 | 8` — the trailing `8` is an unexplained
   parameter that may be the entry/mode selector.

## The vendor tool's scripting API (ProductionPY.dll)

`ProductionPY.dll` exposes the production flow to the tool's Python layer. Complete list
of `Py_*` entry points:

```
Py_OpenDevice  Py_ReOpenDevice  Py_CloseDevice  Py_EjectDevice  Py_DetachDevice
Py_SwitchToAdfu
Py_DownloadMem      upload to RAM
Py_UploadMem        read RAM
Py_SwitchFW         -> CCommUSB::Switch        (CDB[0]=0x10)
Py_CallEntry        -> CCommUSB::CallingEntry  (CDB[0]=0x20)
Py_PollReady        -> CCommUSB::PollingReady
Py_DownloadImage    write a flash image
Py_Send  Py_Recv  Py_DoUserComm  Py_GetStatus  Py_SetCommTimeout
Py_OpenFirmware  Py_CloseFirmware  Py_GetFirmwareBin  Py_ReadFileInFW  Py_GetImageSize
Py_GetProductionConfig  Py_SetProductionConfig
Py_UpdateProgress  Py_UpdateStatus  Py_UpdateCapacity  Py_ShowMessage
```

**Implied sequence:**
`OpenDevice -> SwitchToAdfu -> DownloadMem(payload) -> SwitchFW / CallEntry -> PollReady
-> DownloadImage / Send / Recv`

This confirms `Switch`/`CallingEntry`/`PollingReady` are *script-level* steps, and that
the payload is uploaded with **`Py_DownloadMem`**, not with the ATJ-style `write_mem` we
borrowed from `actions_flash`.

### Likely correction to the opcode semantics

`Py_DownloadMem`/`Py_UploadMem` (memory) exist alongside `Py_DownloadImage` (flash), which
suggests `CDB[0]` distinguishes *memory* from *storage*, with direction coming from the
CBW `flags` byte — not "8 = READ, 9 = WRITE" as first assumed:

| | `ADFURead` (flags 0x80) | `ADFUWrite` (flags 0x00) |
|---|---|---|
| cmd `0x10` -> `CDB[0]=9` | read **flash** | write **flash** |
| cmd `0x11` -> `CDB[0]=8` | read **memory** | write **memory** |

Evidence for this reading: `ADFURead` cmd `0x10` maps to `CDB[0]=9`, which makes no sense
if 9 meant "write". And `ADFUWrite` cmds `0x46`/`0x47` are `CDB[0]=9` with `CDB[1]=0x70`/
`0x71` — i.e. *variants* of the same storage operation.

**Consequence: a flash dump may need `ADFURead(cmd=0x10)`, not `cmd=0x11`.** Both should
be tried. The earlier `cmd 0x11` attempts returned CSW status 2, consistent with it being
a memory op the boot ROM does not implement.

### Still missing

The parameter values for `SwitchFW` / `CallEntry` (16-bit each) and the exact
`Py_DownloadMem` command encoding. Those live in the tool's Python modules, which are
**encrypted on disk** (`SdkCrypt.dll`); `python27.zip` in the newer tool builds contains
only the CPython stdlib (1101 files, no vendor scripts).

Two remaining routes: reverse `SdkCrypt.dll` to decrypt the `.PYD` modules, or capture the
tool's USB traffic on Windows.

---

# IMPORTANT CORRECTION: there are five backends, not one protocol

Everything documented above was decoded from **backend type 0 only**. That was an error.

`CCommUSB::CCommUSB(int type)` dispatches through a 5-way jump table and instantiates one
of **five different transport classes, each with its own vtable and its own ADFU command
set**. Slot 8 = ADFUWrite, slot 7 = ADFURead in every vtable, but they point at different
implementations.

In HardwareEx 2.10.13 (`imagebase 0x10000000`):

| backend | alloc | inner ctor | vtable | ADFUWrite | ADFURead |
|---|---|---|---|---|---|
| type 0 | `0x454` | `0x10001030` | `0x10023b3c` | `0x100012b0` | `0x10001880` |
| type 1 | `0x854` | `0x10003600` | `0x10023ca4` | `0x10003a30` | `0x10004030` |
| types 2,3,4 | — | not yet resolved | — | — | — |

**The ADFU code is byte-identical between tool 2.07.09 and 2.10.13** — same function
addresses, same dispatch bounds, same tables. Newer tool ≠ newer protocol. The real
variation is *across backends*, not versions.

## type 0 (documented above)
`ADFUWrite`: `cmp cmd, 0x47`, tables `0x100017d0`/`0x100017bc`, `cdb_len=0x10`
`ADFURead` : `cmp cmd, 0x42`, tables `0x10001d98`/`0x10001d84`, `cdb_len=0x0c`
Device-version branch on `[this+0x418]` vs **0x42**.

## type 1 — a different protocol entirely
`ADFUWrite`: `cmp cmd, 0x25`, tables `0x10003fb0`/`0x10003f8c`, `cdb_len=0x10`
`ADFURead` : `cmp cmd, 0x21`, tables `0x100045c8`/`0x100045b0`, `cdb_len=0x10` (**not 0x0c**)
Device-version branch on `[this+0x418]` vs **0x41**.

ADFUWrite command map (CDB[0] per handler):

| cmds | CDB[0] |
|---|---|
| `0x00 0x01 0x02 0x04 0x05 0x06 0x08` | `0x15` |
| `0x1e` | `0xc9` |
| `0x1f` | `0xb0` |
| `0x20` | `0xb0` |
| `0x22` | `0xc6` |
| `0x23` | `0xc7` |
| `0x24` | `0xc8` |
| `0x25` | `0xb0` |
| everything else | unsupported |

ADFURead supports `0x00-0x02, 0x04-0x06, 0x08, 0x1e, 0x1f, 0x20, 0x21`.

## Why this matters

The hardcoded commands found at `ProductionCC.dll` call sites are **`0x32`, `0x14`
(ADFUWrite) and `0x1F` (ADFURead)**. Against type 0 all three map to "return 0". But
**`0x1F` is supported by type 1** — so ProductionCC is using **backend type 1** for at
least some operations, and the type-0 tables documented above may be the wrong variant
for this device entirely.

`0x32` and `0x14` are supported by *neither* type 0 nor type 1, so at least one of the
unresolved backends (2, 3, 4) is also in use.

## Open question

Which backend does a LARK device use? That is the `int type` argument to the constructor,
chosen by the caller. Resolving backends 2-4 and finding where `type` is set is the next
static step — it may well explain why our `cmd 0x10`/`0x11` probes were rejected: we were
speaking the **type-0 dialect to a device that expects another**.

---

# RESOLVED: the tool uses backend **type 4**

`ProductionCC.dll` constructs the transport with a literal:

```
1002752b  push  4
1002752d  call  0x100dc8b8          ; operator new
10027545  push  4                   <-- type = 4
10027547  mov   ecx, [ebp-0x54]
1002754a  call  dword ptr [CCommUSB ctor]
```

**`CCommUSB(4)`** — so backend **type 4** is the live one. Types 0 and 1 (which all the
earlier analysis was based on) are not what the tool uses against a modern device.

## All five backends (HardwareEx 2.10.13, imagebase 0x10000000)

| type | alloc | inner ctor | vtable | ADFUWrite | ADFURead | write bound | read bound |
|---|---|---|---|---|---|---|---|
| 0 | `0x454` | `0x10001030` | `0x10023b3c` | `0x100012b0` | `0x10001880` | `0x47` | `0x42` |
| 1 | `0x854` | `0x10003600` | `0x10023ca4` | `0x10003a30` | `0x10004030` | `0x25` | `0x21` |
| 2 | `0x454` | `0x1000ed80` | `0x10023d8c` | `0x1000eea0` | `0x1000f3e0` | — | — |
| 3 | none | — | — | — | — | null backend | |
| **4** | `0x854` | `0x10010150` | `0x10023df4` | `0x100101c0` | `0x10010940` | **`0x3a`** | **`0x43`** |

## type 4 — ADFUWrite (bound `0x3a`, idx `0x100108ac`, jmp `0x1001087c`)

| cmds | CDB[0] |
|---|---|
| `0x00 0x01 0x02 0x04 0x05 0x06 0x08` | `5` |
| `0x10` | `0xb0` |
| **`0x14`** | **`8`** |
| `0x1e` | `0xc9` |
| `0x1f 0x38 0x39 0x3a` | `0xb0` |
| `0x22 0x23 0x24` | `0xc9` |
| `0x25 0x26` | `0xb0` |
| **`0x32`** | **`0xb0`** |

## type 4 — ADFURead (bound `0x43`, idx `0x10010e28`, jmp `0x10010e0c`)

| cmds | CDB[0] | notes |
|---|---|---|
| `0x00 0x01 0x02 0x04 0x05 0x06 0x08` | `5` | takes `extra[0..3]` |
| `0x1e` | `0xca` sub `0xf0` | length only, no address |
| `0x27` | `0xca` sub `0xf1` | length only |
| **`0x32`** | **`0xb0`** | **takes `extra[0..3]` — address-carrying** |
| `0x42` | `0xca` sub `0xf6` | length only |
| `0x43` | `0xca` sub `0xf5` | length only |

## CRITICAL: type 4 uses a different CDB field layout

In type 4 the `extra[0..3]` bytes go to **`CDB[1..4]`** (`ebp-0x30` … `ebp-0x2d`):

```
10010a8b  mov byte [ebp-0x31], 0xb0     ; CDB[0]
10010a8f  mov eax,[ebp+0x14]            ; extra
10010a94  mov byte [ebp-0x30], cl       ; CDB[1] = extra[0]
10010a9d  mov byte [ebp-0x2f], al       ; CDB[2] = extra[1]
10010aa6  mov byte [ebp-0x2e], dl       ; CDB[3] = extra[2]
10010aaf  mov byte [ebp-0x2d], cl       ; CDB[4] = extra[3]
```

Type 0 placed the address at **`CDB[2..5]`**. **Our device probes used the type-0 layout —
wrong offsets and wrong opcodes.** That is very likely why every attempt returned status 2.

## Commands the tool actually hardcodes

```
ADFUWrite cmd 0x32   (ProductionCC 0x10015836)  -> type4 handler[10], CDB[0]=0xb0
ADFUWrite cmd 0x14   (ProductionCC 0x100158a2)  -> type4 handler[2],  CDB[0]=8
ADFURead  cmd 0x1F   (ProductionCC 0x1002a1c2)  -> not in type 4; type 1 only
```

## Best candidate for a flash dump

**`ADFURead(cmd=0x32)`** — the only address-carrying read in type 4 (`CDB[0]=0xb0`,
`CDB[1..4]=addr`). Paired with `ADFUWrite(cmd=0x32)` which has the identical layout, this
looks like the address-set / bulk-transfer pair.

Untested. This supersedes the earlier `cmd 0x11` recommendation, which came from the wrong
backend.

---

# Structural conclusion: CCommUSB describes the PAYLOAD, not the boot ROM

Tested on hardware, all against a device confirmed in ADFU with the boot ROM responding
(`adfu_info` returns `CADFUD` before and after every attempt):

| dialect | commands tried | result |
|---|---|---|
| type 0 | read `0x10`/`0x11`, sector+byte addr | **status 2** |
| type 0 | `Switch` `CDB[0]=0x10`, `CallingEntry` `CDB[0]=0x20`, params 0/1 | **status 2** |
| type 4 | info reads `0x1e/0x27/0x42/0x43` (`CDB[0]=0xca`, subs `f0/f1/f6/f5`) | **status 2** |
| type 4 | address read `0x32` (`CDB[0]=0xb0`, addr in `CDB[1..4]`) | **status 2** |
| type 4 | `CallingEntry` `CDB[0]=0xb0` + 32-bit param `0x118000`/`0x118001`/`0` | **status 2** |
| type 4 | `Switch` `CDB[0]=0x10` + 32-bit param | **status 2** |

Meanwhile the boot ROM **does** accept `actions_flash`'s ATJ-style commands — `adfu_info`
(`0xcc`) and `write_mem 0x118000` both succeed.

**Therefore: the entire `CCommUSB` command set — every backend, every dialect — is the
protocol spoken by the *running payload*, not by the boot ROM.** None of it is reachable
until `adfus.bin` is executing. All the reverse engineering above is correct but describes
the wrong side of the handoff.

## What this narrows the problem to

Starting the payload is a **boot-ROM** operation, and the boot ROM speaks the ATJ dialect.
`actions_flash` has exactly one such command (`CMD_ADFU_SWITCH`, via `switch <addr>`), and
on LARK it reproducibly drops the device out of ADFU into a normal boot — consistent with
the payload starting and immediately faulting.

The most plausible remaining explanation is **missing entry setup**. For ATJ2157,
`actions_flash`'s `nandread_init` writes an argument block before executing:

```
code_addr = 0x11e000   buf_addr = 0x11a000   args_addr = 0x11fff0
```

A bare `switch` provides none of that. The LARK `adfus.bin` may require an equivalent
structure (stack pointer, buffer address, arg block) that the vendor tool sets up via
`Py_DownloadMem` before calling `Py_CallEntry`.

## Next steps, in order of cost

1. **Disassemble the LARK `adfus.bin` entry code** (`0x118000`, 47608 bytes, ARM Thumb) to
   see what it reads at startup — it begins `ldr r0,[pc,#0x23c]; mov sp,r0`, so at minimum
   it takes a stack pointer from a literal. Determine whether it expects an arg block.
2. **USB capture on Windows** — still definitive, and now with a much smaller question:
   just the bytes between `DownloadMem` and the first successful read.

---

# BREAKTHROUGH: the payload load address is `0x01010000`, not `0x118000`

Disassembling the LARK `adfus.bin` entry stub (file offset 0) settles it:

```
00118000  ldr r0,[pc,#0x23c]   ; = 0x01007ff0     stack pointer
          mov sp, r0
          ldr r0,[pc,#0x23c]   ; = 0xe000ed08     VTOR
          ldr r1,[pc,#0x240]   ; = 0x01010100     vector table
          str r1,[r0]
          ldr r1,[pc,#0x240]   ; = 0x0101ba00     BSS start
          ldr r2,[pc,#0x240]   ; = 0x0101ff69     BSS end
          ... zero BSS ...
          ldr r0,[pc,#0x234]   ; = 0x01012541     entry
          bx  r0
```

All absolute addresses are `0x0101xxxx`. Cross-checked against the file:

| | |
|---|---|
| file size | `0xb9f8` (47608) |
| load at `0x01010000` → image ends | `0x0101b9f8` |
| BSS start from code | `0x0101ba00` — an 8-byte gap ✓ |
| vector table `0x01010100` | file offset `0x100` ✓ |
| entry `0x01012541` | file offset `0x2540` ✓ |
| **vector[1] (reset vector)** | **`0x01010000`** ✓ |

**`0x118000` is the ATJ2157 address from `actions_flash`'s README and is wrong for LARK.**

This also explains the long-standing crash: loaded at `0x118000` the stub still runs, sets
VTOR to an empty `0x01010100`, zeroes nothing, then `bx` to `0x01012541` where nothing was
loaded → fault → watchdog reset → normal boot. Exactly the observed behaviour, every time.

## Result of loading at the correct address

```
write_mem 0x01010000 0 0 adfus.bin     -> OK
switch    0x01010000                    -> OK
```

The device **stayed enumerated as `10d6:10d6`** instead of rebooting to normal mode — the
first time that has ever happened. So the payload starts.

But it then **hangs**: bulk writes to EP `0x02` time out, and even the classic boot-ROM
`adfu_info` (`0xcc`) times out. Endpoints are unchanged (`0x02` OUT / `0x81` IN). The
device is enumerated but not servicing USB. Requires a physical reset.

## Next hypothesis: the Thumb bit

The reset vector is `0x01010000` (even), but this is a Cortex-M — code entry must have bit 0
set or the core faults on entering ARM state. The payload's own code does
`ldr r0,=0x01012541` (odd) then `bx r0`, i.e. it sets the bit for its internal jump.

`actions_flash`'s `adfu_switch()` passes the address unmodified, so the core is likely
entered at `0x01010000` in ARM state. Compare `simple_exec`, which uses `addr | 1`.

**Try `switch 0x01010001`.** Untested.

## Thumb-bit test and PollingReady — both negative

`switch 0x01010001` (Thumb bit set) behaves the same as `0x01010000`: device stays
enumerated as `10d6:10d6`, payload starts, then hangs.

Progression of failure modes across the three load attempts is informative:

| load addr | switch | result |
|---|---|---|
| `0x118000` | `0x118000` | device **reboots to normal mode** (payload faults, watchdog) |
| `0x01010000` | `0x01010000` | stays in ADFU; **CBW writes fail** immediately |
| `0x01010000` | `0x01010001` | stays in ADFU; **CBW writes succeed**, no response; then hangs |

So the correct load address genuinely changes behaviour, and the Thumb bit changes it
again — the device now accepts one or two CBWs before ceasing to drain the OUT endpoint.
Something is executing; it just never replies.

`PollingReady` (type 4, `CDB[0]=0xb0`, `CDB[1..4]=0`, 2-byte read, `flags=0x80`) was also
tried as the missing handshake step. First two CBWs accepted, no data returned, then the
endpoint stopped accepting writes. Physical reset required.

## Remaining hypotheses

1. **Missing entry setup.** The vendor uses `Py_DownloadMem` (not the ATJ `write_mem` we
   borrow) and may write an argument block / buffer pointers before `Py_CallEntry`. For
   ATJ2157 `actions_flash` writes args at `0x11fff0` with `code 0x11e000`, `buf 0x11a000`;
   the LARK equivalents are unknown. The payload's own literals give
   SP `0x01007ff0`, VTOR `0x01010100`, BSS `0x0101ba00..0x0101ff69`, entry `0x01012541` —
   but nothing there identifies a host-supplied arg block, so it may be elsewhere.
2. **Wrong BROM entry command.** `actions_flash`'s `switch` (`CMD_ADFU_SWITCH`) may not be
   the mechanism LARK's boot ROM uses to hand over. `Py_CallEntry`/`Py_SwitchFW` are
   *payload*-side (they use the CCommUSB CBW dialect), so the boot-ROM-side handover is
   still unidentified.
3. **Host must re-open after handover.** If the payload re-initialises the USB controller,
   the host handle may need closing and reopening. Descriptors were unchanged and the device
   did not re-enumerate, so this is less likely, but untested.

## Upload verified; settle-time and fresh-handle also negative

`write_mem 0x01010000` followed by `read_mem 0x01010000 0x40` returns the payload bytes
**exactly**:

```
readback  8f48 8546 8f48 9049 0160 9049 904a 0020 ...
source    8f48 8546 8f48 9049 0160 9049 904a 0020 ...
```

So the memory at `0x01010000` is real and writable, the upload lands correctly, and the
load address is confirmed a fourth independent way. **The upload is not the problem.**

Also tried: `switch 0x01010001`, then an 8-second settle (in case the payload re-initialises
the USB controller), then a *fresh* libusb handle with 8-second timeouts, for `PollReady`,
type-4 address read `0x32`, type-4 info read `0x42`, and the boot-ROM `adfu_info`.

**All four: CBW write accepted, no data ever returned.**

That combination — writes accepted but never answered, and the boot ROM also silent — means
control has left the boot ROM but whatever is now running does not service the IN endpoint.
The payload starts and stalls.

## State of the problem

Confirmed working: ADFU entry, payload upload (verified by readback), correct load address,
and a handover that visibly changes CPU behaviour.

Unsolved: getting the started payload to answer. Every host-side dialect recovered from the
vendor DLL has been tried against it.

The boot ROM is mask ROM inside the SoC and is in none of the files available, so the exact
handover it expects cannot be recovered by static analysis. **A USB capture of the vendor
tool on Windows is now the only route that can answer it** — it would show the precise byte
sequence between upload and first successful read.


---

# SOLVED — the real boot-ROM protocol, from a live capture

Captured 2026-08-05 with `tools/adfu-mock` (Raspberry Pi 4 in USB gadget mode
impersonating `10d6:10d6`). The Windows Multimedia Product Tool was fed a
**classic-ATJ** firmware deliberately; the BF07 was never connected.

## The mistake that blocked everything

Every command synthesised from `HardwareEx.dll` put the opcode in **`CDB[0]`**.
The boot ROM does not work that way:

> **`CDB[0]` is a constant vendor escape byte `0xCD`. The opcode is in `CDB[1]`.**

So every probe we ever sent looked like an unknown opcode, and CSW status `2`
was the ROM behaving correctly. The `CCommUSB` reverse engineering was accurate
about *values* and wrong about *placement*.

## CDB layout

```
CDB[0]      = 0xCD           vendor escape, constant
CDB[1]      = opcode
CDB[2..4]   = 0              reserved
CDB[5..8]   = length, LE32   (mirrors CBW dlen)
CDB[9..12]  = address, LE32
CDB[13..15] = 0
```

All five captured commands agree: the length field matches `dlen` exactly in
every case, and the address field decodes to the known ATJ load address
`0x118000`. The layout is unambiguous — `[5..8]`/`[9..12]` is the only field
pair that fits (`[4..7]` gives `0x140000`, `[10..13]` gives `0x1180`).

## Opcodes

| opcode | direction | meaning |
|---|---|---|
| `0x13` | host→device | write memory at address |
| `0x20` | none | **execute at address** — the handover |
| `0x21` | none | execute at address, second variant |
| `0x23` | device→host | read chip-identity block |

## Captured sequence

```
cd 13  5120 B -> 0x118000     probe stage 1
cd 20         -> 0x118000     run it
cd 13  1536 B -> 0x11e000     probe stage 2
cd 21         -> 0x11e000     run it
cd 23   156 B <- 0            read chip identity
```

The tool stopped there because the mock answered 156 zero bytes.

## The probe payload

`payload_0001.bin`: 5120 B transferred, 4952 B real (zero-padded to a 256-byte
boundary), md5 `0f99f457be845ede0ad790ecb7ba3b87`. Close to but not identical
with the 4,980-byte `ADFUS.BIN` from the FWU SQLite DBs.

Its strings are decisive:

```
ic_version:   BDG_CTL:   jtag_ctl:   auto flag:   wd_ctl_val:
sub op:       err:       sta:        fun_value:
USBC          USBS       USBIRQ_HCUSBIRQ:   USBEIRQ:   OUT_HCINSHORTPCKIRQ:
```

- **`sub op:`** independently confirms the sub-opcode model.
- **`USBC` / `USBS` and the USB IRQ strings** mean the payload brings up and
  services the USB controller *itself*. That explains the BF07 symptom recorded
  above — "enumerated but not servicing USB" after a handover attempt is what a
  payload that started at the wrong entry point, or was never really entered,
  looks like from the host side.

## What this does not yet tell us

The capture is the **classic-ATJ** path (`0x118000`, ~5 KB probe). LARK's
`adfus.bin` is 47,608 bytes at `0x01010000`. The framing is boot-ROM level and
should carry over, but the addresses and the payload do not.

Also unknown is the 156-byte `cd 23` reply format. Feeding the tool a plausible
one is what would push the capture past identification into the actual flash
sequence.

## Confirmation: the captured payload is our decrypted `ADFUS.BIN`

The 4,952 real bytes captured off the wire are a **byte-exact prefix** of the
4,980-byte `ADFUS.BIN` extracted from the FWU SQLite DB by `atjboottool`
(md5 `a389934296afa37d7947844b2f83ac16`). 100% identical, no transformation.

| blob | size | match |
|---|---|---|
| FWU DB `ADFUS.BIN` | 4,980 | **exact prefix** |
| tool FAT32 `ADFUS.BIN` (encrypted) | 48,192 | 0.8% |
| SDK LARK `adfus.bin` | 47,608 | 1.8% |

Three things follow:

1. The FWU decryption chain is correct end to end — the tool uploads exactly
   what we extracted, unmodified.
2. **Payloads are sent in the clear.** No obfuscation on the wire.
3. The transfer is the raw image **zero-padded to a 256-byte boundary**
   (4,980 → 5,120), sent in a **single** `cd 13`. For LARK that means 47,608 →
   47,616 bytes at `0x01010000`.

---

# Live test against the BF07 (2026-08-05)

First run of the corrected `0xCD` framing on real hardware. Device entered ADFU
with `dbg reboot adfu`.

## Confirmed on hardware

```
cd 23  len=156        csw=0    00 e8 00 e8 00 e8 ...   <-- first non-status-2 EVER
cd 13  47616 B -> 0x01010000   csw=0
cd 20          @ 0x01010000    csw=0                   <-- handover ACCEPTED
```

The framing is right. Three years of `status 2` were a malformed CDB.

`cd 23` **ignores its address field** — identical `00 e8` filler at `0x0`,
`0x1000`, `0x118000`, `0x01010000`, `0x18010e00`. It is a fixed status/identity
read, not a memory-read primitive. The filler is what the ROM returns before a
payload populates the buffer (in the classic capture the *probe payload* fills
it in before the tool reads it).

## Still failing

After `cd 20` the device stops servicing USB entirely — reads *and* writes time
out. It stays enumerated as `10d6:10d6` (no reboot), and the UART TX line is
held **continuously low** (118,127 bytes captured, every one `0x00`, one
distinct value). That is a driven-low pin, not output.

So control leaves the boot ROM and something runs, but it services neither USB
nor serial. Physical reset required.

## Ruled out: a wrong load address

All four LARK payloads share a byte-identical entry stub and a vector table at
file offset `0x100` with `initSP=0x2000f000`, `reset=0x01010000`:

| payload | size | load |
|---|---|---|
| `zephyr/tools/…/lark/adfus.bin` | 47,608 | `0x01010000` |
| `zephyr/tools/…/lark/adfus_u.bin` | 48,792 | `0x01010000` |
| `bootloader/tools/…/lark/adfus.bin` | 12,820 | `0x01010000` |
| `bootloader/tools/…/lark/adfus_u.bin` | 13,896 | `0x01010000` |

`0x01010000` is correct for every one of them. `adfus.bin` and `adfus_u.bin`
are **variants, not stages** — unlike the classic path, where stage 1 and
stage 2 went to different addresses (`0x118000` / `0x11e000`).

## Remaining hypotheses

1. **`cd 21`, not `cd 20`.** In the classic capture the device was demonstrably
   responsive after `cd 21` (stage 2), never directly after `cd 20`. Cheap to
   test: one reset cycle.
2. **The smaller `bootloader` build** (12,820 B) may have fewer runtime
   dependencies than the 47,608 B one. One reset cycle.
3. **Post-handover re-enumeration.** The payload sets VTOR and may re-init the
   USB controller; the host handle would go stale. Needs a full device rescan
   after `cd 20`, not just a fresh handle.

## The SDK's LARK `adfus.bin` is built for SPI NAND

`Ver1.1-adfu (build Apr 24 2023 15:17:38)`. Every LARK board in the SDK that
ships an `adfus.bin` is an `*_dev_watch_sdnand` variant, and it shows:

```
'%s spinand init failed, please check...'   'Use spinand lib driver ...'
"Can't get spinand id, Please check!"       'SPINand lib driver init err.'
```

`main()` at `0x01012540` hardcodes the NAND backend:

```
01012540  push {r3, lr}
01012542  bl   0x10124b0            hardware init, runs before any printf
01012546  ldr  r0, ='[D] '      \ printf
01012548  bl   0x1013054        /
0101254c  ldr  r0, ='adfus run' \ printf
0101254e  bl   0x1013054        /
01012552  movs r1, #0
01012554  movs r0, #2              <-- storage type 2 = SPI NAND
01012556  bl   0x1012610           storage_bind()
0101255a  b    0x101255a           <-- infinite self-loop if it returns
```

Storage dispatch (switch at `0x1012564`):

| type | handler | identified by |
|---|---|---|
| 0 | `0x01013e2c` | prints `'spinor0_binding'`, installs a 3-entry fn table — **SPI NOR** |
| 1 | `0x01013bc0` | unknown |
| 2 | `0x01012dbc` | the `spinand` path — **currently selected** |

The BF07 is NOR. A failed NAND probe falling into `b .` at `0x101255a` matches
the observed symptom exactly: `cd 20` accepted CSW 0, then enumerated but
servicing neither USB nor UART.

`tools/patch_adfus.py` flips `02 20` -> `00 20` at file offset `0x2554`
(RAM-only payload; touches no flash).

### Result: negative

The NOR-patched payload wedges identically. So the NAND/NOR mismatch is real
and is *a* bug, but it is not the whole story — or the failure happens before
`main()` even reaches the storage bind.

Note `bl 0x10124b0` runs *before* the first printf. If that hangs, nothing is
printed at all and the storage type is irrelevant.

**Next diagnostic:** capture UART *during* the handover rather than after.
The payload printfs its own progress, so the presence or absence of `adfus run`
splits the problem cleanly:

- `adfus run` appears -> init is fine, the storage bind is the problem
- nothing appears -> it dies in `0x10124b0` or never really enters

`tools/../scratchpad/trace_uart.py` does this (listener started before `cd 20`).

## Payload behaviour matrix

| payload | size | `cd 20` | result |
|---|---|---|---|
| bootloader `adfus.bin` | 12,820 | csw 0 | runs, reboots to normal — self-recovers |
| bootloader `adfus_u.bin` | 13,896 | csw 0 | runs, reboots to normal — self-recovers |
| zephyr `adfus.bin` | 47,608 | csw 0 | wedges |
| zephyr `adfus_u.bin` | 48,792 | csw 0 | wedges |
| zephyr `adfus.bin` NOR-patched | 47,608 | csw 0 | wedges |

`cd 21` against the boot ROM returns **status 2** — it is a command of the
*running stage-1 payload*, not of the ROM, exactly as the classic capture
implies. `cd 20` is the ROM's only handover.

## Correction: the payload runs fine. It was the wrong baud all along.

Earlier this document claimed the UART TX line was "held continuously low" after
`cd 20`, and inferred that the payload was dead. **That was wrong.** The
payload reconfigures UART0 to **115200 baud**:

```
010124d6  mov.w r2, #0x1c200      ; 115200
010124da  movs  r1, #1
010124dc  movs  r0, #0
010124de  bl    0x1013108         ; uart_init(port 0, ?, baud)
```

The Zephyr shell runs at 2,000,000. Every capture was at the shell's rate, so
the all-`0x00` result was misread framing, not a low line.

Listening at 115200 during the handover gives:

```
Ver1.1-adfu (build Apr 24 2023 15:17:38)
system_set_svcc: 0x02680be4
WIO0_CTL: 0x00000000
WAKE_CTL_SVCC: 0x00001157
[D] adfus run
c;c<c5c2c'...          <- structured garbage, ~2-byte period
```

So the payload **executes correctly** through init and past `adfus run`. `cd 20`
is a working handover and always was.

### The garbage is a clock change, not a baud change

A full sliding-window disassembly (decoding at every even offset, since linear
disassembly misaligns on interleaved data) finds:

- **exactly one** call site for `uart_init` — `0x010124de`
- **exactly one** baud-plausible immediate in the whole image — `115200`

The payload therefore never reprograms the UART. The output corrupts because
something after `adfus run` changes the **core/peripheral clock**, shifting the
effective baud while the divisor stays fixed. Effective baud becomes
`115200 x (new_clk / old_clk)`.

That happens inside `storage_bind()` — which is exactly where a NOR/NAND
controller brings up its clock.

**Next:** recapture at `115200 x k`. The ~2-byte repeat period suggests `k = 2`
(230400); `k = 1/2` (57600) is the alternative if the clock dropped instead.

---

# BREAKTHROUGH: `adfus_u.bin` brings up USB (2026-08-05)

**The wrong payload was being used all along.** `adfus.bin` is not the USB
variant — `adfus_u.bin` is (`_u` = USB). Evidence:

| | `adfus.bin` | `adfus_u.bin` |
|---|---|---|
| USB strings | `'Adfus_Irq… usb_receive_cbw_isr - null'` only | **also `'usb out'`** |
| `main()` | `storage_bind(type)` then `b .` | `bl 0x10126f0` then `b .`, **no storage arg** |
| ordering | storage init, *then* poll | **polls first**, storage only once a command arrives |
| poller read size | 16 bytes via a context handle | USB |

`adfus.bin`'s poller (`0x1014758`) reads **16 bytes** through a context stamped
with type byte `8` — not a 31-byte CBW. It is a different transport.

`adfus_u.bin`'s service function `0x010126f0`:

```
010126f4  bl   0x10147e0          ; init
01012708  [sp+0x10] = 0x01014931  \
0101270c  [sp+0x14] = 0x01014a05   > three handler callbacks
01012710  [sp+0x18] = 0x01014a55  /
0101271e  bl   0x1014ad8          ; register the callback struct
01012722  bl   0x1014a18          ; POLL  <-- before any storage init
01012728  cbnz r0, 0x101274a      ; only on a command, touch storage
0101274a  movs r1,#0x32 ; movs r0,#1 ; bl 0x1015068   ; storage_init(1,50)
01012754  movs r1,#0x32 ; movs r0,#2 ; bl 0x1015068   ; storage_init(2,50)
```

It tries storage types **1 then 2, never 0** — the same NAND bias as
`adfus.bin`, in a different shape. One byte at file offset `0x274c`
(`01 20` -> `00 20`) redirects the first attempt to **type 0 = SPI NOR**.

## Result on hardware: USB is ALIVE

With `adfus_u_nor.bin` uploaded to `0x01010000` and started with `cd 20`:

- **Control transfers work.** Live `GET_DESCRIPTOR` returns
  `class ff/ff/ff`, `ep 0x81 IN bulk 512`, `ep 0x02 OUT bulk 512` — the payload
  is running its own USB device stack, not the boot ROM's.
- **EP `0x81` streams 6-byte packets continuously**, with or without any command
  sent:

```
01 04 00 00 63 35
01 05 00 00 63 ae
01 06 00 00 63 32     format: 01 <seq4> 00 00 63 <cksum>
```

  `seq` is a 4-bit counter incrementing per packet delivered; the last byte
  correlates with `seq` parity, so it is likely a checksum.
- No UART output at 115200 (the `_u` build may use a different rate, or none).

## What is still missing

The payload does **not** answer our CBW framing — not the `0xCD` ROM dialect,
not either `CCommUSB` dialect, not classic ATJ. It streams its status packet
regardless.

So the remaining task is now well-posed and small: **recover the command format
the running `adfus_u` payload expects**, from its own poller (`0x1014a18`) and
its three handler callbacks (`0x01014931`, `0x01014a05`, `0x01014a55`).

That is ordinary static analysis of a payload we have, against a device that
now talks back — a far better position than any point earlier in this project.

## Trap: `rm` on low addresses is unreliable

The `rm` handler branches on the address (`cmp r5, #0x40000000; blo ...`). The
low-address path is not a straightforward memory read:

* Immediately after a fresh payload start, `rm 0x01010000` works and returns the
  uploaded payload byte-exactly.
* After a few commands, `rm` on low addresses starts returning a **constant**
  (`d0 5a 00 01 d1 01 00 00 ...`) for *every* address — `0x0`, `0x01010000`,
  `0x18000000` all identical — and then wedges the payload entirely (endpoints
  time out, only a power-on reset recovers).

`rm 0x10000000` did once return real decrypted code — matching `fw_code_full.bin`
at offset `0xe8000`, implying the XIP window maps a different flash offset under
ADFU than when the application runs. That was not reproducible.

**Use `rs` for flash. Do not rely on `rm` for probing.**

### The encryption test does not need `rm` anyway

Whether the SoC encrypts on write is answerable with `ws` + `rs` alone:

| write plaintext P to `fw0_sys` padding, then `rs` | conclusion |
|---|---|
| returns **P** | raw write — the cipher must be broken to patch code |
| returns **something else** | hardware encrypted on write — patching is easy |

If the hardware transforms the write, the stored bytes are `E(P)` and a raw read
shows exactly that. No decrypted view is required.
