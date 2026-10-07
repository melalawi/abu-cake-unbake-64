.include "macro.inc"

.set noat
.set noreorder
.set gp=64

.section .text, "ax"

.globl func_800F4DFC
.ent func_800F4DFC
func_800F4DFC:
      addiu      $29, $29, -0x18
      sw         $20, 0x10($29)
      sw         $19, 0xC($29)
      sw         $18, 0x8($29)
      sw         $17, 0x4($29)
      sw         $16, 0x0($29)
      lw         $9, 0x0($4)
      lui        $16, (0xFCFFFFFF >> 16)
      ori        $16, $16, (0xFCFFFFFF & 0xFFFF)
      lui        $15, (0xFFFDF6FB >> 16)
      ori        $15, $15, (0xFFFDF6FB & 0xFFFF)
      lui        $14, (0xB900031D >> 16)
      ori        $14, $14, (0xB900031D & 0xFFFF)
      lui        $13, (0x504240 >> 16)
      ori        $13, $13, (0x504240 & 0xFFFF)
      lui        $12, (0xFA000101 >> 16)
      ori        $12, $12, (0xFA000101 & 0xFFFF)
      lui        $20, (0x4000400 >> 16)
      ori        $20, $20, (0x4000400 & 0xFFFF)
      lui        $2, (0xE7000000 >> 16)
      addu       $7, $5, $7
      sll        $7, $7, 2
      andi       $7, $7, 0xFFF
      sll        $7, $7, 12
      sll        $5, $5, 2
      andi       $5, $5, 0xFFF
      sll        $5, $5, 12
      addu       $3, $9, $0
      addiu      $9, $9, 0x8
      addu       $8, $9, $0
      addiu      $9, $9, 0x8
      addu       $10, $9, $0
      addiu      $9, $9, 0x8
      addu       $11, $9, $0
      addiu      $9, $9, 0x8
      addu       $17, $9, $0
      addiu      $9, $9, 0x8
      addu       $18, $9, $0
      addiu      $9, $9, 0x8
      addu       $19, $9, $0
      sw         $2, 0x0($3)
      sw         $0, 0x4($3)
      sw         $16, 0x0($8)
      sw         $15, 0x4($8)
      sw         $14, 0x0($10)
      sw         $13, 0x4($10)
      sw         $12, 0x0($11)
      lw         $2, 0x2C($29)
      lw         $3, 0x30($29)
      lw         $8, 0x34($29)
      lw         $10, 0x38($29)
      sll        $2, $2, 24
      andi       $3, $3, 0xFF
      sll        $3, $3, 16
      or         $2, $2, $3
      andi       $8, $8, 0xFF
      sll        $8, $8, 8
      or         $2, $2, $8
      andi       $10, $10, 0xFF
      or         $2, $2, $10
      sw         $2, 0x4($11)
      lw         $2, 0x28($29)
      addiu      $9, $9, 0x8
      lui        $3, (0xE4000000 >> 16)
      addu       $2, $6, $2
      sll        $2, $2, 2
      andi       $2, $2, 0xFFF
      or         $2, $2, $3
      or         $7, $7, $2
      sll        $6, $6, 2
      andi       $6, $6, 0xFFF
      or         $5, $5, $6
      lui        $2, (0xB4000000 >> 16)
      sw         $7, 0x0($17)
      sw         $5, 0x4($17)
      sw         $2, 0x0($18)
      lui        $2, (0xB3000000 >> 16)
      sw         $0, 0x4($18)
      sw         $2, 0x0($19)
      sw         $20, 0x4($19)
      sw         $9, 0x0($4)
      lw         $20, 0x10($29)
      lw         $19, 0xC($29)
      lw         $18, 0x8($29)
      lw         $17, 0x4($29)
      lw         $16, 0x0($29)
      addiu      $29, $29, 0x18
      jr         $31
       nop
.end func_800F4DFC
