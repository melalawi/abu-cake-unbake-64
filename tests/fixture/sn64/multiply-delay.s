	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_802307EC
	.type	 func_802307EC,@function
	.ent	func_802307EC
func_802307EC:
	.frame	$sp,56,$31		# vars= 0, regs= 7/0, args= 24, extra= 0
	.mask	0x803f0000,-8
	.fmask	0x00000000,0
	subu	$sp,$sp,56
	sw	$20,40($sp)
	move	$20,$4
	sw	$31,48($sp)
	sw	$21,44($sp)
	sw	$19,36($sp)
	sw	$18,32($sp)
	sw	$17,28($sp)
	sw	$16,24($sp)
	lw	$17,472($20)
	move	$18,$5
	lh	$3,1616($17)
	lh	$5,1582($17)
	sll	$2,$3,1
	addu	$2,$2,$3
	sll	$2,$2,3
	lh	$21,D_800CE8DC($2)
	.set	noreorder
	.set	nomacro
	jal	func_80222A80
	move	$4,$17
	.set	macro
	.set	reorder

	bne	$2,$0,.L8
	.set	noreorder
	.set	nomacro
	jal	func_8022F95C
	move	$4,$17
	.set	macro
	.set	reorder

	sh	$2,1904($17)
.L8:
	lw	$2,1712($17)
	#nop
	andi	$2,$2,0x4000
	.set	noreorder
	.set	nomacro
	beql	$2,$0,.L31
	move	$4,$20
	.set	macro
	.set	reorder

	l.s	$f1,4568($17)
	mtc1	$0,$f0
	#nop
	c.lt.s	$f0,$f1
	#nop
	.set	noreorder
	.set	nomacro
	bc1t	.L11
	move	$2,$0
	.set	macro
	.set	reorder

	lw	$2,5200($17)
	#nop
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L11
	li	$2,1			# 0x00000001
	.set	macro
	.set	reorder

	lbu	$3,D_801462D5
	#nop
	bne	$3,$2,.L11
	lw	$2,1492($17)
	lh	$5,1582($17)
	sll	$4,$2,1
	addu	$4,$4,$2
	sll	$4,$4,3
	addu	$4,$4,$2
	sll	$4,$4,4
	la	$2,D_80102B00
	.set	noreorder
	.set	nomacro
	jal	func_8022F54C
	addu	$4,$4,$2
	.set	macro
	.set	reorder

	move	$16,$2
	.set	noreorder
	.set	nomacro
	bne	$16,$0,.L11
	move	$2,$16
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	jal	func_8025DF54
	li	$4,3405			# 0x00000d4d
	.set	macro
	.set	reorder

	lw	$2,1500($17)
	#nop
	.set	noreorder
	.set	nomacro
	beq	$2,$0,.L11
	move	$2,$16
	.set	macro
	.set	reorder

	la	$4,D_80145040
	.set	noreorder
	.set	nomacro
	jal	func_8022A590
	move	$5,$17
	.set	macro
	.set	reorder

	lw	$6,D_800D70E8
	li.s	$f0,1.00000000000000000000e0
	la	$4,D_80145088
	s.s	$f0,16($sp)
	lw	$5,1500($17)
	.set	noreorder
	.set	nomacro
	jal	func_802398F8
	move	$7,$2
	.set	macro
	.set	reorder

	move	$2,$16
.L11:
	.set	noreorder
	.set	nomacro
	beq	$2,$0,.L9
	li	$2,1			# 0x00000001
	.set	macro
	.set	reorder

	lw	$19,316($18)
	#nop
	.set	noreorder
	.set	nomacro
	bne	$19,$2,.L31
	move	$4,$20
	.set	macro
	.set	reorder

	lh	$2,1526($17)
	#nop
	slt	$2,$2,50
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L16
	move	$5,$18
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	jal	func_80214178
	li	$6,5			# 0x00000005
	.set	macro
	.set	reorder

	li	$2,2			# 0x00000002
	.set	noreorder
	.set	nomacro
	j	.L7
	sw	$2,316($18)
	.set	macro
	.set	reorder

