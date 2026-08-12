# Toolchain: Apple clang compiles Thumb-2; GNU binutils links and extracts.
CC      = clang
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
