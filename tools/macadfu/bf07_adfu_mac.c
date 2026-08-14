/* Enter ADFU on macOS over USB alone, without libusb.
 *
 * macOS binds IOUSBMassStorageDriver to the BF07's only USB interface, and
 * libusb on Darwin cannot detach a kernel driver -- so claim_interface fails
 * with EACCES no matter what, root or not, mounted or not.
 *
 * But the ADFU switch is only two SCSI commands, and macOS deliberately exposes
 * SCSITaskDeviceInterface so userspace can send raw CDBs to storage devices.
 * So we go through IOKit instead of fighting it:
 *
 *     CDB 0xCC ...             -> expect the 11 bytes "ACTIONSUSBD"
 *     CDB 0xCB 0x21 ...        -> device reboots into ADFU (10d6:10d6)
 *
 * Unmount the volume first (diskutil unmountDisk) or ObtainExclusiveAccess
 * will be refused.
 *
 * build: clang -o bf07_adfu_mac bf07_adfu_mac.c -framework IOKit -framework CoreFoundation
 */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/scsi/SCSITaskLib.h>
#include <stdio.h>
#include <string.h>

#define BF07_VID 0x10d6
#define BF07_PID 0xb00b

/* Walk up the IORegistry looking for idVendor/idProduct of the USB device that
   this SCSI nub belongs to. */
static int matches_bf07(io_service_t service)
{
    io_registry_entry_t entry = service;
    IOObjectRetain(entry);
    for (int depth = 0; depth < 12; depth++) {
        CFTypeRef vid = IORegistryEntryCreateCFProperty(entry, CFSTR("idVendor"),
                                                        kCFAllocatorDefault, 0);
        CFTypeRef pid = IORegistryEntryCreateCFProperty(entry, CFSTR("idProduct"),
                                                        kCFAllocatorDefault, 0);
        int ok = 0;
        if (vid && pid) {
            int v = 0, p = 0;
            CFNumberGetValue((CFNumberRef)vid, kCFNumberIntType, &v);
            CFNumberGetValue((CFNumberRef)pid, kCFNumberIntType, &p);
            ok = (v == BF07_VID && p == BF07_PID);
        }
        if (vid) CFRelease(vid);
        if (pid) CFRelease(pid);
        if (ok) { IOObjectRelease(entry); return 1; }

        io_registry_entry_t parent;
        if (IORegistryEntryGetParentEntry(entry, kIOServicePlane, &parent) != KERN_SUCCESS) break;
        IOObjectRelease(entry);
        entry = parent;
    }
    IOObjectRelease(entry);
    return 0;
}

static int send_cdb(SCSITaskDeviceInterface **iface, const UInt8 *cdb, UInt8 cdbLen,
                    void *buf, UInt32 buflen, const char *what)
{
    SCSITaskInterface **task = (*iface)->CreateSCSITask(iface);
    if (!task) { fprintf(stderr, "  %s: CreateSCSITask failed\n", what); return -1; }

    IOVirtualRange range = { .address = (IOVirtualAddress)buf, .length = buflen };
    SCSITaskStatus status = 0;
    SCSI_Sense_Data sense;
    UInt64 transferred = 0;
    memset(&sense, 0, sizeof sense);

    (*task)->SetCommandDescriptorBlock(task, (UInt8 *)cdb, cdbLen);
    if (buflen)
        (*task)->SetScatterGatherEntries(task, &range, 1, buflen,
                                         kSCSIDataTransfer_FromTargetToInitiator);
    (*task)->SetTimeoutDuration(task, 5000);

    IOReturn ret = (*task)->ExecuteTaskSync(task, &sense, &status, &transferred);
    (*task)->Release(task);
    if (ret != kIOReturnSuccess) {
        fprintf(stderr, "  %s: ExecuteTaskSync 0x%08x\n", what, ret);
        return -1;
    }
    printf("  %s: status %d, %llu byte(s)\n", what, status, transferred);
    return 0;
}

