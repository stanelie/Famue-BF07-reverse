#!/usr/bin/env python3
"""Read the page as rendered, and check each line against the measured widths.

Shows, per line: the text, its width in px, the space left, and whether the
first word of the NEXT line would have fitted in that space. That is exactly the
"gap where a word would fit" complaint, decided with the same numbers the wrap
used rather than by eye.
"""
import glob,re,subprocess,time,serial
import os as _os
import serialport
_HERE = _os.path.dirname(_os.path.abspath(__file__))
# Override with BF07_ELF if the build lives elsewhere.
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
MAXW, LINES, LIMIT = 44, 12, 168

def page_offsets(dw):
    """struct page member offsets, from DWARF -- text follows a uint16 with no
    padding (offset 10, not 12), and guessing that shifted every line by two
    characters."""
    i = dw.index("page"); out, name = {}, None
    for ln in dw[i:].splitlines()[1:]:
        if "DW_TAG_structure_type" in ln and out: break
        m = re.search(r"DW_AT_name\b.*?:\s*(\w+)\s*$", ln)
        if m and "DW_TAG" not in ln: name = m.group(1)
        m = re.search(r"DW_AT_data_member_location:\s*(\d+)", ln)
        if m and name: out[name] = int(m.group(1)); name = None
    return out
PG = page_offsets(d)
TXT = PG.get("text", 10)
print(f"struct page: start={PG.get('start')} end={PG.get('end')} "
      f"nlines={PG.get('nlines')} text={TXT}")
s=serialport.open(timeout=0.4); time.sleep(0.2)
def blk(a,n):
    for _ in range(4):
        s.reset_input_buffer(); s.write(f"dbg mdw 0x{a:08x} {n:x}\r\n".encode()); s.flush()
        t=time.time(); b=b""
        while time.time()-t<1.4:
            x=s.read(65536)
            if x: b+=x
            elif b: break
        o={}
        for m in re.finditer(r"^([0-9a-f]{8}): ((?:[0-9a-f]{8} ?){1,4})",b.decode("utf8","replace"),re.M):
            base=int(m.group(1),16)
            for j,w in enumerate(m.group(2).split()): o[base+j*4]=int(w,16)
        if len(o)>=n*0.8: return o
    return {}
def raw(a,nb):
    words=(nb+3)//4; m=blk(a,words)
    return b"".join(m.get(a+i*4,0).to_bytes(4,'little') for i in range(words))
def w(a): return blk(a,1).get(a)
st=w(0x18018E9C)
wt=raw(st+off["wtab"], 95*2)
W=[int.from_bytes(wt[i*2:i*2+2],'little') for i in range(95)]
wp=raw(st+off["wpunct"], 8*2)
PCPS=[0x2018,0x2019,0x201C,0x201D,0x2013,0x2014,0x2026,0x00A0]
WP={PCPS[i]: int.from_bytes(wp[i*2:i*2+2],'little') for i in range(8)}
print("measured punctuation:", {hex(k):v for k,v in WP.items() if v})
def px(text):
    """Same accounting as the reader: per CODE POINT, not per byte."""
    t=0
    for ch in text:
        o=ord(ch)
        if 32<=o<127 and W[o-32]: t += W[o-32]
        elif WP.get(o): t += WP[o]
        else: t += 8
    return t
page=st+off["cur"]
start=w(page+PG.get("start",0)); end=w(page+PG.get("end",4))
nl=(w(page+PG.get("nlines",8)) or 0)&0xffff
print(f"page bytes [{start}..{end}]  lines={nl}  limit={LIMIT}px  table_ok={w(st+off['wtab_ok'])&0xff}")
txt=raw(page+TXT, LINES*MAXW)
lines=[]
for i in range(min(nl or LINES, LINES)):
    b=txt[i*MAXW:(i+1)*MAXW].split(b"\0")[0]
    lines.append(b.decode("utf-8","replace"))
for i,l in enumerate(lines):
    used=px(l); left=LIMIT-used
    nxt=""
    if l.strip() and i+1 < len(lines) and lines[i+1].strip():
        nxt=lines[i+1].split(" ")[0]      # blank line = paragraph break, skip
    fits=""
    if nxt:
        need=px(" "+nxt)
        fits = f"   next '{nxt}' needs {need}px -> " + ("WOULD HAVE FIT" if need<=left else "no")
    print(f"  [{i:2d}] {used:3d}px  left {left:3d}  |{l}|{fits}")
s.close()
