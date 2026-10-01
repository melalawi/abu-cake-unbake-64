	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_804208C0
	.type	 func_804208C0,@function
	.ent	func_804208C0
func_804208C0:
	.frame	$sp,104,$31		# vars= 16, regs= 7/0, args= 56, extra= 0
	.mask	0x803f0000,-8
	.fmask	0x00000000,0
	subu	$sp,$sp,104
	sw	$19,84($sp)
	move	$19,$4
	sw	$18,80($sp)
	move	$18,$0
	sw	$17,76($sp)
	move	$17,$18
	sw	$21,92($sp)
	li	$21,2021130240			# 0x78780000
	ori	$21,$21,0x7879
	sll	$2,$19,3
	addu	$2,$2,$19
	sll	$3,$2,4
	addu	$2,$2,$3
	sw	$20,88($sp)
	sll	$20,$2,3
	sw	$31,96($sp)
	sw	$16,72($sp)
.L2:
	jal	func_80274544
	mult	$2,$21
	sra	$3,$2,31
	mfhi	$8
	#nop
	#nop
	sra	$4,$8,3
	subu	$4,$4,$3
	sll	$3,$4,4
	addu	$3,$3,$4
	subu	$2,$2,$3
	sll	$3,$2,3
	subu	$3,$3,$2
	sll	$3,$3,4
	lw	$2,D_800E42D0
	lw	$16,D_800E3A50($3)
	lw	$4,0($2)
	.set	noreorder
	.set	nomacro
	jal	func_8040ECB0
	andi	$5,$16,0xffff
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	jal	func_8040EC50
	move	$4,$2
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	bnel	$2,$0,.L11
	addu	$18,$18,1
	.set	macro
	.set	reorder

	lw	$2,D_800E42D0
	#nop
	addu	$2,$2,$20
	lw	$2,16($2)
	#nop
	.set	noreorder
	.set	nomacro
	bnel	$2,$16,.L4
	li	$17,1			# 0x00000001
	.set	macro
	.set	reorder

	addu	$18,$18,1
.L11:
	slt	$2,$18,5001
	bne	$2,$0,.L4
	li	$16,146			# 0x00000092
	li	$17,1			# 0x00000001
.L4:
	.set	noreorder
	.set	nomacro
	beq	$17,$0,.L2
	sll	$2,$19,3
	.set	macro
	.set	reorder

	move	$4,$16
	addu	$2,$2,$19
	sll	$3,$2,4
	addu	$2,$2,$3
	lw	$3,D_800E42D0
	sll	$18,$2,3
	addu	$3,$3,$18
	.set	noreorder
	.set	nomacro
	jal	func_8041F248
	sw	$4,16($3)
	.set	macro
	.set	reorder

	lw	$3,D_800E42D0
	#nop
	addu	$3,$3,$18
	lw	$4,16($3)
	.set	noreorder
	.set	nomacro
	jal	func_8041F1FC
	move	$17,$2
	.set	macro
	.set	reorder

	li	$5,9			# 0x00000009
	sll	$3,$19,2
	sll	$6,$2,3
	subu	$6,$6,$2
	sll	$6,$6,4
	addu	$3,$3,$6
	li	$2,24000			# 0x00005dc0
	sll	$16,$19,1
	addu	$16,$16,$19
	li	$7,75			# 0x0000004b
	lw	$4,D_800E42D0
	l.s	$f0,D_800E3A50+12($3)
	addu	$4,$18,$4
	mov.s	$f1,$f0
	mov.s	$f2,$f0
	addu	$4,$4,32
	s.s	$f0,56($sp)
	s.s	$f1,60($sp)
	s.s	$f2,64($sp)
	sw	$2,16($sp)
	sll	$2,$16,2
	addu	$2,$2,$6
	l.s	$f0,D_800E3A50+28($3)
	lw	$3,D_800E3A50+92($3)
	addu	$6,$17,911
	lw	$9,56($sp)
	lw	$10,60($sp)
	lw	$11,64($sp)
	sw	$9,20($sp)
	sw	$10,24($sp)
	sw	$11,28($sp)
	lw	$9,D_800E3A50+44($2)
	lw	$10,D_800E3A50+48($2)
	lw	$11,D_800E3A50+52($2)
	sw	$9,32($sp)
	sw	$10,36($sp)
	sw	$11,40($sp)
	s.s	$f0,44($sp)
	.set	noreorder
	.set	nomacro
	jal	func_8041CB48
	sw	$3,48($sp)
	.set	macro
	.set	reorder

	sll	$16,$16,3
	addu	$16,$16,$19
	sll	$16,$16,4
	addu	$17,$17,$16
	lbu	$16,D_80102B57($17)
	#nop
	.set	noreorder
	.set	nomacro
	blezl	$16,.L9
	li	$16,1			# 0x00000001
	.set	macro
	.set	reorder

.L9:
	jal	func_80274544
	rem	$3,$2,$16
	lw	$2,D_800E42D0
	#nop
	addu	$2,$2,$18
	sw	$3,24($2)
	lw	$31,96($sp)
	lw	$21,92($sp)
	lw	$20,88($sp)
	lw	$19,84($sp)
	lw	$18,80($sp)
	lw	$17,76($sp)
	lw	$16,72($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,104
	.set	macro
	.set	reorder

	.end	func_804208C0
.Lfe1:
	.size	 func_804208C0,.Lfe1-func_804208C0
