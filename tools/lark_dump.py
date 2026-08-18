#!/usr/bin/env python3
"""LARK ADFU: start the uploaded payload, then read flash.

Handoff commands recovered from HardwareEx.dll:
    CCommUSB::Switch        CDB[0] = 0x10, CDB[1..2] = 2-byte param
    CCommUSB::CallingEntry  CDB[0] = 0x20, CDB[1..2] = 2-byte param
both with cdb_len = 0x10, flags = 0x00 (host->device, no data phase).

Flash read (ADFURead cmd 0x11):
    cdb_len = 0x0c, flags = 0x80, CDB[0] = 8, CDB[1] = 0x80,
    CDB[2..5] = addr, CDB[7] = sectors & 0xff, CDB[8] = sectors >> 8
"""
import os as _os
import struct, sys, json, time
import usb.core, usb.util

VID = PID = 0x10D6
EP_OUT, EP_IN = 0x02, 0x81
USBC, USBS = 0x43425355, 0x53425355
SECTOR = 512
SCRATCH = _os.environ.get("BF07_BACKUPS", _os.path.expanduser("~/Documents/bf07-backups"))


class Lark:
    def __init__(self, timeout=4000):
        self.t = timeout
        self.tag = 0
        d = usb.core.find(idVendor=VID, idProduct=PID)
        if d is None:
            raise RuntimeError("not in ADFU mode (no 10d6:10d6)")
        try:
            d.set_configuration()
        except Exception:
            pass
        self.d = d

    def cbw(self, dlen, flags, cdb):
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        return struct.pack("<IIIBBB", USBC, self.tag, dlen, flags, 0, len(cdb)) + cdb

    def csw(self):
        try:
            r = bytes(self.d.read(EP_IN, 13, self.t))
        except Exception as e:
            return ("no-csw", str(e))
        if len(r) >= 13:
            sig, tag, res, st = struct.unpack("<IIIB", r[:13])
            return ("ok" if sig == USBS else "badsig", st)
        return ("short", r.hex())

    def ctrl(self, opcode, param=0):
        """Switch (0x10) / CallingEntry (0x20): no data phase."""
        cdb = bytearray(16)
        cdb[0] = opcode
        cdb[1] = param & 0xFF
        cdb[2] = (param >> 8) & 0xFF
        self.d.write(EP_OUT, self.cbw(0, 0x00, bytes(cdb)), self.t)
        return self.csw()

    def read_flash(self, addr_units, nbytes):
        sec = nbytes // SECTOR
        cdb = bytearray(12)
        cdb[0] = 8
        cdb[1] = 0x80
        struct.pack_into("<I", cdb, 2, addr_units)
        cdb[7] = sec & 0xFF
        cdb[8] = (sec >> 8) & 0xFF
        self.d.write(EP_OUT, self.cbw(nbytes, 0x80, bytes(cdb)), self.t)
        try:
            data = bytes(self.d.read(EP_IN, nbytes, self.t))
        except Exception as e:
            return None, ("read-fail", str(e))
        if len(data) == 13 and struct.unpack("<I", data[:4])[0] == USBS:
            return None, ("csw-instead", data[12])
        return data, self.csw()


def main():
    gt = {int(k, 16): bytes.fromhex(v)
          for k, v in json.load(open(f"{SCRATCH}/ground_truth.json")).items() if v}
    want = gt[0x1000][:16]
    print(f"ground truth @0x1000: {want.hex(' ')}\n")

    a = Lark()

    # --- step 3: start the payload -------------------------------------
    for name, op in (("CallingEntry", 0x20), ("Switch", 0x10)):
        print(f"--- {name}: CDB[0]=0x{op:02x} ---")
        for param in (0, 1):
            try:
                st = a.ctrl(op, param)
            except Exception as e:
                print(f"   param={param}: send failed: {e}")
                continue
            print(f"   param={param}: csw={st}")
            time.sleep(0.4)
            # --- step 4: try a flash read ------------------------------
            for units, lbl in ((0x1000 // SECTOR, "sector"), (0x1000, "byte")):
                try:
                    data, st2 = a.read_flash(units, SECTOR)
                except Exception as e:
                    print(f"      read({lbl}) send failed: {e}")
                    continue
                if data:
                    hit = data[:16] == want
                    print(f"      read({lbl}) -> {data[:16].hex(' ')} {'*** MATCH ***' if hit else ''}")
                    if hit:
                        print(f"\nWORKING: start={name}(param={param}), addr={lbl}")
                        open(f"{SCRATCH}/lark_working.json", "w").write(
                            json.dumps({"start": name, "opcode": op,
                                        "param": param, "addr_units": lbl}))
                        a.d.reset()
                        return 0
                else:
                    print(f"      read({lbl}) -> {st2}")
    print("\nno combination produced flash data")
    return 1


if __name__ == "__main__":
    sys.exit(main())
