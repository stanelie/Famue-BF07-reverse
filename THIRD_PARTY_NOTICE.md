# Third-party notice

Two kinds of third-party binary appear in or alongside this repository.

## 1. Famue BF07 stock firmware — `research/firmware/`

Unmodified firmware images read off two BF07 units the repository owner bought.
These are the vendor's copyrighted firmware. They are archived because the
device appears to be out of production and has **no working update or recovery
channel of its own** (the SD-card OTA path is non-functional on this board), so
without them an owner who damages their device has no route back.

They are provided solely so BF07 owners can restore their own hardware, are not
modified or repackaged, and no part of the vendor's product is being resold or
competed with. **If you hold the rights and would like them taken down, open an
issue and they will be removed.** See
[research/firmware/README.md](research/firmware/README.md).

## 2. LARK ADFU payload — `reference/adfus_u_go.bin`

The release bundle built from this repository also includes one file that isn't
ours: the LARK ADFU payload used to talk to the device's boot ROM over USB.

- **Source:** [lvgl/lv_port_actions_technology](https://github.com/lvgl/lv_port_actions_technology),
  the official Actions Semiconductor LARK SDK, published publicly by lvgl.
  The file corresponds to `action_technology_sdk/zephyr/tools/prebuilt/lark/common/bin/adfus_u.bin`
  in that repository.
- **License:** that repository does not carry a `LICENSE` file or a stated
  license as of this writing. It's redistributed here in good faith, as part
  of the same public SDK its own author publishes, with this notice pointing
  back to the original source — not repackaged, relabeled, or modified. If
  you are the rights holder and would like this handled differently, please
  open an issue.

Everything else in this repository — the notes, the tools, and the reader
patch itself — is CC0 (public domain); see [LICENSE](LICENSE).
