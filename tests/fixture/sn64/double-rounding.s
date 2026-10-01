	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
.section	.rodata
	.align	3
.LC0:
	.word	0x7ff00000		# inf
	.word	0x00000000
	.align	3
.LC1:
	.word	0xfff00000		# -inf
	.word	0x00000000
	.text
	.align	2
	.globl	func_8029BEA4
	.type	 func_8029BEA4,@function
	.ent	func_8029BEA4
func_8029BEA4:
	.frame	$sp,56,$31		# vars= 0, regs= 2/4, args= 16, extra= 0
	.mask	0x80010000,-36
	.fmask	0x00f00000,-8
	li.d	$f0,0.00000000000000000000e0
	subu	$sp,$sp,56
	s.d	$f20,24($sp)
	mov.d	$f20,$f14
	sw	$31,20($sp)
	sw	$16,16($sp)
	s.d	$f23,48($sp)
	s.d	$f22,40($sp)
	s.d	$f21,32($sp)
	c.eq.d	$f20,$f0
	#nop
	bc1t	.L31
	c.eq.d	$f12,$f0
	#nop
	bc1f	.L7
	c.lt.d	$f20,$f0
	#nop
	bc1f	.L30
	l.d	$f0,.LC0
	j	.L30
.L7:
	c.lt.d	$f12,$f0
	#nop
	bc1f	.L9
	trunc.w.d $f5,$f20,$3
	mfc1	$16,$f5
	#nop
	mtc1	$16,$f0
	cvt.d.w	$f0,$f0
	c.eq.d	$f0,$f20
	#nop
	bc1f	.L10
	.set	noreorder
	.set	nomacro
	jal	func_8029BD38
	neg.d	$f12,$f12
	.set	macro
	.set	reorder

	andi	$2,$16,0x0001
	mul.d	$f14,$f0,$f20
	beq	$2,$0,.L11
	li.d	$f0,-2.71050494653762091723e-20
	#nop
	c.lt.d	$f0,$f14
	#nop
	bc1f	.L22
	li.d	$f0,2.71050494653762091723e-20
	#nop
	c.lt.d	$f14,$f0
	li.d	$f0,1.00000000000000000000e0
	.set	noreorder
	.set	nomacro
	bc1tl	.L30
	neg.d	$f0,$f0
	.set	macro
	.set	reorder

	j	.L22
.L11:
	li.d	$f0,-2.71050494653762091723e-20
	#nop
	c.lt.d	$f0,$f14
	#nop
	bc1f	.L26
	li.d	$f0,2.71050494653762091723e-20
	#nop
	c.lt.d	$f14,$f0
	li.d	$f0,1.00000000000000000000e0
	bc1f	.L26
	j	.L30
.L10:
	li.d	$f23,1.00000000000000000000e0
	#nop
	div.d	$f0,$f23,$f20
	trunc.w.d $f5,$f0,$3
	mfc1	$16,$f5
	#nop
	mtc1	$16,$f0
	cvt.d.w	$f0,$f0
	mul.d	$f0,$f0,$f20
	sub.d	$f0,$f20,$f0
	li.d	$f22,-2.71050494653762091723e-20
	#nop
	c.lt.d	$f22,$f0
	#nop
	bc1f	.L21
	li.d	$f21,2.71050494653762091723e-20
	#nop
	c.lt.d	$f0,$f21
	#nop
	.set	noreorder
	.set	nomacro
	bc1f	.L21
	andi	$2,$16,0x0001
	.set	macro
	.set	reorder

	bne	$2,$0,.L20
.L21:
	l.d	$f0,.LC1
	j	.L30
.L20:
	.set	noreorder
	.set	nomacro
	jal	func_8029BD38
	neg.d	$f12,$f12
	.set	macro
	.set	reorder

	mul.d	$f14,$f0,$f20
	c.lt.d	$f22,$f14
	#nop
	bc1f	.L22
	c.lt.d	$f14,$f21
	#nop
	.set	noreorder
	.set	nomacro
	bc1t	.L23
	mov.d	$f0,$f23
	.set	macro
	.set	reorder

.L22:
	li.d	$f0,1.44269502162933349609e0
	#nop
	mul.d	$f3,$f14,$f0
	trunc.w.d $f5,$f3,$3
	mfc1	$6,$f5
	#nop
	.set	noreorder
	.set	nomacro
	bltzl	$6,.L24
	addu	$6,$6,-1
	.set	macro
	.set	reorder

.L24:
	mtc1	$6,$f0
	cvt.d.w	$f0,$f0
	sub.d	$f0,$f3,$f0
	li.d	$f4,5.00000000000000000000e-1
	#nop
	c.le.d	$f4,$f0
	#nop
	.set	noreorder
	.set	nomacro
	bc1tl	.L25
	addu	$6,$6,1
	.set	macro
	.set	reorder