int main(void)
{
    /* The SCSI nubs under this device are !registered/!matched, so
       IOServiceGetMatchingServices never returns them. Walk the registry tree
       down from the USB device instead, which finds unregistered entries too. */
    io_service_t usbdev = 0;
    {
        CFMutableDictionaryRef m = IOServiceMatching("IOUSBHostDevice");
        io_iterator_t it;
        if (IOServiceGetMatchingServices(kIOMainPortDefault, m, &it) == KERN_SUCCESS) {
            io_service_t s;
            while ((s = IOIteratorNext(it))) {
                if (!usbdev && matches_bf07(s)) { usbdev = s; continue; }
                IOObjectRelease(s);
            }
            IOObjectRelease(it);
        }
    }
    if (!usbdev) {
        fprintf(stderr, "BF07 not found on USB (is it in disk drive mode?)\n");
        return 2;
    }

    io_service_t target = 0;
    {
        io_iterator_t iter;
        if (IORegistryEntryCreateIterator(usbdev, kIOServicePlane,
                kIORegistryIterateRecursively, &iter) == KERN_SUCCESS) {
            io_registry_entry_t e;
            while ((e = IOIteratorNext(iter))) {
                io_name_t cls;
                if (IOObjectGetClass(e, cls) == KERN_SUCCESS &&
                    strstr(cls, "IOSCSIPeripheralDeviceType") && !target) {
                    printf("found %s (id in registry)\n", cls);
                    target = e;
                    continue;
                }
                IOObjectRelease(e);
            }
            IOObjectRelease(iter);
        }
    }
    IOObjectRelease(usbdev);
    if (!target) {
        fprintf(stderr, "no SCSI peripheral node under the device\n");
        return 2;
    }

    IOCFPlugInInterface **plugin = NULL;
    SInt32 score = 0;
    kern_return_t kr = IOCreatePlugInInterfaceForService(
        target, kIOSCSITaskDeviceUserClientTypeID, kIOCFPlugInInterfaceID,
        &plugin, &score);
    IOObjectRelease(target);
    if (kr != KERN_SUCCESS || !plugin) {
        fprintf(stderr, "IOCreatePlugInInterfaceForService: 0x%08x\n", kr);
        fprintf(stderr, "  -> macOS does not expose SCSITaskUserClient for this device\n");
        return 3;
    }

    SCSITaskDeviceInterface **iface = NULL;
    HRESULT hr = (*plugin)->QueryInterface(
        plugin, CFUUIDGetUUIDBytes(kIOSCSITaskDeviceInterfaceID), (LPVOID *)&iface);
    IODestroyPlugInInterface(plugin);
    if (hr != S_OK || !iface) {
        fprintf(stderr, "QueryInterface failed (0x%x)\n", (unsigned)hr);
        return 4;
    }

    if ((*iface)->ObtainExclusiveAccess(iface) != kIOReturnSuccess) {
        fprintf(stderr, "ObtainExclusiveAccess refused -- unmount it first:\n"
                        "    diskutil unmountDisk /dev/diskN\n");
        (*iface)->Release(iface);
        return 5;
    }
    printf("exclusive access obtained\n");

    UInt8 ident[16];
    memset(ident, 0, sizeof ident);
    UInt8 cdb_id[16] = { 0xCC, 0, 0, 0, 0, 0, 0, 11 };
    int rc = send_cdb(iface, cdb_id, 12, ident, 11, "identify (0xCC)");
    if (rc == 0) {
        ident[11] = 0;
        printf("  identity: \"%s\"\n", (char *)ident);
        if (memcmp(ident, "ACTIONSUSBD", 11) != 0)
            fprintf(stderr, "  unexpected identity -- not switching\n");
        else {
            UInt8 resp[8]; memset(resp, 0, sizeof resp);
            UInt8 cdb_sw[16] = { 0xCB, 0x21, 0, 0, 0, 0, 0, 2 };
            send_cdb(iface, cdb_sw, 12, resp, 2, "switch to ADFU (0xCB 0x21)");
            printf("switch sent -- the device should re-enumerate as 10d6:10d6\n");
        }
    }

    (*iface)->ReleaseExclusiveAccess(iface);
    (*iface)->Release(iface);
    return rc == 0 ? 0 : 6;
}
