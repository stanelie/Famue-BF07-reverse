#!/usr/bin/env python3
"""Print the last 16 presses in order, to map the soft keypad's grid."""
import glob,re,subprocess,time,serial
import os as _os
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
s=serial.Serial(glob.glob("/dev/cu.usbserial-*")[0],2000000,timeout=0.4); time.sleep(0.2)
def blk(a,n):
    for _ in range(3):
        s.reset_input_buffer(); s.write(f"dbg mdw 0x{a:08x} {n:x}\r\n".encode()); s.flush()
        t=time.time(); b=b""
        while time.time()-t<1.0:
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
st=w(0x18018E9C); n=w(st+off["touch_nz"]); base=st+off["touch"]
ring=blk(base,16)
print(f"{n} presses recorded; oldest first:")
lbl="1 2 3 4 5 6 7 8 9 0".split()
seq=[]
for k in range(16):
    idx=(n-16+k) % 16 if n>=16 else k
    if n<16 and k>=n: break
    v=ring.get(base+idx*4,0)
    if not v: continue
    seq.append((s16(v&0xffff), s16((v>>16)&0xffff)))
start=max(0,len(seq)-10)
for k,(x,y) in enumerate(seq):
    tag=f"   <- '{lbl[k-start]}'" if k>=start and (k-start)<10 else ""
    print(f"  ({x:4d},{y:4d}){tag}")
s.close()
