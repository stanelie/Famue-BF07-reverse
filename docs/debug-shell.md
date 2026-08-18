# The debug shell

Zephyr's shell, on the debug UART at **2,000,000 baud**. Full access, no authentication.

The tools locate the adapter themselves (`tools/serialport.py`); pass `--port`
or set `$BF07_PORT` to override. See *Choosing the serial port* in
[flashing.md](flashing.md).

```
uart:~$ help
  app  audio  clear  date  dbg  device  devmem  fs  hci_log  help  history
  i2c  kernel  resize  shell  system
```

## Commands that matter

```
dbg mdw <addr> [count]      display memory by word    <-- the workhorse
dbg mdh / mdb               half-word / byte
dbg mww <addr> <val> [n]    memory WRITE by word      <-- verified working
dbg fread <dev> <offset>    raw flash read (512 B) — returns CIPHERTEXT
dbg dumpbuf <addr> <len> <path>   BROKEN for XIP — silently writes zeros
dbg nvram <name> [val]      read/write NVRAM keys
dbg nvdump                  dump all NVRAM regions
dbg reboot [adfu|jtag]      reboot, optionally into ADFU
dbg resman / uimem / lvglheap     UI + LVGL diagnostics
fs ls / cat / read / write  filesystem
kernel threads / version    thread list (gives function entry addresses)
```

## Traps

- **`devmem` is stubbed.** It returns `0x0` for every address, including known-good RAM.
  Use `dbg mdw` instead. (This cost real time before it was checked against a live address.)
- **`dbg dumpbuf` silently writes zeros** for the XIP region while reporting success.
- **Paths need a leading slash before the drive**: `/SD1:/file.bin`, not `SD1:/file.bin`.
- **Async log noise interleaves into command output.** Any parser must tolerate lines like
  `lcd idle`, `<I> post fps 2`, `<I> charge not enabled yet` appearing mid-response.
- **Sessions can wedge.** After a hard stall, the whole session stays dead until the serial
  port is reopened. The device itself is fine.

## Useful reconnaissance

`kernel threads` gives entry addresses for every thread — a free set of known-good code
addresses for validating memory reads:

```
launcher 0x1004d87d   ui_service 0x100d9b9d   shell_uart 0x10075b49
media 0x100b589d      res_preload 0x100e1559
```

(Clear the Thumb bit — subtract 1 — before reading.)

`dbg nvdump` reveals all persistent config, e.g. `SYS_SOFTWARE_VERSION=BF07`,
`BT_PRE_NAME=BF01_` (the firmware lineage), `PA_OUTPUT_GPIO=54`, `EARPHONE_DETECT_GPIO=63`.

## Register access

`dbg mww` works and the SDK's **leopard** register map applies to LARK. Verified by
write/readback/restore on `RTC_REMAIN3`:

```
RTC_REG_BASE = 0x4000c000
  WD_CTL      = 0x4000c020
  RTC_REMAIN2 = 0x4000c038
  RTC_REMAIN3 = 0x4000c03c
```

Reboot types (`soc_pm.h`): `NORMAL 0x000`, `GOTO_ADFU 0x100`, `GOTO_SYSTEM 0x200`,
`GOTO_RECOVERY 0x300`, `GOTO_BTSYS 0x400`, `GOTO_WIFISYS 0x500`, `GOTO_SWJTAG 0x600`,
`GOTO_OTA 0x700`. Writing `(0x4252 << 16) | type` to `RTC_REMAIN3` then `0x5f` to
`WD_CTL` is what `sys_pm_reboot()` does — but see [dead-ends.md](dead-ends.md), it does
not actually change mbrec's decision on this device.
