# Toolchain: clang-15 compiles Thumb-2; GNU binutils links and extracts.
# PINNED: -O2 fills 99.5% of the 52K budget, so the compiler version is load-
# bearing. clang-15=52978 (270 free) 16=53050 17=53106 18=53519 (OVER by 271).
# Every version builds at ~46100 with -Os if you want the headroom back.
CC      = clang-15
LD      = arm-none-eabi-ld
OBJCOPY = arm-none-eabi-objcopy
CFLAGS  = --target=thumbv7em-none-eabi -mthumb -mcpu=cortex-m4 -O2 \
          -ffreestanding -fno-builtin -fomit-frame-pointer \
          -ffunction-sections -g -Wall -Iinclude
SRC = $(wildcard src/*.c)
OBJ = $(SRC:.c=.o)

all: reader.bin
%.o: %.c ; $(CC) $(CFLAGS) -c $< -o $@
reader.elf: $(OBJ) ; $(LD) -T link.ld $(OBJ) -o $@ -Map reader.map
reader.bin: reader.elf ; $(OBJCOPY) -O binary $< $@ && wc -c < $@
clean: ; rm -f src/*.o reader.elf reader.bin reader.map
.PHONY: all clean