.L25:
	li.d	$f2,6.93359375000000000000e-1
	mtc1	$6,$f0
	cvt.d.w	$f0,$f0
	mul.d	$f2,$f0,$f2
	li.d	$f1,2.12194441701285568163e-4
	#nop
	mul.d	$f0,$f0,$f1
	sub.d	$f2,$f14,$f2
	add.d	$f2,$f2,$f0
	mul.d	$f3,$f2,$f2
	li.d	$f12,1.65203291544457897544e-5
	#nop
	mul.d	$f12,$f3,$f12
	li.d	$f0,6.94359978660941123962e-3
	#nop
	add.d	$f12,$f12,$f0
	li.d	$f0,4.95862856041640043259e-4
	#nop
	mul.d	$f0,$f3,$f0
	mul.d	$f12,$f12,$f3
	li.d	$f1,5.55538684129714965820e-2
	#nop
	add.d	$f0,$f0,$f1
	mul.d	$f0,$f0,$f3
	li.d	$f1,2.50000000000000000000e-1
	#nop
	add.d	$f12,$f12,$f1
	mul.d	$f12,$f12,$f2
	add.d	$f0,$f0,$f4
	sub.d	$f0,$f0,$f12
	div.d	$f12,$f12,$f0
	add.d	$f12,$f12,$f4
	.set	noreorder
	.set	nomacro
	jal	func_8029BBA0
	addu	$6,$6,1
	.set	macro
	.set	reorder

.L23:
	.set	noreorder
	.set	nomacro
	j	.L30
	neg.d	$f0,$f0
	.set	macro
	.set	reorder

.L9:
	jal	func_8029BD38
	mul.d	$f14,$f0,$f20
	li.d	$f0,-2.71050494653762091723e-20
	#nop
	c.lt.d	$f0,$f14
	#nop
	bc1f	.L26
	li.d	$f0,2.71050494653762091723e-20
	#nop
	c.lt.d	$f14,$f0
	#nop
	bc1f	.L26
.L31:
	li.d	$f0,1.00000000000000000000e0
	j	.L30
.L26:
	li.d	$f0,1.44269502162933349609e0
	#nop
	mul.d	$f3,$f14,$f0
	trunc.w.d $f5,$f3,$3
	mfc1	$6,$f5
	#nop
	.set	noreorder
	.set	nomacro
	bltzl	$6,.L28
	addu	$6,$6,-1
	.set	macro
	.set	reorder

.L28:
	mtc1	$6,$f0
	cvt.d.w	$f0,$f0
	sub.d	$f0,$f3,$f0
	li.d	$f4,5.00000000000000000000e-1
	#nop
	c.le.d	$f4,$f0
	#nop
	.set	noreorder
	.set	nomacro
	bc1tl	.L29
	addu	$6,$6,1
	.set	macro
	.set	reorder

.L29:
	li.d	$f2,6.93359375000000000000e-1
	mtc1	$6,$f0
	cvt.d.w	$f0,$f0
	mul.d	$f2,$f0,$f2
	li.d	$f1,2.12194441701285568163e-4
	#nop
	mul.d	$f0,$f0,$f1
	sub.d	$f2,$f14,$f2
	add.d	$f2,$f2,$f0
	mul.d	$f3,$f2,$f2
	li.d	$f12,1.65203291544457897544e-5
	#nop
	mul.d	$f12,$f3,$f12
	li.d	$f0,6.94359978660941123962e-3
	#nop
	add.d	$f12,$f12,$f0
	li.d	$f0,4.95862856041640043259e-4
	#nop
	mul.d	$f0,$f3,$f0
	mul.d	$f12,$f12,$f3
	li.d	$f1,5.55538684129714965820e-2
	#nop
	add.d	$f0,$f0,$f1
	mul.d	$f0,$f0,$f3
	li.d	$f1,2.50000000000000000000e-1
	#nop
	add.d	$f12,$f12,$f1
	mul.d	$f12,$f12,$f2
	add.d	$f0,$f0,$f4
	sub.d	$f0,$f0,$f12
	div.d	$f12,$f12,$f0
	add.d	$f12,$f12,$f4
	.set	noreorder
	.set	nomacro
	jal	func_8029BBA0
	addu	$6,$6,1
	.set	macro
	.set	reorder

.L30:
	lw	$31,20($sp)
	lw	$16,16($sp)
	l.d	$f23,48($sp)
	l.d	$f22,40($sp)
	l.d	$f21,32($sp)
	l.d	$f20,24($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,56
	.set	macro
	.set	reorder

	.end	func_8029BEA4
.Lfe1:
	.size	 func_8029BEA4,.Lfe1-func_8029BEA4
