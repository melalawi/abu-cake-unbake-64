func_8010AA6C_us:
	.option	O2
	subu	$sp, 56
	s.d	$f28, 40($sp)
	s.d	$f26, 32($sp)
	s.d	$f24, 24($sp)
	s.d	$f22, 16($sp)
	s.d	$f20, 8($sp)
	.fmask	0x3FF00000, -16
	.frame	$sp, 56, $31
	sw	$4, 56($sp)
	sw	$5, 60($sp)
	sw	$6, 64($sp)
	sw	$7, 68($sp)
	l.s	$f14, 56($sp)
	l.s	$f22, 60($sp)
	l.s	$f24, 76($sp)
	.loc	2 13
	.loc	2 15
 #  14	    struct QueryPointPair hit;
 #  15	    f64 ay = (f64)(b.y - a.y);
	.loc	2 16
 #  16	    f64 ax = (f64)(b.x - a.x);
	.loc	2 17
 #  17	    f64 bx = (f64)(d.x - c.x);
	.loc	2 18
 #  18	    f64 by = (f64)(d.y - c.y);
	.loc	2 19
 #  19	    f64 rx = (f64)(a.x - c.x);
	.loc	2 20
 #  20	    f64 ry = (f64)(a.y - c.y);
	.loc	2 21
 #  21	    f64 denominator = bx * ay - by * ax;
	.loc	2 26
 #  22	    f64 t;
 #  23	    f64 u;
 #  24	    s32 state;
 #  25	
 #  26	    if (denominator == 0.0) {
	l.s	$f4, 80($sp)
	l.s	$f6, 72($sp)
	sub.s	$f8, $f4, $f6
	cvt.d.s	$f28, $f8
	l.s	$f10, 68($sp)
	sub.s	$f4, $f10, $f22
	cvt.d.s	$f12, $f4
	mul.d	$f20, $f28, $f12
	l.s	$f8, 84($sp)
	sub.s	$f10, $f8, $f24
	cvt.d.s	$f26, $f10
	l.s	$f4, 64($sp)
	sub.s	$f8, $f4, $f14
	cvt.d.s	$f2, $f8
	mul.d	$f18, $f26, $f2
	c.eq.d	$f20, $f18
