	.file 1 "sample.i"
	.version	"01.01"
gcc2_compiled.:
	.text
	.align	2
	.globl	func_802365FC
	.type	 func_802365FC,@function
	.ent	func_802365FC
func_802365FC:
	.frame	$sp,72,$31		# vars= 0, regs= 2/6, args= 16, extra= 0
	.mask	0x80010000,-52
	.fmask	0x03f00000,-8
	subu	$sp,$sp,72
	s.d	$f21,32($sp)
	mtc1	$5,$f21
	s.d	$f22,40($sp)
	mtc1	$6,$f22
	s.d	$f23,48($sp)
	mtc1	$7,$f23
	sw	$16,16($sp)
	move	$16,$4
	sw	$31,20($sp)
	s.d	$f25,64($sp)
	s.d	$f24,56($sp)
	s.d	$f20,24($sp)
	l.s	$f20,668($16)
	l.s	$f25,88($sp)
	li.s	$f24,1.60000000000000000000e1
	jal	func_80264B8C
	.set	noreorder
	.set	nomacro
	bnel	$2,$0,.L9
	mov.s	$f20,$f21
	.set	macro
	.set	reorder

.L9:
	c.lt.s	$f20,$f21
	#nop
	bc1f	.L10
	li.s	$f0,3.00000000000000000000e1
	add.s	$f20,$f20,$f24
	s.s	$f0,D_80103220+4
	c.lt.s	$f21,$f20
	j	.L37
.L10:
	c.lt.s	$f21,$f20
	#nop
	bc1f	.L12
	li.s	$f0,3.00000000000000000000e1
	sub.s	$f20,$f20,$f24
	s.s	$f0,D_80103220+4
	c.lt.s	$f20,$f21
.L37:
	.set	noreorder
	.set	nomacro
	bc1tl	.L12
	mov.s	$f20,$f21
	.set	macro
	.set	reorder

.L12:
	l.s	$f21,672($16)
	s.s	$f20,668($16)
	li.s	$f20,1.60000000000000000000e1
	jal	func_80264B8C
	.set	noreorder
	.set	nomacro
	bnel	$2,$0,.L16
	mov.s	$f21,$f22
	.set	macro
	.set	reorder

.L16:
	c.lt.s	$f21,$f22
	#nop
	bc1f	.L17
	li.s	$f0,3.00000000000000000000e1
	add.s	$f21,$f21,$f20
	s.s	$f0,D_80103220+4
	c.lt.s	$f22,$f21
	j	.L38
.L17:
	c.lt.s	$f22,$f21
	#nop
	bc1f	.L19
	li.s	$f0,3.00000000000000000000e1
	sub.s	$f21,$f21,$f20
	s.s	$f0,D_80103220+4
	c.lt.s	$f21,$f22
.L38:
	.set	noreorder
	.set	nomacro
	bc1tl	.L19
	mov.s	$f21,$f22
	.set	macro
	.set	reorder

.L19:
	l.s	$f22,676($16)
	li.s	$f20,1.60000000000000000000e1
	.set	noreorder
	.set	nomacro
	jal	func_80264B8C
	s.s	$f21,672($16)
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	bnel	$2,$0,.L23
	mov.s	$f22,$f23
	.set	macro
	.set	reorder

.L23:
	c.lt.s	$f22,$f23
	#nop
	bc1f	.L24
	li.s	$f0,3.00000000000000000000e1
	add.s	$f22,$f22,$f20
	s.s	$f0,D_80103220+4
	c.lt.s	$f23,$f22
	j	.L39
.L24:
	c.lt.s	$f23,$f22
	#nop
	bc1f	.L26
	li.s	$f0,3.00000000000000000000e1
	sub.s	$f22,$f22,$f20
	s.s	$f0,D_80103220+4
	c.lt.s	$f22,$f23
.L39:
	.set	noreorder
	.set	nomacro
	bc1tl	.L26
	mov.s	$f22,$f23
	.set	macro
	.set	reorder

.L26:
	l.s	$f20,680($16)
	li.s	$f21,1.60000000000000000000e1
	.set	noreorder
	.set	nomacro
	jal	func_80264B8C
	s.s	$f22,676($16)
	.set	macro
	.set	reorder

	.set	noreorder
	.set	nomacro
	bnel	$2,$0,.L30
	mov.s	$f20,$f25
	.set	macro
	.set	reorder

.L30:
	c.lt.s	$f20,$f25
	#nop
	bc1f	.L31
	li.s	$f0,3.00000000000000000000e1
	add.s	$f20,$f20,$f21
	s.s	$f0,D_80103220+4
	c.lt.s	$f25,$f20
	j	.L40
.L31:
	c.lt.s	$f25,$f20
	#nop
	.set	noreorder
	.set	nomacro
	bc1fl	.L41
	s.s	$f20,680($16)
	.set	macro
	.set	reorder

	li.s	$f0,3.00000000000000000000e1
	sub.s	$f20,$f20,$f21
	s.s	$f0,D_80103220+4
	c.lt.s	$f20,$f25
.L40:
	.set	noreorder
	.set	nomacro
	bc1tl	.L33
	mov.s	$f20,$f25
	.set	macro
	.set	reorder

.L33:
	s.s	$f20,680($16)
.L41:
	lw	$31,20($sp)
	lw	$16,16($sp)
	l.d	$f25,64($sp)
	l.d	$f24,56($sp)
	l.d	$f23,48($sp)
	l.d	$f22,40($sp)
	l.d	$f21,32($sp)
	l.d	$f20,24($sp)
	#nop
	.set	noreorder
	.set	nomacro
	j	$31
	addu	$sp,$sp,72
	.set	macro
	.set	reorder

	.end	func_802365FC
.Lfe1:
	.size	 func_802365FC,.Lfe1-func_802365FC
