.include "macro.inc"

.set noat
.set noreorder
.set gp=64

.section .text, "ax"

.globl func_80107170_us
.ent func_80107170_us
func_80107170_us:
      addiu      $sp, $sp, -0x18
      addu       $a3, $a0, $zero
      addiu      $a2, $zero, -0x1
      sw         $s1, 0x14($sp)
      beq        $a3, $a2, .L80107498
       sw        $s0, 0x10($sp)
      sll        $v0, $a3, 2
      addu       $v0, $v0, $a3
      sll        $a1, $v0, 3
      lui        $at, %hi(D_803B824C)
      addu       $at, $at, $a1
      lh         $a0, %lo(D_803B824C)($at)
      beq        $a0, $a2, .L80107230
       addu      $t8, $zero, $zero
      sll        $v0, $a0, 2
      lui        $at, %hi(D_803B824A)
      addu       $at, $at, $a1
      lhu        $v1, %lo(D_803B824A)($at)
      addu       $v0, $v0, $a0
      sll        $v0, $v0, 3
      lui        $at, %hi(D_803B824A)
      addu       $at, $at, $v0
      sh         $v1, %lo(D_803B824A)($at)
      lui        $at, %hi(D_803B824A)
      addu       $at, $at, $a1
      lh         $a0, %lo(D_803B824A)($at)
      beq        $a0, $a2, .L80107264
       sll       $v0, $a0, 2
      lui        $at, %hi(D_803B824C)
      addu       $at, $at, $a1
      lhu        $v1, %lo(D_803B824C)($at)
      addu       $v0, $v0, $a0
      j          .L80107258
       sll       $v0, $v0, 3
  .L801071F8:
      sll        $v0, $v1, 2
      addu       $v0, $v0, $v1
      sll        $v0, $v0, 3
      lui        $at, %hi(D_803B8266)
      addu       $at, $at, $v0
      lhu        $a0, %lo(D_803B8266)($at)
      addiu      $v1, $zero, -0x1
      lui        $at, %hi(D_803B8266)
      addu       $at, $at, $v0
      sh         $v1, %lo(D_803B8266)($at)
      lui        $at, %hi(D_803B8230)
      sh         $a0, %lo(D_803B8230)($at)
      j          .L80107498
       nop
  .L80107230:
      lui        $at, %hi(D_803B824A)
      addu       $at, $at, $a1
      lh         $v1, %lo(D_803B824A)($at)
      lui        $at, %hi(D_803B8240)
      sw         $v1, %lo(D_803B8240)($at)
      beq        $v1, $a0, .L80107264
       sll       $v0, $v1, 2
      addu       $v0, $v0, $v1
      sll        $v0, $v0, 3
      addiu      $v1, $zero, -0x1
  .L80107258:
      lui        $at, %hi(D_803B824C)
      addu       $at, $at, $v0
      sh         $v1, %lo(D_803B824C)($at)
  .L80107264:
      sll        $v0, $a3, 2
      addu       $v0, $v0, $a3
      lui        $v1, %hi(D_803B8234)
      lw         $v1, %lo(D_803B8234)($v1)
      sll        $a0, $v0, 3
      addiu      $v0, $zero, -0x1
      lui        $at, %hi(D_803B824C)
      addu       $at, $at, $a0
      sh         $v0, %lo(D_803B824C)($at)
      addiu      $v0, $zero, -0x1
      lui        $at, %hi(D_803B824A)
      addu       $at, $at, $a0
      sh         $v1, %lo(D_803B824A)($at)
      beq        $v1, $v0, .L801072B4
       sll       $v0, $v1, 2
      addu       $v0, $v0, $v1
      sll        $v0, $v0, 3
      lui        $at, %hi(D_803B824C)
      addu       $at, $at, $v0
      sh         $a3, %lo(D_803B824C)($at)
  .L801072B4:
      addu       $t6, $zero, $zero
      addiu      $s0, $zero, -0x1
      addu       $t7, $a0, $zero
      lui        $t9, %hi(D_803B8266)
      addiu      $t9, $t9, %lo(D_803B8266)
      addu       $t0, $t7, $t9
      addu       $t5, $zero, $zero
      lui        $t4, %hi(D_803B75B0)
      addiu      $t4, $t4, %lo(D_803B75B0)
      lui        $at, %hi(D_803B8256)
      addu       $at, $at, $t7
      lh         $a1, %lo(D_803B8256)($at)
      lui        $v1, %hi(D_803B8238)
      lw         $v1, %lo(D_803B8238)($v1)
      lui        $at, %hi(D_803B8258)
      addu       $at, $at, $t7
      lh         $a0, %lo(D_803B8258)($at)
      lui        $v0, %hi(D_803B823C)
      lw         $v0, %lo(D_803B823C)($v0)
      lui        $t3, %hi(D_80142B60)
      addiu      $t3, $t3, %lo(D_80142B60)
      lui        $t2, %hi(D_80142B50)
      addiu      $t2, $t2, %lo(D_80142B50)
      lui        $at, %hi(D_803B8234)
      sw         $a3, %lo(D_803B8234)($at)
      addu       $s1, $a1, $v1
      addu       $a1, $a0, $v0
  .L80107320:
      lw         $a0, 0x0($t2)
      lw         $v1, 0x0($t3)
      addu       $a0, $s1, $a0
      sra        $a0, $a0, 11
      addu       $v1, $a1, $v1
      sra        $v1, $v1, 11
      sll        $v0, $a0, 2
      addu       $v0, $v0, $a0
      sll        $v0, $v0, 3
      addu       $v0, $v0, $t4
      sll        $v1, $v1, 1
      addu       $a0, $v1, $v0
      lh         $v0, 0x0($a0)
      lhu        $v1, 0x0($a0)
      bne        $v0, $a3, .L80107384
       nop
      lhu        $v0, 0x0($t0)
      j          .L8010737C
       addiu     $t8, $zero, 0x1
  .L8010736C:
      addu       $v0, $t7, $t9
      addu       $v0, $a2, $v0
      lhu        $v0, 0x0($v0)
      addiu      $t8, $zero, 0x1
  .L8010737C:
      j          .L801073C4
       sh        $v0, 0x0($a0)
  .L80107384:
      beq        $v0, $s0, .L801073C4
       nop
      addu       $a2, $t5, $zero
      addiu      $t1, $zero, -0x1
      sll        $v1, $v1, 16
  .L80107398:
      sra        $v1, $v1, 16
      sll        $v0, $v1, 2
      addu       $v0, $v0, $v1
      sll        $v0, $v0, 3
      addu       $v0, $v0, $t9
      addu       $a0, $a2, $v0
      lh         $v0, 0x0($a0)
      beq        $v0, $a3, .L8010736C
       addu      $v1, $v0, $zero
      bnel       $v0, $t1, .L80107398
       sll       $v1, $v1, 16
  .L801073C4:
      bnel       $t8, $zero, .L801073CC
       sh        $s0, 0x0($t0)
  .L801073CC:
      addiu      $t0, $t0, 0x2
      addiu      $t5, $t5, 0x2
      addiu      $t4, $t4, 0x320
      addiu      $t3, $t3, 0x4
      addiu      $t6, $t6, 0x1
      sltiu      $v0, $t6, 0x4
      bnez       $v0, .L80107320
       addiu     $t2, $t2, 0x4
      lui        $a0, %hi(D_803B8230)
      lhu        $a0, %lo(D_803B8230)($a0)
      addiu      $v0, $zero, -0x1
      sll        $v1, $a0, 16
      sra        $a1, $v1, 16
      beq        $a1, $v0, .L80107498
       addiu     $a2, $zero, -0x1
      addu       $t1, $a1, $zero
      addiu      $t0, $zero, -0x1
      sll        $v0, $a3, 2
      addu       $v0, $v0, $a3
      sll        $a1, $v0, 3
      sra        $v1, $v1, 16
  .L80107420:
      bnel       $a3, $v1, .L80107470
       addu      $a2, $a0, $zero
      beq        $v1, $t1, .L801071F8
       sll       $v0, $a2, 16
      sra        $a0, $v0, 16
      beq        $a0, $t0, .L8010745C
       sll       $v0, $a0, 2
      lui        $at, %hi(D_803B8266)
      addu       $at, $at, $a1
      lhu        $v1, %lo(D_803B8266)($at)
      addu       $v0, $v0, $a0
      sll        $v0, $v0, 3
      lui        $at, %hi(D_803B8266)
      addu       $at, $at, $v0
      sh         $v1, %lo(D_803B8266)($at)
  .L8010745C:
      lui        $at, %hi(D_803B8266)
      addu       $at, $at, $a1
      sh         $t0, %lo(D_803B8266)($at)
      j          .L80107498
       nop
  .L80107470:
      sll        $v0, $v1, 2
      addu       $v0, $v0, $v1
      sll        $v0, $v0, 3
      lui        $at, %hi(D_803B8266)
      addu       $at, $at, $v0
      lhu        $a0, %lo(D_803B8266)($at)
      sll        $v1, $a0, 16
      sra        $v0, $v1, 16
      bne        $v0, $t0, .L80107420
       sra       $v1, $v1, 16
  .L80107498:
      lw         $s1, 0x14($sp)
      lw         $s0, 0x10($sp)
      addiu      $sp, $sp, 0x18
      jr         $ra
       nop
.end func_80107170_us
