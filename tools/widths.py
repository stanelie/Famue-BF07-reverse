#!/usr/bin/env python3
"""Did the real font widths get measured, and how wrong was the estimate?"""
import glob,re,subprocess,time,serial
ELF="/Users/selie/Documents/bf07-research/reader/reader.elf"
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
st=w(0x18018E9C)
print(f"font ptr = 0x{w(st+off['font']):08x}   table built = {w(st+off['wtab_ok'])&0xff}")
base=st+off["wtab"]; words=blk(base,48)
def gw(ch):
    idx=ord(ch)-32; a=base+(idx//2)*4; v=words.get(a,0)
    return (v>>16)&0xffff if idx%2 else v&0xffff
est={'i':26,'l':26,'.':26,'f':38,'r':38,'t':38,'m':88,'w':88,'W':88,'M':88,
     'A':72,'H':72,'0':60,'e':58,'n':58,'o':58,' ':36}
print(" ch   measured(1/8px)   old estimate   delta")
for ch,e in est.items():
    m16=gw(ch); m8=(m16+1)>>1
    if m16: print(f"  '{ch}'      {m8:4d}            {e:4d}       {m8-e:+d}")
s.close()
