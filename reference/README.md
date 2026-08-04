# Reference material

Third-party projects that were essential. Not vendored here — clone them yourself.

| Project | Used for |
|---|---|
| [lvgl/lv_port_actions_technology](https://github.com/lvgl/lv_port_actions_technology) | **The official Actions LARK SDK.** Source for our platform's framework, the LARK `adfus.bin`, `build_ota_image.py`, bootloader logic. |
| [ilyakurdyukov/actions_flash](https://github.com/ilyakurdyukov/actions_flash) | ADFU host tool (ATJ2127/2157). Builds on macOS. `payload_arm/adfus.c` is a reverse-engineered ADFU payload in C — the device side. |
| [nfd/atj2127decrypt](https://github.com/nfd/atj2127decrypt) | `dfu/adfu.py` — clean pyusb ADFU implementation, the best base for a LARK host tool. |
| [Rockbox atjboottool](https://github.com/Rockbox/rockbox/tree/master/utils/atj2137/atjboottool) | Decrypts the Actions FWU format (ECIES/EC-233) → SQLite firmware DB. |

## Building atjboottool on macOS

```
mkdir atj && cd atj
for f in afi.c afi.h atj_tables.c atj_tables.h atjboottool.c fw.c fw.h \
         fwu.c fwu.h misc.c misc.h Makefile; do
  curl -sLO "https://raw.githubusercontent.com/Rockbox/rockbox/master/utils/atj2137/atjboottool/$f"
done
make CC=cc LD=cc
```

## Building actions_flash on macOS

```
brew install libusb
cc -O2 -std=c99 -DUSE_LIBUSB=1 \
   -I/usr/local/Cellar/libusb/*/include/libusb-1.0 \
   -o actions_dump actions_dump.c \
   -L/usr/local/Cellar/libusb/*/lib -lusb-1.0
```

Works for `inquiry` / `adfu_info` / `simple_switch` on LARK, but **not** for any command
after the payload loads — see [../docs/adfu-protocol.md](../docs/adfu-protocol.md).
