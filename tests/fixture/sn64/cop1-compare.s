	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_80216808
	.type	 func_80216808,@function
	.ent	func_80216808
func_80216808:
	.frame	$sp,24,$31		# vars= 0, regs= 1/0, args= 16, extra= 0
	.mask	0x80000000,-8
	.fmask	0x00000000,0
	mtc1	$6,$f0
	mtc1	$7,$f1
	subu	$sp,$sp,24
	sw	$31,16($sp)
	c.lt.s	$f0,$f1
	#nop
	.set	noreorder
	.set	nomacro
	bc1t	.L3
	mov.s	$f3,$f1
	.set	macro
	.set	reorder

	neg.s	$f0,$f0
	c.lt.s	$f1,$f0
	#nop
	bc1f	.L2
.L3:
	mov.s	$f1,$f0
.L2:
	l.s	$f0,D_800D2988
	#nop
	mul.s	$f1,$f1,$f0
	mtc1	$0,$f0
	#nop
	c.lt.s	$f1,$f0
	#nop
	.set	noreorder
	.set	nomacro
	bc1f	.L4
	mov.s	$f2,$f1
	.set	macro
	.set	reorder

	neg.s	$f2,$f1
.L4:
	c.lt.s	$f3,$f0
	#nop
	bc1f	.L5
	neg.s	$f0,$f3
	c.lt.s	$f0,$f2
	#nop
	.set	noreorder
	.set	nomacro
	bc1tl	.L8
	mov.s	$f1,$f3
	.set	macro
	.set	reorder

	j	.L8
.L5:
	c.lt.s	$f3,$f2
	#nop
	.set	noreorder
	.set	nomacro
	bc1tl	.L8
	mov.s	$f1,$f3
	.set	macro
	.set	reorder

.L8:
	mfc1	$5,$f1
	jal	func_80217074
	lw	$31,16($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,24
	.set	macro
	.set	reorder

	.end	func_80216808
.Lfe1:
	.size	 func_80216808,.Lfe1-func_80216808
