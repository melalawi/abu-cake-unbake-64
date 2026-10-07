.include "macro.inc"

.set noat
.set noreorder
.set gp=64

.section .text, "ax"

.globl func_800B1520_us
.ent func_800B1520_us
func_800B1520_us:
      addiu      $29, $29, -0x48
      lui        $4, %hi(D_803276D4)
      addiu      $4, $4, %lo(D_803276D4)
      addu       $5, $0, $0
      addu       $6, $0, $0
      addiu      $7, $0, 0x140
      addiu      $2, $0, 0xF0
      sw         $16, 0x28($29)
      addiu      $16, $0, 0xFF
      sw         $31, 0x40($29)
      sw         $21, 0x3C($29)
      sw         $20, 0x38($29)
      sw         $19, 0x34($29)
      sw         $18, 0x30($29)
      sw         $17, 0x2C($29)
      sw         $2, 0x10($29)
      sw         $0, 0x14($29)
      sw         $0, 0x18($29)
      sw         $0, 0x1C($29)
      jal        func_800F4DFC
       sw        $16, 0x20($29)
      lui        $4, (0xBA001402 >> 16)
      ori        $4, $4, (0xBA001402 & 0xFFFF)
      lui        $7, (0xFCFFFFFF >> 16)
      ori        $7, $7, (0xFCFFFFFF & 0xFFFF)
      lui        $5, (0xFFFCF279 >> 16)
      ori        $5, $5, (0xFFFCF279 & 0xFFFF)
      lui        $8, (0xB900031D >> 16)
      ori        $8, $8, (0xB900031D & 0xFFFF)
      lui        $6, (0xF0A4000 >> 16)
      ori        $6, $6, (0xF0A4000 & 0xFFFF)
      lui        $9, (0xBA001301 >> 16)
      ori        $9, $9, (0xBA001301 & 0xFFFF)
      lui        $10, (0xBA001001 >> 16)
      ori        $10, $10, (0xBA001001 & 0xFFFF)
      lui        $11, (0xBA000C02 >> 16)
      ori        $11, $11, (0xBA000C02 & 0xFFFF)
      lui        $12, (0xBA000903 >> 16)
      ori        $12, $12, (0xBA000903 & 0xFFFF)
      lui        $2, %hi(D_803276D4)
      lw         $2, %lo(D_803276D4)($2)
      lui        $13, (0xBA000E02 >> 16)
      ori        $13, $13, (0xBA000E02 & 0xFFFF)
      addiu      $3, $2, 0x8
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      lui        $3, (0xE7000000 >> 16)
      sw         $3, 0x0($2)
      addiu      $3, $2, 0x10
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      addiu      $3, $2, 0x18
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      addiu      $3, $2, 0x20
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      addiu      $3, $2, 0x28
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      addiu      $3, $2, 0x30
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      addiu      $3, $2, 0x38
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      addiu      $3, $2, 0x40
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      addiu      $3, $0, 0xC00
      sw         $3, 0x3C($2)
      addiu      $3, $2, 0x48
      lui        $1, %hi(D_803276D4)
      sw         $3, %lo(D_803276D4)($1)
      ori        $3, $0, 0x8000
      sw         $0, 0x4($2)
      sw         $4, 0x8($2)
      sw         $0, 0xC($2)
      sw         $7, 0x10($2)
      sw         $5, 0x14($2)
      sw         $8, 0x18($2)
      sw         $6, 0x1C($2)
      sw         $9, 0x20($2)
      sw         $0, 0x24($2)
      sw         $10, 0x28($2)
      sw         $0, 0x2C($2)
      sw         $11, 0x30($2)
      sw         $0, 0x34($2)
      sw         $12, 0x38($2)
      sw         $13, 0x40($2)
      jal        func_801120A0_us
       sw        $3, 0x44($2)
      lui        $6, %hi(D_80145DA0)
      lw         $6, %lo(D_80145DA0)($6)
      lui        $7, %hi(D_80145DA4)
      lw         $7, %lo(D_80145DA4)($7)
      sll        $8, $2, 5
      srl        $4, $3, 27
      or         $8, $8, $4
      sll        $9, $3, 5
      sltu       $4, $9, $3
      subu       $9, $9, $3
      subu       $8, $8, $2
      subu       $8, $8, $4
      sll        $4, $8, 6
      srl        $10, $9, 26
      or         $4, $4, $10
      sll        $5, $9, 6
      sltu       $10, $5, $9
      subu       $5, $5, $9
      subu       $4, $4, $8
      subu       $4, $4, $10
      sll        $4, $4, 3
      srl        $8, $5, 29
      or         $4, $4, $8
      sll        $5, $5, 3
      addu       $5, $5, $3
      sltu       $8, $5, $3
      addu       $4, $4, $2
      addu       $4, $4, $8
      sll        $4, $4, 6
      srl        $2, $5, 26
      or         $4, $4, $2
      jal        func_80124EC0_us
       sll       $5, $5, 6
      addiu      $4, $0, 0x3
      addu       $18, $2, $0
      jal        func_801054E0
       addu      $19, $3, $0
      lui        $3, %hi(D_803276E0)
      lbu        $3, %lo(D_803276E0)($3)
      addiu      $2, $0, 0xA
      beq        $3, $2, .L800B4B28
       slti      $2, $3, 0xB
      beqz       $2, .L800B17B0
       addiu     $2, $0, 0x3
      beq        $3, $2, .L800B2960
       slti      $2, $3, 0x4
      beqz       $2, .L800B1770