#!/usr/bin/env python3
"""LARK ADFU host implementation.

Protocol recovered from HardwareEx.dll (CCommUSB::ADFUWrite/ADFURead).
See docs/adfu-protocol.md.

  CBW: "USBC" | tag | data_len | flags | lun | cdb_len | CDB[]
       read : flags=0x80, cdb_len=0x0c, CDB[1] |= 0x80
       write: flags=0x00, cdb_len=0x10, CDB[1] &= 0x7f

  flash read : cmd 0x11 -> CDB[0]=8, CDB[2..5]=addr, CDB[7]=len>>9, CDB[8]=len>>17
  flash write: cmd 0x10 -> CDB[0]=9, same layout
"""
import struct
import sys
import usb.core
import usb.util

VID, PID = 0x10D6, 0x10D6
EP_OUT, EP_IN = 0x02, 0x81
USBC_SIG = 0x43425355
USBS_SIG = 0x53425355
SECTOR = 512


class LarkAdfu:
    def __init__(self, timeout=5000, verbose=False):
        self.timeout = timeout
        self.verbose = verbose
        self.tag = 0
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise RuntimeError("no ADFU device (10d6:10d6) — is it in ADFU mode?")
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        try:
            dev.set_configuration()
        except Exception:
            pass
        self.dev = dev

    def _cbw(self, data_len, flags, cdb):
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        return struct.pack("<IIIBBB", USBC_SIG, self.tag, data_len,
                           flags, 0, len(cdb)) + cdb

    def _status(self):
        try:
            csw = bytes(self.dev.read(EP_IN, 13, self.timeout))
        except Exception as e:
            if self.verbose:
                print(f"    [csw read failed: {e}]")
            return None
        if len(csw) >= 13:
            sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
            if sig != USBS_SIG and self.verbose:
                print(f"    [bad CSW sig 0x{sig:08x}]")
            return status
        return None

    # ---- flash read: cmd 0x11 -> CDB[0] = 8 ----
    def read_flash(self, addr, nbytes, addr_is_sector=True, big_endian=False):
        """addr semantics are configurable because the DLL copies extra[0..3] verbatim."""
        a = addr // SECTOR if addr_is_sector else addr
        sectors = nbytes // SECTOR
        cdb = bytearray(12)
        cdb[0] = 8                       # SCSI-style READ
        cdb[1] = 0x80                    # read handler does CDB[1] |= 0x80
        struct.pack_into(">I" if big_endian else "<I", cdb, 2, a)
        cdb[7] = sectors & 0xFF
        cdb[8] = (sectors >> 8) & 0xFF
        self.dev.write(EP_OUT, self._cbw(nbytes, 0x80, bytes(cdb)), self.timeout)
        try:
            data = bytes(self.dev.read(EP_IN, nbytes, self.timeout))
        except Exception as e:
            if self.verbose:
                print(f"    [data read failed: {e}]")
            self._status()
            return None
        self._status()
        return data

    def close(self):
        usb.util.dispose_resources(self.dev)


def main():
    import json
    SCRATCH = "/private/tmp/claude-504/-Users-user/5d7d024b-ca45-4829-b929-aa9b9dba425d/scratchpad"
    gt = json.load(open(f"{SCRATCH}/ground_truth.json"))
    truth = {int(k, 16): bytes.fromhex(v) for k, v in gt.items() if v}

    a = LarkAdfu(verbose=True)
    print("device opened\n")

    # Probe all four address interpretations against a known non-zero offset.
    target = 0x1000
    want = truth[target][:16]
    print(f"ground truth @0x{target:x}: {want.hex(' ')}\n")

    for is_sec in (True, False):
        for be in (False, True):
            tag = f"addr={'sector' if is_sec else 'byte'} {'BE' if be else 'LE'}"
            got = a.read_flash(target, SECTOR, addr_is_sector=is_sec, big_endian=be)
            if not got:
                print(f"  {tag:22s} -> no data")
                continue
            ok = got[:16] == want
            print(f"  {tag:22s} -> {got[:16].hex(' ')}  {'*** MATCH ***' if ok else ''}")
            if ok:
                print(f"\nWORKING: addr_is_sector={is_sec}, big_endian={be}")
                a.close()
                return 0
    a.close()
    print("\nno interpretation matched")
    return 1


if __name__ == "__main__":
    sys.exit(main())
