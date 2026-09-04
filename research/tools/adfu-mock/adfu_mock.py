#!/usr/bin/env python3
"""adfu_mock — pretend to be a LARK device in ADFU mode, and log what the
Actions Multimedia Product Tool sends at it.

Purpose
-------
The one unsolved problem on the BF07 is the **boot-ROM handover**: the command
that transfers control to an uploaded adfus.bin payload. It is not in any file
we have (the boot ROM is mask ROM), and every command recovered from
HardwareEx.dll turned out to belong to the *running payload*, not the ROM.

The vendor's Windows tool knows the command. This makes the tool say it out
loud, to a device that is not the BF07.

Runs on a Raspberry Pi in USB gadget mode (dwc2 + FunctionFS). Presents
VID:PID 10d6:10d6, vendor class ff/ff/ff, high speed, bulk 0x81 IN / 0x02 OUT
at 512 bytes — matching the real device's measured descriptors. Accepts every
CBW, answers with a success CSW, and writes a decoded trace plus a raw JSONL
log.

The BF07 is never connected.

Usage
-----
    sudo ./gadget-up.sh
    sudo python3 adfu_mock.py            # binds the UDC, then services
    # ... plug USB-C into the Windows PC, press Flash in the tool ...
    # Ctrl-C when done
    sudo ./gadget-down.sh

Output
------
    adfu_trace.jsonl    one JSON object per CBW / data phase / event
    stdout              human-readable decode

Take adfu_trace.jsonl back to the Mac; the interesting line is whatever the
tool sends immediately after the ~47 KB payload upload.
"""

import argparse
import json
import os
import struct
import sys
import threading
import time

# ---------------------------------------------------------------- constants

FFS_PATH = "/dev/ffs-adfu"
GADGET   = "/sys/kernel/config/usb_gadget/adfu"

# linux/usb/functionfs.h
DESCRIPTORS_MAGIC_V2 = 3
STRINGS_MAGIC        = 2
HAS_FS_DESC          = 1
HAS_HS_DESC          = 2

EVENT_NAMES = {0: "BIND", 1: "UNBIND", 2: "ENABLE", 3: "DISABLE",
               4: "SETUP", 5: "SUSPEND", 6: "RESUME"}

CBW_SIG = 0x43425355   # "USBC"
CSW_SIG = 0x53425355   # "USBS"

# What we know about the two command dialects, for readable logging.
# (from bf07-research/docs/adfu-protocol.md)
ROM_OPCODES = {
    0xCC: "adfu_info      (boot ROM, classic ATJ)",
    0xCB: "reboot         (boot ROM, classic ATJ)",
    0xB0: "reset / type-4 addressed read",
    0x12: "INQUIRY",
    0x05: "write_mem      (classic ATJ upload)",
    0x10: "CCommUSB::Switch",
    0x20: "CCommUSB::CallingEntry",
    0x08: "type-4 addressed write / type-0 flash read",
    0x09: "type-0 flash read (cmd 0x10)",
    0xCA: "type-4 info read (sub in CDB[1])",
}

# Canned device->host replies, keyed by CDB[0]. Anything not listed gets zeros.
# The adfu_info reply below is the shape the real ROM returns ("CADFUD" is the
# part we recorded verbatim); if the tool rejects it, recapture the real reply
# from the BF07 with tools/lark_adfu.py and paste the exact bytes here.
CANNED = {
    0xCC: b"\x00CADFUD#QA",
}


# ---------------------------------------------------------------- descriptors

def _iface():
    # bLength, bDescriptorType=INTERFACE, bInterfaceNumber, bAlternateSetting,
    # bNumEndpoints, class, subclass, protocol, iInterface
    return struct.pack("<BBBBBBBBB", 9, 0x04, 0, 0, 2, 0xFF, 0xFF, 0xFF, 1)


def _ep(addr, mps):
    # bLength, bDescriptorType=ENDPOINT, bEndpointAddress, bmAttributes=BULK,
    # wMaxPacketSize, bInterval
    return struct.pack("<BBBBHB", 7, 0x05, addr, 0x02, mps, 0)


