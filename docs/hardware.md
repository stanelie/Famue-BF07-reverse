# Hardware

## Identification

| | |
|---|---|
| Product | Famue BF07, 2.7" e-ink reader / audio player |
| Board name | `xlx_58120_bf07` (printed by mbrec every boot; ODM "XLX") |
| SoC | Actions Technology, **LARK** family, ARM Cortex-M |
| ADFU chip id | `0x2351` (cf. ATJ2127 `0x10d6`, ATJ2157 `0x3051`) |
| Chip marking | reported as `ATJ2158` on the package |
| Display | 176 x 264 e-ink, LVGL v8 |
| Flash | 4 MB SPI NOR, **inside the SoC package** |

The SoC codename is confirmed by the driver `lcdc_lark.c` referenced in the firmware's
embedded `WEST_TOPDIR` paths, and by `Actions_LARK_BurnChipID_V1.0` in a vendor config.

## Flash

Boot log prints `read spi nor chipid:0x1166085`. Decoding the low three bytes as JEDEC:

```
manufacturer 0x85 = Puya Semiconductor (P25Q series)
type         0x60
capacity     0x16 = 2^22 = 4 MiB
```

4 MiB is consistent with `fw0_temp` at `0x3ff000` being the last 4 KB of the chip.

Also from the boot log: `nor is 4 line mode` (quad SPI), `SPI0: set rate 96000000 Hz`.

**There is no separate flash chip on the board.** The only two 8-pin parts are an
**AiP4890** audio amplifier (NVRAM `PA_OUTPUT_GPIO = 54`) and an **RDA5807M** FM tuner
(`device list` → `FM (READY)`, NVRAM `APP_RADIO_INFO`). The NOR die is stacked in the
SoC package, so there is nothing to attach a programmer clip to.

## Debug UART

A labelled debug header on the PCB, connected via an FTDI USB-serial adapter.

**Baud rate: 2,000,000.** This is unusual and easy to miss — at 115200 the output is
garbage that looks like line noise rather than an obviously wrong baud rate.

## USB modes

| enumeration | mode |
|---|---|
| `10d6:b00b` | normal — mass storage, "ZEPHYR USB DISK" (exposes the microSD) |
| `10d6:10d6` | ADFU (firmware update) |

Fast detection on macOS — `system_profiler SPUSBDataType` takes 5-20 s and will miss
transient states; `ioreg -p IOUSB -l -w 0` takes ~0.03 s. Look for `"idVendor" = 4310`
(0x10d6) and check `idProduct` (`45067` = 0xb00b, `4310` = 0x10d6).

## Storage devices

`device list` shows both `SD` and `SD1` controllers. The microSD card is on **SD1**
(mounted `SD1:`); `SD` (MMC_0) is unpopulated. This matters — see
[dead-ends.md](dead-ends.md) for why it kills the SD-card OTA path.
