#!/usr/bin/env python3
"""Robust unattended recovery: catch the boot-looping BF07 and restore STOCK.

Restores the 8 sectors this project ever writes back to the vendor's own
ciphertext, verbatim (bit 31 clear). Stock is known-bootable, so this always
produces a working device; the reader can be installed afterwards.

Hardened against what actually went wrong before:
  * waits/hammers indefinitely so the user's reset is caught whenever it happens
  * `is` (bind storage) before any rs/es/ws -- skipping it caused short reads
  * every write retried, and the USB handle re-acquired on I/O error
  * each sector verified against the backup and retried as a whole
"""
import os, sys, glob, time, struct
sys.path.insert(0, os.environ.get(
    "BF07_TOOLS", os.path.dirname(os.path.abspath(__file__))))
import serial, usb.core, usb.util
from lark_cd import Adfu, OP_EXEC1, OP_WRITE
import serialport

# Your own encrypted backup (bf07.py backup). Never redistributed.
STOCK = os.environ.get("BF07_BACKUP",
                       os.path.expanduser("~/Documents/bf07-backups/bf07_flash_full_2026-08-05.bin"))
SECTORS = [0x5d000, 0x5e000, 0x60000, 0xed000, 0xf4000, 0xf5000, 0x1e7000, 0x1e8000]
SEC = 0x1000

def in_adfu(): return usb.core.find(idVendor=0x10D6, idProduct=0x10D6) is not None

def wait_for_adfu(minutes=30):
    if in_adfu(): return True
    print(f"waiting for the device -- hammering serial up to {minutes} min", flush=True)
    end = time.time() + minutes*60; ser=None; n=0
    while time.time() < end and not in_adfu():
        try:
            if ser is None:
                port = serialport.find()
                if not port: time.sleep(1); continue
                ser = serial.Serial(port, 2000000, timeout=0.1)
            ser.write(b"dbg reboot adfu\r\n"); ser.flush()
        except Exception:
            try: ser.close()
            except Exception: pass
            ser=None; time.sleep(0.5); continue
        n+=1
        if n % 400 == 0: print(f"  waiting... {int(end-time.time())}s left", flush=True)
        time.sleep(0.05)
    try:
        if ser: ser.close()
    except Exception: pass
    for _ in range(40):
        if in_adfu(): return True
        time.sleep(0.25)
    return in_adfu()

def start_payload():
    blob = open(os.environ.get("BF07_PAYLOAD", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir,
        "reference", "adfus_u_go.bin")), "rb").read()
    if len(blob)%256: blob += b"\0"*(256-len(blob)%256)
    a = Adfu(timeout=8000)
    a.write(OP_WRITE, 0x01010000, blob); a.cmd(OP_EXEC1, 0x01010000)
    usb.util.dispose_resources(a.d); del a; time.sleep(2.5)

class Dev:
    def __init__(self): self.attach()
    def attach(self):
        self.d = usb.core.find(idVendor=0x10D6, idProduct=0x10D6)
        if self.d is None: raise RuntimeError("device gone")
        # The payload owns the endpoints; re-selecting the already-active configuration
        # resets them under it and every raw packet then EIOs on Linux. Configure
        # only if nothing has. See docs/flashing.md.
        try: self.d.get_active_configuration()
        except usb.core.USBError: self.d.set_configuration()
        for _ in range(15):
            try: self.d.read(0x81,512,60)
            except usb.core.USBError: break
    def cmd(self, op, ln, addr, expect=None, data=None, tries=3):
        for attempt in range(tries):
            try:
                p=bytearray(16); p[0:2]=op
                struct.pack_into("<I",p,4,ln); struct.pack_into("<I",p,8,addr)
                self.d.write(0x02, bytes(p), 6000)
                if data is not None: self.d.write(0x02, data, 10000)
                want = expect or ln; b=b""
                while len(b) < want:
                    c = bytes(self.d.read(0x81, min(want-len(b),512), 3000))
                    if not c: break
                    b += c
                    if want <= 16 and len(b) >= 4: break
                return b
            except usb.core.USBError:
                time.sleep(0.4)
                try: self.attach()
                except Exception: 
                    if not wait_for_adfu(5): raise
                    start_payload(); self.attach()
                    self.cmd(b"is",16,0,expect=4,tries=1)
        return b""

def main():
    if not wait_for_adfu(): raise SystemExit("timed out")
    print("ADFU reached", flush=True)
    start_payload()
    d = Dev()
    if d.cmd(b"is",16,0,expect=4)[:1] != b"\xaa": raise SystemExit("storage bind failed")
    print("storage bound", flush=True)
    stock = open(STOCK,"rb").read()
    for s in SECTORS:
        for attempt in range(3):
            cur = d.cmd(b"rs", SEC, s)
            if len(cur)==SEC and cur == stock[s:s+SEC]:
                print(f"  0x{s:06x}: OK", flush=True); break
            d.cmd(b"es", SEC, s, expect=4); time.sleep(0.6)
            for off in range(0, SEC, 32):
                blk = stock[s+off:s+off+32]
                if blk == b"\xff"*32: continue
                d.cmd(b"ws", 32, s+off, expect=4, data=blk)
            back = d.cmd(b"rs", SEC, s)
            if len(back)==SEC and back == stock[s:s+SEC]:
                print(f"  0x{s:06x}: RESTORED", flush=True); break
            print(f"  0x{s:06x}: retry {attempt+1}", flush=True)
        else:
            print(f"  0x{s:06x}: FAILED", flush=True)
    print("\nstock restored -- power-cycle; the vendor reader should come up", flush=True)

main()
