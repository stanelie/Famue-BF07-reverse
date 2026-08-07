/* Injected code lives in the erased tail of fw0_sys:
 * flash 0x1e7000..0x1f4000 (52 KB) -> XIP 0x101d3000..0x101e0000 */
ENTRY(hook)
MEMORY { INJECT (rx) : ORIGIN = 0x101d3000, LENGTH = 52K }
SECTIONS {
  .text : { *(.text.hook) *(.text*) *(.rodata*) } > INJECT
  /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.note*) }
}
