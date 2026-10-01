	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	local_address
	.type	 local_address,@function
	.ent	local_address
local_address:
	.frame	$sp,0,$31		# vars= 0, regs= 0/0, args= 0, extra= 0
	.mask	0x00000000,0
	.fmask	0x00000000,0
	la	$2,local_words
	j	$31
	.end	local_address
.Lfe1:
	.size	 local_address,.Lfe1-local_address
	.comm	shared_small,4,4
	.comm	shared_large,8,8
	.local	local_words
	.comm	local_words,12,4
