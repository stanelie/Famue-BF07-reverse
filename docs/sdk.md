# The official Actions LARK SDK is public

**https://github.com/lvgl/lv_port_actions_technology**

This is the Actions Technology SDK for the **LARK** SoC family — the BF07's exact
platform. It independently corroborates most of this repo's reverse engineering.

## Proof it's the right platform

The firmware's embedded `WEST_TOPDIR` debug paths match the SDK tree exactly:

- `application/ framework/ thirdparty/ zephyr/ bootloader/`
- `framework/{audio,base,bluetooth,display,media,ota,system,usb}`
- **`zephyr/drivers/display/controller/lcdc_lark.c`** — referenced by our dump
- `framework/ota/ota_backend_sdcard.c` — the code we reverse engineered by hand

Enumerate it in one call:
```
curl -s "https://api.github.com/repos/lvgl/lv_port_actions_technology/git/trees/master?recursive=1"
```
(17,007 paths, not truncated.)

## What it provides

| Path | Why it matters |
|---|---|
| `zephyr/tools/prebuilt/lark/common/bin/adfus.bin` | **The official LARK ADFU payload**, 47,608 B, loads at `0x118000` |
| `zephyr/tools/prebuilt/lark/common/bin/adfus_u.bin` | variant |
| `zephyr/tools/prebuilt/lark/common/firmware.xml` | partition layout + per-partition flags |
| `zephyr/tools/prebuilt/lark/common/bin/encrypt.bin` | 4.2 MB encryption key material |
| `zephyr/tools/prebuilt/lark/bin/mbrec.bin` | the bootloader |
| `bootloader/tools/build_ota_image.py` | official OTA packer — validates our format |
| `bootloader/application/ota_app/src/main.c` | boot decision + recovery logic |
| `bootloader/soc/arm/actions/leopard/soc_pm.{c,h}` | reboot types, RTC registers |

## What it does NOT provide

`application/` contains only `app_demo` and `bt_watch` — **not `bt_mplayer`**, the vendor
application containing the BF07's ebook reader. The text-layout code in
[ebook-layout.md](ebook-layout.md) is therefore only available as a binary; patching
remains the route for those changes.

## Recovery logic (important)

From `bootloader/application/ota_app/src/main.c`:

```c
int check_adfu(void) {
#if IS_ENABLED(CONFIG_TXRX_ADFU)
    if (check_adfu_connect(CONFIG_ADFU_TX_GPIO, CONFIG_ADFU_RX_GPIO)) return 1;
#endif
#if IS_ENABLED(CONFIG_GPIO_ADFU)
    if (check_adfu_gpiokey(CONFIG_ADFU_KEY_GPIO)) return 1;
#endif
    return 0;
}

if (partition_valid_check())             goto exit_to_ota;   /* bad partition -> OTA */
if (reboot_type == REBOOT_TYPE_GOTO_OTA) goto exit_to_ota;
if (check_adfu()) { ... sys_pm_reboot(REBOOT_TYPE_GOTO_ADFU); }
if (ota_upgrade_is_allowed()) { boot_to_ota_app(); recovery_main(); }
exit_to_ota:
    if (ota_main()) sys_pm_reboot(REBOOT_TYPE_GOTO_ADFU);     /* OTA fails -> ADFU */
```

Two consequences:
1. ADFU has **two** entry mechanisms — serial-line (`CONFIG_TXRX_ADFU`) and button
   (`CONFIG_GPIO_ADFU`). The BF07 evidently isn't built with the button one, which
   explains why no combination ever worked.
2. A corrupt `fw0_sys` routes to OTA, and a failed OTA reboots to ADFU. Recovery paths
   exist — though the device's mbrec is a customised Aug 2022 build that predates this
   source, so treat it as likely rather than guaranteed.

## Local copy

Cloned to `~/Documents/bf07-actions-lark-sdk` (1.1 GB, 14,753 files, commit `26f51e5`).
