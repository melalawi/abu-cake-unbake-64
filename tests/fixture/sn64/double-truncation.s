	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_8029CDEC
	.type	 func_8029CDEC,@function
	.ent	func_8029CDEC
func_8029CDEC:
	.frame	$sp,32,$31		# vars= 0, regs= 1/1, args= 16, extra= 0
	.mask	0x80000000,-16
	.fmask	0x00100000,-8
	subu	$sp,$sp,32
	s.d	$f20,24($sp)
	mov.d	$f20,$f12
	li.d	$f0,0.00000000000000000000e0
	mov.d	$f4,$f20
	c.lt.d	$f20,$f0
	#nop
	.set	noreorder
	.set	nomacro
	bc1f	.L6
	sw	$31,16($sp)
	.set	macro
	.set	reorder

	neg.d	$f4,$f20
.L6:
	li.d	$f0,2.52999992370605468750e1
	#nop
	c.lt.d	$f0,$f4
	#nop
	bc1f	.L7
	li.d	$f1,1.00000000000000000000e0
	j	.L8
.L7:
	li.d	$f0,5.49306154251098632812e-1
	#nop
	c.lt.d	$f0,$f4
	#nop
	bc1f	.L9
	add.d	$f3,$f4,$f4
	li.d	$f0,-2.71050494653762091723e-20
	#nop
	c.lt.d	$f0,$f3
	#nop
	bc1f	.L10
	li.d	$f0,2.71050494653762091723e-20
	#nop
	c.lt.d	$f3,$f0
	#nop
	bc1f	.L10
	li.d	$f1,1.00000000000000000000e0
	j	.L11
.L10:
	li.d	$f0,1.44269502162933349609e0
	#nop
	mul.d	$f4,$f3,$f0
	trunc.w.d $f6,$f4,$2
	mfc1	$6,$f6
	#nop
	.set	noreorder
	.set	nomacro
	bltzl	$6,.L12
	addu	$6,$6,-1
	.set	macro
	.set	reorder

.L12:
	mtc1	$6,$f0
	cvt.d.w	$f0,$f0
	sub.d	$f0,$f4,$f0
	li.d	$f5,5.00000000000000000000e-1
	#nop
	c.le.d	$f5,$f0
	#nop
	.set	noreorder
	.set	nomacro
	bc1tl	.L13
	addu	$6,$6,1
	.set	macro
	.set	reorder

.L13:
	li.d	$f2,6.93359375000000000000e-1
	mtc1	$6,$f0
	cvt.d.w	$f0,$f0
	mul.d	$f2,$f0,$f2
	li.d	$f1,2.12194441701285568163e-4
	#nop
	mul.d	$f0,$f0,$f1
	sub.d	$f2,$f3,$f2
	add.d	$f2,$f2,$f0
	mul.d	$f4,$f2,$f2
	li.d	$f12,1.65203291544457897544e-5
	#nop
	mul.d	$f12,$f4,$f12
	li.d	$f0,6.94359978660941123962e-3
	#nop
	add.d	$f12,$f12,$f0
	li.d	$f0,4.95862856041640043259e-4
	#nop
	mul.d	$f0,$f4,$f0
	mul.d	$f12,$f12,$f4
	li.d	$f1,5.55538684129714965820e-2
	#nop
	add.d	$f0,$f0,$f1
	mul.d	$f0,$f0,$f4
	li.d	$f1,2.50000000000000000000e-1
	#nop
	add.d	$f12,$f12,$f1
	mul.d	$f12,$f12,$f2
	add.d	$f0,$f0,$f5
	sub.d	$f0,$f0,$f12
	div.d	$f12,$f12,$f0
	add.d	$f12,$f12,$f5
	.set	noreorder
	.set	nomacro
	jal	func_8029BBA0
	addu	$6,$6,1
	.set	macro
	.set	reorder

	mov.d	$f1,$f0
.L11:
	li.d	$f0,1.00000000000000000000e0
	#nop
	add.d	$f1,$f1,$f0
	li.d	$f2,5.00000000000000000000e-1
	div.d	$f0,$f0,$f1
	sub.d	$f1,$f2,$f0
	.set	noreorder
	.set	nomacro
	j	.L8
	add.d	$f1,$f1,$f1
	.set	macro
	.set	reorder

.L9:
	li.d	$f0,2.30000005152497521976e-10
	#nop
	c.lt.d	$f4,$f0
	#nop
	.set	noreorder
	.set	nomacro
	bc1t	.L8
	mov.d	$f1,$f4
	.set	macro
	.set	reorder

	mul.d	$f3,$f4,$f4
	li.d	$f1,-9.64374899864196777344e-1
	#nop
	mul.d	$f1,$f3,$f1
	li.d	$f0,9.92259292602539062500e1
	#nop
	sub.d	$f1,$f1,$f0
	mul.d	$f1,$f1,$f3
	li.d	$f0,1.61341186523437500000e3
	#nop
	sub.d	$f1,$f1,$f0
	mul.d	$f1,$f1,$f3
	li.d	$f0,1.12744743347167954539e2
	#nop
	add.d	$f0,$f3,$f0
	mul.d	$f0,$f0,$f3
	mul.d	$f1,$f4,$f1
	li.d	$f2,2.23377197265625000000e3
	#nop
	add.d	$f0,$f0,$f2
	mul.d	$f0,$f0,$f3
	li.d	$f2,4.84023583984375000000e3
	#nop
	add.d	$f0,$f0,$f2
	div.d	$f1,$f1,$f0
	add.d	$f1,$f4,$f1
.L8:
	li.d	$f0,0.00000000000000000000e0
	#nop
	c.lt.d	$f20,$f0
	#nop
	.set	noreorder
	.set	nomacro
	bc1tl	.L17
	neg.d	$f1,$f1
	.set	macro
	.set	reorder

.L17:
	lw	$31,16($sp)
	l.d	$f20,24($sp)
	mov.d	$f0,$f1
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,32
	.set	macro
	.set	reorder

	.end	func_8029CDEC
.Lfe1:
	.size	 func_8029CDEC,.Lfe1-func_8029CDEC
