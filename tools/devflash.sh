#!/bin/sh
# Build + flash the reader from the DURABLE work directory.
#   ./build.sh <name> [extra-bw-patches]
# Everything this needs lives here or in the repo -- nothing in /tmp, which has
# been garbage-collected mid-session more than once, taking the venv, the
# flasher template and the stock sectors with it.
set -e
WORK=$(cd "$(dirname "$0")" && pwd)
export BF07_ROOT="$HOME/Documents/bf07-research"
export BF07_BACKUPS="$HOME/Documents/bf07-backups"
NAME="${1:-out}"
PY="$WORK/venv/bin/python3"

( cd "$BF07_ROOT/reader" && make clean >/dev/null && make 2>&1 | tail -1 )

N="$BF07_ROOT/reader/reader.elf"
H=$(arm-none-eabi-nm -n "$N"  | awk '/ T hook$/{print "0x"$1}')
PH=$(arm-none-eabi-nm -n "$N" | awk '/ T prepare_hook$/{print "0x"$1}')
TH=$(arm-none-eabi-nm -n "$N" | awk '/ T tail_hook$/{print "0x"$1}')

cd "$WORK"
cp flash_template.py flash_full.py
RH=$(arm-none-eabi-nm -n "$N" | awk '/ T render_hook$/{print "0x"$1}')
TH=$(arm-none-eabi-nm -n "$N" | awk '/ T tail_hook$/{print "0x"$1}')
GH=$(arm-none-eabi-nm -n "$N" | awk '/ T page_hook$/{print "0x"$1}')

# Choose the display hook by what the build actually exports, and say so.
# A previous version required BOTH tail_hook and page_hook before using the
# tail, so a build with only tail_hook silently fell back to the conditional
# render call -- and the change under test was never on the device.
BW="{"
if [ -n "$TH" ]; then
  BW="$BW 0x100493B2:$TH"                       # unconditional timer tail
  HOOKS="{0x1004A288:$H, 0x1004C002:$PH}"
  echo "display hook: TAIL 0x100493B2 -> $TH"
else
  HOOKS="{0x1004A288:$H, 0x100493A8:$RH, 0x1004C002:$PH}"
  echo "display hook: render call 0x100493A8 -> $RH"
fi
[ -n "$GH" ] && BW="$BW, 0x100EB534:$GH"
FH=$(arm-none-eabi-nm -n "$N" | awk '/ T font_hook$/{print "0x"$1}')
if [ -n "$FH" ]; then
  BW="$BW, 0x100E1348:$FH"                      # glyph dsc cb (font capture)
  echo "font hook:    0x100E1348 -> $FH"
fi
PT=$(arm-none-eabi-nm -n "$N" | awk '/ T pointer_hook$/{print "0x"$1}')
if [ -n "$PT" ]; then
  BW="$BW, 0x100E07B4:$PT"                      # _lvgl_pointer_put (touch driver)
  echo "touch hook:   0x100E07B4 -> $PT"
fi
XH=$(arm-none-eabi-nm -n "$N" | awk '/ T gesture_hook$/{print "0x"$1}')
if [ -n "$XH" ]; then
  BW="$BW, 0x100D92E8:$XH"                      # gesture handler entry
  echo "input hook:   gesture 0x100D92E8 -> $XH"
fi
BW="$BW }"

"$PY" "$BF07_ROOT/tools/mkflash.py" "$NAME" "$HOOKS" "$BW" "{}"

# Always re-upload the ADFU payload. Reusing a live one fails with
# "is failed" whenever a previous run aborted, which has cost an extra
# round trip nearly every time.
sed 's/^ALREADY = payload_alive()/ALREADY = False/' "flash_$NAME.py" > "flash_${NAME}_run.py"
"$PY" "flash_${NAME}_run.py" 2>&1 | tee "logs/$NAME.log" | tail -1
