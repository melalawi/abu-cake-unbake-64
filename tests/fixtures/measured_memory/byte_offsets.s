      sw         $5, 0x14($2)
      lui        $5, %hi(D_8037ADCC)
      addiu      $5, $5, %lo(D_8037ADCC)
      sw         $8, 0x0($2)
      sw         $3, %lo(D_8037A174)($1)
      addiu      $3, $5, -0x34
      addu       $4, $4, $3
      lui        $3, (0xF2000000 >> 16)
      addiu      $3, $2, 0x88
      addiu      $5, $5, -0x54
      sw         $0, 0x64($2)
      sw         $13, 0x68($2)

