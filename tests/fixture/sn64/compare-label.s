	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_8024D860
	.type	 func_8024D860,@function
	.ent	func_8024D860
func_8024D860:
	.frame	$sp,400,$31		# vars= 320, regs= 5/4, args= 24, extra= 0
	.mask	0x800f0000,-40
	.fmask	0x00f00000,-8
	subu	$sp,$sp,400
	sw	$17,348($sp)
	move	$17,$5
	sw	$31,360($sp)
	sw	$19,356($sp)
	sw	$18,352($sp)
	sw	$16,344($sp)
	s.d	$f23,392($sp)
	s.d	$f22,384($sp)
	s.d	$f21,376($sp)
	s.d	$f20,368($sp)
	lw	$2,20($17)
	#nop
	.set	noreorder
	.set	nomacro
	beq	$2,$0,.L11
	move	$19,$4
	.set	macro
	.set	reorder

	lbu	$3,0($17)
	li	$2,1			# 0x00000001
	.set	noreorder
	.set	nomacro
	bnel	$3,$2,.L6
	move	$2,$0
	.set	macro
	.set	reorder

	lw	$2,256($17)
	#nop
	andi	$2,$2,0x0001
.L6:
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L11
	li	$2,1			# 0x00000001
	.set	macro
	.set	reorder

	lbu	$3,0($17)
	#nop
	.set	noreorder
	.set	nomacro
	bne	$3,$2,.L21
	move	$4,$19
	.set	macro
	.set	reorder

	lw	$2,56($17)
	#nop
	andi	$2,$2,0x0003
	.set	noreorder
	.set	nomacro
	bne	$2,$0,.L11
	move	$18,$17
	.set	macro
	.set	reorder

	lw	$2,256($17)
	li	$3,3145728			# 0x00300000
	and	$2,$2,$3
	beq	$2,$0,.L21
	lw	$4,20($17)
	jal	func_80275854
	bne	$2,$0,.L11
	lw	$5,20($17)
	.set	noreorder
	.set	nomacro
	jal	func_80275D04
	addu	$4,$sp,24
	.set	macro
	.set	reorder

	addu	$16,$sp,56
	move	$4,$16
	addu	$5,$sp,40
	li.s	$f0,1.00000000000000000000e0
	addu	$6,$sp,24
	sw	$0,40($sp)
	sw	$0,48($sp)
	.set	noreorder
	.set	nomacro
	jal	func_80272088
	s.s	$f0,44($sp)
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	jal	func_802720EC
	move	$4,$16
	.set	macro
	.set	reorder

	l.s	$f2,40($sp)
	l.s	$f0,24($sp)
	#nop
	mul.s	$f2,$f2,$f0
	l.s	$f1,44($sp)
	l.s	$f0,28($sp)
	#nop
	mul.s	$f1,$f1,$f0
	l.s	$f12,48($sp)
	l.s	$f0,32($sp)
	#nop
	mul.s	$f12,$f12,$f0
	add.s	$f2,$f2,$f1
	.set	noreorder
	.set	nomacro
	jal	func_80274640
	add.s	$f12,$f2,$f12
	.set	macro
	.set	reorder

	li.s	$f1,4.36332345008850097656e-1
	mov.s	$f20,$f0
	c.lt.s	$f1,$f20
	#nop
	bc1f	.L14
	li.s	$f12,1.63624629378318786621e-1
	j	.L15
.L14:
	li.s	$f0,3.75000000000000000000e-1
	#nop
	mul.s	$f12,$f20,$f0
.L15:
	jal	func_802BC200
	l.s	$f4,56($sp)
	#nop
	mul.s	$f4,$f4,$f0
	l.s	$f2,60($sp)
	#nop
	mul.s	$f2,$f2,$f0
	l.s	$f1,64($sp)
	#nop
	mul.s	$f1,$f1,$f0
	li.s	$f3,4.36332345008850097656e-1
	s.s	$f0,D_80115DEC
	c.lt.s	$f3,$f20
	s.s	$f4,72($sp)
	s.s	$f2,76($sp)
	.set	noreorder
	.set	nomacro
	bc1f	.L16
	s.s	$f1,80($sp)
	.set	macro
	.set	reorder

	li.s	$f12,1.63624629378318786621e-1
	j	.L17
.L16:
	li.s	$f0,3.75000000000000000000e-1
	#nop
	mul.s	$f12,$f20,$f0
