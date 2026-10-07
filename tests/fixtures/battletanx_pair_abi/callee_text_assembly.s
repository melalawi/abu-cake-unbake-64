.include "macro.inc"

.set noat
.set noreorder
.set gp=64

.section .text, "ax"

.globl func_80105A50
.ent func_80105A50
func_80105A50:
      addiu      $29, $29, -0x20
      sw         $17, 0x14($29)
      addu       $17, $4, $0
      sw         $16, 0x10($29)
      addu       $16, $5, $0
      addu       $4, $0, $0
      sw         $31, 0x18($29)
      lbu        $5, 0x0($16)
      addiu      $8, $16, 0x1
      beqz       $5, .L80105AF8
       subu      $7, $7, $6
      addiu      $12, $0, 0x20
      lui        $1, %hi(D_803AAD90)
      lwc1       $f4, %lo(D_803AAD90)($1)
      lui        $3, %hi(D_803B75A0)
      lw         $3, %lo(D_803B75A0)($3)
      lui        $9, %hi(D_803AAD98)
      lw         $9, %lo(D_803AAD98)($9)
      addiu      $11, $0, 0xFF
      sll        $2, $3, 1
      addu       $10, $2, $3
  .L80105AA4:
      bne        $5, $12, .L80105AC0
       addu      $2, $9, $5
      mtc1       $10, $f0
      cvt.s.w    $f0, $f0
      mul.s      $f0, $f0, $f4
      j          .L80105AE0
       nop
  .L80105AC0:
      lbu        $5, 0x18($2)
      beq        $5, $11, .L80105AEC
       sll       $2, $5, 2
      addu       $2, $9, $2
      lbu        $2, 0x9A($2)
      mtc1       $2, $f0
      cvt.s.w    $f0, $f0
      mul.s      $f0, $f0, $f4
  .L80105AE0:
      trunc.w.s  $f2, $f0
      mfc1       $2, $f2
      addu       $4, $4, $2
  .L80105AEC:
      lbu        $5, 0x0($8)
      bnez       $5, .L80105AA4
       addiu     $8, $8, 0x1
  .L80105AF8:
      subu       $2, $7, $4
      srl        $3, $2, 31
      addu       $2, $2, $3
      sra        $2, $2, 1
      addu       $2, $2, $6
      lui        $1, %hi(D_803AAD88)
      sw         $2, %lo(D_803AAD88)($1)
      j          .L80105B24
       nop
  .L80105B1C:
      jal        func_80106010_us
       addu      $4, $17, $0
  .L80105B24:
      lbu        $5, 0x0($16)
      bnez       $5, .L80105B1C
       addiu     $16, $16, 0x1
      lw         $31, 0x18($29)
      lw         $17, 0x14($29)
      lw         $16, 0x10($29)
      addiu      $29, $29, 0x20
      jr         $31
       nop
.end func_80105A50
