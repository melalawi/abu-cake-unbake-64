      lwl        $at, 0x0($t9)
      lwr        $at, 0x3($t9)
      addiu      $t9, $t9, 0xC
      addiu      $t8, $t8, 0xC
      sw         $at, -0xC($t8)
      lwl        $at, -0x8($t9)
      lwr        $at, -0x5($t9)
      sw         $at, -0x8($t8)
      lwl        $at, -0x4($t9)
      lwr        $at, -0x1($t9)
      bne        $t9, $t1, .L80113F28
       sw        $at, -0x4($t8)
      lwl        $at, 0x0($t9)
      lwr        $at, 0x3($t9)
      sw         $at, 0x0($t8)
      lbu        $t2, 0x2E($sp)
      andi       $t3, $t2, 0xC0
      sra        $t4, $t3, 4
      bnez       $t4, .L80113FC4
