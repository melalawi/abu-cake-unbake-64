	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_802B9800
	.type	 func_802B9800,@function
	.ent	func_802B9800
func_802B9800:
	.frame	$sp,72,$31		# vars= 8, regs= 10/0, args= 24, extra= 0
	.mask	0xc0ff0000,-4
	.fmask	0x00000000,0
	subu	$sp,$sp,72
	sw	$19,44($sp)
	lw	$19,88($sp)
	sw	$22,56($sp)
	move	$22,$6
	sw	$16,32($sp)
	move	$16,$4
	sw	$31,68($sp)
	sw	$fp,64($sp)
	sw	$23,60($sp)
	sw	$21,52($sp)
	sw	$20,48($sp)
	sw	$18,40($sp)
	sw	$17,36($sp)
	sw	$7,84($sp)
	lw	$2,60($16)
	move	$fp,$7
	sh	$0,26($sp)
	.set	noreorder
	.set	nomacro
	beq	$2,$0,.L4
	sh	$0,24($sp)
	.set	macro
	.set	reorder

	li	$23,1			# 0x00000001
	la	$21,D_800D8240
	lw	$2,60($16)
	move	$3,$fp
.L49:
	lw	$fp,4($2)
	#nop
	subu	$18,$fp,$3
	slt	$2,$22,$18
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L47
	move	$4,$16
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	bgez	$18,.L48
	slt	$2,$18,161
	.set	macro
	.set	reorder

	la	$4,D_800CC850
	la	$5,D_800CC854
	.set	noreorder
	.set	nomacro
	jal	func_802BFD40
	li	$6,103			# 0x00000067
	.set	macro
	.set	reorder

	slt	$2,$18,161
.L48:
	bne	$2,$0,.L10
	la	$4,D_800CC850
	la	$5,D_800CC854
	.set	noreorder
	.set	nomacro
	jal	func_802BFD40
	li	$6,104			# 0x00000068
	.set	macro
	.set	reorder

.L10:
	lw	$2,60($16)
	#nop
	lh	$3,8($2)
	#nop
	sltu	$2,$3,17
	.set	noreorder
	.set	nomacro
	beq	$2,$0,.L39
	sll	$2,$3,2
	.set	macro
	.set	reorder

	lw	$2,.L40($2)
	#nop
	j	$2
.section	.rodata
	.align	3
	.align	2
.L40:
	.word	.L38
	.word	.L39
	.word	.L39
	.word	.L39
	.word	.L39
	.word	.L39
	.word	.L39
	.word	.L39
	.word	.L39
	.word	.L39
	.word	.L39
	.word	.L23
	.word	.L23
	.word	.L12
	.word	.L35
	.word	.L37
	.word	.L23
	.text
.L12:
	lw	$17,60($16)
	#nop
	lh	$2,10($17)
	#nop
	.set	noreorder
	.set	nomacro
	beq	$2,$0,.L13
	move	$20,$16
	.set	macro
	.set	reorder

	move	$4,$16
	li	$5,8			# 0x00000008
	lw	$2,8($16)
	#nop
	.set	noreorder
	.set	nomacro
	jal	$31,$2
	move	$6,$0
	.set	macro
	.set	reorder

.L13:
	move	$4,$16
	lw	$6,24($17)
	lw	$2,8($16)
	#nop
	.set	noreorder
	.set	nomacro
	jal	$31,$2
	li	$5,5			# 0x00000005
	.set	macro
	.set	reorder

	move	$4,$16
	li	$5,9			# 0x00000009
	lw	$2,8($16)
	#nop
	.set	noreorder
	.set	nomacro
	jal	$31,$2
	move	$6,$0
	.set	macro
	.set	reorder

	sw	$23,56($16)
	sw	$0,48($16)
	lw	$2,20($17)
	#nop
	sw	$2,52($16)
	lh	$2,16($17)
	#nop
	mult	$2,$2
	mflo	$8
	#nop
	#nop
	sra	$2,$8,15
	sh	$2,26($16)
	lbu	$2,18($17)
	#nop
	sh	$2,24($16)
	lbu	$2,19($17)
	#nop
	sll	$2,$2,1
	addu	$2,$2,$21
	lhu	$2,0($2)
	#nop
	sh	$2,32($16)
	lbu	$2,19($17)
	li	$8,127			# 0x0000007f
	subu	$2,$8,$2
	sll	$2,$2,1
	addu	$2,$2,$21
	lhu	$2,0($2)
	#nop
	sh	$2,34($16)
	lw	$2,20($17)
	#nop
	beq	$2,$0,.L16
	sh	$23,28($16)
	.set	noreorder
	.set	nomacro
	j	.L17
	sh	$23,30($16)
	.set	macro
	.set	reorder

