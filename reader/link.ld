/* Code: erased tail of fw0_sys, flash 0x1e7000 -> XIP 0x101d3000 (52 KB).
 * Data: the vendor's STANDALONE page context. Only 0x1004938a ever decodes
 * into it, and our replacement of that function does not, so the whole 0x3cc
 * is ours. Nothing zeroes it -- our code must initialise it itself. */
ENTRY(hook)
MEMORY {
  INJECT (rx) : ORIGIN = 0x101d3000, LENGTH = 52K
  INJRAM (rw) : ORIGIN = 0x18018a4c, LENGTH = 0x3cc
}
SECTIONS {
  .text : { *(.text.hook) *(.text*) *(.rodata*) } > INJECT
  .bss (NOLOAD) : { *(.bss*) *(COMMON) } > INJRAM
  /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.note*) }
}
