#!/bin/sh
# Enter ADFU on macOS over USB alone: unmount the BF07 volume, then send the
# two SCSI commands through IOKit (libusb cannot claim the interface on Darwin).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)

DISK=$(diskutil list external 2>/dev/null | awk '/^\/dev\/disk/{d=$1} /BF07|ZEPHYR/{print d; exit}')
if [ -z "$DISK" ]; then
    # fall back: any external disk whose USB vendor is Actions
    DISK=$(diskutil list external 2>/dev/null | awk '/^\/dev\/disk/{print $1; exit}')
fi
if [ -n "$DISK" ]; then
    echo "unmounting $DISK"
    diskutil unmountDisk "$DISK" >/dev/null 2>&1 || echo "  (unmount failed; continuing)"
else
    echo "no mounted BF07 volume found (already unmounted?)"
fi

"$HERE/bf07_adfu_mac"
rc=$?
if [ $rc -eq 0 ]; then
    echo "waiting for the device to come back as ADFU..."
    i=0
    while [ $i -lt 20 ]; do
        if system_profiler SPUSBDataType 2>/dev/null | grep -q "0x10d6"; then
            if ioreg -p IOUSB -l 2>/dev/null | grep -q "idProduct.*4310"; then break; fi
        fi
        sleep 0.5
        i=$((i+1))
    done
fi
exit $rc
