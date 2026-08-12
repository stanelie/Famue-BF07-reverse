/* Pack a RAM payload tightly: the default script pads .data onto a fresh 4 KB
   page, which turned a 312-byte function into a 4546-byte image -- far past the
   1.5 KB upload buffer. Everything is contiguous here, and .bss is discarded:
   an uploaded blob carries no zeroed section, so any zero-initialised static
   would start as garbage. Initialise statics to non-zero so they land in .data
   and travel with the image. */
ENTRY(inj_entry)
SECTIONS
{
    . = 0x01006fa4;
    .all : {
        *(.text.inj_entry)
        *(.text*)
        *(.rodata*)
        *(.data*)
    }
    /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.bss*) *(COMMON) }
}