.L16:
	lh	$2,24($16)
	lh	$3,26($16)
	sll	$2,$2,1
	addu	$2,$2,$21
	lh	$2,0($2)
	#nop
	mult	$3,$2
	lh	$2,24($16)
	mflo	$8
	#nop
	#nop
	sra	$3,$8,15
	li	$8,127			# 0x0000007f
	subu	$2,$8,$2
	sll	$2,$2,1
	addu	$2,$2,$21
	sh	$3,28($16)
	lh	$3,26($16)
	lh	$2,0($2)
	#nop
	mult	$3,$2
	mflo	$8
	#nop
	#nop
	sra	$2,$8,15
	sh	$2,30($16)
.L17:
	lw	$4,0($20)
	#nop
	.set	noreorder
	.set	nomacro
	beq	$4,$0,.L11
	li	$5,7			# 0x00000007
	.set	macro
	.set	reorder

	lw	$6,12($17)
	lw	$2,8($4)
	j	.L44
.L23:
	move	$4,$16
	addu	$5,$sp,24
	addu	$6,$sp,26
	lw	$8,84($sp)
	move	$7,$18
	sw	$19,20($sp)
	.set	noreorder
	.set	nomacro
	jal	func_802B9D4C
	sw	$8,16($sp)
	.set	macro
	.set	reorder

	lw	$5,48($16)
	lw	$3,52($16)
	#nop
	slt	$3,$5,$3
	.set	noreorder
	.set	nomacro
	bne	$3,$0,.L24
	move	$19,$2
	.set	macro
	.set	reorder

	lh	$2,24($16)
	lh	$3,26($16)
	sll	$2,$2,1
	addu	$2,$2,$21
	lh	$2,0($2)
	#nop
	mult	$3,$2
	lh	$2,24($16)
	mflo	$8
	#nop
	#nop
	sra	$3,$8,15
	li	$8,127			# 0x0000007f
	subu	$2,$8,$2
	sll	$2,$2,1
	addu	$2,$2,$21
	sh	$3,40($16)
	lh	$3,26($16)
	lh	$2,0($2)
	#nop
	mult	$3,$2
	lhu	$3,40($16)
	mflo	$8
	#nop
	#nop
	sra	$2,$8,15
	sh	$2,46($16)
	lw	$2,52($16)
	lhu	$4,46($16)
	sh	$3,28($16)
	sw	$2,48($16)
	.set	noreorder
	.set	nomacro
	j	.L27
	sh	$4,30($16)
	.set	macro
	.set	reorder

.L24:
	lh	$6,38($16)
	lh	$2,28($16)
	lhu	$7,36($16)
	mtc1	$2,$f12
	cvt.s.w	$f12,$f12
	jal	func_802BA358
	lw	$5,48($16)
	lh	$6,44($16)
	lhu	$7,42($16)
	lh	$3,30($16)
	trunc.w.s $f1,$f0,$8
	mfc1	$2,$f1
	mtc1	$3,$f12
	cvt.s.w	$f12,$f12
	.set	noreorder
	.set	nomacro
	jal	func_802BA358
	sh	$2,28($16)
	.set	macro
	.set	reorder

	trunc.w.s $f1,$f0,$8
	mfc1	$2,$f1
	#nop
	sh	$2,30($16)
.L27:
	lh	$2,28($16)
	#nop
	.set	noreorder
	.set	nomacro
	beql	$2,$0,.L28
	sh	$23,28($16)
	.set	macro
	.set	reorder

.L28:
	lh	$2,30($16)
	#nop
	.set	noreorder
	.set	nomacro
	beql	$2,$0,.L29
	sh	$23,30($16)
	.set	macro
	.set	reorder

.L29:
	lw	$4,60($16)
	#nop
	lh	$3,8($4)
	li	$2,12			# 0x0000000c
	bne	$3,$2,.L30
	lhu	$2,14($4)
	#nop
	sh	$2,24($16)
.L30:
	lw	$2,60($16)
	#nop
	lh	$3,8($2)
	li	$2,11			# 0x0000000b
	bne	$3,$2,.L31
	lw	$2,60($16)
	sw	$0,48($16)
	lw	$3,12($2)
	#nop
	mult	$3,$3
	mflo	$8
	#nop
	#nop
	sra	$3,$8,15
	sh	$3,26($16)
	lw	$2,16($2)
	#nop
	sw	$2,52($16)
