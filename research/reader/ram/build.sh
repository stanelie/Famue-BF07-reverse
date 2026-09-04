#!/bin/sh
# usage: build.sh <source.c> <load-address>   e.g. build.sh trace.c 0x01006fa4
set -e
SRC="$1"; BASE="$2"
DIR=$(cd "$(dirname "$0")" && pwd)
NAME=$(basename "$SRC" .c)
sed "s/@BASE@/$BASE/" "$DIR/ram.ld.in" > "$DIR/$NAME.ld"
clang --target=thumbv7em-none-eabi -mthumb -mcpu=cortex-m4 -O2 -ffreestanding \
      -fno-builtin -fno-exceptions -I"$DIR/../include" -c "$DIR/$SRC" -o "$DIR/$NAME.o"
arm-none-eabi-ld -T "$DIR/$NAME.ld" "$DIR/$NAME.o" -o "$DIR/$NAME.elf"
arm-none-eabi-objcopy -O binary "$DIR/$NAME.elf" "$DIR/$NAME.bin"
echo "$NAME.bin: $(wc -c < "$DIR/$NAME.bin") bytes at $BASE"
