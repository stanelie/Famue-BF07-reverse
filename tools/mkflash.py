import sys,re,os
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.environ.get("BF07_ROOT", os.path.dirname(_HERE))
_BACKUPS = os.environ.get("BF07_BACKUPS", os.path.join(os.path.dirname(_ROOT), "bf07-backups"))
sys.path.insert(0, os.path.join(os.environ.get('BF07_ROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'tools'))
import patch_lines as P
FW0=0x14000; XIP=0x10000000
CONT_TOP=24; CONT_SUB=24   # container y=12, height=264-12=252
out=sys.argv[1]; hooks=eval(sys.argv[2])   # {site: symbol_addr}
bwh=eval(sys.argv[3]) if len(sys.argv)>3 else {}  # sites needing B.W not BL
os.makedirs(out,exist_ok=True)
blob=open(os.path.join(_ROOT,'reader','reader.bin'),'rb').read()
# The reader spans as many sectors as it needs -- there are 53 KB of free 0xFF
# padding at 0x1e7000-0x1f4000, so one sector was never the real limit.
CODE_BASE=0x1e7000; CODE_LIMIT=0x1f4000
assert CODE_BASE+len(blob) <= CODE_LIMIT, (
    f"blob is {len(blob)} bytes; free space is {CODE_LIMIT-CODE_BASE}")
code_jobs=[]
for n in range((len(blob)+0xfff)//0x1000):
    chunk=blob[n*0x1000:(n+1)*0x1000]
    sec=bytearray(b'\xff'*0x1000); sec[0:len(chunk)]=chunk
    addr=CODE_BASE+n*0x1000
    open(f'{out}/sector_{addr:06x}.bin','wb').write(bytes(sec))
    b=[i for i in range(0,0x1000,32) if bytes(sec[i:i+32])!=b'\xff'*32]
    code_jobs.append((addr,b))
bs=code_jobs[0][1]
stock=open(os.path.join(_BACKUPS,'fw_code_full.bin'),'rb').read()
data=bytearray(stock)
data[0x1004A1FC-XIP:0x1004A1FC-XIP+2]=P.movs_imm8(2,CONT_TOP)
data[0x1004A222-XIP:0x1004A222-XIP+2]=P.sub_imm8(0,CONT_SUB)
for site,target in hooks.items():
    data[site-XIP:site-XIP+4]=P.bl(site,target)
for site,target in bwh.items():
    data[site-XIP:site-XIP+4]=P.bw(site,target)
# Sectors are derived from the patch addresses -- hardcoding them silently
# dropped the 0x1004c002 hook, which lives in 0x60000.
touched={0x5d000,0x5e000}
for site in list(hooks)+list(bwh):
    touched.add((FW0+(site-XIP)) & ~0xfff)
jobs=[]
for s in sorted(touched):
    o=s-FW0
    open(f'{out}/sector_{s:06x}.bin','wb').write(bytes(data[o:o+0x1000]))
    b=[i for i in range(0,0x1000,32) if bytes(data[o+i:o+i+32])!=stock[o+i:o+i+32]]
    jobs.append((s,b,True))
for addr,b in code_jobs:
    jobs.append((addr,b,False))
src=open('flash_full.py').read().replace('OUT = SPD + "outfull/"',f'OUT = SPD + "{out}/"')
lines=[f'    (0x{s:X}, OUT + "sector_{s:06x}.bin", [{", ".join(hex(x) for x in b)}], {f}),' for s,b,f in jobs]
lines.append('    (0x5F000, SPD + "stock/sector_05f000.bin", [], True),')
lines.append('    (0xFF000, SPD + "stock/sector_0ff000.bin", [], True),')
src=re.sub(r'JOBS = \[.*?\n\]',"JOBS = [\n"+"\n".join(lines)+"\n]",src,flags=re.S)
src=src.replace('"FULL 12-LINE BUILD FLASHED"',f'"{out} FLASHED ({len(blob)} bytes of C)"')
open(f'flash_{out}.py','w').write(src)
print(f"  {len(blob)} bytes of compiled C across {len(code_jobs)} sector(s): "
      f"{[hex(a) for a,_ in code_jobs]}")
