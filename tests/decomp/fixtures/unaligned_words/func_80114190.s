      lwl        $at, 0x0($t7)
      lwr        $at, 0x3($t7)
      addiu      $t7, $t7, 0xC
      addiu      $t6, $t6, 0xC
      sw         $at, -0xC($t6)
      lwl        $at, -0x8($t7)
      lwr        $at, -0x5($t7)
      sw         $at, -0x8($t6)
      lwl        $at, -0x4($t7)
      lwr        $at, -0x1($t7)
      bne        $t7, $t9, .L80114274
       sw        $at, -0x4($t6)
      lwl        $at, 0x0($t7)
      lwr        $at, 0x3($t7)
      sw         $at, 0x0($t6)
      lbu        $t0, 0x2E($sp)
      andi       $t1, $t0, 0xC0
      sra        $t2, $t1, 4
      bnez       $t2, .L8011435C