def build_descriptors():
    """IN first, then OUT — FunctionFS numbers ep files in declaration order,
    so ep1 = IN (0x81) and ep2 = OUT (0x02)."""
    fs = _iface() + _ep(0x81, 64)  + _ep(0x02, 64)
    hs = _iface() + _ep(0x81, 512) + _ep(0x02, 512)

    body   = struct.pack("<II", 3, 3) + fs + hs      # fs_count, hs_count
    length = 12 + len(body)
    return struct.pack("<III", DESCRIPTORS_MAGIC_V2, length,
                       HAS_FS_DESC | HAS_HS_DESC) + body


def build_strings():
    tab    = struct.pack("<H", 0x0409) + b"ADFU\0"   # lang id + iInterface=1
    length = 16 + len(tab)
    return struct.pack("<IIII", STRINGS_MAGIC, length, 1, 1) + tab


# ---------------------------------------------------------------- log

class Trace:
    def __init__(self, path):
        self.fh = open(path, "a", buffering=1)
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.n = 0

    def write(self, kind, **kw):
        rec = {"t": round(time.time() - self.t0, 4), "kind": kind}
        rec.update(kw)
        with self.lock:
            self.fh.write(json.dumps(rec) + "\n")

    def close(self):
        self.fh.close()


def hexs(b, limit=64):
    s = b[:limit].hex(" ")
    return s + (f" ... (+{len(b) - limit} more)" if len(b) > limit else "")


# ---------------------------------------------------------------- ep0 events

def ep0_loop(fd, trace, stop):
    """Log BIND/ENABLE/SETUP etc. Standard control requests are handled by the
    kernel's composite layer; anything that reaches us here is notable."""
    while not stop.is_set():
        try:
            buf = os.read(fd, 12 * 8)
        except OSError as e:
            if stop.is_set():
                return
            trace.write("ep0_error", err=str(e))
            time.sleep(0.1)
            continue
        if not buf:
            continue
        for off in range(0, len(buf) - 11, 12):
            ev = buf[off:off + 12]
            etype = ev[8]
            name = EVENT_NAMES.get(etype, f"?{etype}")
            if etype == 4:  # SETUP
                bmr, breq, wval, widx, wlen = struct.unpack("<BBHHH", ev[:8])
                print(f"  [ep0] SETUP bmRequestType=0x{bmr:02x} "
                      f"bRequest=0x{breq:02x} wValue=0x{wval:04x} "
                      f"wIndex=0x{widx:04x} wLength={wlen}")
                trace.write("setup", bmRequestType=bmr, bRequest=breq,
                            wValue=wval, wIndex=widx, wLength=wlen)
                # Stall anything we do not understand; harmless for tracing.
                try:
                    if bmr & 0x80:
                        os.read(fd, 0)
                    else:
                        os.write(fd, b"")
                except OSError:
                    pass
            else:
                print(f"  [ep0] {name}")
                trace.write("event", event=name)


# ---------------------------------------------------------------- bulk