.L17:
	jal	func_802BB630
	addu	$4,$sp,72
	move	$5,$17
	.set	noreorder
	.set	nomacro
	jal	func_8024D718
	s.s	$f0,84($sp)
	.set	macro
	.set	reorder

	l.s	$f12,108($18)
	jal	func_802BC200
	l.s	$f12,108($18)
	.set	noreorder
	.set	nomacro
	jal	func_802BB630
	mov.s	$f23,$f0
	.set	macro
	.set	reorder

	lw	$8,68($18)
	lw	$9,72($18)
	lw	$10,76($18)
	sw	$8,192($sp)
	sw	$9,196($sp)
	sw	$10,200($sp)
	l.s	$f1,8($18)
	#nop
	s.s	$f1,144($sp)
	l.s	$f1,64($18)
	#nop
	s.s	$f1,148($sp)
	l.s	$f1,16($18)
	mov.s	$f22,$f0
	s.s	$f1,152($sp)
	l.s	$f0,8($17)
	#nop
	sub.s	$f0,$f0,$f23
	l.s	$f1,16($17)
	#nop
	sub.s	$f1,$f1,$f22
	mfc1	$7,$f0
	s.s	$f1,16($sp)
	lw	$5,8($17)
	lw	$6,16($17)
	.set	noreorder
	.set	nomacro
	jal	func_80240A88
	addu	$4,$sp,120
	.set	macro
	.set	reorder

	li.s	$f1,2.50000000000000000000e1
	mov.s	$f20,$f0
	c.lt.s	$f1,$f20
	li.s	$f0,1.87500000000000000000e1
	bc1t	.L19
	li.s	$f0,7.50000000000000000000e-1
	#nop
	mul.s	$f0,$f20,$f0
.L19:
	li.s	$f21,5.00000000000000000000e-1
	#nop
	mul.s	$f20,$f0,$f21
	.set	noreorder
	.set	nomacro
	jal	func_802BC200
	mov.s	$f12,$f20
	.set	macro
	.set	reorder

	mul.s	$f2,$f22,$f0
	neg.s	$f1,$f23
	mul.s	$f1,$f1,$f0
	mov.s	$f12,$f20
	s.s	$f0,D_80115DEC
	sw	$0,92($sp)
	s.s	$f2,88($sp)
	.set	noreorder
	.set	nomacro
	jal	func_802BB630
	s.s	$f1,96($sp)
	.set	macro
	.set	reorder

	addu	$4,$sp,104
	addu	$6,$sp,88
	mfc1	$5,$f21
	addu	$7,$sp,72
	.set	noreorder
	.set	nomacro
	jal	func_80270D40
	s.s	$f0,100($sp)
	.set	macro
	.set	reorder

	lw	$8,104($sp)
	lw	$9,108($sp)
	lw	$10,112($sp)
	lw	$11,116($sp)
	sw	$8,0($19)
	sw	$9,4($19)
	sw	$10,8($19)
	sw	$11,12($19)
	.set	noreorder
	.set	nomacro
	j	.L20
	move	$2,$19
	.set	macro
	.set	reorder

.L21:
	.set	noreorder
	.set	nomacro
	jal	func_8024D718
	move	$5,$17
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	j	.L20
	move	$2,$19
	.set	macro
	.set	reorder

.L11:
	mtc1	$0,$f0
	li.s	$f1,1.00000000000000000000e0
	s.s	$f0,80($sp)
	s.s	$f0,76($sp)
	s.s	$f0,72($sp)
	s.s	$f1,84($sp)
	lw	$8,72($sp)
	lw	$9,76($sp)
	lw	$10,80($sp)
	lw	$11,84($sp)
	sw	$8,0($19)
	sw	$9,4($19)
	sw	$10,8($19)
	sw	$11,12($19)
	move	$2,$19
.L20:
	lw	$31,360($sp)
	lw	$19,356($sp)
	lw	$18,352($sp)
	lw	$17,348($sp)
	lw	$16,344($sp)
	l.d	$f23,392($sp)
	l.d	$f22,384($sp)
	l.d	$f21,376($sp)
	l.d	$f20,368($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,400
	.set	macro
	.set	reorder

	.end	func_8024D860
.Lfe1:
	.size	 func_8024D860,.Lfe1-func_8024D860
