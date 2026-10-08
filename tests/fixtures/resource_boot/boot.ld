OUTPUT_ARCH(mips)
SECTIONS {
  .rsp.boot 0x04001000 : AT(0x800DCC40) { KEEP(*(.rsp.boot)) }
  /DISCARD/ : { *(.text*) *(.data*) *(.bss*) *(.reginfo) *(.MIPS.abiflags) *(.pdr) *(.gnu.attributes) }
}
ASSERT(SIZEOF(.rsp.boot) == 208, "RSP boot must occupy 208 resident bytes")
