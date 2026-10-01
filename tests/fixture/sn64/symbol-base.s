	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_80405160
	.type	 func_80405160,@function
	.ent	func_80405160
func_80405160:
	.frame	$sp,0,$31		# vars= 0, regs= 0/0, args= 0, extra= 0
	.mask	0x00000000,0
	.fmask	0x00000000,0
	sll	$6,$4,2
	lw	$3,D_801534F0($6)
	li	$2,3			# 0x00000003
	.set	noreorder
	.set	nomacro
	bne	$3,$2,.L7
	li	$2,-2			# 0xfffffffe
	.set	macro
	.set	reorder

	lw	$2,D_80153500($6)
	#nop
	.set	noreorder
	.set	nomacro
	bnel	$2,$0,.L3
	sw	$0,0($5)
	.set	macro
	.set	reorder

	sll	$2,$4,7
	addu	$2,$2,$4
	lw	$3,D_800E2854
	sll	$2,$2,2
	addu	$2,$2,$3
	lw	$2,0($2)
	#nop
	.set	noreorder
	.set	nomacro
	bltzl	$2,.L4
	addu	$2,$2,255
	.set	macro
	.set	reorder

.L4:
	sra	$2,$2,8
	sw	$2,0($5)
.L3:
	sll	$2,$4,2
	lw	$2,D_80153500($2)
.L7:
	j	$31
	.end	func_80405160
.Lfe1:
	.size	 func_80405160,.Lfe1-func_80405160
