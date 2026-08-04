#!/bin/sh
# Tear the ADFU gadget down. Safe to run repeatedly.
# Run as root:  sudo ./gadget-down.sh

G=/sys/kernel/config/usb_gadget/adfu
FFS=/dev/ffs-adfu

[ -d "$G" ] || { echo "no gadget"; exit 0; }

# Unbind from the UDC first.
echo "" > "$G/UDC" 2>/dev/null || true
sleep 0.3

umount "$FFS" 2>/dev/null || true
rmdir  "$FFS" 2>/dev/null || true

rm -f "$G/configs/c.1/ffs.adfu"
rmdir "$G/configs/c.1/strings/0x409" 2>/dev/null || true
rmdir "$G/configs/c.1"               2>/dev/null || true
rmdir "$G/functions/ffs.adfu"        2>/dev/null || true
rmdir "$G/strings/0x409"             2>/dev/null || true
rmdir "$G"                           2>/dev/null || true

echo "gadget removed"
