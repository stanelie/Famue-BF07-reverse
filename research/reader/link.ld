/* Code: erased tail of fw0_sys, flash 0x1e7000 -> XIP 0x101d3000 (52 KB).
 *
 * Data: the LINE-RECORD area of the vendor's standalone page context, i.e.
 * 0x18018a4c + 0x2c. The first 0x2c bytes are NOT ours: a page context BEGINS
 * WITH A KERNEL MUTEX --
 *     1004bbec  bl 0x100f12a0   ; k_mutex_lock(ctx, 0)
 *     1004bc16  bl 0x100e4ba4   ; k_mutex_unlock(ctx)
 * -- followed by the vendor's fields (+0x14 state, +0x18 page, +0x1c encoding,
 * +0x20 buf, +0x24 len, +0x28 line count). Overwriting that header destroyed a
 * live mutex and produced ASSERTION FAIL [pended_on] inside ebook_decode_page,
 * far from the damage.
 *
 * Only the records (0x2c..0x3cc, 928 bytes) are free, because our replacement
 * of 0x1004937c never decodes into this context. Nothing zeroes it. */
ENTRY(hook)
MEMORY {
  INJECT (rx) : ORIGIN = 0x101d3000, LENGTH = 52K
  INJRAM (rw) : ORIGIN = 0x18018a78, LENGTH = 0x3a0
}
SECTIONS {
  .text : { *(.text.hook) *(.text*) *(.rodata*) } > INJECT
  .bss (NOLOAD) : { *(.bss*) *(COMMON) } > INJRAM
  /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.note*) }
}
