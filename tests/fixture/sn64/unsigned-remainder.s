	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_80256130
	.type	 func_80256130,@function
	.ent	func_80256130
func_80256130:
	.frame	$sp,32,$31		# vars= 0, regs= 3/0, args= 16, extra= 0
	.mask	0x80030000,-8
	.fmask	0x00000000,0
	subu	$sp,$sp,32
	sw	$17,20($sp)
	move	$17,$4
	sw	$31,24($sp)
	sw	$16,16($sp)
	lw	$16,0($17)
	#nop
	.set	noreorder
	.set	nomacro
	beq	$16,$0,.L8
	move	$2,$0
	.set	macro
	.set	reorder

	jal	func_80274544
	lw	$3,16($17)
	#nop
	remu	$3,$2,$3
	li	$2,-1			# 0xffffffff
	addu	$3,$3,-1
	.set	noreorder
	.set	nomacro
	beql	$3,$2,.L8
	move	$2,$16
	.set	macro
	.set	reorder

	lw	$4,12($17)
	move	$5,$2
	addu	$2,$16,$4
.L9:
	lw	$16,0($2)
	addu	$3,$3,-1
	.set	noreorder
	.set	nomacro
	bne	$3,$5,.L9
	addu	$2,$16,$4
	.set	macro
	.set	reorder

	move	$2,$16
.L8:
	lw	$31,24($sp)
	lw	$17,20($sp)
	lw	$16,16($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,32
	.set	macro
	.set	reorder

	.end	func_80256130
.Lfe1:
	.size	 func_80256130,.Lfe1-func_80256130
