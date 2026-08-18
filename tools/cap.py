import glob, sys, time, serial
import serialport
keys = sys.argv[1:] or ["CB registered","BOUNCE","DELTA","TURN","PCT tenths"]
s=serialport.open(timeout=0.5)
out=open("logs/cap.log","w"); t0=time.time(); buf=b""
while time.time()-t0 < 300:
    d=s.read(8192)
    if not d: continue
    buf+=d
    while b"\n" in buf:
        ln,buf=buf.split(b"\n",1)
        t=ln.decode("utf8","replace").strip()
        if any(k in t for k in keys):
            stamp=f"{time.time()-t0:6.1f}"
            print(stamp, t, flush=True); out.write(stamp+" "+t+"\n"); out.flush()
