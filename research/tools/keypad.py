#!/usr/bin/env python3
"""Dump what the reader saw at the last press -- run right after a keypad tap."""
import glob,re,subprocess,time,serial
import os as _os
import serialport
_HERE = _os.path.dirname(_os.path.abspath(__file__))
ELF = _os.environ.get("BF07_ELF",
                      _os.path.join(_HERE, _os.pardir, "reader", "reader.elf"))
d=subprocess.run(["arm-none-eabi-objdump","--dwarf=info",ELF],capture_output=True,text=True).stdout
i=d.index("inj_state"); off={}; nm=None
for ln in d[i:].splitlines()[1:]:
    if "DW_TAG_structure_type" in ln and off: break
    m=re.search(r"DW_AT_name\b.*?:\s*(\w+)\s*$",ln)
    if m and "DW_TAG" not in ln: nm=m.group(1)
    m=re.search(r"DW_AT_data_member_location:\s*(\d+)",ln)
    if m and nm: off[nm]=int(m.group(1)); nm=None
s=serialport.open(timeout=0.4); time.sleep(0.2)
def blk(a,n):
    for _ in range(3):
        s.reset_input_buffer(); s.write(f"dbg mdw 0x{a:08x} {n:x}\r\n".encode()); s.flush()
        t=time.time(); b=b""
        while time.time()-t<0.8:
            x=s.read(65536)
            if x: b+=x
            elif b: break
        o={}
        for m in re.finditer(r"^([0-9a-f]{8}): ((?:[0-9a-f]{8} ?){1,4})",b.decode("utf8","replace"),re.M):
            base=int(m.group(1),16)
            for j,w in enumerate(m.group(2).split()): o[base+j*4]=int(w,16)
        if len(o)>=n*0.8: return o
    return {}
def w(a): return blk(a,1).get(a)
def s16(v): return v-0x10000 if v>=0x8000 else v
st=w(0x18018E9C)
r1=w(st+off["last_rect"]); r2=w(st+off["last_rect2"])
print(f"page rect at press : ({s16((r1>>16)&0xffff)},{s16(r1&0xffff)}) .. "
      f"({s16((r2>>16)&0xffff)},{s16(r2&0xffff)})")
print(f"container at press : 0x{w(st+off['last_cont']):08x}   drawn: 0x{w(st+off['draw_cont']):08x}")
print(f"scene at press     : 0x{w(st+off['last_rd']):08x}")
tn=w(st+off["touch_nz"]); base=st+off["touch"]; ring=blk(base,12)
print(f"presses: {tn}")
for i in range(4):
    v=[ring.get(base+(i*3+j)*4,0) for j in range(3)]
    if not any(v): continue
    print(f"  [{i}] ({s16(v[0]&0xffff)},{s16((v[0]>>16)&0xffff)})"
          + ("  <- newest" if tn and i==(tn-1)%4 else ""))
print(f"cur.start={w(st+off['cur'])}  sp={(w(st+0x14)>>8)&0xff}")
# live geometry while the keypad is still up
g=w(0x18018978); rd=w(g+0x3c) if g else 0
c=w(rd+0x18) if rd else 0
print(f"NOW container=0x{c:08x}")
if c and 0x01000000<=c<0x01020000:
    print(f"  rect ({s16(w(c+0x14)&0xffff)},{s16((w(c+0x14)>>16)&0xffff)}) .. "
          f"({s16(w(c+0x18)&0xffff)},{s16((w(c+0x18)>>16)&0xffff)})")
    par=w(c+4); pspec=w(par+8) if par and 0x01000000<=par<0x01020000 else 0
    if pspec and 0x01000000<=pspec<0x01020000:
        print(f"  screen children = {w(pspec+4)}")
s.close()
