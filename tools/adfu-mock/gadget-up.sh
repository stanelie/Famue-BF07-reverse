#!/bin/sh
# Create the ADFU gadget in configfs, but do NOT bind it to the UDC.
#
# Binding must happen *after* adfu_mock.py has written its descriptors to ep0,
# otherwise the host sees a half-configured device. adfu_mock.py does the bind
# itself as its last setup step.
#
# Run as root:  sudo ./gadget-up.sh
set -e

G=/sys/kernel/config/usb_gadget/adfu
FFS=/dev/ffs-adfu

modprobe libcomposite

mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config

if [ -d "$G" ]; then
    echo "gadget already exists; run ./gadget-down.sh first" >&2
    exit 1
fi

mkdir -p "$G"

# Identify as the BF07 in ADFU mode.
echo 0x10d6 > "$G/idVendor"
echo 0x10d6 > "$G/idProduct"
echo 0x0200 > "$G/bcdUSB"        # USB 2.0 -> high speed, 512-byte bulk
echo 0x0100 > "$G/bcdDevice"

mkdir -p "$G/strings/0x409"
echo "Actions"          > "$G/strings/0x409/manufacturer"
echo "ADFU"             > "$G/strings/0x409/product"
echo "0000000000000001" > "$G/strings/0x409/serialnumber"

mkdir -p "$G/configs/c.1/strings/0x409"
echo "ADFU" > "$G/configs/c.1/strings/0x409/configuration"
echo 500    > "$G/configs/c.1/MaxPower"

mkdir -p "$G/functions/ffs.adfu"
ln -sf "$G/functions/ffs.adfu" "$G/configs/c.1/"

mkdir -p "$FFS"
mountpoint -q "$FFS" || mount -t functionfs adfu "$FFS"

echo "gadget created (unbound). UDCs available:"
ls /sys/class/udc
echo
echo "now run:  sudo python3 adfu_mock.py"
