# Entering ADFU on macOS — tested, and why it does not work

**Result: not possible from userspace on macOS.** Recorded here so nobody spends
the day on it twice. `bf07_adfu_mac.c` is the attempt, kept because it documents
the dead end precisely.

## What blocks it

The BF07 in normal mode exposes **one** USB interface: mass storage (class
`0x08`, SCSI, bulk-only). macOS binds a full driver stack to it at enumeration:

```
IOUSBHostInterface -> IOUSBMassStorageInterfaceNub -> IOUSBMassStorageDriverNub
                   -> IOUSBMassStorageDriver -> IOSCSIPeripheralDeviceType00
                   -> IOBlockStorageDriver
```

Three routes, all closed:

| route | result |
|---|---|
| libusb `claim_interface` | `EACCES` — the kernel driver owns the interface, and **libusb on Darwin has no `detach_kernel_driver`** |
| unmount first, then claim | no change — `diskutil unmountDisk` removes the *filesystem*, the driver stays bound to the SCSI device |
| IOKit `SCSITaskDeviceInterface` (send the two CDBs directly) | **`IOCreatePlugInInterfaceForService` -> `kIOReturnUnsupported` (0xe00002c7)** |

The last one is the interesting failure. macOS *does* provide a raw-CDB path,
which would have been ideal — the ADFU switch is only two SCSI commands (`0xCC`
identify, `0xCB 0x21` switch). But Apple publishes `SCSITaskUserClient` only for
**authoring/optical** devices, not for plain disks. Confirmed on hardware: the
`IOSCSIPeripheralDeviceType00` nodes under the device are `!registered` and
`!matched`, so `IOServiceGetMatchingServices` will not even return them; walking
the registry tree finds them, and the user client is then refused outright.

Unloading Apple's kext would work but is blocked by SIP, and a DriverKit
replacement needs an Apple-granted USB transport entitlement.

## What to do instead

1. **Linux** — detach `usb-storage` and the switch works; nothing else in the
   toolchain is macOS-specific.
2. **A Linux VM with USB passthrough** (UTM, VMware, Parallels, VirtualBox) —
   practical on a Mac, and the whole tool runs inside it.
3. **A serial cable, once.** Only *entering* ADFU needs it; everything after —
   backup, verify, restore, install, and the decrypted firmware dump — is USB.

## The firmware-side fix, if someone wants it

We control the firmware, so the reader could publish an **extra vendor-specific
USB interface**. macOS binds no driver to vendor-specific interfaces, so libusb
could claim that one freely — giving a macOS-native control channel for
rebooting into ADFU, reading memory and streaming logs.

That does not solve first-time entry on a stock device (you would still need one
serial session to install the patch), but after that a Mac would need no cable
at all.