.L16:
	.set	noreorder
	.set	nomacro
	jal	func_8025DF54
	li	$4,3405			# 0x00000d4d
	.set	macro
	.set	reorder

	lw	$2,1500($17)
	#nop
	.set	noreorder
	.set	nomacro
	beql	$2,$0,.L7
	sw	$19,316($18)
	.set	macro
	.set	reorder

	la	$16,D_80145040
	move	$4,$16
	.set	noreorder
	.set	nomacro
	jal	func_8022A590
	move	$5,$17
	.set	macro
	.set	reorder

	lw	$6,D_800D70E8
	li.s	$f0,1.00000000000000000000e0
	addu	$4,$16,72
	s.s	$f0,16($sp)
	lw	$5,1500($17)
	.set	noreorder
	.set	nomacro
	jal	func_802398F8
	move	$7,$2
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	j	.L7
	sw	$19,316($18)
	.set	macro
	.set	reorder

.L9:
	move	$4,$20
.L31:
	.set	noreorder
	.set	nomacro
	jal	func_802301E4
	move	$5,$18
	.set	macro
	.set	reorder

	bne	$2,$0,.L19
	lw	$2,256($20)
	#nop
	andi	$2,$2,0x0400
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L7
	move	$4,$20
	.set	macro
	.set	reorder

	move	$5,$18
	.set	noreorder
	.set	nomacro
	jal	func_80214178
	move	$6,$21
	.set	macro
	.set	reorder

	j	.L7
.L19:
	l.s	$f0,296($18)
	l.s	$f1,D_800D2988
	#nop
	mul.s	$f1,$f0,$f1
	add.s	$f1,$f1,$f1
	add.s	$f1,$f0,$f1
	mtc1	$0,$f0
	s.s	$f1,296($18)
	lw	$4,1688($17)
	c.lt.s	$f1,$f0
	#nop
	bc1f	.L23
	li.s	$f0,1.79049289226531982422e0
	neg.s	$f1,$f1
	mul.s	$f1,$f1,$f0
	li.s	$f0,1.00000000000000000000e0
	#nop
	c.lt.s	$f0,$f1
	j	.L28
.L23:
	li.s	$f0,1.79049289226531982422e0
	#nop
	mul.s	$f0,$f1,$f0
	li.s	$f1,1.00000000000000000000e0
	#nop
	c.lt.s	$f1,$f0
.L28:
	bc1t	.L22
	l.s	$f2,296($18)
	mtc1	$0,$f0
	#nop
	c.lt.s	$f2,$f0
	#nop
	bc1f	.L25
	li.s	$f1,1.79049289226531982422e0
	neg.s	$f0,$f2
	mul.s	$f0,$f0,$f1
	.set	noreorder
	.set	nomacro
	j	.L30
	s.s	$f0,360($4)
	.set	macro
	.set	reorder

.L25:
	li.s	$f0,1.79049289226531982422e0
	#nop
	mul.s	$f0,$f2,$f0
	.set	noreorder
	.set	nomacro
	j	.L30
	s.s	$f0,360($4)
	.set	macro
	.set	reorder

.L22:
	li.s	$f0,1.00000000000000000000e0
	#nop
	s.s	$f0,360($4)
.L30:
	l.s	$f0,296($18)
	l.s	$f1,D_800D2988
	#nop
	mul.s	$f0,$f0,$f1
	add.s	$f0,$f0,$f0
	li	$2,1			# 0x00000001
	sw	$2,316($18)
	s.s	$f0,292($18)
.L7:
	lw	$31,48($sp)
	lw	$21,44($sp)
	lw	$20,40($sp)
	lw	$19,36($sp)
	lw	$18,32($sp)
	lw	$17,28($sp)
	lw	$16,24($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,56
	.set	macro
	.set	reorder

	.end	func_802307EC
.Lfe1:
	.size	 func_802307EC,.Lfe1-func_802307EC
