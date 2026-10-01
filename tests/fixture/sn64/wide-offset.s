	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_80285F28
	.type	 func_80285F28,@function
	.ent	func_80285F28
func_80285F28:
	.frame	$sp,32,$31		# vars= 0, regs= 3/0, args= 16, extra= 0
	.mask	0x80030000,-8
	.fmask	0x00000000,0
	subu	$sp,$sp,32
	sw	$31,24($sp)
	sw	$17,20($sp)
	sw	$16,16($sp)
	lw	$2,256($5)
	li	$3,524288			# 0x00080000
	and	$2,$2,$3
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L2
	move	$6,$4
	.set	macro
	.set	reorder

	lw	$4,312($6)
	#nop
	sltu	$2,$5,$4
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L5
	li	$17,-1			# 0xffffffff
	.set	macro
	.set	reorder

	lw	$2,320($6)
	#nop
	sll	$3,$2,1
	addu	$3,$3,$2
	sll	$2,$3,5
	subu	$2,$2,$3
	sll	$2,$2,3
	addu	$2,$2,-744
	addu	$2,$4,$2
	sltu	$2,$2,$5
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L9
	li	$2,-1			# 0xffffffff
	.set	macro
	.set	reorder

	subu	$3,$5,$4
	li	$2,-1339290877			# 0xb02c0b03
	multu	$3,$2
	mfhi	$7
	#nop
	.set	noreorder
	.set	nomacro
	j	.L5
	srl	$17,$7,9
	.set	macro
	.set	reorder

.L2:
	li	$17,-1			# 0xffffffff
.L5:
	li	$2,-1			# 0xffffffff
.L9:
	.set	noreorder
	.set	nomacro
	beq	$17,$2,.L8
	move	$2,$0
	.set	macro
	.set	reorder

	lw	$4,128($6)
	lw	$16,111628($6)
	.set	noreorder
	.set	nomacro
	jal	func_8028FD94
	move	$5,$0
	.set	macro
	.set	reorder

	move	$4,$2
	.set	noreorder
	.set	nomacro
	jal	func_8028FD94
	move	$5,$16
	.set	macro
	.set	reorder

	move	$4,$2
	.set	noreorder
	.set	nomacro
	jal	func_8028FD94
	move	$5,$0
	.set	macro
	.set	reorder

	move	$16,$2
	move	$4,$16
	.set	noreorder
	.set	nomacro
	jal	func_8028FD94
	move	$5,$0
	.set	macro
	.set	reorder

	move	$4,$16
	.set	noreorder
	.set	nomacro
	jal	func_8028FDD8
	li	$5,1			# 0x00000001
	.set	macro
	.set	reorder

	move	$4,$16
	.set	noreorder
	.set	nomacro
	jal	func_8028FD94
	li	$5,1			# 0x00000001
	.set	macro
	.set	reorder

	move	$4,$17
	move	$5,$2
	andi	$3,$4,0x0007
	li	$2,1			# 0x00000001
	.set	noreorder
	.set	nomacro
	bgez	$4,.L7
	sll	$3,$2,$3
	.set	macro
	.set	reorder

	addu	$4,$4,7
.L7:
	sra	$2,$4,3
	addu	$2,$5,$2
	lbu	$2,0($2)
	#nop
	and	$2,$2,$3
	sltu	$2,$0,$2
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

	.end	func_80285F28
.Lfe1:
	.size	 func_80285F28,.Lfe1-func_80285F28