.L31:
	lw	$4,60($16)
	#nop
	lh	$3,8($4)
	li	$2,16			# 0x00000010
	.set	noreorder
	.set	nomacro
	bnel	$3,$2,.L11
	sw	$23,56($16)
	.set	macro
	.set	reorder

	lw	$2,12($4)
	#nop
	sll	$2,$2,1
	addu	$2,$2,$21
	lhu	$2,0($2)
	move	$3,$4
	sh	$2,32($16)
	lw	$2,12($3)
	li	$8,127			# 0x0000007f
	subu	$2,$8,$2
	sll	$2,$2,1
	addu	$2,$2,$21
	lhu	$2,0($2)
	#nop
	sh	$2,34($16)
	.set	noreorder
	.set	nomacro
	j	.L11
	sw	$23,56($16)
	.set	macro
	.set	reorder

.L35:
	lw	$17,60($16)
	#nop
	lh	$2,10($17)
	#nop
	.set	noreorder
	.set	nomacro
	beq	$2,$0,.L36
	move	$4,$16
	.set	macro
	.set	reorder

	li	$5,8			# 0x00000008
	lw	$2,8($16)
	#nop
	.set	noreorder
	.set	nomacro
	jal	$31,$2
	move	$6,$0
	.set	macro
	.set	reorder

.L36:
	move	$4,$16
	lw	$6,12($17)
	lw	$2,8($16)
	#nop
	.set	noreorder
	.set	nomacro
	jal	$31,$2
	li	$5,5			# 0x00000005
	.set	macro
	.set	reorder

	move	$4,$16
	li	$5,9			# 0x00000009
	lw	$2,8($16)
	.set	noreorder
	.set	nomacro
	j	.L44
	move	$6,$0
	.set	macro
	.set	reorder

.L37:
	move	$4,$16
	addu	$5,$sp,24
	addu	$6,$sp,26
	lw	$8,84($sp)
	move	$7,$18
	sw	$19,20($sp)
	.set	noreorder
	.set	nomacro
	jal	func_802B9D4C
	sw	$8,16($sp)
	.set	macro
	.set	reorder

	move	$19,$2
	move	$4,$16
	li	$5,4			# 0x00000004
	lw	$2,8($16)
	.set	noreorder
	.set	nomacro
	j	.L44
	move	$6,$0
	.set	macro
	.set	reorder

.L38:
	lw	$2,60($16)
	lw	$4,D_800D80A0
	lw	$3,12($2)
	#nop
	sw	$0,216($3)
	lw	$5,12($2)
	.set	noreorder
	.set	nomacro
	jal	func_802B8D0C
	subu	$22,$22,$18
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	j	.L46
	sll	$2,$18,1
	.set	macro
	.set	reorder

.L39:
	move	$4,$16
	addu	$5,$sp,24
	addu	$6,$sp,26
	lw	$8,84($sp)
	move	$7,$18
	sw	$19,20($sp)
	.set	noreorder
	.set	nomacro
	jal	func_802B9D4C
	sw	$8,16($sp)
	.set	macro
	.set	reorder

	move	$19,$2
	lw	$3,60($16)
	lw	$2,8($16)
	lh	$5,8($3)
	lw	$6,12($3)
	move	$4,$16
.L44:
	jal	$31,$2
.L11:
	subu	$22,$22,$18
	sll	$2,$18,1
.L46:
	lw	$5,60($16)
	lhu	$3,26($sp)
	lw	$4,0($5)
	addu	$3,$3,$2
	sh	$3,26($sp)
	.set	noreorder
	.set	nomacro
	bne	$4,$0,.L41
	sw	$4,60($16)
	.set	macro
	.set	reorder

	sw	$0,64($16)
.L41:
	.set	noreorder
	.set	nomacro
	jal	func_802B8CF4
	move	$4,$5
	.set	macro
	.set	reorder

	lw	$2,60($16)
	#nop
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L49
	move	$3,$fp
	.set	macro
	.set	reorder

.L4:
	move	$4,$16
.L47:
	addu	$5,$sp,24
	addu	$6,$sp,26
	lw	$8,84($sp)
	move	$7,$22
	sw	$19,20($sp)
	.set	noreorder
	.set	nomacro
	jal	func_802B9D4C
	sw	$8,16($sp)
	.set	macro
	.set	reorder

	lw	$3,48($16)
	lw	$4,52($16)
	#nop
	slt	$3,$4,$3
	.set	noreorder
	.set	nomacro
	beq	$3,$0,.L43
	move	$19,$2
	.set	macro
	.set	reorder

	sw	$4,48($16)
.L43:
	move	$2,$19
	lw	$31,68($sp)
	lw	$fp,64($sp)
	lw	$23,60($sp)
	lw	$22,56($sp)
	lw	$21,52($sp)
	lw	$20,48($sp)
	lw	$19,44($sp)
	lw	$18,40($sp)
	lw	$17,36($sp)
	lw	$16,32($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,72
	.set	macro
	.set	reorder

	.end	func_802B9800
.Lfe1:
	.size	 func_802B9800,.Lfe1-func_802B9800
