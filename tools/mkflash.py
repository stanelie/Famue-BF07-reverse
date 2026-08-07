import sys,re,os
sys.path.insert(0,'$BF07_ROOT/tools')
import patch_lines as P
FW0=0x14000; XIP=0x10000000
CONT_TOP=24; CONT_SUB=24   # container y=12, height=264-12=252
out=sys.argv[1]; hooks=eval(sys.argv[2])   # {site: symbol_addr}
bwh=eval(sys.argv[3]) if len(sys.argv)>3 else {}  # sites needing B.W not BL
os.makedirs(out,exist_ok=True)
blob=open('$BF07_ROOT/reader/reader.bin','rb').read()
assert len(blob)<=0x1000, "blob exceeds one sector"
sec=bytearray(b'\xff'*0x1000); sec[0:len(blob)]=blob
open(f'{out}/sector_1e7000.bin','wb').write(bytes(sec))
bs=[i for i in range(0,0x1000,32) if bytes(sec[i:i+32])!=b'\xff'*32]
stock=open('$BF07_BACKUPS/fw_code_full.bin','rb').read()
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
jobs.append((0x1e7000,bs,False))
src=open('flash_full.py').read().replace('OUT = SPD + "outfull/"',f'OUT = SPD + "{out}/"')
lines=[f'    (0x{s:X}, OUT + "sector_{s:06x}.bin", [{", ".join(hex(x) for x in b)}], {f}),' for s,b,f in jobs]
lines.append('    (0x5F000, SPD + "stock/sector_05f000.bin", [], True),')
lines.append('    (0xFF000, SPD + "stock/sector_0ff000.bin", [], True),')
src=re.sub(r'JOBS = \[.*?\n\]',"JOBS = [\n"+"\n".join(lines)+"\n]",src,flags=re.S)
src=src.replace('"FULL 12-LINE BUILD FLASHED"',f'"{out} FLASHED ({len(blob)} bytes of C)"')
open(f'flash_{out}.py','w').write(src)
print(f"  {len(blob)} bytes of compiled C; stub blocks {[hex(x) for x in bs]}")
