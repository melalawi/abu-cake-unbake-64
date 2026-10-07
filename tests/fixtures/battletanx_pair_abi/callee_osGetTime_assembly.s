.include "macro.inc"

.set noat
.set noreorder
.set gp=64

.section .text, "ax"

.globl func_801120A0_us
.ent func_801120A0_us
func_801120A0_us:
      addiu      $29, $29, -0x38
      sw         $31, 0x1C($29)
      jal        func_80112480_us
       sw        $16, 0x18($29)
      jal        func_80112130
       or        $16, $2, $0
      sw         $2, 0x34($29)
      lui        $15, %hi(D_803C7648)
      lw         $15, %lo(D_803C7648)($15)
      lw         $14, 0x34($29)
      lui        $8, %hi(D_803C7640)
      lui        $9, %hi(D_803C7644)
      lw         $9, %lo(D_803C7644)($9)
      lw         $8, %lo(D_803C7640)($8)
      subu       $24, $14, $15
      sw         $24, 0x30($29)
      or         $4, $16, $0
      sw         $9, 0x2C($29)
      jal        func_801124A0_us
       sw        $8, 0x28($29)
      lw         $25, 0x30($29)
      lw         $13, 0x2C($29)
      lw         $31, 0x1C($29)
      or         $11, $25, $0
      addu       $3, $11, $13
      lw         $12, 0x28($29)
      addiu      $10, $0, 0x0
      sltu       $1, $3, $13
      addu       $2, $1, $10
      lw         $16, 0x18($29)
      addiu      $29, $29, 0x38
      jr         $31
       addu      $2, $2, $12
.end func_801120A0_us
      nop
      nop
      nop