def read_exact(fd, n, chunk_max=16384):
    """Bulk OUT reads must be at least wMaxPacketSize; round requests up to a
    512-byte multiple."""
    buf = b""
    while len(buf) < n:
        want = min(chunk_max, ((n - len(buf) + 511) // 512) * 512)
        part = os.read(fd, max(512, want))
        if not part:
            break
        buf += part
    return buf


def decode_cbw(cbw):
    sig, tag, dlen, flags, lun, cdblen = struct.unpack("<IIIBBB", cbw[:15])
    cdb = cbw[15:15 + max(0, min(cdblen, 16))]
    return sig, tag, dlen, flags, lun, cdblen, cdb


def service(ep_in, ep_out, trace, stop):
    n = 0
    while not stop.is_set():
        try:
            pkt = os.read(ep_out, 512)
        except OSError as e:
            if stop.is_set():
                return
            trace.write("bulk_error", where="cbw", err=str(e))
            time.sleep(0.05)
            continue

        if not pkt:
            continue

        if len(pkt) < 15 or struct.unpack("<I", pkt[:4])[0] != CBW_SIG:
            print(f"[{n:04d}] non-CBW ({len(pkt)} B): {hexs(pkt)}")
            trace.write("raw_out", data=pkt.hex())
            continue

        sig, tag, dlen, flags, lun, cdblen, cdb = decode_cbw(pkt)
        n += 1
        direction = "IN (dev->host)" if flags & 0x80 else "OUT (host->dev)"
        op = cdb[0] if cdb else -1
        meaning = ROM_OPCODES.get(op, "unknown")

        print(f"\n[{n:04d}] CBW tag=0x{tag:08x} dlen={dlen} {direction} "
              f"cdb_len=0x{cdblen:02x}")
        print(f"       CDB[0]=0x{op:02x}  {meaning}")
        print(f"       CDB   = {cdb.hex(' ')}")

        trace.write("cbw", n=n, tag=tag, dlen=dlen, flags=flags, lun=lun,
                    cdb_len=cdblen, cdb=cdb.hex(), dir=direction,
                    guess=meaning)

        status = 0
        if dlen:
            if flags & 0x80:
                # Device -> host.
                body = CANNED.get(op, b"")
                body = (body + b"\x00" * dlen)[:dlen]
                try:
                    os.write(ep_in, body)
                    print(f"       -> replied {dlen} B: {hexs(body, 32)}")
                    trace.write("data_in", n=n, length=dlen,
                                data=body[:256].hex())
                except OSError as e:
                    print(f"       -> IN write failed: {e}")
                    trace.write("data_in_error", n=n, err=str(e))
                    status = 1
            else:
                # Host -> device. This is where the ~47 KB payload lands.
                try:
                    body = read_exact(ep_out, dlen)
                    print(f"       <- received {len(body)}/{dlen} B: "
                          f"{hexs(body, 32)}")
                    trace.write("data_out", n=n, length=len(body),
                                expected=dlen, head=body[:256].hex())
                    if len(body) > 4096:
                        fn = f"payload_{n:04d}.bin"
                        with open(fn, "wb") as f:
                            f.write(body)
                        print(f"       <- saved to {fn}")
                        trace.write("data_saved", n=n, file=fn,
                                    length=len(body))
                except OSError as e:
                    print(f"       <- OUT read failed: {e}")
                    trace.write("data_out_error", n=n, err=str(e))
                    status = 1

        csw = struct.pack("<IIIB", CSW_SIG, tag, 0, status)
        try:
            os.write(ep_in, csw)
        except OSError as e:
            print(f"       CSW write failed: {e}")
            trace.write("csw_error", n=n, err=str(e))


# ---------------------------------------------------------------- main

def pick_udc(explicit):
    if explicit:
        return explicit
    udcs = sorted(os.listdir("/sys/class/udc"))
    if not udcs:
        sys.exit("no UDC found — is dtoverlay=dwc2 set and dwc2 loaded?")
    if len(udcs) > 1:
        print(f"multiple UDCs {udcs}, using {udcs[0]} (override with --udc)")
    return udcs[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ffs", default=FFS_PATH)
    ap.add_argument("--udc", default=None, help="UDC name (default: autodetect)")
    ap.add_argument("--trace", default="adfu_trace.jsonl")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("must run as root")
    if not os.path.isdir(args.ffs):
        sys.exit(f"{args.ffs} not mounted — run ./gadget-up.sh first")

    trace = Trace(args.trace)
    stop = threading.Event()

    ep0 = os.open(os.path.join(args.ffs, "ep0"), os.O_RDWR)
    os.write(ep0, build_descriptors())
    os.write(ep0, build_strings())
    print("descriptors written")

    # ep1/ep2 only exist once descriptors are accepted.
    ep_in = os.open(os.path.join(args.ffs, "ep1"), os.O_RDWR)
    ep_out = os.open(os.path.join(args.ffs, "ep2"), os.O_RDWR)
    print("endpoints open: ep1 = IN 0x81, ep2 = OUT 0x02")

    t = threading.Thread(target=ep0_loop, args=(ep0, trace, stop), daemon=True)
    t.start()

    udc = pick_udc(args.udc)
    with open(os.path.join(GADGET, "UDC"), "w") as f:
        f.write(udc)
    print(f"bound to UDC {udc}")
    print(f"presenting 10d6:10d6 — plug USB-C into the Windows PC now.")
    print(f"tracing to {args.trace}; Ctrl-C to stop.\n")

    try:
        service(ep_in, ep_out, trace, stop)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop.set()
        try:
            with open(os.path.join(GADGET, "UDC"), "w") as f:
                f.write("")
        except OSError:
            pass
        for fd in (ep_in, ep_out, ep0):
            try:
                os.close(fd)
            except OSError:
                pass
        trace.close()
        print(f"trace written to {args.trace}")


if __name__ == "__main__":
    main()
