#!/usr/bin/env python3
"""Extract decrypted firmware regions from the BF07 via the Zephyr shell's
`dbg mdw` command, working around the fact that bulk `dbg dumpbuf` silently
returns zeros for the XIP flash-mapped window. Robust to interleaved async
log noise on the console by validating strict address continuity per chunk
and retrying on any corruption."""
import serial
import re
import time
import json
import os
import sys
import serialport

PORT = serialport.resolve()
BAUD = 2000000
SCRATCH = os.environ.get("BF07_BACKUPS", os.path.expanduser("~/Documents/bf07-backups"))

REGIONS = [
    {'name': 'fw_code', 'base': 0x10000000, 'size': 0x1E0000},
    {'name': 'fw_sdfs', 'base': 0x13000000, 'size': 0xA0000},
]

WORDS_PER_CALL = 1024          # request count; actual return clamps ~1024-1050 words
MIN_ACCEPT_BYTES = 2048        # below this, retry instead of accepting partial chunk
MAX_RETRIES = 8
READ_TIMEOUT = 6.0             # max seconds to wait for a chunk response
QUIET_GAP = 0.35               # seconds of silence indicating response is complete

LINE_RE = re.compile(
    r'^([0-9a-fA-F]{8}): ((?:[0-9a-fA-F]{8}(?: |$)){1,4})',
    re.MULTILINE,
)

log_path = f'{SCRATCH}/extract_progress.log'
def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(log_path, 'a') as f:
        f.write(line + '\n')

def read_until_quiet(ser, timeout=READ_TIMEOUT, quiet_gap=QUIET_GAP):
    buf = bytearray()
    start = time.time()
    last_data = start
    while True:
        chunk = ser.read(4096)
        now = time.time()
        if chunk:
            buf.extend(chunk)
            last_data = now
        if now - last_data > quiet_gap:
            break
        if now - start > timeout:
            break
    return bytes(buf)

def parse_chunk(raw_text, expect_addr):
    """Return (bytes_obj, coverage_len) for the longest clean contiguous run
    starting exactly at expect_addr."""
    matches = []
    for m in LINE_RE.finditer(raw_text):
        addr = int(m.group(1), 16)
        words = m.group(2).split()
        matches.append((addr, words))
    matches.sort(key=lambda t: t[0])

    out = bytearray()
    expected_next = expect_addr
    for addr, words in matches:
        if addr != expected_next:
            break
        for w in words:
            out.extend(int(w, 16).to_bytes(4, 'little'))
        expected_next = addr + len(words) * 4
    return bytes(out)

def fetch_chunk(ser, addr):
    cmd = f'dbg mdw 0x{addr:08x} {WORDS_PER_CALL}\r\n'
    ser.reset_input_buffer()
    ser.write(cmd.encode())
    raw = read_until_quiet(ser)
    text = raw.decode('utf-8', errors='replace')
    return parse_chunk(text, addr)

def reconnect(ser):
    try:
        ser.close()
    except Exception:
        pass
    time.sleep(1.5)
    new_ser = serial.Serial(PORT, BAUD, timeout=1)
    time.sleep(0.5)
    new_ser.reset_input_buffer()
    return new_ser

def extract_region(ser_holder, name, base, size, checkpoint):
    out_path = f'{SCRATCH}/{name}_full.bin'
    done = checkpoint.get(name, 0)
    mode = 'r+b' if done else 'wb'
    with open(out_path, mode) as f:
        if mode == 'wb':
            f.truncate(size)
        while done < size:
            addr = base + done
            data = b''
            for attempt in range(MAX_RETRIES):
                data = fetch_chunk(ser_holder[0], addr)
                if len(data) >= MIN_ACCEPT_BYTES or done + len(data) >= size:
                    break
                log(f'{name} @0x{addr:08x}: only {len(data)}B clean, retry {attempt+1}/{MAX_RETRIES}')
            if not data:
                log(f'{name} @0x{addr:08x}: FAILED after {MAX_RETRIES} retries, reconnecting serial port')
                for reconnect_attempt in range(3):
                    ser_holder[0] = reconnect(ser_holder[0])
                    data = fetch_chunk(ser_holder[0], addr)
                    if data:
                        log(f'{name} @0x{addr:08x}: recovered after reconnect #{reconnect_attempt+1}, got {len(data)}B')
                        break
                    log(f'{name} @0x{addr:08x}: still 0B after reconnect #{reconnect_attempt+1}')
                if not data:
                    log(f'{name} @0x{addr:08x}: giving up this round, will retry on next script run')
                    break
            # clamp to region size
            take = min(len(data), size - done)
            f.seek(done)
            f.write(data[:take])
            f.flush()
            done += take
            checkpoint[name] = done
            with open(f'{SCRATCH}/extract_checkpoint.json', 'w') as cf:
                json.dump(checkpoint, cf)
            pct = 100.0 * done / size
            if (done // (64*1024)) != ((done - take) // (64*1024)):
                log(f'{name}: {done}/{size} bytes ({pct:.1f}%)')
    log(f'{name}: DONE {done}/{size} bytes')

def main():
    ckpt_path = f'{SCRATCH}/extract_checkpoint.json'
    try:
        with open(ckpt_path) as f:
            checkpoint = json.load(f)
    except FileNotFoundError:
        checkpoint = {}

    ser = serial.Serial(PORT, BAUD, timeout=1)
    time.sleep(0.5)
    ser.reset_input_buffer()
    ser_holder = [ser]
    log('=== extraction run starting ===')
    for region in REGIONS:
        if checkpoint.get(region['name'], 0) >= region['size']:
            log(f"{region['name']}: already complete, skipping")
            continue
        extract_region(ser_holder, region['name'], region['base'], region['size'], checkpoint)
    ser_holder[0].close()
    log('=== extraction run finished ===')

if __name__ == '__main__':
    main()
