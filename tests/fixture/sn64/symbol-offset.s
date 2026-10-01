	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_8041E478
	.type	 func_8041E478,@function
	.ent	func_8041E478
func_8041E478:
	.frame	$sp,40,$31		# vars= 0, regs= 6/0, args= 16, extra= 0
	.mask	0x801f0000,-4
	.fmask	0x00000000,0
	subu	$sp,$sp,40
	move	$5,$4
	lw	$2,D_800E39C0
	sll	$4,$5,2
	sw	$31,36($sp)
	sw	$20,32($sp)
	sw	$19,28($sp)
	sw	$18,24($sp)
	sw	$17,20($sp)
	sw	$16,16($sp)
	lw	$3,28($2)
	addu	$2,$2,$4
	lw	$20,8($2)
	slt	$3,$3,$5
	.set	noreorder
	.set	nomacro
	bne	$3,$0,.L4
	move	$4,$20
	.set	macro
	.set	reorder

	sll	$2,$5,3
	subu	$2,$2,$5
	sll	$19,$2,2
	lw	$2,D_80153F80($19)
	#nop
	bgez	$2,.L2
.L4:
	.set	noreorder
	.set	nomacro
	jal	func_8040E958
	move	$5,$0
	.set	macro
	.set	reorder

	j	.L1
.L2:
	.set	noreorder
	.set	nomacro
	jal	func_8040ECB0
	li	$5,909			# 0x0000038d
	.set	macro
	.set	reorder

	move	$18,$2
	li	$17,125			# 0x0000007d
	sb	$17,16($18)
	lw	$4,D_80153F80($19)
	jal	func_8041F1B0
	move	$4,$20
	sll	$3,$2,3
	subu	$3,$3,$2
	sll	$3,$3,4
	lw	$2,D_800E3A58($3)
	li	$5,911			# 0x0000038f
	.set	noreorder
	.set	nomacro
	jal	func_8040ECB0
	sw	$2,44($18)
	.set	macro
	.set	reorder

	move	$18,$2
	sb	$17,16($18)
	lw	$4,D_80153F80+4($19)
	lw	$5,D_80153F80($19)
	jal	func_8041ECB4
	lw	$4,D_80153F80+4($19)
	la	$16,D_80153F80+16
	sll	$3,$4,4
	addu	$3,$3,$4
	addu	$3,$3,$2
	sll	$3,$3,3
	lw	$2,D_800E381C+4($3)
	addu	$16,$19,$16
	lw	$5,0($2)
	.set	noreorder
	.set	nomacro
	jal	func_802A125C
	move	$4,$16
	.set	macro
	.set	reorder

	move	$4,$20
	li	$5,910			# 0x0000038e
	.set	noreorder
	.set	nomacro
	jal	func_8040ECB0
	sw	$16,56($18)
	.set	macro
	.set	reorder

	move	$18,$2
	sb	$17,16($18)
	lw	$2,D_80153F80+4($19)
	#nop
	sll	$2,$2,2
	lw	$2,D_800E39B4($2)
	move	$4,$20
	lw	$2,0($2)
	li	$5,912			# 0x00000390
	.set	noreorder
	.set	nomacro
	jal	func_8040ECB0
	sw	$2,56($18)
	.set	macro
	.set	reorder

	move	$18,$2
	sb	$17,16($18)
	lw	$2,D_80153F80+8($19)
	move	$4,$20
	sll	$2,$2,3
	lw	$2,D_800E3790($2)
	li	$5,913			# 0x00000391
	.set	noreorder
	.set	nomacro
	jal	func_8040ECB0
	sw	$2,44($18)
	.set	macro
	.set	reorder

	move	$18,$2
	sb	$17,16($18)
	lw	$2,D_80153F80+8($19)
	#nop
	sll	$2,$2,3
	lw	$2,D_800E3790+4($2)
	#nop
	sw	$2,44($18)
.L1:
	lw	$31,36($sp)
	lw	$20,32($sp)
	lw	$19,28($sp)
	lw	$18,24($sp)
	lw	$17,20($sp)
	lw	$16,16($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,40
	.set	macro
	.set	reorder

	.end	func_8041E478
.Lfe1:
	.size	 func_8041E478,.Lfe1-func_8041E478
