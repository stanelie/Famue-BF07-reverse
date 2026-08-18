import sys,re,os
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.environ.get("BF07_ROOT", os.path.dirname(_HERE))
_BACKUPS = os.environ.get("BF07_BACKUPS", os.path.join(os.path.dirname(_ROOT), "bf07-backups"))
sys.path.insert(0, os.path.join(os.environ.get('BF07_ROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'tools'))
import patch_lines as P
FW0=0x14000; XIP=0x10000000
CONT_TOP=26; CONT_SUB=36   # container y=26, height=264-36=228 (12 * 19px)
out=sys.argv[1]; hooks=eval(sys.argv[2])   # {site: symbol_addr}
bwh=eval(sys.argv[3]) if len(sys.argv)>3 else {}  # sites needing B.W not BL
# Plain 32-bit data patches {xip_addr: value} -- for vendor TABLES, not code.
words=eval(sys.argv[4]) if len(sys.argv)>4 else {}
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
for a,v in words.items():
    data[a-XIP:a-XIP+4]=v.to_bytes(4,'little')
# EVERY sector this project has ever patched is rewritten on every flash --
# from stock, plus whatever THIS build patches.
#
# Deriving the list from the current build's hooks was the bug that invalidated
# days of testing: when a hook moved (render -> tail -> tap -> page setter, plus
# the scroll and button probes), the OLD site kept its branch and pointed into a
# code region that now held a different build. Several stale branches ran at
# once, jumping into whatever happened to sit at those addresses -- which is
# almost certainly behind the phantom stalls, the "no change" results and the
# reboots. Rewriting the full set makes each flash a clean slate.
EVER_PATCHED = {
    0x5c000,   # 0x10048d64  button probe
    0x5d000,   # 0x100493a8 render, 0x100493b2 tail, 0x100495d8 tap, 0x10049684 scroll
    0x5e000,   # 0x1004a288 line height, container geometry
    0xed000,   # 0x100d92e8 gesture handler entry (input capture)
    0xf4000,   # 0x100e07b4 _lvgl_pointer_put (touch driver capture)
    0xf5000,   # 0x100e1348 bitmap_font_get_glyph_dsc_cb (font capture)
    0x60000,   # 0x1004c002 message loop
    0xff000,   # 0x100eb534 page setter
    0x6d000,   # 0x1005934c app_menulist_load_res_id (menu label follows the file)
}          # note 0x100e1440 (font open) shares 0xf5000 with the glyph callback
touched = set(EVER_PATCHED)
for site in list(hooks)+list(bwh)+list(words):
    touched.add((FW0+(site-XIP)) & ~0xfff)
jobs=[]
for s in sorted(touched):
    o=s-FW0
    open(f'{out}/sector_{s:06x}.bin','wb').write(bytes(data[o:o+0x1000]))
    b=[i for i in range(0,0x1000,32) if bytes(data[o+i:o+i+32])!=stock[o+i:o+i+32]]
    jobs.append((s,b,True))
for addr,b in code_jobs:
    jobs.append((addr,b,False))
src=open('flash_full.py').read()
# Substitute by PATTERN, not by literal. The template is sometimes restored from
# a previously generated flasher (the scratchpad is ephemeral), and then these
# literals no longer match -- the substitution silently did nothing and the new
# flasher wrote the OLD build's sectors. That looked exactly like a truncated
# two-sector write: 39 blocks landed because the stale build's second sector was
# 39 blocks long.
src, n1 = re.subn(r'OUT = SPD \+ "[^"]*/"', f'OUT = SPD + "{out}/"', src)
assert n1 == 1, f"OUT path substitution failed ({n1} matches)" 
lines=[f'    (0x{s:X}, OUT + "sector_{s:06x}.bin", [{", ".join(hex(x) for x in b)}], {f}),' for s,b,f in jobs]
lines.append('    (0x5F000, SPD + "stock/sector_05f000.bin", [], True),')
lines.append('    (0xFF000, SPD + "stock/sector_0ff000.bin", [], True),')
src=re.sub(r'JOBS = \[.*?\n\]',"JOBS = [\n"+"\n".join(lines)+"\n]",src,flags=re.S)
src, n2 = re.subn(r'"[^"]*FLASHED[^"]*"', f'"{out} FLASHED ({len(blob)} bytes of C)"', src)
assert n2 == 1, f"result-string substitution failed ({n2} matches)" 
open(f'flash_{out}.py','w').write(src)
print(f"  {len(blob)} bytes of compiled C across {len(code_jobs)} sector(s): "
      f"{[hex(a) for a,_ in code_jobs]}")
