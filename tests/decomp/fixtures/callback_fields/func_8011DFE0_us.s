      lw         $t9, 0x10($a1)
      or         $s6, $a0, $zero
      or         $fp, $a1, $zero
      addiu      $t0, $zero, 0x1C
      sw         $t9, 0x24($a0)
      sw         $t0, 0x10($sp)
      lw         $a1, 0x24($s6)
      or         $a0, $s3, $zero
      jal        func_8011E564
       or        $a2, $s7, $zero
      or         $a0, $s3, $zero
