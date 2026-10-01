	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
.section	.rodata
	.align	2
.LC0:
	.string	"GNUAS\n\t\"quote\"\\A"
	.text
	.align	2
	.globl	string_probe
	.type	 string_probe,@function
	.ent	string_probe
string_probe:
	.frame	$sp,24,$31		# vars= 0, regs= 1/0, args= 16, extra= 0
	.mask	0x80000000,-8
	.fmask	0x00000000,0
	subu	$sp,$sp,24
	la	$4,.LC0
	sw	$31,16($sp)
	jal	consume
	lw	$31,16($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,24
	.set	macro
	.set	reorder

	.end	string_probe
.Lfe1:
	.size	 string_probe,.Lfe1-string_probe
